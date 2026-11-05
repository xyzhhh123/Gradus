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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..agent.hooks import (
    AfterToolCallContext,
    AfterToolCallResult,
    BeforeToolCallContext,
    BeforeToolCallResult,
)

Action = Literal["allow", "ask", "deny"]
Confirm = Callable[[str], Awaitable[bool]]

READ_TOOLS = frozenset(
    {
        "read_file",
        "list_dir",
        "grep",
        "glob",
        "tool_search",
        "list_mcp_resources",
        "list_mcp_resource_templates",
        "read_mcp_resource",
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

    def decide(self, tool_name: str, arguments: dict[str, Any]) -> Decision:
        path = arguments.get("path")
        if isinstance(path, str) and self._path_denied(path):
            return Decision("deny", f"受保护的路径：{path}")

        if tool_name in self.write_tools:
            if not isinstance(path, str) or not path:
                return Decision("ask", "写操作缺少 path")
            resolved = _resolve(path, self.workspace)
            if not resolved.is_relative_to(self.workspace):
                return Decision("ask", f"写入 workspace 之外：{path}")
            return Decision("ask", f"写入 {path}")

        if tool_name in self.command_tools:
            command = str(arguments.get("command", "")).strip()
            if _matches_prefix(command, self.denied_commands):
                return Decision("deny", f"命令被禁止：{command}")
            return Decision("ask", f"执行命令：{command}")

        if tool_name in self.read_tools:
            return Decision("allow")

        return Decision("ask", f"未知工具：{tool_name}")

    def _path_denied(self, path: str) -> bool:
        resolved = _resolve(path, self.workspace)
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


def _resolve(path: str, workspace: Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    return candidate.resolve()


def _matches_prefix(command: str, prefixes: tuple[str, ...]) -> bool:
    return any(
        command == prefix or command.startswith(prefix + " ") for prefix in prefixes
    )


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


def make_after_tool_call():
    """把结果治理接成 `AgentHooks.after_tool_call`；未变化时返回 `None`。"""

    async def after_tool_call(
        context: AfterToolCallContext,
    ) -> AfterToolCallResult | None:
        sanitized = sanitize_result(context.result.content)
        if sanitized == context.result.content:
            return None
        return AfterToolCallResult(content=sanitized)

    return after_tool_call
