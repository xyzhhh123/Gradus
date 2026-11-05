"""`codemode`：让模型写一段 Python，通过桥调用已声明的工具。

- 脚本在独立子进程里跑（`python -I`），工具调用走 stdin/stdout 的 JSON 协议；
- 只有 `text(...)` 与 `return` 的值进模型上下文，`print` 的输出丢弃；
- 脚本内 `tools.<name>(**kwargs)` 调用宿主工具，返回内容字符串；工具报错时抛 `RuntimeError`；
- 写工具与命令在同一次脚本里只确认一次，只读工具直接放行；
- 子进程用受限 builtins，并拒绝 import 与下划线属性访问。

这不是沙箱：进程仍是同一 OS 用户，绕过桥直接读写文件是可能的；真隔离归 M14。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ...agent.base import AgentTool, ToolUpdateCallback
from ...agent.types import ToolResult
from ...ai.base import ToolSpec
from ..permissions import Confirm, Policy, sanitize_result
from ..sandbox import HARNESS_PATH, Environment, LocalEnvironment, SandboxError

DEFAULT_TIMEOUT = 120.0
MAX_TIMEOUT = 600.0
MAX_OUTPUT_CHARS = 30_000

#: 脚本里不暴露的工具：自己（递归）与 tool_search（对脚本没有意义）。
EXCLUDED_TOOLS = frozenset({"codemode", "tool_search"})

HARNESS = HARNESS_PATH

CODEMODE_SPEC = ToolSpec(
    name="codemode",
    description=(
        "Run a Python script that can call the other tools through `tools.<name>(**kwargs)`. "
        "Use `text(value)` for output and `return` for a final value; printed output is discarded. "
        "Writes and commands ask once for the whole script."
    ),
    parameters={
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python source. Call tools as tools.read_file(path=...).",
            },
            "timeout": {
                "type": "number",
                "description": f"Seconds, default {DEFAULT_TIMEOUT:g}, max {MAX_TIMEOUT:g}",
            },
        },
        "required": ["code"],
        "additionalProperties": False,
    },
)


class CodemodeTool:
    """执行脚本，并在子进程与宿主工具之间转发调用。"""

    spec = CODEMODE_SPEC
    execution_mode = "serial"

    def __init__(
        self,
        tools: Sequence[AgentTool],
        policy: Policy,
        confirm: Confirm | None,
        env: Environment | None = None,
    ) -> None:
        self._tools = {
            tool.spec.name: tool
            for tool in tools
            if tool.spec.name not in EXCLUDED_TOOLS
        }
        self._policy = policy
        self._confirm = confirm
        self._env = env or LocalEnvironment(Path.cwd())

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        code = str(arguments.get("code", ""))
        if not code.strip():
            return ToolResult("code is required", is_error=True)
        return await self._run(code, _timeout(arguments.get("timeout")))

    async def _run(self, code: str, timeout: float) -> ToolResult:
        scratch = self._env.scratch_dir()
        handle, path = tempfile.mkstemp(
            suffix=".py", prefix="gradus-codemode-", dir=scratch
        )
        with open(handle, "w", encoding="utf-8") as stream:
            stream.write(code)

        try:
            harness = self._harness(scratch)
            process = await self._env.spawn(
                [
                    self._env.python(),
                    "-I",
                    self._env.map_path(harness),
                    self._env.map_path(path),
                ]
            )
            try:
                async with asyncio.timeout(timeout):
                    return await self._serve(process)
            except TimeoutError:
                await self._env.recycle()
                return ToolResult(f"script timed out after {timeout:g}s", is_error=True)
            finally:
                if process.returncode is None:
                    process.kill()
                await process.wait()
        except (OSError, SandboxError) as exc:
            return ToolResult(f"cannot start script: {type(exc).__name__}: {exc}", is_error=True)
        finally:
            Path(path).unlink(missing_ok=True)

    def _harness(self, scratch: Path) -> Path:
        """AppContainer 只能读 scratch 与解释器目录，harness 复制一份进去。"""
        if self._env.name != "appcontainer":
            return HARNESS
        target = scratch / "codemode_harness.py"
        shutil.copyfile(HARNESS, target)
        return target

    async def _serve(self, process: Any) -> ToolResult:
        assert process.stdout is not None and process.stdin is not None
        approved = False
        errors = asyncio.create_task(_drain(process.stderr))

        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    return ToolResult("script exited without a result", is_error=True)

                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    return ToolResult(
                        f"script wrote invalid output: {line.decode(errors='replace').strip()}",
                        is_error=True,
                    )

                kind = message.get("type")
                if kind == "call":
                    content, is_error, asked = await self._invoke(
                        str(message.get("name", "")),
                        message.get("arguments") or {},
                        approved,
                    )
                    approved = approved or asked
                    reply = {
                        "type": "result",
                        "id": message.get("id"),
                        "content": content,
                        "is_error": is_error,
                    }
                    process.stdin.write((json.dumps(reply, ensure_ascii=False) + "\n").encode())
                    await process.stdin.drain()
                elif kind == "done":
                    return ToolResult(_render(message))
                elif kind == "error":
                    return ToolResult(str(message.get("message") or "script error"), is_error=True)
        finally:
            errors.cancel()

    async def _invoke(
        self, name: str, arguments: dict[str, Any], approved: bool
    ) -> tuple[str, bool, bool]:
        tool = self._tools.get(name)
        if tool is None:
            return f"unknown tool: {name}", True, False

        decision = self._policy.decide(name, arguments)
        if decision.action == "deny":
            return decision.reason, True, False

        asked = False
        if decision.action == "ask" and not approved:
            if self._confirm is None:
                return f"permission denied: {decision.reason}", True, False
            question = f"脚本请求：{decision.reason}。允许本次脚本内的写操作与命令？"
            if not await self._confirm(question):
                return f"permission denied: {decision.reason}", True, False
            asked = True

        result = await tool.execute(f"codemode-{name}", arguments)
        return sanitize_result(result.content), result.is_error, asked


async def _drain(stream: Any) -> str:
    if stream is None:
        return ""
    data = await stream.read()
    return data.decode("utf-8", errors="replace")


def _render(message: dict[str, Any]) -> str:
    parts = [str(item) for item in message.get("outputs") or []]
    returned = message.get("returned")
    if returned is not None:
        parts.append(str(returned))

    text = "\n".join(parts) if parts else "(no output)"
    if len(text) > MAX_OUTPUT_CHARS:
        removed = len(text) - MAX_OUTPUT_CHARS
        text = f"{text[:MAX_OUTPUT_CHARS]}\n…{removed} chars truncated…"
    return text


def _timeout(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return DEFAULT_TIMEOUT
    return min(float(value), MAX_TIMEOUT)


def create_codemode_tool(
    tools: Sequence[AgentTool],
    policy: Policy,
    confirm: Confirm | None,
    env: Environment | None = None,
) -> CodemodeTool:
    return CodemodeTool(tools, policy, confirm, env)
