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

import hashlib
import json
import math
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from ..ai.base import Request, ToolSpec
from ..ai.providers import create_provider
from ..ai.types import (
    AssistantMessage,
    Message,
    StreamDone,
    StreamError,
    TextBlock,
    TextDelta,
    ThinkingBlock,
    ToolCallBlock,
    ToolCallStart,
    ToolResultMessage,
    UserMessage,
)
from .config import Settings

#: 非 CJK 字符折算比例，与 Pi 的 `CHARS_PER_TOKEN` 一致。
CHARS_PER_TOKEN = 3.5

#: 触发压缩时给输出留的余量，与 Pi 的默认值一致。
DEFAULT_RESERVE_TOKENS = 16_384

#: 压缩后保留的近期原文上限（旧口径：固定值）。保留给退化窗口与显式覆盖用。
DEFAULT_KEEP_RECENT_TOKENS = 20_000

#: Internal operating policy. These are policy constants, not user settings:
#: a proportional trigger bounded by a safe floor and ceiling.
DEFAULT_COMPACT_RATIO = 0.75
MIN_COMPACT_TRIGGER_TOKENS = 32_768
MAX_COMPACT_TRIGGER_TOKENS = 160_000
DEFAULT_TOOL_RESULT_PRUNE_CHARS = 8_192
DEFAULT_TOOL_RESULT_PRUNE_HEAD_CHARS = 4_096
DEFAULT_TOOL_RESULT_PRUNE_TAIL_CHARS = 1_024
TOOL_RESULT_PRUNE_MARKER = "\n\n[... tool result middle pruned ...]\n\n"

#: 保留量的三个预设：`(占触发线的比例, 下限, 上限)`。
#:
#: 为什么按比例：固定 20,000 在 20 万窗口上占 11%、在 1M 窗口上只占 2%——窗口越大，
#: 压完丢得越多、压得越勤，而每次压缩都要重建一次前缀缓存。按比例缩放才对得上
#: 「窗口是买来的容量」这件事；上限是防止大窗口把单次请求的输入成本顶上去。
#:
#: 下限只在小窗口下起作用（还会被 `触发线 / 2` 再压一次，保证压完确实明显变小）。
KEEP_PRESETS: dict[str, tuple[float, int, int]] = {
    "conservative": (0.10, 8_000, 24_000),
    "balanced": (0.25, 12_000, 64_000),
    "aggressive": (0.40, 16_000, 120_000),
}

DEFAULT_KEEP_PRESET = "balanced"

#: 预设的中文名与一句话说明，给界面用；改 `KEEP_PRESETS` 时一起改这里。
KEEP_PRESET_LABELS: dict[str, tuple[str, str]] = {
    "conservative": ("省（压得更狠）", "保留触发线的 10%，下限 8k、上限 24k；请求更小、压缩更频繁"),
    "balanced": ("均衡（默认）", "保留触发线的 25%，下限 12k、上限 64k"),
    "aggressive": ("激进（少压几次）", "保留触发线的 40%，下限 16k、上限 120k；单次请求更大"),
}


def preset_catalog() -> list[dict[str, str]]:
    """界面上可选的预设：`[{name, label, detail}]`，顺序固定。"""
    return [
        {
            "name": name,
            "label": KEEP_PRESET_LABELS.get(name, (name, ""))[0],
            "detail": KEEP_PRESET_LABELS.get(name, ("", ""))[1],
        }
        for name in KEEP_PRESETS
    ]


#: 压缩失败后的退避：连续失败第 n 次就跳过这么多轮再试（封顶 8）。
#: 设计里写明「失败后若干轮内不再自动尝试」——不这么做的话，一次摘要失败会变成
#: 每轮都重付一次摘要请求的钱，而且往往注定失败（例如端点一直回 400）。
COMPACT_RETRY_SKIPS = (1, 2, 4, 8)

#: 摘要请求的输出上限，防止摘要本身再占满一次上下文。
SUMMARY_MAX_OUTPUT_TOKENS = 4_096

#: 序列化历史时工具结果的截断长度，与 Pi 一致。
SUMMARY_TOOL_RESULT_CHARS = 2_000

#: 兜底摘要里助手正文压成一行后保留多少字符（多了就只剩开头一句）。
SUMMARY_PROSE_CHARS = 200

SUMMARY_MARKER = "[compacted history]"

SUMMARY_SYSTEM = (
    "You compress conversation history for a coding assistant. "
    "Keep facts needed to continue the work: goals, decisions, files touched, "
    "current progress, open problems, and next steps. "
    "Drop small talk and repeated tool output. Be concise."
)

#: 视图里最多保留几份摘要；超了就把最旧两份合并成一份。
MAX_SUMMARY_LAYERS = 3

#: 视图里所有摘要的合计长度上限（估算 token）。份数没超但都很长时也合并。
MAX_SUMMARY_TOKENS = 1_500

#: 合并两份摘要用的指令。合并发生在压缩之后，不是每轮。
MERGE_INSTRUCTION = (
    "把下面两份交接摘要合并成一份。保留所有约束、决策、动过的文件、"
    "错误与修复、待办；去掉重复表述，不要丢弃事实。只输出合并后的摘要。"
)


async def merge_summaries(
    settings: Settings,
    texts: Sequence[str],
    *,
    client: httpx.AsyncClient | None = None,
    max_output_tokens: int = SUMMARY_MAX_OUTPUT_TOKENS,
) -> str:
    """把若干份旧摘要合成一份；失败抛 `CompactionError`。"""
    body = "\n\n---\n\n".join(texts)
    provider = create_provider(settings.spec, settings.api_key, client=client, headers=settings.headers)
    request = Request(
        system=SUMMARY_SYSTEM,
        messages=[UserMessage(content=f"{MERGE_INSTRUCTION}\n\n{body}")],
        max_output_tokens=max_output_tokens,
    )

    parts: list[str] = []
    async for event in provider.stream(request):
        if isinstance(event, TextDelta):
            parts.append(event.delta)
        elif isinstance(event, StreamError):
            raise CompactionError(event.error_message)
    return "".join(parts).strip()


#: 子 agent 的消息在上下文里的形状（`Bus.render` 产出的 `<message from=...>`）。
#: 它们算「工作产物」：压缩时不该被一起摘要掉。
WORK_PRODUCT_PREFIX = "<message from="


def is_work_product(message: Message) -> bool:
    """子 agent 发来的结果。"""
    return isinstance(message, UserMessage) and message.content.lstrip().startswith(
        WORK_PRODUCT_PREFIX
    )


def keep_recent_work_products(
    messages: Sequence[Message],
    first_kept: int,
    base: int,
    *,
    recent: int = 3,
) -> int:
    """把切点前移到「最近几条工作产物所在的轮次起点」，让它们留在视野里。

    摘要按 token 说话，不会区分「闲聊」和「子 agent 交回来的成果」；这里补一刀：
    最近 `recent` 条子结果所在的那一轮不进入被摘要的区段。
    """
    if first_kept <= base:
        return first_kept

    positions = [
        index
        for index in range(base, len(messages))
        if is_work_product(messages[index])
    ]
    if not positions:
        return first_kept

    guard = positions[-min(recent, len(positions))]
    if guard >= first_kept:
        return first_kept

    start = guard
    while start > base:
        message = messages[start]
        if isinstance(message, UserMessage) and message.origin == "user":
            break
        start -= 1
    return max(base, min(first_kept, start))
SUMMARY_SECTIONS = (
    "目标与意图",
    "约束与偏好",
    "已完成的工作",
    "当前状态",
    "关键决策与理由",
    "错误与修复",
    "待办",
    "关键文件与位置",
)

#: 追在重放历史末尾的摘要指令。放在最后一条用户消息里，前缀因此与主请求一致。
SUMMARY_INSTRUCTION = (
    "把上面的对话压缩成一份交接摘要，供另一个模型接着干活。"
    "不要继续对话，不要调用工具，只输出摘要。"
    "按下面 8 个栏目写，缺项写 (none)："
    + " / ".join(SUMMARY_SECTIONS)
    + "。路径、命令、报错原文、标识符与数字原样保留，不要改写用户原话。"
)

#: 摘要请求自己装不下时的退路：只把要压缩的那段拍成文本再发一次。
SUMMARY_FALLBACK_INSTRUCTION = (
    "把下面的对话压缩成交接摘要，只输出摘要。按 8 个栏目标题写："
    + " / ".join(SUMMARY_SECTIONS)
    + "；缺项写 (none)。路径、命令、报错原文原样保留。"
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


class ContextBudgetError(ValueError):
    """预算配置自相矛盾：保留量不小于触发线，压缩会原地打转。"""


def budget_reserve(
    max_output_tokens: int,
    reserve_tokens: int = DEFAULT_RESERVE_TOKENS,
) -> int:
    """给输出留的余量。

    预留至少要覆盖一次最大输出：否则「留给输出的空间」比模型一次能生成的上限还小，
    请求会被自己顶到窗口外。不同模型的 `max_output_tokens` 差别很大，所以按模型算。
    """
    return max(reserve_tokens, max_output_tokens)


def compact_threshold(
    context_window: int,
    reserve_tokens: int = DEFAULT_RESERVE_TOKENS,
) -> int:
    """触发压缩的输入 token 数。

    Use a proportional trigger with internal bounds. The caller does not need
    to tune a ratio or headroom: small windows get a safe floor, large windows
    are capped so a 1M-capable model does not accumulate a 1M working prompt.
    """
    usable = max(context_window - reserve_tokens, 0)
    if usable <= 0:
        return 0
    candidate = int(usable * DEFAULT_COMPACT_RATIO)
    lower = min(MIN_COMPACT_TRIGGER_TOKENS, usable)
    upper = min(MAX_COMPACT_TRIGGER_TOKENS, usable)
    return min(max(candidate, lower), upper)


def retention_budget(
    context_window: int,
    reserve_tokens: int = DEFAULT_RESERVE_TOKENS,
    preset: str = DEFAULT_KEEP_PRESET,
) -> int:
    """压缩后保留的原文预算：按触发线的比例给，带下限与上限。

    固定值在小窗口下会大于触发线（压完仍在线上、每轮重复付费），在大窗口下又保守得
    离谱（1M 窗口只留 2%）。所以改成三段：

        keep = clamp(比例 × 触发线, 下限, 上限)，且不超过触发线的一半

    最后那个「不超过一半」是硬约束：保证压完的视图确实明显变小，不会刚压完又触发。
    窗口小于预留（退化配置，常见于测试）时退回默认值，保持旧的「尽量多留」行为。
    """
    ratio, floor, cap = KEEP_PRESETS.get(preset, KEEP_PRESETS[DEFAULT_KEEP_PRESET])
    threshold = compact_threshold(context_window, reserve_tokens)
    if threshold <= 0:
        return DEFAULT_KEEP_RECENT_TOKENS
    keep = int(ratio * threshold)
    keep = max(floor, min(cap, keep))
    return max(1, min(keep, threshold // 2))


def validate_budget(
    context_window: int,
    reserve_tokens: int = DEFAULT_RESERVE_TOKENS,
    keep_recent_tokens: int | None = None,
    preset: str = DEFAULT_KEEP_PRESET,
) -> int:
    """校验「保留量 < 触发线」，返回实际使用的保留量。"""
    budget = ContextBudget.for_model(
        context_window,
        max_output_tokens=0,
        reserve_tokens=reserve_tokens,
        keep_tokens=keep_recent_tokens,
        keep_preset=preset,
    )
    return budget.keep_recent


@dataclass(frozen=True)
class ContextBudget:
    """一次会话的上下文预算：由模型参数推导，可在配置里微调上下限。

    三个派生量放在一起，避免「窗口、预留、保留量」散布在各处各算一遍——
    它们之间的不变式（保留量 < 触发线）只在这里表达。
    """

    window: int
    reserve: int
    keep_recent: int

    @property
    def threshold(self) -> int:
        """触发压缩的输入 token 数。"""
        return compact_threshold(self.window, self.reserve)

    @classmethod
    def for_model(
        cls,
        context_window: int,
        max_output_tokens: int = 0,
        *,
        reserve_tokens: int = DEFAULT_RESERVE_TOKENS,
        keep_tokens: int | None = None,
        keep_preset: str = DEFAULT_KEEP_PRESET,
    ) -> ContextBudget:
        """按模型推导预算；配不出可用的组合时抛 `ContextBudgetError`。

        - 预留至少覆盖一次最大输出（`max_output_tokens`），否则请求会被自己顶到窗口外；
        - `keep_tokens` 为 `None` 时按 `keep_preset` 推导：占触发线的比例，带下限与上限，
          再被「触发线的一半」压一次——窗口越大保留越多，但永远明显小于触发线；
        - 显式给出的 `keep_tokens` 视为硬要求：不小于触发线就报错，而不是悄悄改小；
        - 窗口本身小于预留（退化配置，常见于测试）时不报错，退回「尽量多留」。
        """
        if context_window <= 0:
            raise ContextBudgetError(
                f"context window must be positive, got {context_window}"
            )
        reserve = budget_reserve(max_output_tokens, reserve_tokens)
        threshold = compact_threshold(context_window, reserve)
        if threshold <= 0:
            return cls(
                context_window,
                reserve,
                DEFAULT_KEEP_RECENT_TOKENS if keep_tokens is None else keep_tokens,
            )
        if keep_tokens is None:
            keep = retention_budget(context_window, reserve, keep_preset)
        else:
            keep = keep_tokens
            if keep >= threshold:
                raise ContextBudgetError(
                    f"keep_recent {keep} >= compaction threshold {threshold} "
                    f"(context window {context_window}, reserve {reserve}): "
                    "compact would run again immediately"
                )
        return cls(context_window, reserve, keep)


@dataclass(frozen=True)
class UsageAnchor:
    """一次真实请求的用量：输入 token 数，以及该请求包含的消息条数。

    锚点绑在「请求」而不是「消息」上：压缩之后下一次请求会带来新的真实用量，
    锚点自动更新，不会出现「锚点停在压缩前、下一轮又超线」的死循环。
    """

    tokens: int
    messages: int


@dataclass(frozen=True)
class TrimResult:
    """请求前硬裁的结果：保留了什么、丢了多少条、约多少 token。"""

    kept: list[Message]
    dropped: int
    dropped_tokens: int


@dataclass(frozen=True)
class ToolResultPrune:
    """A request-view tool-result reduction; the original history is untouched."""

    messages: list[Message]
    count: int
    chars_removed: int


def prune_tool_results(
    messages: Sequence[Message],
    *,
    before: int | None = None,
    keep_recent: int = 8,
    threshold_chars: int = DEFAULT_TOOL_RESULT_PRUNE_CHARS,
    head_chars: int = DEFAULT_TOOL_RESULT_PRUNE_HEAD_CHARS,
    tail_chars: int = DEFAULT_TOOL_RESULT_PRUNE_TAIL_CHARS,
) -> ToolResultPrune:
    """Trim old tool-result middles for one model-visible view.

    This is intentionally a projection operation, not a session rewrite. The
    full result remains available in the session and can be recovered later.
    """
    if threshold_chars < 1:
        raise ValueError("threshold_chars must be positive")
    if head_chars < 0 or tail_chars < 0 or head_chars + tail_chars >= threshold_chars:
        raise ValueError("tool-result prune budgets must fit below threshold")
    if keep_recent < 0:
        raise ValueError("keep_recent must be non-negative")
    end = (
        max(0, len(messages) - keep_recent)
        if before is None
        else max(0, min(before, len(messages)))
    )
    changed: list[Message] = []
    count = 0
    chars_removed = 0
    for index, message in enumerate(messages):
        if (
            index < end
            and isinstance(message, ToolResultMessage)
            and len(message.content) > threshold_chars
        ):
            tail = message.content[-tail_chars:] if tail_chars else ""
            content = message.content[:head_chars] + TOOL_RESULT_PRUNE_MARKER + tail
            chars_removed += len(message.content) - len(content)
            count += 1
            changed.append(
                ToolResultMessage(
                    tool_call_id=message.tool_call_id,
                    content=content,
                    is_error=message.is_error,
                )
            )
        else:
            changed.append(message)
    return ToolResultPrune(changed, count, chars_removed)


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
    """一次压缩的结果；`first_kept` 是保留原文的起始下标（相对于传入的历史）。"""

    summary: str
    first_kept: int
    tokens_before: int


@dataclass(frozen=True)
class SummaryLayer:
    """一份摘要：`key` 是文件里的条目 id，`text` 是摘要正文。

    分层是为了不重写旧摘要——每代只概括「新增的那一段」，早期的结论原样留着。
    """

    key: str
    text: str


@dataclass(frozen=True)
class CompactionState:
    """已安装的压缩状态：若干份摘要 + 「从完整历史的第几条开始可见」。

    历史本身不被替换，视图由这里派生——运行中与恢复后走同一个 `project_view`。
    """

    layers: tuple[SummaryLayer, ...]
    first_kept: int
    version: int = 0

    @property
    def summary(self) -> str:
        """最新一份摘要；没有分层时就是唯一那份。"""
        return self.layers[-1].text if self.layers else ""


def project_view(
    messages: Sequence[Message],
    state: CompactionState | None,
) -> list[Message]:
    """把完整历史投影成模型可见的消息。

    没有压缩状态时原样返回；有的话是「若干份摘要 + 从 `first_kept` 起的原文」。
    这是**唯一**的视图构造入口，运行中与恢复后都必须走它。
    """
    if state is None:
        return list(messages)
    if not 0 <= state.first_kept <= len(messages):
        raise ValueError(
            f"compaction state points past the history: "
            f"{state.first_kept} > {len(messages)}"
        )
    head = [summary_message(layer.text) for layer in state.layers]
    return [*head, *messages[state.first_kept :]]


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


def context_tokens(
    messages: Sequence[Message],
    anchor: UsageAnchor | None = None,
) -> ContextUsage:
    """估算当前上下文。

    `anchor` 给出时以它为准，只估算锚点之后新增的消息；没有锚点时退回
    「从末尾找最近一条带 usage 的 assistant」。后者在压缩之后会看到压缩前的
    规模，因此调用方应在压缩后清掉锚点（下一次请求会重新给出真实值）。
    """
    if anchor is not None and 0 <= anchor.messages <= len(messages):
        trailing = estimate_messages_tokens(messages[anchor.messages :])
        return ContextUsage(
            anchor.tokens + trailing, anchor.tokens, trailing, anchor.messages
        )

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
    anchor: UsageAnchor | None = None,
) -> bool:
    """上下文是否已经吃掉预留余量。"""
    threshold = compact_threshold(context_window, reserve_tokens)
    return context_tokens(messages, anchor).tokens > threshold


def active_turn_start(messages: Sequence[Message]) -> int:
    """当前这一轮的起点：最近一条**用户自己发的**消息下标。

    插话（`origin="harness"`）属于本轮的一部分，不能当作轮次起点——否则切点
    可能落在插话上，把一轮劈成两半。
    """
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, UserMessage) and message.origin == "user":
            return index
    return 0


def select_cut(
    messages: Sequence[Message],
    keep_recent_tokens: int = DEFAULT_KEEP_RECENT_TOKENS,
    turn_start: int | None = None,
) -> int | None:
    """从末尾往前累加，找最早一个仍在上限内的 user 消息下标。

    只切在 user 消息处：assistant 的 tool_call 与随后的 tool_result 因此不会被拆开。

    `turn_start` 给出时，**切点不许落在当前轮内部**（`cut > turn_start`）：那时本轮
    已超保留预算，压缩救不了，返回 `None` 让调用方放弃。这条是显式检查，不依赖
    「活跃轮总在尾部」的隐含假设——插话、steer 之后那个假设并不成立。

    返回 `None` 表示没有值得压缩的部分。
    """
    total = 0
    cut: int | None = None

    for index in range(len(messages) - 1, -1, -1):
        total += estimate_message_tokens(messages[index])
        if total > keep_recent_tokens:
            break
        if isinstance(messages[index], UserMessage) and index > 0:
            cut = index

    if cut is None:
        return None
    if turn_start is not None and cut > turn_start:
        return None
    return cut


def plan_trim(
    messages: Sequence[Message],
    context_window: int,
    reserve_tokens: int = DEFAULT_RESERVE_TOKENS,
) -> TrimResult:
    """请求前的兜底裁剪：从头部按 user 边界整块丢弃，直到符合预算。

    压缩解决的是「历史太长」；单轮工具结果就能撑爆窗口时，靠这里保证请求仍能发出去。
    找不到下一个 user 边界时不裁剪，避免拆散工具调用与结果。

    返回被丢弃的条数与估算 token：调用方要把它记进会话文件——静默丢弃会让
    「它为什么忘事」无从回答。
    """
    budget = compact_threshold(context_window, reserve_tokens)
    kept = list(messages)
    dropped = 0
    dropped_tokens = 0

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
        dropped_tokens += estimate_messages_tokens(kept[:index])
        dropped += index
        kept = kept[index:]

    return TrimResult(kept=kept, dropped=dropped, dropped_tokens=dropped_tokens)


def trim_messages(
    messages: Sequence[Message],
    context_window: int,
    reserve_tokens: int = DEFAULT_RESERVE_TOKENS,
) -> list[Message]:
    """`plan_trim` 的兼容包装：只要裁剪后的消息。"""
    return plan_trim(messages, context_window, reserve_tokens).kept


def serialize_messages(
    messages: Sequence[Message],
    tool_result_chars: int = SUMMARY_TOOL_RESULT_CHARS,
    prose_chars: int = SUMMARY_PROSE_CHARS,
) -> str:
    """把历史拍成纯文本，避免模型把摘要请求当成继续对话。

    这里的目标是**尽量省窗口**，因为它是「重放装不下」时的兜底。三招：

    1. 工具结果截断（`tool_result_chars`）；
    2. **相同工具结果去重**：同一段输出被读多次（读同一个文件、重跑同一条命令）时，
       后面几次只留一行「与第 N 条相同」——这类重复在真实会话里非常常见；
    3. **助手正文压成一行**：旧的讲解只留开头一句 + 原长度，细节由摘要栏目承载。

    用户自己说的话原样保留：那是意图，不能概括。工具调用的名字与参数也保留，
    否则看不出「当时在干什么」。
    """
    lines: list[str] = []
    seen_results: dict[str, int] = {}

    for message in messages:
        if isinstance(message, UserMessage):
            label = "User" if message.origin == "user" else "System notice"
            lines.append(f"[{label}]: {message.content}")
        elif isinstance(message, ToolResultMessage):
            fingerprint = hashlib.sha1(
                message.content.encode("utf-8", "replace")
            ).hexdigest()
            first = seen_results.get(fingerprint)
            if first is not None:
                lines.append(f"[Tool result]: 与第 {first} 条相同（{len(message.content)} 字符，已省略）")
                continue
            seen_results[fingerprint] = len(seen_results) + 1
            lines.append(f"[Tool result]: {_clip(message.content, tool_result_chars)}")
        else:
            for block in message.content:
                if isinstance(block, TextBlock):
                    lines.append(f"[Assistant]: {_one_line(block.text, prose_chars)}")
                elif isinstance(block, ThinkingBlock):
                    continue
                elif isinstance(block, ToolCallBlock):
                    lines.append(
                        f"[Assistant tool call]: {block.name}({_dump(block.arguments)})"
                    )

    return "\n".join(lines)


def _one_line(text: str, limit: int) -> str:
    """把一段正文压成一行：换行变空格，超长截断并标出原长度。"""
    flattened = " ".join(text.split())
    if len(flattened) <= limit:
        return flattened
    return f"{flattened[:limit]}…（共 {len(flattened)} 字）"


def summary_message(summary: str) -> UserMessage:
    """压缩摘要回填到历史里的形状；标记为运行时注入而非用户发言。"""
    return UserMessage(content=f"{SUMMARY_MARKER}\n{summary}", origin="harness")


def make_summarizer(
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
    system: str = SUMMARY_SYSTEM,
    tools: Sequence[ToolSpec] = (),
    max_output_tokens: int = SUMMARY_MAX_OUTPUT_TOKENS,
) -> Callable[[Sequence[Message], int], Awaitable[str]]:
    """把可见历史**原样重放**，末尾追加一条摘要指令。

    重放而不是拍成文本，是为了让这次调用成为「上一次真实请求」的真前缀：
    工具定义、系统提示、历史逐字节相同，provider 的提示缓存可以直接复用——
    长会话下摘要的输入成本大约变成原来的十分之一。

    代价是摘要请求看起来像「继续对话」，所以指令里必须明说不要继续、不要调工具；
    真出现工具调用就当作失败（调用方会退到拍文本的兜底路径）。
    """

    async def run(history: Sequence[Message], cut: int) -> str:
        provider = create_provider(settings.spec, settings.api_key, client=client, headers=settings.headers)
        request = Request(
            system=system,
            messages=[*history, UserMessage(content=SUMMARY_INSTRUCTION)],
            tools=list(tools),
            max_output_tokens=max_output_tokens,
        )

        parts: list[str] = []
        async for event in provider.stream(request):
            if isinstance(event, TextDelta):
                parts.append(event.delta)
            elif isinstance(event, StreamDone):
                # 记下这次摘要的真实用量：它是「摘要有没有吃到前缀缓存」的唯一证据。
                # 调用方（`Session.last_summary_usage`）与真机脚本据此断言。
                run.last_usage = event.usage
            elif isinstance(event, ToolCallStart):
                raise CompactionError("摘要请求尝试调用工具")
            elif isinstance(event, StreamError):
                raise CompactionError(event.error_message)

        return "".join(parts).strip()

    run.last_usage = None
    return run


def make_text_summarizer(
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
    max_output_tokens: int = SUMMARY_MAX_OUTPUT_TOKENS,
) -> Callable[[Sequence[Message], int], Awaitable[str]]:
    """兜底摘要：把要压缩的那段拍成文本，另起一个请求。

    重放路径装不下（`CompactionError`）时才用——它没有缓存可复用，但更省窗口。
    """

    async def run(history: Sequence[Message], cut: int) -> str:
        provider = create_provider(settings.spec, settings.api_key, client=client, headers=settings.headers)
        text = serialize_messages(history[:cut])
        request = Request(
            system=SUMMARY_SYSTEM,
            messages=[
                UserMessage(content=f"{SUMMARY_FALLBACK_INSTRUCTION}\n\n{text}")
            ],
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
    summarizer: Callable[[Sequence[Message], int], Awaitable[str]],
    keep_recent_tokens: int = DEFAULT_KEEP_RECENT_TOKENS,
) -> Compaction | None:
    """摘要切点之前的消息；没有可压缩的内容时返回 `None`。

    `summarizer` 收到的是**完整的可见历史**与切点：重放式摘要需要整段历史才能
    与主请求对齐前缀，而真正要概括的是 `messages[:cut]`。
    """
    cut = select_cut(messages, keep_recent_tokens, active_turn_start(messages))
    if cut is None:
        return None

    summary = (await summarizer(list(messages), cut)).strip()
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
