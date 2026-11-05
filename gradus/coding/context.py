"""上下文预算：token 估算、压缩点选择、旧消息摘要。

token 估算沿用 Pi 的做法，不引分词器：

1. 字符数折算。CJK 按 1 字 ≈ 1 token，其余按 3.5 字符 / token（Pi 的常数）；
2. 用最近一条成功 assistant 消息上报的真实 usage 做锚点，只估算它之后的消息，
   误差不会跨轮累积；
3. 只在 user 消息处切分，保证工具调用与结果不被拆开。

压缩只做两件事：把切点之前的消息交给模型摘要，切点之后保留原文。回填与重建
Agent 由 `session.py` 负责，本模块不碰会话文件。
"""

from __future__ import annotations

import json
import math
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from ..ai.base import Request
from ..ai.providers import create_provider
from ..ai.types import (
    AssistantMessage,
    Message,
    StreamError,
    TextBlock,
    TextDelta,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultMessage,
    UserMessage,
)
from .config import Settings

#: 非 CJK 字符折算比例，与 Pi 的 `CHARS_PER_TOKEN` 一致。
CHARS_PER_TOKEN = 3.5

#: 触发压缩时给输出留的余量，与 Pi 的默认值一致。
DEFAULT_RESERVE_TOKENS = 16_384

#: 压缩后保留的近期原文上限，与 Pi 的默认值一致。
DEFAULT_KEEP_RECENT_TOKENS = 20_000

#: 摘要请求的输出上限，防止摘要本身再占满一次上下文。
SUMMARY_MAX_OUTPUT_TOKENS = 4_096

#: 序列化历史时工具结果的截断长度，与 Pi 一致。
SUMMARY_TOOL_RESULT_CHARS = 2_000

SUMMARY_MARKER = "[compacted history]"

SUMMARY_SYSTEM = (
    "You compress conversation history for a coding assistant. "
    "Keep facts needed to continue the work: goals, decisions, files touched, "
    "current progress, open problems, and next steps. "
    "Drop small talk and repeated tool output. Be concise."
)

SUMMARY_INSTRUCTION = (
    "Summarize the conversation below in under 400 words. "
    "Use short markdown sections: Goal, Progress, Decisions, Files, Next Steps."
)

#: 一个宽字符（CJK / 假名 / 谚文）在常见分词器里约占 1 个 token。
_WIDE_RANGES = (
    (0x1100, 0x115F),
    (0x2E80, 0x303E),
    (0x3041, 0x33FF),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xA000, 0xA4CF),
    (0xAC00, 0xD7A3),
    (0xF900, 0xFAFF),
    (0xFE30, 0xFE4F),
    (0xFF00, 0xFF60),
    (0xFFE0, 0xFFE6),
    (0x20000, 0x3FFFD),
)


class CompactionError(RuntimeError):
    """摘要请求失败，本次不压缩。"""


@dataclass(frozen=True)
class ContextUsage:
    """一次上下文估算的结果。

    `usage_tokens` 是锚点消息上报的真实值，`trailing_tokens` 是其后消息的估算值；
    没有可用锚点时 `last_usage_index` 为 `None`，`trailing_tokens` 即全量估算。
    """

    tokens: int
    usage_tokens: int
    trailing_tokens: int
    last_usage_index: int | None


@dataclass(frozen=True)
class Compaction:
    """一次压缩的结果；`first_kept` 是保留原文的起始下标。"""

    summary: str
    first_kept: int
    tokens_before: int


def _is_wide(char: str) -> bool:
    code = ord(char)
    return any(start <= code <= end for start, end in _WIDE_RANGES)


def estimate_text_tokens(text: str) -> int:
    """按字符类型折算 token：宽字符 1 个 1 token，其余 3.5 个 1 token。"""
    if not text:
        return 0
    wide = sum(1 for char in text if _is_wide(char))
    narrow = len(text) - wide
    return math.ceil(wide + narrow / CHARS_PER_TOKEN)


def estimate_message_tokens(message: Message) -> int:
    """估算单条消息的 token，口径对齐 Pi：只算真正会进请求的内容。"""
    if isinstance(message, UserMessage):
        return estimate_text_tokens(message.content)
    if isinstance(message, ToolResultMessage):
        return estimate_text_tokens(message.content)

    total = 0
    for block in message.content:
        if isinstance(block, TextBlock):
            total += estimate_text_tokens(block.text)
        elif isinstance(block, ThinkingBlock):
            total += estimate_text_tokens(block.thinking)
        elif isinstance(block, ToolCallBlock):
            total += estimate_text_tokens(block.name)
            total += estimate_text_tokens(_dump(block.arguments))
    return total


def estimate_messages_tokens(messages: Sequence[Message]) -> int:
    return sum(estimate_message_tokens(message) for message in messages)


def context_tokens(messages: Sequence[Message]) -> ContextUsage:
    """估算当前上下文，优先用最近一条成功 assistant 的真实 usage 做锚点。"""
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, AssistantMessage) or message.usage is None:
            continue
        if message.stop_reason in {"error", "aborted"}:
            continue

        used = message.usage.input_tokens + message.usage.output_tokens
        if used <= 0:
            continue

        trailing = estimate_messages_tokens(messages[index + 1 :])
        return ContextUsage(used + trailing, used, trailing, index)

    total = estimate_messages_tokens(messages)
    return ContextUsage(total, 0, total, None)


def should_compact(
    messages: Sequence[Message],
    context_window: int,
    reserve_tokens: int = DEFAULT_RESERVE_TOKENS,
) -> bool:
    """上下文是否已经吃掉预留余量。"""
    threshold = max(context_window - reserve_tokens, 0)
    return context_tokens(messages).tokens > threshold


def select_cut(
    messages: Sequence[Message],
    keep_recent_tokens: int = DEFAULT_KEEP_RECENT_TOKENS,
) -> int | None:
    """从末尾往前累加，找最早一个仍在上限内的 user 消息下标。

    只切在 user 消息处：assistant 的 tool_call 与随后的 tool_result 因此不会被拆开。
    返回 `None` 表示没有值得压缩的部分（切点会落在 0）。
    """
    total = 0
    cut: int | None = None

    for index in range(len(messages) - 1, -1, -1):
        total += estimate_message_tokens(messages[index])
        if total > keep_recent_tokens:
            break
        if isinstance(messages[index], UserMessage) and index > 0:
            cut = index

    return cut


def trim_messages(
    messages: Sequence[Message],
    context_window: int,
    reserve_tokens: int = DEFAULT_RESERVE_TOKENS,
) -> list[Message]:
    """请求前的兜底裁剪：从头部按 user 边界整块丢弃，直到符合预算。

    压缩解决的是「历史太长」；单轮工具结果就能撑爆窗口时，靠这里保证请求仍能发出去。
    找不到下一个 user 边界时不裁剪，避免拆散工具调用与结果。
    """
    budget = max(context_window - reserve_tokens, 0)
    kept = list(messages)

    while kept and estimate_messages_tokens(kept) > budget:
        index = next(
            (
                position
                for position in range(1, len(kept))
                if isinstance(kept[position], UserMessage)
            ),
            None,
        )
        if index is None:
            break
        kept = kept[index:]

    return kept


def serialize_messages(
    messages: Sequence[Message],
    tool_result_chars: int = SUMMARY_TOOL_RESULT_CHARS,
) -> str:
    """把历史拍成纯文本，避免模型把摘要请求当成继续对话。"""
    lines: list[str] = []

    for message in messages:
        if isinstance(message, UserMessage):
            lines.append(f"[User]: {message.content}")
        elif isinstance(message, ToolResultMessage):
            lines.append(f"[Tool result]: {_clip(message.content, tool_result_chars)}")
        else:
            for block in message.content:
                if isinstance(block, TextBlock):
                    lines.append(f"[Assistant]: {block.text}")
                elif isinstance(block, ThinkingBlock):
                    continue
                elif isinstance(block, ToolCallBlock):
                    lines.append(
                        f"[Assistant tool call]: {block.name}({_dump(block.arguments)})"
                    )

    return "\n".join(lines)


def summary_message(summary: str) -> UserMessage:
    """压缩摘要回填到历史里的形状。"""
    return UserMessage(content=f"{SUMMARY_MARKER}\n{summary}")


def make_summarizer(
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
    max_output_tokens: int = SUMMARY_MAX_OUTPUT_TOKENS,
) -> Callable[[str], Awaitable[str]]:
    """用会话同一个模型和连接池做摘要；失败抛 `CompactionError`。"""

    async def run(text: str) -> str:
        provider = create_provider(settings.spec, settings.api_key, client=client)
        request = Request(
            system=SUMMARY_SYSTEM,
            messages=[UserMessage(content=f"{SUMMARY_INSTRUCTION}\n\n{text}")],
            max_output_tokens=max_output_tokens,
        )

        parts: list[str] = []
        async for event in provider.stream(request):
            if isinstance(event, TextDelta):
                parts.append(event.delta)
            elif isinstance(event, StreamError):
                raise CompactionError(event.error_message)

        return "".join(parts).strip()

    return run


async def compact(
    messages: Sequence[Message],
    *,
    summarizer: Callable[[str], Awaitable[str]],
    keep_recent_tokens: int = DEFAULT_KEEP_RECENT_TOKENS,
) -> Compaction | None:
    """摘要切点之前的消息；没有可压缩的内容时返回 `None`。"""
    cut = select_cut(messages, keep_recent_tokens)
    if cut is None:
        return None

    summary = (await summarizer(serialize_messages(messages[:cut]))).strip()
    if not summary:
        return None

    return Compaction(
        summary=summary,
        first_kept=cut,
        tokens_before=context_tokens(messages).tokens,
    )


def _dump(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [{len(text) - limit} chars truncated]"
