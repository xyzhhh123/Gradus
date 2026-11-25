"""`codemode`：让模型写一段 Python，通过桥调用 direct + deferred 工具。

- 脚本在独立子进程里跑（`python -I`），工具调用走 stdin/stdout 的 JSON 协议；
- 只有 `text(...)` 与 `return` 的值进模型上下文，`print` 的输出丢弃；
- 脚本内兼容 `tools.<name>(**kwargs)`；deferred 工具可先用 `search_tools()` /
  `describe_tool()` 发现，再用 `tools[name](**kwargs)` 调用；
- 工具调用返回内容字符串；工具报错时抛 `RuntimeError`；
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...agent.base import AgentTool, ToolUpdateCallback, tool_capability
from ...agent.types import ToolCapability, ToolResult
from ...ai.base import ToolSpec
from ..permissions import Confirm, Policy, sanitize_result
from ..sandbox import HARNESS_PATH, Environment, LocalEnvironment, SandboxError
from .spill import SpillStore
from .tool_search import rank_tools

DEFAULT_TIMEOUT = 120.0
MAX_TIMEOUT = 600.0
MAX_OUTPUT_CHARS = 30_000
DEFAULT_SEARCH_LIMIT = 8
MAX_SEARCH_LIMIT = 32
DESCRIPTION_MAX_CHARS = 12_000
CODEMODE_CAPABILITY = ToolCapability(
    effects=("delegate", "control"),
    result_kind="output",
    idempotent=False,
    retryable=False,
    contained_effects=True,
)


@dataclass(frozen=True)
class ExecutionBoundary:
    """What the Python process is actually isolated from."""

    name: str
    isolated: bool
    description: str


def execution_boundary(env: Environment) -> ExecutionBoundary:
    if env.name == "docker":
        return ExecutionBoundary("docker", True, "isolated container")
    if env.name in {"appcontainer", "restricted"}:
        return ExecutionBoundary(
            env.name,
            True,
            "OS-restricted token/container",
        )
    if env.name == "exec":
        return ExecutionBoundary(
            "exec",
            False,
            "external environment with unknown isolation",
        )
    return ExecutionBoundary(
        "local",
        False,
        "same OS user; restricted builtins only, not a security sandbox",
    )

#: 脚本里不暴露的工具：自己（递归）与 tool_search（对脚本没有意义）。
EXCLUDED_TOOLS = frozenset({"codemode", "tool_search"})

HARNESS = HARNESS_PATH

CODEMODE_SPEC = ToolSpec(
    name="codemode",
    description=(
        "Run a Python script that can call the other tools through `tools.<name>(**kwargs)`. "
        "For potentially deferred tools, call `search_tools(query, limit=8)` to discover names "
        "and descriptions, `describe_tool(name)` for its parameter schema, then execute with "
        "`tools[name](**kwargs)` without another model call. "
        "Use `text(value)` for output and `return` for a final value; printed output is discarded. "
        "`import` is not available and underscore attributes are blocked: the script only gets the "
        "`tools` namespace plus side-effect-free builtins, so there is no `open` and no `print` — "
        "read data with `tools.read_file(...)` and filter it with plain loops and comprehensions. "
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
    capability = CODEMODE_CAPABILITY

    def __init__(
        self,
        tools: Sequence[AgentTool],
        policy: Policy,
        confirm: Confirm | None,
        env: Environment | None = None,
        output_chars: int | None = None,
        spill_store: SpillStore | None = None,
    ) -> None:
        self._tools = {
            tool.spec.name: tool
            for tool in tools
            if tool.spec.name not in EXCLUDED_TOOLS
        }
        self._env = env or LocalEnvironment(Path.cwd())
        self.boundary = execution_boundary(self._env)
        self.spec = ToolSpec(
            name=CODEMODE_SPEC.name,
            description=_codemode_description(
                self._tools,
                self.boundary,
            ),
            parameters=CODEMODE_SPEC.parameters,
        )
        self._policy = policy.with_tools(tuple(self._tools.values()))
        self._confirm = confirm
        self.capability = ToolCapability(
            effects=("delegate", "control"),
            result_kind="output",
            mutates_workspace=not self.boundary.isolated,
            idempotent=False,
            retryable=False,
            contained_effects=self.boundary.isolated,
        )
        self._output_chars = output_chars or MAX_OUTPUT_CHARS
        self._spill = spill_store or SpillStore()

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
        nested_calls: list[dict[str, Any]] = []

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
                if kind in ("call", "search", "describe"):
                    if kind == "call":
                        result, asked = await self._invoke(
                            str(message.get("name", "")),
                            message.get("arguments") or {},
                            approved,
                        )
                        content = result.content
                        is_error = result.is_error
                        approved = approved or asked
                        nested_calls.append(
                            {
                                "name": str(message.get("name", "")),
                                "is_error": is_error,
                                "content": content,
                                "structured_content": result.structured_content,
                                "details": result.details,
                            }
                        )
                    elif kind == "search":
                        try:
                            content = self._search(
                                message.get("query"),
                                message.get("limit"),
                            )
                            is_error = False
                        except ValueError as exc:
                            content = str(exc)
                            is_error = True
                    else:
                        content = self._describe(message.get("name"))
                        is_error = False
                    reply = {
                        "type": "result",
                        "id": message.get("id"),
                        "content": content,
                        "is_error": is_error,
                        "structured_content": (
                            result.structured_content if kind == "call" else None
                        ),
                        "details": result.details if kind == "call" else None,
                    }
                    process.stdin.write((json.dumps(reply, ensure_ascii=False) + "\n").encode())
                    await process.stdin.drain()
                elif kind == "done":
                    return ToolResult(
                        _render(message, self._output_chars, self._spill),
                        structured_content=(
                            {"nested_calls": nested_calls}
                            if nested_calls
                            else None
                        ),
                        details={
                            "execution_boundary": self.boundary.name,
                            "isolated": self.boundary.isolated,
                            "nested_calls": nested_calls,
                        },
                    )
                elif kind == "error":
                    return ToolResult(str(message.get("message") or "script error"), is_error=True)
                else:
                    return ToolResult(f"invalid bridge message: {kind}", is_error=True)
        finally:
            errors.cancel()

    def _search(self, query: Any, limit: Any) -> list[dict[str, Any]]:
        text = str(query or "").strip()
        if not text:
            raise ValueError("search_tools query must not be empty")
        if isinstance(limit, bool):
            raise ValueError("search_tools limit must be a positive integer")
        try:
            count = DEFAULT_SEARCH_LIMIT if limit is None else int(limit)
        except (TypeError, ValueError) as exc:
            raise ValueError("search_tools limit must be a positive integer") from exc
        if count <= 0:
            raise ValueError("search_tools limit must be a positive integer")
        count = min(count, MAX_SEARCH_LIMIT)
        matches = rank_tools(tuple(self._tools.values()), text)[:count]
        return [
            {"name": tool.spec.name, "description": tool.spec.description}
            for tool in matches
        ]

    def _describe(self, name: Any) -> dict[str, Any] | None:
        tool = self._tools.get(str(name or ""))
        if tool is None:
            return None
        return {
            "name": tool.spec.name,
            "description": tool.spec.description,
            "parameters": tool.spec.parameters,
        }

    async def _invoke(
        self, name: str, arguments: dict[str, Any], approved: bool
    ) -> tuple[ToolResult, bool]:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(f"unknown tool: {name}", is_error=True), False

        decision = self._policy.decide(name, arguments)
        if decision.action == "deny":
            return ToolResult(decision.reason, is_error=True), False

        asked = False
        if decision.action == "ask" and not approved:
            if self._confirm is None:
                return (
                    ToolResult(
                        f"permission denied: {decision.reason}",
                        is_error=True,
                    ),
                    False,
                )
            question = f"脚本请求：{decision.reason}。允许本次脚本内的写操作与命令？"
            if not await self._confirm(question):
                return (
                    ToolResult(
                        f"permission denied: {decision.reason}",
                        is_error=True,
                    ),
                    False,
                )
            asked = True

        result = await tool.execute(f"codemode-{name}", arguments)
        return (
            ToolResult(
                content=sanitize_result(result.content),
                is_error=result.is_error,
                details=_json_value(result.details),
                structured_content=_json_value(result.structured_content),
            ),
            asked,
        )


async def _drain(stream: Any) -> str:
    if stream is None:
        return ""
    data = await stream.read()
    return data.decode("utf-8", errors="replace")


def _render(
    message: dict[str, Any],
    limit: int,
    spill_store: SpillStore,
) -> str:
    parts = [str(item) for item in message.get("outputs") or []]
    returned = message.get("returned")
    if returned is not None:
        parts.append(str(returned))

    text = "\n".join(parts) if parts else "(no output)"
    # 超限先落盘、再给指针：截断掉的部分就再也拿不回来了。
    return spill_store.with_pointer(text, limit, hint="codemode")


def _timeout(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return DEFAULT_TIMEOUT
    return min(float(value), MAX_TIMEOUT)


def create_codemode_tool(
    tools: Sequence[AgentTool],
    policy: Policy,
    confirm: Confirm | None,
    env: Environment | None = None,
    output_chars: int | None = None,
    spill_store: SpillStore | None = None,
) -> CodemodeTool:
    return CodemodeTool(
        tools,
        policy,
        confirm,
        env,
        output_chars,
        spill_store,
    )


def _codemode_description(
    tools: dict[str, AgentTool],
    boundary: ExecutionBoundary,
) -> str:
    lines = [
        "Run Python that calls other tools through `tools.<name>(...)`, to chain calls, "
        "run independent calls together, or filter large results before they enter context.",
        "Only `text(value)` and `return` output enters the model context; `print` is discarded.",
        "Use `search_tools(query)` and `describe_tool(name)` for deferred tools.",
        f"Execution boundary: {boundary.name} ({boundary.description}).",
        "",
        "Callable tools:",
    ]
    for name, tool in sorted(tools.items())[:32]:
        parameters = tool.spec.parameters
        properties = parameters.get("properties", {}) if isinstance(parameters, dict) else {}
        required = set(
            parameters.get("required", [])
            if isinstance(parameters, dict)
            else ()
        )
        args = []
        if isinstance(properties, dict):
            for key, schema in properties.items():
                kind = schema.get("type", "unknown") if isinstance(schema, dict) else "unknown"
                marker = "" if key in required else "?"
                args.append(f"{key}{marker}: {kind}")
        signature = ", ".join(args) or "**kwargs"
        description = " ".join(tool.spec.description.split())[:100]
        access = f".{name}" if name.isidentifier() else f"[{name!r}]"
        capability = tool_capability(tool)
        semantics = (
            f" [{','.join(capability.effects)}]" if capability and capability.effects else ""
        )
        lines.append(
            f"- tools{access}({signature}) - {description}{semantics}"
        )
    if len(tools) > 32:
        lines.append("- Additional tools: use search_tools(query).")
    result = "\n".join(lines)
    if len(result) > DESCRIPTION_MAX_CHARS:
        result = result[:DESCRIPTION_MAX_CHARS] + "\n- ..."
    return result


def _json_value(value: Any) -> Any:
    """Keep bridge payload JSON-serializable without changing normal values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    return repr(value)
