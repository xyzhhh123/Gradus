"""事件渲染。

负责：把 `AgentEvent` 渲染成终端文本。
不负责：定义或组装 hook（agent 的 `AgentHooks`）、权限判断（`permissions.py`）。

约束：只消费事件，不改事件；返回 `None` 表示该事件无需输出。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from ..agent.types import (
    AgentDone,
    AgentError,
    AgentEvent,
    ToolExecutionEnd,
    ToolExecutionStart,
)
from ..ai.types import TextDelta

_MAX_PREVIEW = 120


def render(event: AgentEvent) -> str | None:
    """把事件转成终端文本；返回 `None` 表示不需要输出。"""
    if isinstance(event, TextDelta):
        return event.delta

    if isinstance(event, ToolExecutionStart):
        arguments = _shorten(json.dumps(event.call.arguments, ensure_ascii=False))
        return f"\n[tool] {event.call.name} {arguments}"

    if isinstance(event, ToolExecutionEnd):
        status = "error" if event.result.is_error else "ok"
        duration = (
            f" {event.duration_ms}ms" if event.duration_ms is not None else ""
        )
        first_line = event.result.content.splitlines()[0] if event.result.content else ""
        preview = f": {_shorten(first_line)}" if first_line else ""
        return f"\n[tool] {event.call.name} {status}{duration}{preview}"

    if isinstance(event, AgentDone):
        return (
            f"\n[done] {event.turns} turns, "
            f"in {event.usage.input_tokens} / out {event.usage.output_tokens} tokens"
        )

    if isinstance(event, AgentError):
        return f"\n[error] {event.kind}: {event.message}"

    return None


def render_log(event: AgentEvent) -> str | None:
    """把需要留痕的事件转成一行 JSON；其余事件返回 `None`。"""
    timestamp = datetime.now(timezone.utc).isoformat()

    if isinstance(event, ToolExecutionEnd):
        payload = {
            "ts": timestamp,
            "event": "tool_end",
            "tool": event.call.name,
            "call_id": event.call.id,
            "duration_ms": event.duration_ms,
            "is_error": event.result.is_error,
        }
    elif isinstance(event, AgentError):
        payload = {
            "ts": timestamp,
            "event": "agent_error",
            "kind": event.kind,
            "message": event.message,
        }
    else:
        return None

    return json.dumps(payload, ensure_ascii=False)


def _shorten(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= _MAX_PREVIEW:
        return text
    return text[: _MAX_PREVIEW - 3] + "..."
