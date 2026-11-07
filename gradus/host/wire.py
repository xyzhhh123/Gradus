"""Agent 事件与 Web 协议之间的形状转换。

只做转换，不做 IO，也不持有会话。与 `web/src/protocol/frames.ts` 一一对应：
改动任一字段都要同步另一端。
"""

from __future__ import annotations

from typing import Any

from ..agent.types import (
    AgentDone,
    AgentError,
    AgentStart,
    ToolExecutionEnd,
    ToolExecutionStart,
    ToolExecutionUpdate,
    TurnEnd,
    TurnStart,
)
from ..ai.types import (
    AssistantMessage,
    Message,
    TextBlock,
    TextDelta,
    ToolCallBlock,
    Usage,
    UserMessage,
)

PROTOCOL_VERSION = 1

# 前端据此判断能不能显示某些内容；暂时只有回显用户消息。
CAPABILITIES = ("session/message",)


def usage_payload(usage: Usage) -> dict[str, int]:
    return {"inputTokens": usage.input_tokens, "outputTokens": usage.output_tokens}


def tool_call_payload(call: ToolCallBlock) -> dict[str, Any]:
    return {"id": call.id, "name": call.name, "args": call.arguments}


def encode_event(event: Any) -> dict[str, Any] | None:
    """把一条 `AgentEvent` 转成 wire payload；不认识的返回 `None`。

    ai 层的 12 个事件里只转 `TextDelta`：思考增量、工具参数增量不进入 UI 主流程。
    """
    if isinstance(event, AgentStart):
        return {"type": "agent/start"}
    if isinstance(event, TurnStart):
        return {"type": "turn/start", "turn": event.turn}
    if isinstance(event, TextDelta):
        return {"type": "assistant/delta", "text": event.delta}
    if isinstance(event, ToolExecutionStart):
        return {"type": "tool/start", "call": tool_call_payload(event.call)}
    if isinstance(event, ToolExecutionUpdate):
        return {"type": "tool/update", "id": event.call.id, "partial": event.partial}
    if isinstance(event, ToolExecutionEnd):
        return {
            "type": "tool/end",
            "id": event.call.id,
            "result": {
                "content": event.result.content,
                "isError": event.result.is_error,
            },
            "durationMs": event.duration_ms,
        }
    if isinstance(event, TurnEnd):
        return {"type": "turn/end", "usage": usage_payload(event.usage)}
    if isinstance(event, AgentDone):
        return {
            "type": "agent/done",
            "usage": usage_payload(event.usage),
            "turns": event.turns,
        }
    if isinstance(event, AgentError):
        return {"type": "agent/error", "kind": event.kind, "message": event.message}
    return None


def message_payload(message: Message, *, message_id: str) -> dict[str, Any] | None:
    """历史消息 → `MessageWire`。

    只保留文本：工具结果消息不单独呈现，等事件级日志补上后由工具卡片承担。
    """
    if isinstance(message, UserMessage):
        return {"id": message_id, "role": "user", "text": message.content}
    if isinstance(message, AssistantMessage):
        text = "".join(
            block.text for block in message.content if isinstance(block, TextBlock)
        )
        if not text:
            return None
        return {"id": message_id, "role": "assistant", "text": text}
    return None


def history_payload(messages: tuple[Message, ...]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        payload = message_payload(message, message_id=f"m-{index}")
        if payload is not None:
            payloads.append(payload)
    return payloads


def snapshot_payload(session: Any) -> dict[str, Any]:
    """连接建立后的第一帧。`session` 是 `gradus.coding.session.Session`。"""
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": list(CAPABILITIES),
        "messages": history_payload(session.messages),
        "status": "idle",
        "tools": [tool.spec.name for tool in session.tools],
        "sandbox": {
            "backend": session.sandbox_backend,
            "warning": session.sandbox_warning,
        },
    }
