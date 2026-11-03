"""Gradus ai 层内部协议：与提供商无关的消息形状与流式事件。

本模块是四家提供商（OpenAI 兼容 / Anthropic / Gemini / Responses）与 agent 层之间
唯一的共同语言：
- 消息：内容块 + 三种消息，不含任何厂商字段名
- 事件：一次流式响应切成 12 个事件，结构为 1 + 3×3 + 2

约定：本模块除标准库外不 import 任何东西，也不 import gradus 内的其它模块。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Union

# =============================================================================
# 用量
# =============================================================================


@dataclass(frozen=True)
class Usage:
    """一次请求的 token 用量。分多个事件给出的用量（如 Anthropic）由适配器合并。

    四家对"输入"的记账方式不同（OpenAI 系含缓存，Anthropic 不含），适配器在出口
    统一成一种含义：`input_tokens` 是总输入，缓存两项是它的子集。因此可推导

        未缓存输入 = input_tokens - cache_read_tokens - cache_write_tokens

    `cache_read_tokens` 为 0 有两种可能：真的没命中缓存，或该厂商不上报这个字段，
    内部形状无法区分（D25）。
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )


# =============================================================================
# 终止原因
# =============================================================================

# 终止原因。除了"为什么停"，它还规定上层下一步怎么做：
#   stop      正常说完       -> 有工具结果就再发一轮，没有就结束
#   length    被输出上限截断  -> 整批工具都不得执行（可能还有调用没写出来）
#   tool_use  要求执行工具    -> 先确认 arguments 非空
#   deferred  服务端暂停      -> 把消息原样再发一次，接着跑（pause_turn）
#   filtered  被拦截 / 拒答   -> 不是失败，别自动重试，向用户说明
#   error     运行时失败      -> 看 StreamError.error_message，视情况重试
#   aborted   调用方中断      -> 丢弃，或保留 partial 里已收到的部分
# 失败走 StreamError，它没有 stop_reason，只有 kind（error / aborted）。
StopReason = Literal[
    "stop",
    "length",
    "tool_use",
    "deferred",
    "filtered",
    "error",
    "aborted",
]


# =============================================================================
# 内容块
# =============================================================================


@dataclass
class TextBlock:
    """一段文本。

    refusal 为真表示这段是模型的拒答内容（安全策略下的拒绝），不是正常回答。
    事件序列与普通文本完全一样，消费方靠这个字段决定怎么呈现。

    只有部分厂商给得出这个信号：官方 OpenAI 两套形状有独立字段（Chat Completions
    的 delta.refusal、Responses 的 part.type == "refusal"）；Anthropic / Gemini
    没有，只能靠 stop_reason == "filtered" 知道整条被拒，定位不到具体段落；
    兼容端点（Kimi / DeepSeek 等）连 filtered 都可能没有，拒答文字会混在正文里。

    它与 stop_reason 是两条独立信息：有标签不一定 filtered（被 length 截断时保留
    length），filtered 也不一定有标签。消费方不能假设标签一定存在（D18 / D21）。
    """

    text: str
    refusal: bool = False


@dataclass
class ThinkingBlock:
    """思考内容。

    signature 是给厂商回传的不透明数据：Anthropic 的 thinking signature、
    Responses 的 encrypted_content。DeepSeek / Gemini 留 None。
    """

    thinking: str
    signature: str | None = None


@dataclass
class ToolCallBlock:
    """工具调用。arguments 在内部永远是已解析的 dict。

    raw 是厂商给的原始 JSON 文本，流式期间逐步变长，用来观察参数生成进度，
    也能在解析失败（截断）时看出收到了什么。它与 arguments 是同一份数据的
    两种表示，回传厂商时以 arguments 为准。
    """

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw: str = ""


ContentBlock = Union[TextBlock, ThinkingBlock, ToolCallBlock]


# =============================================================================
# 消息
# =============================================================================


@dataclass
class UserMessage:
    content: str


@dataclass
class AssistantMessage:
    content: list[ContentBlock] = field(default_factory=list)
    stop_reason: StopReason | None = None
    usage: Usage | None = None


@dataclass
class ToolResultMessage:
    """工具结果是一等消息，不依附角色。

    OpenAI 映射为 role="tool"，Anthropic / Gemini 映射为带 tool_result /
    functionResponse 的 user 消息，由适配器决定。
    """

    tool_call_id: str
    content: str
    is_error: bool = False


Message = Union[UserMessage, AssistantMessage, ToolResultMessage]


# =============================================================================
# 流式事件（12 个）
# =============================================================================
# 每个事件都带 partial：到目前为止累积出来的完整消息快照。
# 好处是 UI 不需要自己攒 delta；中止时也能直接把 partial 落成一条消息。


@dataclass
class StreamStart:
    """1. 请求已被接受，开始接收。"""

    partial: AssistantMessage


@dataclass
class TextStart:
    """2. 文本块开始。"""

    content_index: int
    partial: AssistantMessage


@dataclass
class TextDelta:
    """3. 文本增量。"""

    content_index: int
    delta: str
    partial: AssistantMessage


@dataclass
class TextEnd:
    """4. 文本块结束，text 为该块完整内容。"""

    content_index: int
    text: str
    partial: AssistantMessage


@dataclass
class ThinkingStart:
    """5. 思考块开始。"""

    content_index: int
    partial: AssistantMessage


@dataclass
class ThinkingDelta:
    """6. 思考增量。"""

    content_index: int
    delta: str
    partial: AssistantMessage


@dataclass
class ThinkingEnd:
    """7. 思考块结束；signature 原样保存，下一轮回传给厂商。"""

    content_index: int
    thinking: str
    signature: str | None
    partial: AssistantMessage


@dataclass
class ToolCallStart:
    """8. 工具调用开始，此后的 delta 拼的是参数 JSON 字符串。"""

    content_index: int
    id: str
    name: str
    partial: AssistantMessage


@dataclass
class ToolCallDelta:
    """9. 参数 JSON 字符串片段，拼接与解析由适配器负责。"""

    content_index: int
    delta: str
    partial: AssistantMessage


@dataclass
class ToolCallEnd:
    """10. 工具调用结束，arguments 为解析后的 dict。"""

    content_index: int
    arguments: dict[str, Any]
    partial: AssistantMessage


@dataclass
class StreamDone:
    """11. 正常终止。message 是最终消息，等价于最后一个 partial。"""

    stop_reason: StopReason
    message: AssistantMessage
    usage: Usage


@dataclass
class StreamError:
    """12. 失败终止。契约要求所有失败都走这里，不允许抛异常。"""

    kind: Literal["error", "aborted"]
    error_message: str
    partial: AssistantMessage


AssistantEvent = Union[
    StreamStart,
    TextStart,
    TextDelta,
    TextEnd,
    ThinkingStart,
    ThinkingDelta,
    ThinkingEnd,
    ToolCallStart,
    ToolCallDelta,
    ToolCallEnd,
    StreamDone,
    StreamError,
]
