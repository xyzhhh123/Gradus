"""gemini 适配器的测试：请求体转换 + SSE 解析，全部离线。

请求侧是纯函数，用普通 `TestCase`；流式侧边收边断言，用 `StreamTestCase`
（`IsolatedAsyncioTestCase` + `consume()`），与运行时同一条链路。

运行（在 Gradus 目录下）：
    uv run pytest gradus/ai/tests/test_gemini.py
"""

from __future__ import annotations

import json
import unittest
from typing import Any
from unittest.mock import patch

from gradus.ai.base import ModelSpec, Request, ToolSpec
from gradus.ai.providers.gemini import GeminiProvider, build_payload
from gradus.ai.tests.support import StreamTestCase, fake_sse
from gradus.ai.transport import SseEvent, TransportFailure
from gradus.ai.types import (
    AssistantMessage,
    StreamError,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultMessage,
    UserMessage,
)


def make_spec() -> ModelSpec:
    return ModelSpec(
        provider="gemini",
        model="gemini-2.5-pro",
        base_url="https://generativelanguage.googleapis.com",
        context_window=1000000,
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


def sse(payload: dict[str, Any]) -> SseEvent:
    return SseEvent(event=None, data=json.dumps(payload, ensure_ascii=False))


def text_chunk(text: str) -> SseEvent:
    return sse(
        {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "index": 0}]}
    )


async def run(events: list[SseEvent], api_key: str = "gm-test"):
    """顶掉网络后逐条转发适配器的事件，patch 在迭代期间一直有效。"""
    provider = GeminiProvider(make_spec(), api_key=api_key)
    with patch("gradus.ai.providers.gemini.post_sse") as fake:
        fake.return_value = fake_sse(events)
        async for event in provider.stream(Request(system="", messages=[UserMessage("hi")])):
            yield event


class TestRequestSide(unittest.TestCase):
    def test_system_becomes_system_instruction(self):
        request = Request(system="你是代码助手", messages=[UserMessage("hi")])
        payload = build_payload(request, make_spec())

        self.assertEqual(payload["systemInstruction"], {"parts": [{"text": "你是代码助手"}]})
        self.assertNotIn("system", payload)

    def test_roles_are_user_and_model(self):
        request = Request(
            system="",
            messages=[
                UserMessage("hi"),
                AssistantMessage(content=[]),
            ],
        )
        payload = build_payload(request, make_spec())

        self.assertEqual(payload["contents"][0]["role"], "user")
        # 空 content 的助手消息被跳过，不会产生空 parts
        self.assertEqual(len(payload["contents"]), 1)

    def test_tool_result_maps_by_name(self):
        request = Request(
            system="",
            messages=[
                UserMessage("看下 a.py"),
                AssistantMessage(
                    content=[ToolCallBlock(id="c0", name="read_file", arguments={"path": "a.py"})]
                ),
                ToolResultMessage(tool_call_id="c0", content="print(1)"),
            ],
        )
        contents = build_payload(request, make_spec())["contents"]

        self.assertEqual([c["role"] for c in contents], ["user", "model", "user"])
        # 本厂商没有 call id，靠 name 匹配
        self.assertEqual(
            contents[2]["parts"][0],
            {"functionResponse": {"name": "read_file", "response": {"result": "print(1)"}}},
        )

    def test_error_tool_result_uses_error_key(self):
        request = Request(
            system="",
            messages=[
                AssistantMessage(
                    content=[ToolCallBlock(id="c0", name="read_file", arguments={})]
                ),
                ToolResultMessage(tool_call_id="c0", content="boom", is_error=True),
            ],
        )
        contents = build_payload(request, make_spec())["contents"]

        self.assertEqual(contents[-1]["parts"][0]["functionResponse"]["response"], {"error": "boom"})

    def test_tools_use_function_declarations(self):
        request = Request(system="", messages=[], tools=[READ_FILE])
        payload = build_payload(request, make_spec())

        self.assertEqual(
            payload["tools"],
            [
                {
                    "functionDeclarations": [
                        {
                            "name": "read_file",
                            "description": "读取文件内容",
                            "parameters": READ_FILE.parameters,
                        }
                    ]
                }
            ],
        )

    def test_generation_config(self):
        request = Request(system="", messages=[], max_output_tokens=256, temperature=0.3)
        config = build_payload(request, make_spec())["generationConfig"]

        self.assertEqual(config, {"maxOutputTokens": 256, "temperature": 0.3})

        # 请求不覆盖时回退到 spec 的值
        fallback = build_payload(Request(system="", messages=[]), make_spec())["generationConfig"]
        self.assertEqual(fallback, {"maxOutputTokens": 8192})

    def test_reasoning_becomes_thinking_config(self):
        payload = build_payload(Request(system="", messages=[], reasoning="low"), make_spec())

        self.assertEqual(
            payload["generationConfig"]["thinkingConfig"],
            {"thinkingBudget": 2048, "includeThoughts": True},
        )


class TestStream(StreamTestCase):
    async def test_text_stream(self):
        done = await self.consume(
            run(
                [
                    text_chunk("你"),
                    text_chunk("好"),
                    sse({"candidates": [{"content": {"role": "model", "parts": []}, "finishReason": "STOP"}]}),
                ]
            ),
            ["StreamStart", "TextStart", "TextDelta", "TextDelta", "TextEnd", "StreamDone"],
        )

        self.assertEqual(done.stop_reason, "stop")
        self.assertEqual(done.message.content[0].text, "你好")

    async def test_function_call_is_synthesized(self):
        # functionCall 一次给完整，适配器仍要产出三段
        done = await self.consume(
            run(
                [
                    sse(
                        {
                            "candidates": [
                                {
                                    "content": {
                                        "role": "model",
                                        "parts": [
                                            {"functionCall": {"name": "read_file", "args": {"path": "a.py"}}}
                                        ],
                                    },
                                    "index": 0,
                                }
                            ]
                        }
                    ),
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
        self.assertEqual(block.name, "read_file")
        self.assertEqual(block.arguments, {"path": "a.py"})
        # 没有 call id，由适配器合成
        self.assertEqual(block.id, "call_0")
        # 没有 finishReason 时按内容判定
        self.assertEqual(done.stop_reason, "tool_use")

    async def test_thought_becomes_thinking_with_signature(self):
        done = await self.consume(
            run(
                [
                    sse(
                        {
                            "candidates": [
                                {
                                    "content": {
                                        "role": "model",
                                        "parts": [
                                            {"text": "想一下", "thought": True, "thoughtSignature": "sig-1"}
                                        ],
                                    }
                                }
                            ]
                        }
                    ),
                    text_chunk("好"),
                ]
            )
        )

        contents = done.message.content
        self.assertIsInstance(contents[0], ThinkingBlock)
        self.assertEqual(contents[0].thinking, "想一下")
        self.assertEqual(contents[0].signature, "sig-1")
        self.assertEqual(contents[1].text, "好")

    async def test_max_tokens_maps_to_length(self):
        done = await self.consume(
            run(
                [
                    text_chunk("半"),
                    sse({"candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}]}),
                ]
            )
        )
        self.assertEqual(done.stop_reason, "length")

    async def test_safety_maps_to_filtered(self):
        done = await self.consume(
            run([sse({"candidates": [{"content": {"parts": []}, "finishReason": "SAFETY"}]})])
        )
        self.assertEqual(done.stop_reason, "filtered")

    async def test_prompt_block_ends_stream_as_filtered(self):
        done = await self.consume(run([sse({"promptFeedback": {"blockReason": "SAFETY"}})]))

        self.assertEqual(done.stop_reason, "filtered")
        self.assertEqual(done.message.content, [])

    async def test_malformed_function_call_becomes_error(self):
        done = await self.consume(
            run(
                [
                    sse(
                        {
                            "candidates": [
                                {"content": {"parts": []}, "finishReason": "MALFORMED_FUNCTION_CALL"}
                            ]
                        }
                    )
                ]
            )
        )
        self.assertIsInstance(done, StreamError)
        self.assertIn("MALFORMED_FUNCTION_CALL", done.error_message)

    async def test_usage_includes_thoughts(self):
        done = await self.consume(
            run(
                [
                    text_chunk("hi"),
                    sse(
                        {
                            "candidates": [{"content": {"parts": []}, "finishReason": "STOP"}],
                            "usageMetadata": {
                                "promptTokenCount": 100,
                                "candidatesTokenCount": 20,
                                "thoughtsTokenCount": 30,
                                "cachedContentTokenCount": 64,
                            },
                        }
                    ),
                ]
            )
        )

        usage = done.usage
        self.assertEqual(usage.input_tokens, 100)
        self.assertEqual(usage.output_tokens, 50)
        self.assertEqual(usage.cache_read_tokens, 64)

    async def test_transport_failure_becomes_stream_error(self):
        provider = GeminiProvider(make_spec(), api_key="k")
        with patch("gradus.ai.providers.gemini.post_sse") as fake:
            fake.return_value = fake_sse([TransportFailure("HTTP 500: oops", 500)])
            last = await self.consume(
                provider.stream(Request(system="", messages=[UserMessage("hi")]))
            )

        self.assertIsInstance(last, StreamError)
        self.assertIn("500", last.error_message)

    async def test_non_zero_candidate_index_is_ignored(self):
        done = await self.consume(
            run(
                [
                    sse(
                        {
                            "candidates": [
                                {
                                    "content": {"role": "model", "parts": [{"text": "第一个"}]},
                                    "index": 0,
                                },
                                {
                                    "content": {"role": "model", "parts": [{"text": "第二个"}]},
                                    "index": 1,
                                },
                            ]
                        }
                    )
                ]
            )
        )

        self.assertEqual(done.message.content[0].text, "第一个")

    async def test_max_tokens_does_not_override_tool_use(self):
        done = await self.consume(
            run(
                [
                    sse(
                        {
                            "candidates": [
                                {
                                    "content": {
                                        "parts": [
                                            {"functionCall": {"name": "read_file", "args": {"path": "a.p"}}}
                                        ]
                                    },
                                    "finishReason": "MAX_TOKENS",
                                }
                            ]
                        }
                    )
                ]
            )
        )

        # 截断优先：不能因为是工具调用就改判成 tool_use
        self.assertEqual(done.stop_reason, "length")

    async def test_non_object_json_becomes_stream_error(self):
        # 合法 JSON 但不是对象：`[]` 没有 `.get`，必须收敛成 StreamError 而不是抛出去
        done = await self.consume(run([SseEvent(event=None, data="[]")]))

        self.assertIsInstance(done, StreamError)
        self.assertIn("parse failed", done.error_message)

    async def test_url_and_headers(self):
        provider = GeminiProvider(make_spec(), api_key="gm-test")
        with patch("gradus.ai.providers.gemini.post_sse") as fake:
            fake.return_value = fake_sse([])
            await self.consume(
                provider.stream(Request(system="", messages=[UserMessage("hi")]))
            )
            args, kwargs = fake.call_args

        self.assertEqual(
            args[0],
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-2.5-pro:streamGenerateContent?alt=sse",
        )
        self.assertEqual(kwargs["headers"]["x-goog-api-key"], "gm-test")
        self.assertNotIn("Authorization", kwargs["headers"])


if __name__ == "__main__":
    unittest.main()
