"""ai 层对外接口：请求、模型配置、工具声明、Provider 契约。

依赖方向：providers/* -> base -> types。本模块不 import 任何厂商 SDK。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from uuid import uuid4

from .types import AssistantEvent, Message

ProviderName = Literal[
    "openai_compat",
    "anthropic",
    "gemini",
    "openai_responses",
    "fake",
]

ReasoningLevel = Literal["minimal", "low", "medium", "high"]

#: 自定义请求头里的占位符：换成本次运行的随机 id。
RUN_PLACEHOLDER = "{run}"


def expand_headers(
    headers: Mapping[str, str] | None,
    *,
    run_id: str | None = None,
) -> dict[str, str]:
    """展开自定义请求头：把 `{run}` 换成本次运行的随机 id。

    有些网关要求客户端带一个「会话 id」来优化路由与缓存（例如 OpenCode Go 的
    `x-opencode-session`），而这个 id 只需要在**一次会话里稳定**、不该麻烦用户自己编。
    所以配置里写 `{run}`，这里换成一个随机值。**幂等**：已经展开过的值再调用一次不变，
    所以上层可以提前固化（`coding.session` 在会话创建时展开一次，主对话、摘要、合并都
    带同一个 id——摘要若换 id，网关会把它路由到没缓存的机器上，前缀缓存直接归零）。
    不提前固化时，退化为「一个 Provider 实例一个 id」。`run_id` 只在测试里显式给。
    """
    if not headers:
        return {}
    resolved = run_id or uuid4().hex[:16]
    return {
        str(key): str(value).replace(RUN_PLACEHOLDER, resolved)
        for key, value in headers.items()
    }

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

    `context_window` / `max_output_tokens` 是**预算口径**（我们按多大做上下文管理），
    `window_limit` / `output_limit` 是**模型能力**（元数据或静态表给的物理上限）。
    前者可以比后者小（压低是成本控制），不该比后者大——`config` 负责收敛。
    """

    provider: ProviderName
    model: str
    base_url: str
    context_window: int
    max_output_tokens: int
    window_limit: int | None = None
    output_limit: int | None = None


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

    构造约定：所有实现都必须能以 (spec, api_key) 构造，并接受两个可选关键字
    `client`（复用的连接池）与 `headers`（配置里的自定义请求头）。api_key 由上层
    读好传入，本层不读环境变量；空字符串表示端点不需要鉴权（如本地 Ollama）。
    Protocol 表达不了构造函数，这条靠实现与测试保证。

    同一个实例可以被多个会话并发调用：真实适配器的 `stream()` 不写实例状态，
    每次调用各自持有自己的 assembler 与缓冲。fake 例外——它按剧本计数。
    """

    spec: ModelSpec

    def stream(self, request: Request) -> AsyncIterator[AssistantEvent]:
        ...
