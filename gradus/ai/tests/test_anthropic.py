"""anthropic 适配器的测试：请求体转换 + SSE 解析，全部离线。

请求侧是纯函数，用普通 `TestCase`；流式侧边收边断言，用 `StreamTestCase`
（`IsolatedAsyncioTestCase` + `consume()`），与运行时同一条链路。

运行（在 Gradus 目录下）：
    uv run pytest gradus/ai/tests/test_anthropic.py
"""

from __future__ import annotations

import json
import unittest
from typing import Any
from unittest.mock import patch

from gradus.ai.base import ModelSpec, Request, ToolSpec
from gradus.ai.providers.anthropic import (
    AnthropicProvider,
    build_assistant_blocks,
    build_payload,
)
from gradus.ai.tests.support import StreamTestCase, fake_sse
from gradus.ai.transport import SseEvent, TransportFailure
from gradus.ai.types import (
    AssistantMessage,
    StreamError,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultMessage,
    UserMessage,
)


def make_spec() -> ModelSpec:
    return ModelSpec(
        provider="anthropic",
        model="claude-sonnet-4-5",
        base_url="https://api.anthropic.com",
        context_window=200000,
        max_output_tokens=8192,
    )


READ_FILE = ToolSpec(
    name="read_file",
    description="读取文件内容",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
)


def sse(event: str, payload: dict[str, Any]) -> SseEvent:
    return SseEvent(event=event, data=json.dumps(payload, ensure_ascii=False))


async def run(events: list[SseEvent], api_key: str = "sk-ant-test"):
    """顶掉网络后逐条转发适配器的事件，patch 在迭代期间一直有效。"""
    provider = AnthropicProvider(make_spec(), api_key=api_key)
    with patch("gradus.ai.providers.anthropic.post_sse") as fake:
        fake.return_value = fake_sse(events)
        async for event in provider.stream(Request(system="", messages=[UserMessage("hi")])):
            yield event


class TestRequestSide(unittest.TestCase):
    def test_system_is_top_level_with_cache_control(self):
        request = Request(system="你是代码助手", messages=[UserMessage("看下 a.py")])
        payload = build_payload(request, make_spec())

        # system 是块数组：字符串形式带不了 cache_control
        self.assertEqual(
            payload["system"],
            [{"type": "text", "text": "你是代码助手", "cache_control": {"type": "ephemeral"}}],
        )
        self.assertEqual(
            payload["messages"],
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "看下 a.py",
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                }
            ],
        )

    def test_empty_system_is_omitted(self):
        payload = build_payload(Request(system="", messages=[]), make_spec())
        self.assertNotIn("system", payload)

    def test_max_tokens_fallback_and_override(self):
        fallback = build_payload(Request(system="", messages=[]), make_spec())["max_tokens"]
        override = build_payload(
            Request(system="", messages=[], max_output_tokens=512), make_spec()
        )["max_tokens"]

        self.assertEqual(fallback, 8192)
        self.assertEqual(override, 512)

    def test_reasoning_becomes_thinking_budget(self):
        request = Request(system="", messages=[], max_output_tokens=1024, reasoning="high")
        payload = build_payload(request, make_spec())

        self.assertEqual(payload["thinking"], {"type": "enabled", "budget_tokens": 16384})
        # 本厂商要求预算小于 max_tokens，否则请求被拒
        self.assertGreater(payload["max_tokens"], payload["thinking"]["budget_tokens"])

    def test_tools_use_input_schema(self):
        request = Request(system="", messages=[], tools=[READ_FILE])
        payload = build_payload(request, make_spec())

        self.assertEqual(payload["tool_choice"], {"type": "auto"})
        self.assertEqual(
            payload["tools"],
            [
                {
                    "name": "read_file",
                    "description": "读取文件内容",
                    "input_schema": READ_FILE.parameters,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        )

    def test_last_tool_result_block_gets_cache_control(self):
        payload = build_payload(
            Request(system="", messages=[ToolResultMessage(tool_call_id="t1", content="A")]),
            make_spec(),
        )

        # 官方把 tool_result 列为可缓存块，断点应落在它上面
        self.assertEqual(
            payload["messages"][-1]["content"][-1]["cache_control"], {"type": "ephemeral"}
        )

    def test_thinking_block_is_skipped_for_cache_control(self):
        # thinking 不能显式打点：往回找同一消息里的 text 块
        payload = build_payload(
            Request(
                system="",
                messages=[
                    UserMessage("hi"),
                    AssistantMessage(content=[TextBlock("好"), ThinkingBlock("想一下")]),
                ],
            ),
            make_spec(),
        )
        blocks = payload["messages"][-1]["content"]

        self.assertNotIn("cache_control", blocks[1])
        self.assertEqual(blocks[0]["cache_control"], {"type": "ephemeral"})

    def test_cache_breakpoints_never_exceed_three(self):
        request = Request(system="s", messages=[UserMessage("hi")], tools=[READ_FILE])
        payload = build_payload(request, make_spec())

        # system / 最后一个工具 / 最后一条消息，最多 3 个（官方上限 4）
        self.assertEqual(json.dumps(payload).count('"cache_control"'), 3)

    def test_roles_strictly_alternate_with_merged_tool_results(self):
        request = Request(
            system="",
            messages=[
                UserMessage("看一下 a.py 和 b.py"),
                AssistantMessage(
                    content=[
                        ToolCallBlock(id="t1", name="read_file", arguments={"path": "a.py"}),
                        ToolCallBlock(id="t2", name="read_file", arguments={"path": "b.py"}),
                    ]
                ),
                ToolResultMessage(tool_call_id="t1", content="A"),
                ToolResultMessage(tool_call_id="t2", content="B"),
            ],
        )
        messages = build_payload(request, make_spec())["messages"]

        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user"])
        # 两个 tool_result 必须落在同一条 user 消息里
        self.assertEqual(len(messages[2]["content"]), 2)
        self.assertEqual(messages[2]["content"][0]["type"], "tool_result")

    def test_assistant_keeps_block_order_and_signature(self):
        message = AssistantMessage(
            content=[
                ThinkingBlock("想一下", signature="sig-abc"),
                TextBlock("好"),
                ToolCallBlock(id="t1", name="read_file", arguments={"path": "a.py"}),
            ]
        )
        blocks = build_assistant_blocks(message)

        self.assertEqual(
            blocks,
            [
                {"type": "thinking", "thinking": "想一下", "signature": "sig-abc"},
                {"type": "text", "text": "好"},
                {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "a.py"}},
            ],
        )


class TestStream(StreamTestCase):
    async def test_text_stream(self):
        done = await self.consume(
            run(
                [
                    sse("message_start", {"message": {"usage": {"input_tokens": 25}}}),
                    sse("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
                    sse("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "你"}}),
                    sse("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "好"}}),
                    sse("content_block_stop", {"index": 0}),
                    sse(
                        "message_delta",
                        {"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 15}},
                    ),
                    sse("message_stop", {"type": "message_stop"}),
                ]
            ),
            ["StreamStart", "TextStart", "TextDelta", "TextDelta", "TextEnd", "StreamDone"],
        )

        self.assertEqual(done.stop_reason, "stop")
        self.assertEqual(done.message.content[0].text, "你好")

    async def test_usage_merged_across_two_events(self):
        # message_delta 报的是累计值，不是增量：Kimi 会把整份 usage 再报一遍，
        # 逐字段覆盖才对；累加会让 input / cache 翻倍、output 多算一次
        done = await self.consume(
            run(
                [
                    sse(
                        "message_start",
                        {
                            "message": {
                                "usage": {
                                    "input_tokens": 25,
                                    "cache_read_input_tokens": 10,
                                    "cache_creation_input_tokens": 4,
                                }
                            }
                        },
                    ),
                    sse(
                        "message_delta",
                        {
                            "delta": {},
                            "usage": {
                                "input_tokens": 25,
                                "output_tokens": 15,
                                "cache_read_input_tokens": 10,
                                "cache_creation_input_tokens": 4,
                            },
                        },
                    ),
                ]
            )
        )

        usage = done.usage
        # 出口补成总输入：25 未缓存 + 10 缓存读 + 4 缓存写（D25）
        self.assertEqual(usage.input_tokens, 39)
        self.assertEqual(usage.output_tokens, 15)
        self.assertEqual(usage.cache_read_tokens, 10)
        self.assertEqual(usage.cache_write_tokens, 4)

    async def test_thinking_with_signature(self):
        done = await self.consume(
            run(
                [
                    sse("content_block_start", {"index": 0, "content_block": {"type": "thinking", "thinking": ""}}),
                    sse("content_block_delta", {"index": 0, "delta": {"type": "thinking_delta", "thinking": "想一下"}}),
                    sse("content_block_delta", {"index": 0, "delta": {"type": "signature_delta", "signature": "sig-abc"}}),
                    sse("content_block_stop", {"index": 0}),
                ]
            )
        )

        block = done.message.content[0]
        self.assertIsInstance(block, ThinkingBlock)
        self.assertEqual(block.thinking, "想一下")
        self.assertEqual(block.signature, "sig-abc")

    async def test_tool_use_with_partial_json(self):
        done = await self.consume(
            run(
                [
                    sse(
                        "content_block_start",
                        {"index": 0, "content_block": {"type": "tool_use", "id": "toolu_01", "name": "read_file", "input": {}}},
                    ),
                    sse("content_block_delta", {"index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"path"'}}),
                    sse("content_block_delta", {"index": 0, "delta": {"type": "input_json_delta", "partial_json": ': "a.py"}'}}),
                    sse("content_block_stop", {"index": 0}),
                    sse("message_delta", {"delta": {"stop_reason": "tool_use"}}),
                ]
            )
        )

        self.assertEqual(done.stop_reason, "tool_use")
        block = done.message.content[0]
        self.assertIsInstance(block, ToolCallBlock)
        self.assertEqual(block.name, "read_file")
        self.assertEqual(block.arguments, {"path": "a.py"})

    async def test_ping_is_ignored(self):
        done = await self.consume(
            run(
                [
                    sse("ping", {"type": "ping"}),
                    sse("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
                    sse("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "hi"}}),
                    sse("content_block_stop", {"index": 0}),
                    sse("ping", {"type": "ping"}),
                ]
            )
        )

        self.assertEqual(done.message.content[0].text, "hi")

    async def test_max_tokens_maps_to_length(self):
        done = await self.consume(run([sse("message_delta", {"delta": {"stop_reason": "max_tokens"}})]))
        self.assertEqual(done.stop_reason, "length")

    async def test_error_event_becomes_stream_error(self):
        done = await self.consume(
            run(
                [
                    sse("message_start", {"message": {"usage": {}}}),
                    sse("error", {"type": "error", "error": {"type": "overloaded_error", "message": "overloaded"}}),
                ]
            )
        )

        self.assertIsInstance(done, StreamError)
        self.assertIn("overloaded", done.error_message)

    async def test_transport_failure_becomes_stream_error(self):
        provider = AnthropicProvider(make_spec(), api_key="k")
        with patch("gradus.ai.providers.anthropic.post_sse") as fake:
            fake.return_value = fake_sse([TransportFailure("HTTP 529: busy", 529)])
            last = await self.consume(
                provider.stream(Request(system="", messages=[UserMessage("hi")]))
            )

        self.assertIsInstance(last, StreamError)
        self.assertIn("529", last.error_message)

    async def test_assistant_first_is_rejected_before_any_request(self):
        provider = AnthropicProvider(make_spec(), api_key="k")
        with patch("gradus.ai.providers.anthropic.post_sse") as fake:
            last = await self.consume(
                provider.stream(
                    Request(system="", messages=[AssistantMessage(content=[TextBlock("hi")])])
                )
            )

        # 本地拦下，根本没发请求
        fake.assert_not_called()
        self.assertIsInstance(last, StreamError)
        self.assertIn("user", last.error_message)

    async def test_tool_result_first_is_allowed(self):
        provider = AnthropicProvider(make_spec(), api_key="k")
        with patch("gradus.ai.providers.anthropic.post_sse") as fake:
            fake.return_value = fake_sse([])
            last = await self.consume(
                provider.stream(
                    Request(
                        system="",
                        messages=[ToolResultMessage(tool_call_id="t1", content="A")],
                    )
                )
            )

        # tool_result 映射为 user，属于合法开头
        fake.assert_called_once()
        self.assertEqual(last.stop_reason, "stop")

    async def test_non_object_json_becomes_stream_error(self):
        # 合法 JSON 但不是对象：`[]` 没有 `.get`，必须收敛成 StreamError 而不是抛出去
        done = await self.consume(run([SseEvent(event="message_delta", data="[]")]))

        self.assertIsInstance(done, StreamError)
        self.assertIn("parse failed", done.error_message)

    async def test_url_and_headers(self):
        provider = AnthropicProvider(make_spec(), api_key="sk-ant-test")
        with patch("gradus.ai.providers.anthropic.post_sse") as fake:
            fake.return_value = fake_sse([])
            await self.consume(
                provider.stream(Request(system="", messages=[UserMessage("hi")]))
            )
            args, kwargs = fake.call_args

        self.assertEqual(args[0], "https://api.anthropic.com/v1/messages")
        self.assertEqual(kwargs["headers"]["x-api-key"], "sk-ant-test")
        self.assertEqual(kwargs["headers"]["anthropic-version"], "2023-06-01")
        self.assertNotIn("Authorization", kwargs["headers"])

    async def test_no_api_key_header_when_empty(self):
        provider = AnthropicProvider(make_spec(), api_key="")
        with patch("gradus.ai.providers.anthropic.post_sse") as fake:
            fake.return_value = fake_sse([])
            await self.consume(
                provider.stream(Request(system="", messages=[UserMessage("hi")]))
            )
            _, kwargs = fake.call_args

        self.assertNotIn("x-api-key", kwargs["headers"])


if __name__ == "__main__":
    unittest.main()
