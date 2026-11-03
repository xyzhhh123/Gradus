"""连接池生命周期的离线测试。

`Agent.connect()` 是唯一会建 client 的入口，所以这里只验证三件事：
自建的池由 `aclose()` 关闭、注入的池跨轮复用且不由 agent 关闭、关闭后不能再跑。
网络用 `httpx.MockTransport` 顶掉，不依赖真实 key。
"""

from __future__ import annotations

import unittest
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import httpx

from gradus.agent.agent import Agent
from gradus.agent.base import LoopConfig
from gradus.ai.base import ModelSpec

TEXT_STREAM = """\
event: message_start
data: {"message":{"usage":{"input_tokens":1}}}

event: content_block_start
data: {"index":0,"content_block":{"type":"text","text":""}}

event: content_block_delta
data: {"index":0,"delta":{"type":"text_delta","text":"ok"}}

event: content_block_stop
data: {"index":0}

event: message_delta
data: {"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}

event: message_stop
data: {"type":"message_stop"}

"""


def make_spec(provider: str = "anthropic") -> ModelSpec:
    return ModelSpec(
        provider=provider,
        model="claude-sonnet-4-5",
        base_url="https://api.anthropic.com",
        context_window=200_000,
        max_output_tokens=8192,
    )


def make_client(calls: list[httpx.Request] | None = None) -> httpx.AsyncClient:
    """每次请求都回一段完整的文本流；calls 用来数请求次数。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        return httpx.Response(
            200,
            text=TEXT_STREAM,
            headers={"content-type": "text/event-stream"},
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def drain(iterator: AsyncIterator[Any]) -> list[Any]:
    return [event async for event in iterator]


class TestClientLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_owned_client_is_closed_by_aclose(self):
        agent = await Agent.connect(make_spec(), LoopConfig(system=""))
        client = agent._client
        self.assertIsNotNone(client)
        self.assertFalse(client.is_closed)

        await agent.aclose()

        self.assertTrue(client.is_closed)
        self.assertIsNone(agent._client)

    async def test_injected_client_is_reused_across_runs_and_not_closed(self):
        calls: list[httpx.Request] = []
        client = make_client(calls)
        agent = await Agent.connect(
            make_spec(), LoopConfig(system=""), api_key="k", client=client
        )

        await drain(agent.prompt("第一轮"))
        await drain(agent.prompt("第二轮"))
        await agent.wait_for_idle()

        # 两轮请求走同一个池子，provider 也持有这同一个对象
        self.assertEqual(len(calls), 2)
        self.assertIs(agent._provider._client, client)

        await agent.aclose()

        self.assertFalse(client.is_closed)
        await client.aclose()

    async def test_aclose_is_idempotent_and_rejects_new_runs(self):
        agent = await Agent.connect(make_spec(), LoopConfig(system=""))
        await agent.aclose()
        await agent.aclose()

        with self.assertRaises(RuntimeError):
            agent.prompt("hi")
        with self.assertRaises(RuntimeError):
            agent.continue_()
        with self.assertRaises(RuntimeError):
            agent.reset()

    async def test_unknown_provider_does_not_leak_owned_client(self):
        created: list[httpx.AsyncClient] = []
        # patch 的是 httpx 模块上的同名属性，所以先留住原始类，否则工厂会递归调自己
        real_client = httpx.AsyncClient

        def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            client = real_client(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
            created.append(client)
            return client

        with (
            patch("gradus.agent.agent.httpx.AsyncClient", side_effect=factory),
            self.assertRaises(ValueError),
        ):
            await Agent.connect(make_spec("unknown"), LoopConfig(system=""))

        self.assertTrue(created[0].is_closed)


if __name__ == "__main__":
    unittest.main()
