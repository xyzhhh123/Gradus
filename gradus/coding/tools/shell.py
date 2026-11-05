r"""命令工具：`bash` 与 `powershell`。

对齐 Pi：

- 两个工具分开注册，模型按当前环境选；
- 只注册真的能执行的工具：`bash` 需要 PATH 里有 bash，Windows 上跳过
  `System32\bash.exe`（那只是 WSL 入口，没有发行版时必然失败），
  `powershell` 只在 Windows 上注册；
- 命令在 workspace 里执行，输出合并 stdout / stderr，非零退出码标记为错误；
- 单次调用有超时（默认 120s），输出上限 30000 字符。

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
from ..sandbox import Environment, LocalEnvironment, SandboxError

DEFAULT_TIMEOUT = 120.0
MAX_TIMEOUT = 600.0
MAX_OUTPUT = 30_000

_TIMEOUT_SCHEMA = {
    "type": "number",
    "minimum": 1,
    "maximum": int(MAX_TIMEOUT),
    "description": f"Timeout in seconds (default: {DEFAULT_TIMEOUT:g})",
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
    ) -> None:
        self._root = root
        self._executable = executable
        self._prefix = tuple(prefix)
        self.spec = spec
        self._env = env or LocalEnvironment(root)

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        command = str(arguments.get("command", "")).strip()
        if not command:
            return ToolResult("command is required", is_error=True)

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
        if len(text) > MAX_OUTPUT:
            text = f"{text[:MAX_OUTPUT]}\n… {len(text) - MAX_OUTPUT} chars truncated …"

        code = process.returncode or 0
        if code != 0:
            text = f"{text}\n[exit code {code}]"
        return ToolResult(text, is_error=code != 0)


def create_shell_tools(
    root: str | Path,
    env: Environment | None = None,
) -> list[AgentTool]:
    """按当前环境能跑什么决定注册哪些命令工具。

    `docker` 后端只在容器里注册 `bash`：宿主上的 `bash` 路径在容器里不存在，
    `powershell` 也不该在 Linux 容器里出现。
    """
    base = Path(root).expanduser().resolve()
    environment = env or LocalEnvironment(base)
    tools: list[AgentTool] = []

    if environment.name == "docker":
        return [ShellTool(base, "bash", ("-lc",), BASH_SPEC, environment)]

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
            )
        ]

    bash = _usable_bash()
    if bash:
        tools.append(ShellTool(base, bash, ("-lc",), BASH_SPEC, environment))

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
