r"""命令工具：`bash` 与 `powershell`。

对齐 Pi：

- 两个工具分开注册，模型按当前环境选；
- 只注册真的能执行的工具：`bash` 需要 PATH 里有 bash，Windows 上跳过
  `System32\bash.exe`（那只是 WSL 入口，没有发行版时必然失败），
  `powershell` 只在 Windows 上注册；
- 命令在 workspace 里执行，输出合并 stdout / stderr，非零退出码标记为错误；
- 单次调用有超时（默认 120s），输出上限 30000 字符；
- `run_in_background=true` 时交给 `background.BackgroundTasks`，立即返回任务号，
  输出在完成时以通知送达（见 `background.py`）。

命令是否放行由 `permissions` 的 `before_tool_call` 负责，本模块不做权限判断。
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ...agent.base import AgentTool, ToolUpdateCallback
from ...agent.types import ToolResult
from ...ai.base import ToolSpec
from ..background import BackgroundLimit, BackgroundTasks
from ..sandbox import Environment, LocalEnvironment, SandboxError
from .spill import with_pointer

DEFAULT_TIMEOUT = 120.0
MAX_TIMEOUT = 600.0
MAX_OUTPUT = 30_000

_TIMEOUT_SCHEMA = {
    "type": "number",
    "minimum": 1,
    "maximum": int(MAX_TIMEOUT),
    "description": f"Timeout in seconds (default: {DEFAULT_TIMEOUT:g})",
}

_BACKGROUND_SCHEMA = {
    "type": "boolean",
    "description": (
        "Run the command in the background and return a task id immediately. "
        "Use only for independent long-running commands (install, build, full "
        "test suite) whose result later work does not need."
    ),
}

BASH_SPEC = ToolSpec(
    name="bash",
    description=(
        "Execute a bash command in the working directory. Returns combined stdout "
        "and stderr; a non-zero exit code is reported as an error. "
        f"Output is capped at {MAX_OUTPUT // 1000}KB and the call times out after "
        f"{DEFAULT_TIMEOUT:g}s unless timeout is given."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Command to run"},
            "timeout": _TIMEOUT_SCHEMA,
            "run_in_background": _BACKGROUND_SCHEMA,
        },
        "required": ["command"],
    },
)

POWERSHELL_SPEC = ToolSpec(
    name="powershell",
    description=(
        "Execute a PowerShell command in the working directory on Windows. Returns "
        "combined stdout and stderr; a non-zero exit code is reported as an error. "
        f"Output is capped at {MAX_OUTPUT // 1000}KB and the call times out after "
        f"{DEFAULT_TIMEOUT:g}s unless timeout is given."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Command to run"},
            "timeout": _TIMEOUT_SCHEMA,
            "run_in_background": _BACKGROUND_SCHEMA,
        },
        "required": ["command"],
    },
)


class ShellTool:
    """按给定解释器执行一条命令。"""

    execution_mode = "serial"

    def __init__(
        self,
        root: Path,
        executable: str,
        prefix: Sequence[str],
        spec: ToolSpec,
        env: Environment | None = None,
        background: BackgroundTasks | None = None,
        output_chars: int | None = None,
    ) -> None:
        self._root = root
        self._executable = executable
        self._prefix = tuple(prefix)
        self.spec = spec
        self._env = env or LocalEnvironment(root)
        self._background = background
        self._output_chars = output_chars or MAX_OUTPUT

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        command = str(arguments.get("command", "")).strip()
        if not command:
            return ToolResult("command is required", is_error=True)

        if arguments.get("run_in_background") is True:
            return await self._start_background(command)

        timeout = _timeout(arguments.get("timeout"))

        try:
            process = await self._env.spawn(
                [self._executable, *self._prefix, command],
                cwd=self._root,
                merge_stderr=True,
            )
        except (OSError, SandboxError) as exc:
            return ToolResult(f"Cannot start {self.spec.name}: {exc}", is_error=True)

        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            await self._env.recycle()
            return ToolResult(
                f"Command timed out after {timeout:g}s", is_error=True
            )

        text = stdout.decode("utf-8", errors="replace").replace("\r\n", "\n")
        if not text.strip():
            text = "(no output)"
        # 超限先落盘、再给指针：截断掉的部分就再也拿不回来了。
        text = with_pointer(text, self._output_chars, hint=self.spec.name)

        code = process.returncode or 0
        if code != 0:
            text = f"{text}\n[exit code {code}]"
        return ToolResult(text, is_error=code != 0)

    async def _start_background(self, command: str) -> ToolResult:
        """交给 `BackgroundTasks` 后立即返回占位结果。

        占位结果就是这个调用的真实返回值：模型问的是“把它跑起来”，答案是
        “已启动，任务号是 …”。命令的输出在完成时以一条通知送达。
        """
        if self._background is None:
            return ToolResult(
                "background execution is not available in this session",
                is_error=True,
            )

        try:
            task = await self._background.start(
                tool=self.spec.name,
                command=command,
                argv=[self._executable, *self._prefix, command],
                cwd=self._root,
            )
        except BackgroundLimit as exc:
            return ToolResult(str(exc), is_error=True)

        return ToolResult(
            f"[background {task.id} started]\n"
            f"status: running\n"
            f"log: {task.log_path}\n"
            "A <task_notification> arrives when it finishes; "
            "call check_background to inspect it earlier.",
            details={"background_task_id": task.id, "status": "running"},
        )


def create_shell_tools(
    root: str | Path,
    env: Environment | None = None,
    background: BackgroundTasks | None = None,
    output_chars: int | None = None,
) -> list[AgentTool]:
    """按当前环境能跑什么决定注册哪些命令工具。

    `docker` / `exec` 后端只在容器里注册 `bash`：宿主上的 `bash`/`powershell` 路径
    在 Linux 容器里不存在。
    `background=None` 时不注册后台执行能力（`run_in_background` 会报错）。
    `output_chars` 是单次输出的落盘上限，`None` 用模块默认值。
    """
    base = Path(root).expanduser().resolve()
    environment = env or LocalEnvironment(base)
    tools: list[AgentTool] = []

    if environment.name in ("docker", "exec"):
        return [
            ShellTool(
                base, "bash", ("-lc",), BASH_SPEC, environment, background, output_chars
            )
        ]

    if environment.name == "appcontainer":
        # AppContainer 只放行系统目录；Git Bash 的安装目录不在授权范围，
        # 用 System32 里的 powershell。
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if not powershell:
            return []
        return [
            ShellTool(
                base,
                powershell,
                ("-NoProfile", "-NonInteractive", "-Command"),
                POWERSHELL_SPEC,
                environment,
                background,
                output_chars,
            )
        ]

    bash = _usable_bash()
    if bash:
        tools.append(
            ShellTool(
                base, bash, ("-lc",), BASH_SPEC, environment, background, output_chars
            )
        )

    if platform.system() == "Windows":
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if powershell:
            tools.append(
                ShellTool(
                    base,
                    powershell,
                    ("-NoProfile", "-NonInteractive", "-Command"),
                    POWERSHELL_SPEC,
                    environment,
                    background,
                    output_chars,
                )
            )

    return tools


def _usable_bash() -> str | None:
    """PATH 里的 bash；Windows 上 `System32\bash.exe` 只是 WSL 入口，跳过。"""
    bash = shutil.which("bash")
    if bash is None:
        return None
    if platform.system() == "Windows" and _is_wsl_launcher(bash):
        return None
    return bash


def _is_wsl_launcher(executable: str) -> bool:
    r"""`%SystemRoot%\System32\bash.exe` 需要 WSL 发行版，不是可用的 bash。"""
    root = os.environ.get("SystemRoot", r"C:\Windows")
    expected = f"{root}\\System32\\bash.exe".lower()
    return executable.replace("/", "\\").lower() == expected


def _timeout(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_TIMEOUT
    return min(max(float(value), 1.0), MAX_TIMEOUT)
