"""工具权限策略。

负责：判断一次工具调用是放行、拒绝还是需要用户确认，并把它接成
`agent.hooks.BeforeToolCall`；同时提供工具结果的截断与脱敏。

不负责：具体工具的读写（`tools/`）、终端交互（`cli.py`）。

约束：
- 策略是纯函数，可离线测试；
- 确认流程用注入的 async 回调，不直接读 stdin；
- 没有确认回调时，询问类调用按拒绝处理。
"""

from __future__ import annotations

import fnmatch
import json
import re
import shlex
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ..agent.base import AgentTool, tool_capability
from ..agent.hooks import (
    AfterToolCallContext,
    AfterToolCallResult,
    BeforeToolCallContext,
    BeforeToolCallResult,
)
from ..agent.types import ToolCapability
from .paths import WorkspacePathError, workspace_path

if TYPE_CHECKING:
    from .changes import WorkspaceChangeLedger
    from .completion import CompletionGate

ToolOutcomeObserver = Callable[[str, bool], None]

Action = Literal["allow", "ask", "deny"]
Confirm = Callable[[str], Awaitable[bool]]

READ_TOOLS = frozenset(
    {
        "read_file",
        "grep",
        "glob",
        "tool_search",
        # 召回只读已压缩的历史，不产生任何副作用。
        "recall_history",
        "check_background",
        "list_mcp_resources",
        "list_mcp_resource_templates",
        "read_mcp_resource",
        # 列任务与取消任务都只动任务表，不会立刻执行任何东西；新建任务走询问。
        "manage_schedules",
        # 六个任务图工具同样只动协作状态；真正干活发生在派活之后。
        "create_task",
        "update_task",
        "list_tasks",
        "get_task",
        "claim_task",
        "complete_task",
        # 派活、等待、收尾与消息同样只动协作状态：真正的写操作发生在子 agent 自己
        # 的会话里，那边仍按同一套策略逐次询问。
        "spawn_agent",
        "wait",
        "list_agents",
        "send_message",
        "close_agent",
        "interrupt_agent",
        # 脚本本身只是运行器；脚本内的写操作与命令由 codemode 逐次按本策略确认。
        "codemode",
    }
)
WRITE_TOOLS = frozenset({"write_file", "edit", "delete_file"})
COMMAND_TOOLS = frozenset({"bash", "powershell"})

DENIED_PATHS = (".env*", "*.pem", "id_rsa*", ".git/*")
DENIED_COMMANDS = ("rm -rf", "sudo", "git push --force")

MAX_RESULT_CHARS = 50_000
_SECRET_PATTERNS = (
    (re.compile(r"sk-[A-Za-z0-9_-]{16,}"), "sk-***"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+"), r"\1***"),
    (
        re.compile(r"(?i)((?:api[_-]?key|token|secret)\s*[=:]\s*)[^\s'\"]+"),
        r"\1***",
    ),
)


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str = ""


@dataclass(frozen=True)
class Policy:
    """默认策略：只读放行，写与命令询问，敏感路径 / 命令直接拒绝。"""

    workspace: Path
    read_tools: frozenset[str] = READ_TOOLS
    write_tools: frozenset[str] = WRITE_TOOLS
    command_tools: frozenset[str] = COMMAND_TOOLS
    denied_paths: tuple[str, ...] = DENIED_PATHS
    denied_commands: tuple[str, ...] = DENIED_COMMANDS
    capabilities: Mapping[str, ToolCapability] = field(default_factory=dict)

    def with_tools(self, tools: Sequence[AgentTool]) -> "Policy":
        """Attach capability metadata to this policy.

        Coding supplies this mapping after tool construction. The agent layer
        remains unaware of concrete tool names.
        """
        capabilities = {
            tool.spec.name: capability
            for tool in tools
            if (capability := tool_capability(tool)) is not None
        }
        return replace(self, capabilities=capabilities)

    def decide(self, tool_name: str, arguments: dict[str, Any]) -> Decision:
        paths = _argument_paths(arguments)
        resolved_paths: list[tuple[str, Path]] = []
        for path in paths:
            try:
                resolved_paths.append(
                    (path, workspace_path(self.workspace, path))
                )
            except WorkspacePathError as exc:
                return Decision("deny", str(exc))

        denied = next(
            (
                path
                for path, resolved in resolved_paths
                if self._path_denied(path, resolved)
            ),
            None,
        )
        if denied is not None:
            return Decision("deny", f"受保护的路径：{denied}")

        capability = self.capabilities.get(tool_name)
        mutates = (
            capability.mutates_workspace
            if capability is not None
            else tool_name in self.write_tools
        )
        executes = (
            capability.executes
            if capability is not None
            else tool_name in self.command_tools
        )
        read_only = capability.read_only if capability is not None else False

        if mutates and not executes:
            if not paths:
                return Decision("ask", "写操作缺少 path")
            return Decision("ask", f"写入 {', '.join(paths)}")

        if executes:
            command = str(arguments.get("command", "")).strip()
            if _matches_prefix(command, self.denied_commands):
                return Decision("deny", f"命令被禁止：{command}")
            writes = detect_shell_source_writes(command)
            if writes:
                return Decision(
                    "ask",
                    "命令疑似修改源码 "
                    f"({', '.join(writes)})；优先使用 edit/apply_patch",
                )
            return Decision("ask", f"执行命令：{command}")

        if read_only or tool_name in self.read_tools:
            return Decision("allow")

        if capability is not None and capability.contained_effects:
            return Decision("allow")

        return Decision("ask", f"未知工具：{tool_name}")

    def _path_denied(self, path: str, resolved: Path) -> bool:
        try:
            relative = resolved.relative_to(self.workspace).as_posix()
        except ValueError:
            relative = resolved.as_posix()

        return any(
            fnmatch.fnmatch(resolved.name, pattern)
            or fnmatch.fnmatch(relative, pattern)
            for pattern in self.denied_paths
        )


def make_before_tool_call(policy: Policy, confirm: Confirm | None):
    """把策略接成 `AgentHooks.before_tool_call`。

    `confirm` 为 `None` 时，询问类调用直接拒绝。
    """

    async def before_tool_call(
        context: BeforeToolCallContext,
    ) -> BeforeToolCallResult | None:
        decision = policy.decide(context.call.name, context.arguments)

        if decision.action == "allow":
            return None
        if decision.action == "deny":
            return BeforeToolCallResult(block=True, reason=decision.reason)

        if confirm is None:
            return BeforeToolCallResult(
                block=True,
                reason=f"需要确认但没有确认回调：{decision.reason}",
            )

        question = (
            f"允许 {context.call.name} "
            f"{json.dumps(context.arguments, ensure_ascii=False)}？"
        )
        if await confirm(question):
            return None
        return BeforeToolCallResult(block=True, reason=f"用户拒绝：{decision.reason}")

    return before_tool_call


def _matches_prefix(command: str, prefixes: tuple[str, ...]) -> bool:
    return any(
        command == prefix or command.startswith(prefix + " ") for prefix in prefixes
    )


def _argument_paths(arguments: Mapping[str, Any]) -> tuple[str, ...]:
    """Extract path-like arguments, including apply_patch headers."""
    found: list[str] = []
    path = arguments.get("path")
    if isinstance(path, str) and path:
        found.append(path)

    raw_paths = arguments.get("paths")
    if isinstance(raw_paths, list):
        found.extend(
            item for item in raw_paths if isinstance(item, str) and item
        )

    patch = arguments.get("patch")
    if isinstance(patch, str):
        found.extend(
            match.group(2)
            for match in re.finditer(
                r"(?m)^\*\*\* (Add File|Update File|Delete File): (.+)$",
                patch,
            )
        )
    return tuple(dict.fromkeys(found))


def sanitize_result(content: str) -> str:
    """去掉常见密钥形态，并对超长结果截断。"""
    for pattern, replacement in _SECRET_PATTERNS:
        content = pattern.sub(replacement, content)

    if len(content) > MAX_RESULT_CHARS:
        content = (
            content[:MAX_RESULT_CHARS]
            + f"\n\n[truncated to {MAX_RESULT_CHARS} chars]"
        )
    return content


_SOURCE_SUFFIXES = (
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".java",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".sh",
    ".yaml",
    ".yml",
    ".json",
    ".toml",
)
_REDIRECT = re.compile(
    r"(?:^|[;&|]\s*|\s)(?:\d?>\s*|\d?>>\s*)(?P<path>\"[^\"]+\"|'[^']+'|[^\s;&|]+)"
)
_TEE = re.compile(r"(?:^|[;&|]\s*|\s)tee(?:\s+-[a-zA-Z]+)*\s+(?P<path>[^\s;&|]+)")


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _is_source_path(path: str) -> bool:
    return path.lower().endswith(_SOURCE_SUFFIXES)


def detect_shell_source_writes(command: str) -> tuple[str, ...]:
    """Return source paths a shell command appears to modify.

    This is intentionally conservative and runs before execution so the policy
    can describe the risk in the approval prompt. It is heuristic, not a
    sandbox; unknown writers fall back to the normal command approval.
    """
    found: list[str] = []

    for match in _REDIRECT.finditer(command):
        path = _unquote(match.group("path"))
        if path not in {"/dev/null", "NUL"} and _is_source_path(path):
            found.append(path)
    for match in _TEE.finditer(command):
        path = _unquote(match.group("path"))
        if _is_source_path(path):
            found.append(path)

    lowered = command.lower()
    if re.search(r"\b(?:sed|perl)\b[^;&|]*\s-[a-z]*i", lowered):
        for token in reversed(_shell_tokens(command)):
            if _is_source_path(token):
                found.append(token)
                break
    if "git apply" in lowered or re.search(r"(?:^|[;&|]\s*)patch(?:\s|$)", lowered):
        found.append("<patch>")
    if re.search(r"\bpython3?\s+(?:-\s*)?(?:<<|-c\b)", lowered):
        found.append("<inline-python>")

    return tuple(dict.fromkeys(found))


def _shell_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=False)
    except ValueError:
        return command.split()


def make_after_tool_call(
    change_ledger: WorkspaceChangeLedger | None = None,
    completion_gate: CompletionGate | None = None,
    tool_outcome: ToolOutcomeObserver | None = None,
):
    """把结果治理接成 `AgentHooks.after_tool_call`；未变化时返回 `None`。"""
    warned: set[str] = set()

    async def after_tool_call(
        context: AfterToolCallContext,
    ) -> AfterToolCallResult | None:
        if change_ledger is not None:
            change_ledger.observe(
                context.call.name,
                context.arguments,
                context.result,
            )
        if completion_gate is not None:
            completion_gate.observe(
                context.call.name,
                context.arguments,
                context.result,
            )
        if tool_outcome is not None:
            tool_outcome(context.call.name, context.result.is_error)
        sanitized = sanitize_result(context.result.content)
        guidance = ""
        if context.call.name in {"bash", "powershell"}:
            command = str(context.arguments.get("command") or "")
            writes = detect_shell_source_writes(command)
            if writes and command not in warned:
                warned.add(command)
                guidance = (
                    "\n\n[tool guidance] This command appears to modify source "
                    f"files ({', '.join(writes)}). For future source edits, use "
                    "edit or apply_patch; keep Bash for builds, tests, git, and "
                    "commands that cannot be expressed by the file tools."
                )
        if sanitized == context.result.content and not guidance:
            return None
        return AfterToolCallResult(content=sanitized + guidance)

    return after_tool_call
