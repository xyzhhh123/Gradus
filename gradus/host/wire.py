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

#: 可选能力：宿主能弹系统「选择文件夹」对话框时才会报出去。是否算数由
#: `host/serve.py` 启动时探测一次决定；前端据此决定要不要显示「浏览…」。
PICK_CAPABILITY = "workspace/pick"


def usage_payload(usage: Usage) -> dict[str, int]:
    return {"inputTokens": usage.input_tokens, "outputTokens": usage.output_tokens}


def usage_totals_payload(totals: dict[str, int]) -> dict[str, int]:
    """会话累计用量：状态栏用，字段名跟事件里的 `usage` 保持一致。"""
    return {
        "turns": totals.get("turns", 0),
        "inputTokens": totals.get("input", 0),
        "outputTokens": totals.get("output", 0),
        "cacheReadTokens": totals.get("cacheRead", 0),
        "cacheWriteTokens": totals.get("cacheWrite", 0),
    }


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


def model_summary(settings: Any) -> dict[str, Any]:
    """当前模型的展示信息；`settings` 是 `coding.config.Settings`。

    除了名字还给**能力与预算**：窗口上限来自模型元数据/静态表，预算口径可能被显式值
    压低，触发线与保留量由两者推导。界面上「这个模型能用多大、现在按多大管」需要
    这三个数才说得清，所以在这里一次算好，前端不必重算。
    """
    from ..coding.context import ContextBudget, ContextBudgetError

    spec = settings.spec
    name = next(
        (choice.name for choice in settings.models if choice.spec == spec),
        f"{spec.model} · {spec.provider}",
    )
    summary: dict[str, Any] = {
        "name": name,
        "provider": spec.provider,
        "model": spec.model,
    }
    # 能力与预算只有在知道的时候才报：缺席表示「这个端点没说」，不是「0」。
    window = getattr(spec, "context_window", None)
    output = getattr(spec, "max_output_tokens", None)
    if window is None or output is None:
        return summary

    summary["contextWindow"] = window
    summary["maxOutputTokens"] = output
    for key, value in (
        ("windowLimit", getattr(spec, "window_limit", None)),
        ("outputLimit", getattr(spec, "output_limit", None)),
        ("keepPreset", getattr(settings, "keep_preset", None)),
    ):
        if value is not None:
            summary[key] = value

    try:
        budget = ContextBudget.for_model(
            spec.context_window,
            spec.max_output_tokens,
            reserve_tokens=getattr(settings, "context_reserve_tokens", None)
            or 16_384,
            keep_tokens=getattr(settings, "context_keep_tokens", None),
            keep_preset=getattr(settings, "keep_preset", "balanced"),
        )
    except ContextBudgetError as exc:
        summary["budgetError"] = str(exc)
        return summary
    summary["compactionThreshold"] = budget.threshold
    summary["keepRecent"] = budget.keep_recent
    return summary


def snapshot_payload(
    session: Any, capabilities: tuple[str, ...] = CAPABILITIES
) -> dict[str, Any]:
    """连接建立后的第一帧。`session` 是 `gradus.coding.session.Session`。

    `capabilities` 由宿主按当前环境裁剪：服务不了的能力不报，前端就不显示入口。
    """
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": list(capabilities),
        "messages": history_payload(session.messages),
        "status": "idle",
        "tools": [tool.spec.name for tool in session.tools],
        "workspace": str(session.workspace),
        "model": model_summary(session.settings),
        "sandbox": {
            "backend": session.sandbox_backend,
            "warning": session.sandbox_warning,
        },
    }
