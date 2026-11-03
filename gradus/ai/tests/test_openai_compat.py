"""openai_compat 请求体转换与流式解析的测试：全部离线。

请求侧是纯函数，用普通 `TestCase`；流式侧边收边断言，用 `StreamTestCase`
（`IsolatedAsyncioTestCase` + `consume()`），与运行时同一条链路。

运行（在 Gradus 目录下）：
    uv run pytest gradus/ai/tests/test_openai_compat.py
"""

from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any
from unittest.mock import patch

from gradus.ai.base import ModelSpec, Request, ToolSpec
from gradus.ai.providers.openai_compat import (
    OpenAIChatProvider,
    build_assistant,
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
        provider="openai_compat",
        model="deepseek-chat",
        base_url="https://api.deepseek.com",
        context_window=65536,
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


class TestBuildPayload(unittest.TestCase):
    def test_system_placement_and_omission(self):
        messages = build_payload(
            Request(system="你是代码助手", messages=[UserMessage("看下 a.py")]), make_spec()
        )["messages"]

        self.assertEqual(messages[0], {"role": "system", "content": "你是代码助手"})
        self.assertEqual(messages[1], {"role": "user", "content": "看下 a.py"})

        # 空 system 不占位
        empty = build_payload(Request(system="", messages=[UserMessage("hi")]), make_spec())
        self.assertEqual(len(empty["messages"]), 1)

    def test_stream_always_on(self):
        payload = build_payload(Request(system="", messages=[]), make_spec())

        self.assertIs(payload["stream"], True)
        self.assertEqual(payload["stream_options"], {"include_usage": True})

    def test_max_tokens_fallback_and_override(self):
        fallback = build_payload(Request(system="", messages=[]), make_spec())["max_tokens"]
        override = build_payload(
            Request(system="", messages=[], max_output_tokens=256), make_spec()
        )["max_tokens"]

        self.assertEqual(fallback, 8192)
        self.assertEqual(override, 256)

    def test_temperature_only_when_set(self):
        omitted = build_payload(Request(system="", messages=[]), make_spec())
        included = build_payload(
            Request(system="", messages=[], temperature=0.2), make_spec()
        )

        self.assertNotIn("temperature", omitted)
        self.assertEqual(included["temperature"], 0.2)

    def test_reasoning_effort_only_when_set(self):
        omitted = build_payload(Request(system="", messages=[]), make_spec())
        included = build_payload(Request(system="", messages=[], reasoning="high"), make_spec())

        self.assertNotIn("reasoning_effort", omitted)
        self.assertEqual(included["reasoning_effort"], "high")

    def test_tools_absent_when_empty(self):
        payload = build_payload(Request(system="", messages=[]), make_spec())

        self.assertNotIn("tools", payload)
        self.assertNotIn("tool_choice", payload)

    def test_tools_shape(self):
        request = Request(system="", messages=[], tools=[READ_FILE])
        payload = build_payload(request, make_spec())

        self.assertEqual(payload["tool_choice"], "auto")
        self.assertEqual(
            payload["tools"],
            [
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "读取文件内容",
                        "parameters": READ_FILE.parameters,
                    },
                }
            ],
        )


class TestBuildMessages(unittest.TestCase):
    def test_assistant_text_only(self):
        message = AssistantMessage(content=[TextBlock("我先看下")])
        self.assertEqual(build_assistant(message), {"role": "assistant", "content": "我先看下"})

    def test_assistant_multiple_text_blocks_joined(self):
        message = AssistantMessage(content=[TextBlock("我先"), TextBlock("看下")])
        self.assertEqual(build_assistant(message)["content"], "我先看下")

    def test_assistant_tool_call_only_has_null_content(self):
        message = AssistantMessage(
            content=[ToolCallBlock(id="call_0", name="read_file", arguments={"path": "a.py"})]
        )
        built = build_assistant(message)

        self.assertIsNone(built["content"])
        self.assertEqual(
            built["tool_calls"],
            [
                {
                    "id": "call_0",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                }
            ],
        )

    def test_thinking_is_not_sent_back(self):
        message = AssistantMessage(content=[ThinkingBlock("想一下"), TextBlock("好")])
        self.assertEqual(build_assistant(message), {"role": "assistant", "content": "好"})

    def test_tool_result(self):
        request = Request(
            system="",
            messages=[ToolResultMessage(tool_call_id="call_0", content="print(1)")],
        )
        self.assertEqual(
            build_payload(request, make_spec())["messages"],
            [{"role": "tool", "tool_call_id": "call_0", "content": "print(1)"}],
        )

    def test_tool_result_error_is_structured(self):
        request = Request(
            system="",
            messages=[
                ToolResultMessage(tool_call_id="call_0", content="FileNotFoundError", is_error=True)
            ],
        )
        self.assertEqual(
            build_payload(request, make_spec())["messages"],
            [
                {
                    "role": "tool",
                    "tool_call_id": "call_0",
                    "content": '{"error": "FileNotFoundError"}',
                }
            ],
        )


def json_chunk(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def delta_chunk(delta: dict[str, Any], finish: str | None = None) -> str:
    return json_chunk({"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})


async def run(chunks: list[str], api_key: str = "sk-test"):
    """把剧本字符串喂给 stream()，逐条转发事件；patch 在迭代期间一直有效。"""
    provider = OpenAIChatProvider(make_spec(), api_key=api_key)
    events = [SseEvent(event=None, data=chunk) for chunk in chunks]
    with patch("gradus.ai.providers.openai_compat.post_sse") as fake:
        fake.return_value = fake_sse(events)
        async for event in provider.stream(Request(system="", messages=[UserMessage("hi")])):
            yield event


class TestStream(StreamTestCase):
    async def test_text_stream(self):
        done = await self.consume(
            run(
                [
                    delta_chunk({"role": "assistant", "content": "你"}),
                    delta_chunk({"content": "好"}),
                    delta_chunk({}, "stop"),
                    "[DONE]",
                ]
            ),
            ["StreamStart", "TextStart", "TextDelta", "TextDelta", "TextEnd", "StreamDone"],
        )

        self.assertEqual(done.stop_reason, "stop")
        self.assertEqual(done.message.content[0].text, "你好")

    async def test_tool_call_fragments_joined(self):
        done = await self.consume(
            run(
                [
                    delta_chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_0",
                                    "type": "function",
                                    "function": {"name": "read_file", "arguments": '{"path"'},
                                }
                            ]
                        }
                    ),
                    delta_chunk({"tool_calls": [{"index": 0, "function": {"arguments": ': "a.py"}'}}]}),
                    delta_chunk({}, "tool_calls"),
                    "[DONE]",
                ]
            )
        )

        self.assertEqual(done.stop_reason, "tool_use")
        block = done.message.content[0]
        self.assertIsInstance(block, ToolCallBlock)
        self.assertEqual(block.arguments, {"path": "a.py"})

    async def test_parallel_tool_calls_split_by_index(self):
        done = await self.consume(
            run(
                [
                    delta_chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c0",
                                    "function": {"name": "read_file", "arguments": '{"path"'},
                                }
                            ]
                        }
                    ),
                    delta_chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": 1,
                                    "id": "c1",
                                    "function": {"name": "list_dir", "arguments": '{"dir"'},
                                }
                            ]
                        }
                    ),
                    delta_chunk({"tool_calls": [{"index": 0, "function": {"arguments": ': "a.py"}'}}]}),
                    delta_chunk({"tool_calls": [{"index": 1, "function": {"arguments": ': "."}'}}]}),
                    delta_chunk({}, "tool_calls"),
                    "[DONE]",
                ]
            )
        )

        blocks = done.message.content
        self.assertEqual([block.name for block in blocks], ["read_file", "list_dir"])
        self.assertEqual([block.arguments for block in blocks], [{"path": "a.py"}, {"dir": "."}])

    async def test_reasoning_content_becomes_thinking(self):
        done = await self.consume(
            run(
                [
                    delta_chunk({"reasoning_content": "想一下"}),
                    delta_chunk({"content": "好"}),
                    delta_chunk({}, "stop"),
                    "[DONE]",
                ]
            )
        )

        contents = done.message.content
        self.assertIsInstance(contents[0], ThinkingBlock)
        self.assertEqual(contents[0].thinking, "想一下")
        self.assertEqual(contents[1].text, "好")

    async def test_usage_mapped(self):
        done = await self.consume(
            run(
                [
                    delta_chunk({"content": "hi"}),
                    delta_chunk({}, "stop"),
                    json_chunk(
                        {
                            "choices": [],
                            "usage": {
                                "prompt_tokens": 100,
                                "completion_tokens": 20,
                                "prompt_tokens_details": {"cached_tokens": 64},
                            },
                        }
                    ),
                    "[DONE]",
                ]
            )
        )

        usage = done.usage
        self.assertEqual(usage.input_tokens, 100)
        self.assertEqual(usage.output_tokens, 20)
        self.assertEqual(usage.cache_read_tokens, 64)

    async def test_usage_inside_choice_not_double_counted(self):
        # Kimi 把 usage 放在 choices[0] 里，与 finish_reason 同一个 chunk
        def choice_usage(top_level: dict | None = None) -> dict:
            chunk = {
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop",
                        "usage": {"prompt_tokens": 65, "completion_tokens": 47},
                    }
                ]
            }
            if top_level is not None:
                chunk["usage"] = top_level
            return chunk

        inside = (await self.consume(run([json_chunk(choice_usage()), "[DONE]"]))).usage
        self.assertEqual((inside.input_tokens, inside.output_tokens), (65, 47))

        # 两处都有时只认顶层，不能相加
        both = (
            await self.consume(
                run([json_chunk(choice_usage({"prompt_tokens": 10, "completion_tokens": 5})), "[DONE]"])
            )
        ).usage
        self.assertEqual((both.input_tokens, both.output_tokens), (10, 5))

    async def test_length_stop_reason(self):
        done = await self.consume(
            run([delta_chunk({"content": "半"}), delta_chunk({}, "length"), "[DONE]"])
        )
        self.assertEqual(done.stop_reason, "length")

    async def test_refusal_becomes_text_and_filtered(self):
        done = await self.consume(
            run(
                [
                    delta_chunk({"refusal": "我不能帮你写这个"}),
                    delta_chunk({}, "stop"),
                    "[DONE]",
                ]
            )
        )

        self.assertEqual(done.stop_reason, "filtered")
        self.assertEqual(done.message.content[0].text, "我不能帮你写这个")
        # 拒答单独成块，消费方能按块决定怎么呈现
        self.assertIs(done.message.content[0].refusal, True)

    async def test_interrupted_tool_call_keeps_skeleton(self):
        chunk = delta_chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "c0",
                        "function": {"name": "read_file", "arguments": '{"path"'},
                    }
                ]
            }
        )
        provider = OpenAIChatProvider(make_spec(), api_key="k")
        stream = [SseEvent(event=None, data=chunk), TransportFailure("stream aborted", None)]
        with patch("gradus.ai.providers.openai_compat.post_sse") as fake:
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

    async def test_non_zero_choice_index_is_ignored(self):
        done = await self.consume(
            run(
                [
                    json_chunk({"choices": [{"index": 1, "delta": {"content": "别的候选"}}]}),
                    delta_chunk({"content": "正片"}),
                    delta_chunk({}, "stop"),
                    "[DONE]",
                ]
            )
        )

        self.assertEqual(done.message.content[0].text, "正片")

    async def test_in_stream_error_becomes_stream_error(self):
        done = await self.consume(run([json_chunk({"error": {"message": "quota exceeded"}})]))

        self.assertIsInstance(done, StreamError)
        self.assertIn("quota exceeded", done.error_message)

    async def test_transport_failure_becomes_stream_error(self):
        provider = OpenAIChatProvider(make_spec(), api_key="k")
        with patch("gradus.ai.providers.openai_compat.post_sse") as fake:
            fake.return_value = fake_sse([TransportFailure("HTTP 401: bad key", 401)])
            last = await self.consume(provider.stream(Request(system="", messages=[])))

        self.assertIsInstance(last, StreamError)
        self.assertEqual(last.kind, "error")
        self.assertIn("401", last.error_message)

    async def test_bad_json_becomes_stream_error(self):
        done = await self.consume(run(["{not json", "[DONE]"]))

        self.assertIsInstance(done, StreamError)
        self.assertIn("parse failed", done.error_message)

    async def test_non_object_json_becomes_stream_error(self):
        # 合法 JSON 但不是对象：`[]` 没有 `.get`，必须收敛成 StreamError 而不是抛出去
        done = await self.consume(run(["[]"]))

        self.assertIsInstance(done, StreamError)
        self.assertIn("parse failed", done.error_message)

    async def test_cancellation_propagates(self):
        # 契约里唯一的例外：CancelledError 必须穿透，不能被兜底捕获吞掉
        async def cancelled():
            yield SseEvent(event=None, data=delta_chunk({"content": "半"}))
            raise asyncio.CancelledError

        provider = OpenAIChatProvider(make_spec(), api_key="k")
        with patch("gradus.ai.providers.openai_compat.post_sse") as fake:
            fake.return_value = cancelled()
            with self.assertRaises(asyncio.CancelledError):
                await self.consume(provider.stream(Request(system="", messages=[])))

    async def test_url_and_headers(self):
        provider = OpenAIChatProvider(make_spec(), api_key="sk-test")
        with patch("gradus.ai.providers.openai_compat.post_sse") as fake:
            fake.return_value = fake_sse([SseEvent(event=None, data="[DONE]")])
            await self.consume(
                provider.stream(Request(system="", messages=[UserMessage("hi")]))
            )
            args, kwargs = fake.call_args

        self.assertEqual(args[0], "https://api.deepseek.com/chat/completions")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer sk-test")

    async def test_no_authorization_header_without_key(self):
        provider = OpenAIChatProvider(make_spec(), api_key="")
        with patch("gradus.ai.providers.openai_compat.post_sse") as fake:
            fake.return_value = fake_sse([SseEvent(event=None, data="[DONE]")])
            await self.consume(
                provider.stream(Request(system="", messages=[UserMessage("hi")]))
            )
            _, kwargs = fake.call_args

        self.assertNotIn("Authorization", kwargs["headers"])


if __name__ == "__main__":
    unittest.main()
