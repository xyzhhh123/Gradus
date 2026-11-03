"""适配器入口：按 `ModelSpec.provider` 选一个实现。

映射表放在这里而不是 `base.py`：依赖方向是 `providers -> base`，工厂要 import 各实现，
写进 `base` 会成环。上层与 `scripts/` 都从这里取，新增一家只改这一张表。
"""

from __future__ import annotations

import httpx

from ..base import ModelSpec, Provider
from .anthropic import AnthropicProvider
from .gemini import GeminiProvider
from .openai_compat import OpenAIChatProvider
from .openai_responses import OpenAIResponsesProvider

CLASSES = {
    "openai_compat": OpenAIChatProvider,
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
    "openai_responses": OpenAIResponsesProvider,
}


def create_provider(
    spec: ModelSpec,
    api_key: str = "",
    client: httpx.AsyncClient | None = None,
) -> Provider:
    """按 `spec.provider` 构造适配器。api_key 由上层读好传入，空串表示端点不需要鉴权。

    `client` 用于复用连接池：传入一个长活的 `httpx.AsyncClient`，多次 `stream()` 共用
    同一批连接，多轮对话不必每轮重做 TLS 握手。**所有权：注入的 client 由调用方关闭**，
    本层不会 `aclose()` 它；传 `None` 时每次 `stream()` 自建并在结束时关闭（D29）。

    `fake` 不走工厂：它按剧本构造，签名是 `(spec, scripts)`，只在测试里直接实例化。
    """
    try:
        cls = CLASSES[spec.provider]
    except KeyError:
        raise ValueError(
            f"{spec.provider} 不能通过工厂构造（fake 按剧本构造，测试里直接实例化）"
        ) from None
    return cls(spec, api_key, client=client)
