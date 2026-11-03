"""openai_responses 适配器的测试：请求体转换 + SSE 解析，全部离线。

请求侧是纯函数，用普通 `TestCase`；流式侧边收边断言，用 `StreamTestCase`
（`IsolatedAsyncioTestCase` + `consume()`），与运行时同一条链路。

运行（在 Gradus 目录下）：
    uv run pytest gradus/ai/tests/test_openai_responses.py
"""

from __future__ import annotations

import json
import unittest
from typing import Any
from unittest.mock import patch

from gradus.ai.base import ModelSpec, Request, ToolSpec
from gradus.ai.providers.openai_responses import (
    OpenAIResponsesProvider,
    build_input,
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
        provider="openai_responses",
        model="gpt-5",
        base_url="https://api.openai.com/v1",
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


async def run(events: list[SseEvent], api_key: str = "sk-test"):
    """顶掉网络后逐条转发适配器的事件，patch 在迭代期间一直有效。"""
    provider = OpenAIResponsesProvider(make_spec(), api_key=api_key)
    with patch("gradus.ai.providers.openai_responses.post_sse") as fake:
        fake.return_value = fake_sse(events)
        async for event in provider.stream(Request(system="", messages=[UserMessage("hi")])):
            yield event


class TestRequestSide(unittest.TestCase):
    def test_system_becomes_instructions(self):
        request = Request(system="你是代码助手", messages=[UserMessage("hi")])
        payload = build_payload(request, make_spec())

        self.assertEqual(payload["instructions"], "你是代码助手")
        self.assertNotIn("system", payload)

    def test_store_always_false(self):
        payload = build_payload(Request(system="", messages=[]), make_spec())
        self.assertIs(payload["store"], False)

    def test_reasoning_effort(self):
        payload = build_payload(Request(system="", messages=[], reasoning="medium"), make_spec())

        self.assertEqual(payload["reasoning"], {"effort": "medium", "summary": "auto"})

    def test_user_message_item(self):
        items = build_input(Request(system="", messages=[UserMessage("看下 a.py")]))

        self.assertEqual(
            items, [{"role": "user", "content": [{"type": "input_text", "text": "看下 a.py"}]}]
        )

    def test_assistant_splits_into_message_and_function_call(self):
        request = Request(
            system="",
            messages=[
                AssistantMessage(
                    content=[
                        TextBlock("我先看下"),
                        ToolCallBlock(id="call_0", name="read_file", arguments={"path": "a.py"}),
                    ]
                )
            ],
        )
        items = build_input(request)

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["content"][0]["type"], "output_text")
        # 工具调用与消息平级，不在 content 里
        self.assertEqual(
            items[1],
            {
                "type": "function_call",
                "call_id": "call_0",
                "name": "read_file",
                "arguments": '{"path": "a.py"}',
            },
        )

    def test_tool_result_is_function_call_output(self):
        request = Request(
            system="",
            messages=[ToolResultMessage(tool_call_id="call_0", content="print(1)")],
        )
        items = build_input(request)

        self.assertEqual(
            items,
            [{"type": "function_call_output", "call_id": "call_0", "output": "print(1)"}],
        )

    def test_tools_are_flat(self):
        payload = build_payload(
            Request(system="", messages=[], tools=[READ_FILE]), make_spec()
        )

        self.assertEqual(
            payload["tools"],
            [
                {
                    "type": "function",
                    "name": "read_file",
                    "description": "读取文件内容",
                    "parameters": READ_FILE.parameters,
                }
            ],
        )

    def test_tool_result_error_is_structured(self):
        request = Request(
            system="",
            messages=[
                ToolResultMessage(tool_call_id="call_0", content="FileNotFoundError", is_error=True)
            ],
        )
        items = build_input(request)

        self.assertEqual(items[0]["output"], '{"error": "FileNotFoundError"}')


class TestStream(StreamTestCase):
    async def test_text_stream(self):
        done = await self.consume(
            run(
                [
                    sse("response.created", {"response": {"id": "resp_1", "status": "in_progress"}}),
                    sse(
                        "response.output_item.added",
                        {"output_index": 0, "item": {"type": "message", "id": "msg_1"}},
                    ),
                    sse(
                        "response.content_part.added",
                        {"output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": ""}},
                    ),
                    sse("response.output_text.delta", {"delta": "你"}),
                    sse("response.output_text.delta", {"delta": "好"}),
                    sse("response.content_part.done", {"output_index": 0, "content_index": 0}),
                    sse("response.output_item.done", {"output_index": 0, "item": {"type": "message"}}),
                    sse(
                        "response.completed",
                        {"response": {"status": "completed", "usage": {"input_tokens": 100, "output_tokens": 20}}},
                    ),
                ]
            ),
            ["StreamStart", "TextStart", "TextDelta", "TextDelta", "TextEnd", "StreamDone"],
        )

        self.assertEqual(done.stop_reason, "stop")
        self.assertEqual(done.message.content[0].text, "你好")
        self.assertEqual(done.usage.input_tokens, 100)

    async def test_function_call_folded_from_three_layers(self):
        done = await self.consume(
            run(
                [
                    sse(
                        "response.output_item.added",
                        {
                            "output_index": 0,
                            "item": {
                                "type": "function_call",
                                "id": "fc_1",
                                "call_id": "call_0",
                                "name": "read_file",
                                "arguments": "",
                            },
                        },
                    ),
                    sse("response.function_call_arguments.delta", {"item_id": "fc_1", "delta": '{"path"'}),
                    sse("response.function_call_arguments.delta", {"item_id": "fc_1", "delta": ': "a.py"}'}),
                    sse(
                        "response.function_call_arguments.done",
                        {"item_id": "fc_1", "arguments": '{"path": "a.py"}'},
                    ),
                    sse(
                        "response.output_item.done",
                        {
                            "output_index": 0,
                            "item": {
                                "type": "function_call",
                                "id": "fc_1",
                                "call_id": "call_0",
                                "name": "read_file",
                                "arguments": '{"path": "a.py"}',
                            },
                        },
                    ),
                    sse("response.completed", {"response": {"status": "completed"}}),
                ]
            ),
            [
                "StreamStart",
                "ToolCallStart",
                "ToolCallDelta",
                "ToolCallEnd",
                "StreamDone",
            ],
        )

        block = done.message.content[0]
        self.assertIsInstance(block, ToolCallBlock)
        self.assertEqual(block.id, "call_0")
        self.assertEqual(block.arguments, {"path": "a.py"})
        self.assertEqual(done.stop_reason, "tool_use")

    async def test_parallel_function_calls_are_replayed_in_order(self):
        done = await self.consume(
            run(
                [
                    sse(
                        "response.output_item.added",
                        {
                            "output_index": 0,
                            "item": {"type": "function_call", "id": "fc_1", "call_id": "call_0", "name": "read_file"},
                        },
                    ),
                    sse(
                        "response.output_item.added",
                        {
                            "output_index": 1,
                            "item": {"type": "function_call", "id": "fc_2", "call_id": "call_1", "name": "read_file"},
                        },
                    ),
                    # 两个调用的参数交错到达，只能靠 item_id 分辨归属
                    sse("response.function_call_arguments.delta", {"item_id": "fc_1", "delta": '{"path"'}),
                    sse("response.function_call_arguments.delta", {"item_id": "fc_2", "delta": '{"path"'}),
                    sse("response.function_call_arguments.delta", {"item_id": "fc_1", "delta": ': "a.py"}'}),
                    sse("response.function_call_arguments.delta", {"item_id": "fc_2", "delta": ': "b.py"}'}),
                    sse(
                        "response.output_item.done",
                        {
                            "output_index": 0,
                            "item": {"type": "function_call", "id": "fc_1", "call_id": "call_0", "name": "read_file"},
                        },
                    ),
                    sse(
                        "response.output_item.done",
                        {
                            "output_index": 1,
                            "item": {"type": "function_call", "id": "fc_2", "call_id": "call_1", "name": "read_file"},
                        },
                    ),
                    sse("response.completed", {"response": {"status": "completed"}}),
                ]
            ),
            [
                "StreamStart",
                "ToolCallStart",
                "ToolCallDelta",
                "ToolCallEnd",
                "ToolCallStart",
                "ToolCallDelta",
                "ToolCallEnd",
                "StreamDone",
            ],
        )

        blocks = done.message.content
        self.assertEqual([block.id for block in blocks], ["call_0", "call_1"])
        self.assertEqual(
            [block.arguments for block in blocks],
            [{"path": "a.py"}, {"path": "b.py"}],
        )
        self.assertEqual(done.stop_reason, "tool_use")

    async def test_refusal_becomes_text_and_filtered(self):
        done = await self.consume(
            run(
                [
                    sse(
                        "response.content_part.added",
                        {"output_index": 0, "content_index": 0, "part": {"type": "refusal", "refusal": ""}},
                    ),
                    sse("response.refusal.delta", {"item_id": "msg_1", "delta": "我不能帮你写这个"}),
                    sse("response.content_part.done", {"output_index": 0, "content_index": 0}),
                    sse("response.completed", {"response": {"status": "completed"}}),
                ]
            )
        )

        self.assertEqual(done.stop_reason, "filtered")
        self.assertEqual(done.message.content[0].text, "我不能帮你写这个")
        # 拒答单独成块，消费方能按块决定怎么呈现
        self.assertIs(done.message.content[0].refusal, True)

    async def test_reasoning_summary_becomes_thinking(self):
        done = await self.consume(
            run(
                [
                    sse(
                        "response.output_item.added",
                        {"output_index": 0, "item": {"type": "reasoning", "id": "rs_1"}},
                    ),
                    sse(
                        "response.reasoning_summary_text.delta",
                        {"item_id": "rs_1", "delta": "先确认文件"},
                    ),
                    sse(
                        "response.reasoning_summary_text.delta",
                        {"item_id": "rs_1", "delta": "在不在"},
                    ),
                    sse(
                        "response.output_item.done",
                        {
                            "output_index": 0,
                            "item": {"type": "reasoning", "id": "rs_1", "encrypted_content": "gAAA"},
                        },
                    ),
                    sse("response.completed", {"response": {"status": "completed"}}),
                ]
            )
        )

        block = done.message.content[0]
        self.assertIsInstance(block, ThinkingBlock)
        self.assertEqual(block.thinking, "先确认文件在不在")
        self.assertEqual(block.signature, "gAAA")

    async def test_interrupted_tool_call_keeps_skeleton(self):
        provider = OpenAIResponsesProvider(make_spec(), api_key="k")
        stream = [
            sse(
                "response.output_item.added",
                {
                    "output_index": 0,
                    "item": {
                        "type": "function_call",
                        "id": "fc_1",
                        "call_id": "call_0",
                        "name": "read_file",
                    },
                },
            ),
            sse("response.function_call_arguments.delta", {"item_id": "fc_1", "delta": '{"path"'}),
            TransportFailure("stream aborted", None),
        ]
        with patch("gradus.ai.providers.openai_responses.post_sse") as fake:
            fake.return_value = fake_sse(stream)
            last = await self.consume(
                provider.stream(Request(system="", messages=[UserMessage("hi")]))
            )

        self.assertIsInstance(last, StreamError)
        block = last.partial.content[0]
        self.assertIsInstance(block, ToolCallBlock)
        self.assertEqual(block.name, "read_file")
        self.assertEqual(block.arguments, {})
        self.assertEqual(block.raw, '{"path"')

    async def test_incomplete_max_output_tokens_maps_to_length(self):
        done = await self.consume(
            run(
                [
                    sse(
                        "response.incomplete",
                        {
                            "response": {
                                "status": "incomplete",
                                "incomplete_details": {"reason": "max_output_tokens"},
                            }
                        },
                    )
                ]
            )
        )
        self.assertEqual(done.stop_reason, "length")

    async def test_failed_becomes_stream_error(self):
        done = await self.consume(
            run(
                [sse("response.failed", {"response": {"status": "failed", "error": {"message": "boom"}}})]
            )
        )

        self.assertIsInstance(done, StreamError)
        self.assertIn("boom", done.error_message)

    async def test_error_event_becomes_stream_error(self):
        done = await self.consume(run([sse("error", {"error": {"message": "bad request"}})]))

        self.assertIsInstance(done, StreamError)
        self.assertIn("bad request", done.error_message)

    async def test_usage_cached_tokens(self):
        done = await self.consume(
            run(
                [
                    sse(
                        "response.completed",
                        {
                            "response": {
                                "status": "completed",
                                "usage": {
                                    "input_tokens": 100,
                                    "output_tokens": 20,
                                    "input_tokens_details": {"cached_tokens": 64},
                                    "output_tokens_details": {"reasoning_tokens": 12},
                                },
                            }
                        },
                    )
                ]
            )
        )

        usage = done.usage
        self.assertEqual(usage.input_tokens, 100)
        self.assertEqual(usage.output_tokens, 20)
        self.assertEqual(usage.cache_read_tokens, 64)

    async def test_transport_failure_becomes_stream_error(self):
        provider = OpenAIResponsesProvider(make_spec(), api_key="k")
        with patch("gradus.ai.providers.openai_responses.post_sse") as fake:
            fake.return_value = fake_sse([TransportFailure("HTTP 500: oops", 500)])
            last = await self.consume(
                provider.stream(Request(system="", messages=[UserMessage("hi")]))
            )

        self.assertIsInstance(last, StreamError)
        self.assertIn("500", last.error_message)

    async def test_non_object_json_becomes_stream_error(self):
        # 合法 JSON 但不是对象：`[]` 没有 `.get`，必须收敛成 StreamError 而不是抛出去
        done = await self.consume(
            run([SseEvent(event="response.output_text.delta", data="[]")])
        )

        self.assertIsInstance(done, StreamError)
        self.assertIn("parse failed", done.error_message)

    async def test_url_and_headers(self):
        provider = OpenAIResponsesProvider(make_spec(), api_key="sk-test")
        with patch("gradus.ai.providers.openai_responses.post_sse") as fake:
            fake.return_value = fake_sse([])
            await self.consume(
                provider.stream(Request(system="", messages=[UserMessage("hi")]))
            )
            args, kwargs = fake.call_args

        self.assertEqual(args[0], "https://api.openai.com/v1/responses")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer sk-test")


if __name__ == "__main__":
    unittest.main()
