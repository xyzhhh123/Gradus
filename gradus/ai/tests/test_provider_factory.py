"""工厂与 client 注入的测试：离线、不联网。

验证 D29 的所有权约定：注入 `httpx.AsyncClient` 后，多次 `stream()` 都走同一个连接对象，
且适配器不会替调用方关闭它。不注入时由 `post_sse` 自建并关闭，那条路径由
`test_transport.py` 覆盖（每次调用一个 client）。

运行（在 Gradus 目录下）：
    uv run pytest gradus/ai/tests/test_provider_factory.py
"""

from __future__ import annotations

import unittest

import httpx

from gradus.ai.base import ModelSpec, Request
from gradus.ai.providers import create_provider
from gradus.ai.tests.support import StreamTestCase
from gradus.ai.types import UserMessage

# 四家各给一个最小可用的响应体：本文件只关心连接走哪，不关心解析结果
BODIES = {
    "openai_compat": "data: [DONE]\n\n",
    "anthropic": 'event: message_stop\ndata: {"type":"message_stop"}\n\n',
    "gemini": "data: {}\n\n",
    "openai_responses": 'event: response.completed\ndata: {"response":{"status":"completed"}}\n\n',
}

BASE_URLS = {
    "openai_compat": "https://api.deepseek.com",
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com",
    "openai_responses": "https://api.openai.com/v1",
}


def make_spec(provider: str) -> ModelSpec:
    return ModelSpec(
        provider=provider,  # type: ignore[arg-type]
        model="test-model",
        base_url=BASE_URLS[provider],
        context_window=8000,
        max_output_tokens=1024,
    )


class TestClientInjection(StreamTestCase):
    async def test_injected_client_is_reused_and_left_open(self):
        for provider, body in BODIES.items():
            with self.subTest(provider=provider):
                calls = {"n": 0}

                def handler(request: httpx.Request) -> httpx.Response:
                    calls["n"] += 1
                    return httpx.Response(200, content=body.encode())

                client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
                instance = create_provider(make_spec(provider), "sk-test", client=client)

                for _ in range(2):
                    await self.consume(
                        instance.stream(Request(system="", messages=[UserMessage("hi")]))
                    )

                # 两次都打到注入 client 的 MockTransport 上，说明没有另建连接；
                # 且它仍可用，说明适配器没有替调用方关闭。
                self.assertEqual(calls["n"], 2)
                self.assertFalse(client.is_closed)

                await client.aclose()


if __name__ == "__main__":
    unittest.main()
