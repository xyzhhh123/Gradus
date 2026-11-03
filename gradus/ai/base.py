"""ai 层对外接口：请求、模型配置、工具声明、Provider 契约。

依赖方向：providers/* -> base -> types。本模块不 import 任何厂商 SDK。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from .types import AssistantEvent, Message

ProviderName = Literal[
    "openai_compat",
    "anthropic",
    "gemini",
    "openai_responses",
    "fake",
]

ReasoningLevel = Literal["minimal", "low", "medium", "high"]

# 思考档位对应的 token 预算，只给按预算控制强度的厂商（Anthropic、Gemini）用。
# 数值沿用 Pi 的默认表；我们没有 xhigh / max，四档足够区分"少想"和"多想"。
THINKING_BUDGETS: dict[ReasoningLevel, int] = {
    "minimal": 1024,
    "low": 2048,
    "medium": 8192,
    "high": 16384,
}


@dataclass(frozen=True)
class ToolSpec:
    """工具的对外声明，parameters 是 JSON Schema。"""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ModelSpec:
    """一个模型的接入参数，不含任何凭证。

    同一套 openai_compat 适配器靠这张表覆盖 DeepSeek / Qwen / Kimi / GLM 等，
    不为每个品牌写一个类。api_key 由上层读好，构造 Provider 时传入。
    """

    provider: ProviderName
    model: str
    base_url: str
    context_window: int
    max_output_tokens: int


@dataclass(frozen=True)
class Request:
    """一次补全请求。messages 是全量历史，不使用服务端会话。

    模型名与 base_url 只从 Provider.spec 取，Request 里不重复。
    max_output_tokens 是本次调用的覆盖值，None 表示用 spec 的上限。
    reasoning 是思考强度档位，None 表示不下发参数、由厂商默认决定；
    各适配器把它翻成本厂商的字段，不支持的厂商静默忽略。
    """

    system: str
    messages: list[Message]
    tools: list[ToolSpec] = field(default_factory=list)
    max_output_tokens: int | None = None
    temperature: float | None = None
    reasoning: ReasoningLevel | None = None


class Provider(Protocol):
    """适配器契约。

    唯一出口是 stream()，返回 12 个事件的异步迭代器。

    契约：
    1. 除 `asyncio.CancelledError` 外不得抛出异常；网络错误、HTTP 4xx/5xx、解析失败
       一律编码为 StreamError。取消必须能穿透——调用方靠它打断一次阻塞读。
    2. 正常结束必须发且只发一个 StreamDone，之后迭代结束。
    3. 事件顺序：StreamStart -> 各内容块的三阶段 -> StreamDone / StreamError。
    4. content_index 由本层统一编号，从 0 递增，与厂商自己的索引无关。

    构造约定：所有实现都必须能以 (spec, api_key) 构造。api_key 由上层读好传入，
    本层不读环境变量；空字符串表示端点不需要鉴权（如本地 Ollama）。
    Protocol 表达不了构造函数，这条靠实现与测试保证。

    同一个实例可以被多个会话并发调用：真实适配器的 `stream()` 不写实例状态，
    每次调用各自持有自己的 assembler 与缓冲。fake 例外——它按剧本计数。
    """

    spec: ModelSpec

    def stream(self, request: Request) -> AsyncIterator[AssistantEvent]:
        ...
