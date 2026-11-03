"""fake provider 的回归测试：离线、确定性、不联网。

覆盖五件事：
1. 12 个事件的完整序列与 content_index 分配
2. partial 是快照，不随后续 delta 变化
3. 参数截断时 arguments 留空 dict
4. 中断时保留已产出内容，且以 StreamError 收尾
5. 多剧本按调用顺序消费

用例不带收集步骤：事件一到就断言（`StreamTestCase.consume`），与运行时消费
`stream()` 的方式一致。

运行（在 Gradus 目录下）：
    uv run pytest
"""

from __future__ import annotations

import unittest
from typing import Any

from gradus.ai.base import ModelSpec, Request
from gradus.ai.providers.fake import FakeProvider
from gradus.ai.tests.support import StreamTestCase
from gradus.ai.types import (
    StreamDone,
    StreamError,
    StreamStart,
    TextBlock,
    TextDelta,
    TextEnd,
    TextStart,
    ThinkingBlock,
    ToolCallBlock,
)


def make_spec() -> ModelSpec:
    return ModelSpec(
        provider="fake",
        model="fake-1",
        base_url="http://localhost",
        context_window=8000,
        max_output_tokens=1024,
    )


def make_request() -> Request:
    return Request(system="", messages=[])


def stream(scripts: list[dict[str, Any]]):
    provider = FakeProvider(make_spec(), scripts)
    return provider.stream(make_request())


class TestFakeProvider(StreamTestCase):
    async def test_full_sequence_and_content_index(self):
        names = [
            "StreamStart",
            "ThinkingStart",
            "ThinkingDelta",
            "ThinkingEnd",
            "TextStart",
            "TextDelta",
            "TextDelta",
            "TextEnd",
            "ToolCallStart",
            "ToolCallDelta",
            "ToolCallDelta",
            "ToolCallEnd",
            "StreamDone",
        ]
        indexes: set[int] = set()
        done = None
        index = 0

        async for event in stream(
            [
                {
                    "name": "tool_call",
                    "steps": [
                        {"kind": "thinking", "chunks": ["需要读文件"], "signature": "sig"},
                        {"kind": "text", "chunks": ["我先看下", "文件"]},
                        {
                            "kind": "tool_call",
                            "id": "call_0",
                            "name": "read_file",
                            "arg_chunks": ['{"path"', ': "a.py"}'],
                        },
                        {"kind": "usage", "input": 100, "output": 20},
                        {"kind": "finish", "reason": "tool_use"},
                    ],
                }
            ]
        ):
            self.assertEqual(type(event).__name__, names[index], f"第 {index} 个事件")
            index += 1
            if hasattr(event, "content_index"):
                indexes.add(event.content_index)
            if isinstance(event, StreamDone):
                done = event

        self.assertEqual(index, len(names))
        # content_index 由本层从 0 分配：thinking=0, text=1, tool_call=2
        self.assertEqual(indexes, {0, 1, 2})

        self.assertIsInstance(done, StreamDone)
        self.assertEqual(done.stop_reason, "tool_use")
        self.assertEqual(len(done.message.content), 3)
        self.assertIsInstance(done.message.content[0], ThinkingBlock)
        self.assertEqual(done.message.content[0].signature, "sig")
        self.assertIsInstance(done.message.content[1], TextBlock)
        self.assertEqual(done.message.content[1].text, "我先看下文件")
        self.assertIsInstance(done.message.content[2], ToolCallBlock)
        self.assertEqual(done.message.content[2].arguments, {"path": "a.py"})
        self.assertEqual(done.usage.input_tokens, 100)
        self.assertEqual(done.usage.output_tokens, 20)

    async def test_partial_is_snapshot(self):
        start_snapshot = None
        text_start_snapshot = None
        text_end_text = None
        first_delta_text = None

        async for event in stream(
            [
                {
                    "name": "answer",
                    "steps": [
                        {"kind": "text", "chunks": ["a", "b"]},
                        {"kind": "finish", "reason": "stop"},
                    ],
                }
            ]
        ):
            if isinstance(event, StreamStart):
                self.assertEqual(event.partial.content, [])
                start_snapshot = event.partial
            elif isinstance(event, TextStart):
                # TextStart 时块已建好但内容为空
                self.assertEqual(event.partial.content[0].text, "")
                text_start_snapshot = event.partial
            elif isinstance(event, TextDelta):
                # 首个 delta 的 partial 只含首片
                if first_delta_text is None:
                    first_delta_text = event.partial.content[0].text
            elif isinstance(event, TextEnd):
                # TextEnd 时是完整内容
                text_end_text = event.partial.content[0].text

        self.assertEqual(first_delta_text, "a")
        self.assertEqual(text_end_text, "ab")
        # 先前的快照不随后续 delta 改变：流结束后回看仍是最初的内容
        self.assertEqual(start_snapshot.content, [])
        self.assertEqual(text_start_snapshot.content[0].text, "")

    async def test_abort_keeps_partial(self):
        saw_done = False
        error = None

        async for event in stream(
            [
                {
                    "name": "aborted",
                    "steps": [
                        {"kind": "text", "chunks": ["写到一半"]},
                        {"kind": "error", "error_kind": "aborted", "message": "用户中断"},
                    ],
                }
            ]
        ):
            if isinstance(event, StreamDone):
                saw_done = True
            if isinstance(event, StreamError):
                error = event

        self.assertIsInstance(error, StreamError)
        self.assertEqual(error.kind, "aborted")
        self.assertEqual(error.partial.content[0].text, "写到一半")
        # 不允许出现 StreamDone
        self.assertFalse(saw_done)

    async def test_scripts_consumed_in_order(self):
        provider = FakeProvider(
            make_spec(),
            [
                {"name": "first", "steps": [{"kind": "text", "chunks": ["一"]}, {"kind": "finish"}]},
                {"name": "second", "steps": [{"kind": "text", "chunks": ["二"]}, {"kind": "finish"}]},
            ],
        )

        first = await self.consume(provider.stream(make_request()))
        second = await self.consume(provider.stream(make_request()))
        third = await self.consume(provider.stream(make_request()))

        self.assertEqual(first.message.content[0].text, "一")
        self.assertEqual(second.message.content[0].text, "二")
        # 剧本用尽后返回空内容的正常结束
        self.assertEqual(third.stop_reason, "stop")
        self.assertEqual(third.message.content, [])

    async def test_tool_arguments_raw_grows_with_partial(self):
        expected_raw = ['{"path": ', '{"path": "a.py"}']
        cursor = 0
        last = None

        async for event in stream(
            [
                {
                    "name": "tool",
                    "steps": [
                        {
                            "kind": "tool_call",
                            "id": "call_0",
                            "name": "read_file",
                            "arg_chunks": ['{"path": ', '"a.py"}'],
                        },
                        {"kind": "finish", "reason": "tool_use"},
                    ],
                }
            ]
        ):
            if type(event).__name__ == "ToolCallDelta":
                # 每片的 partial 都能看到已收到的原文
                self.assertEqual(event.partial.content[0].raw, expected_raw[cursor])
                if cursor == 0:
                    # 但 arguments 在 delta 期间仍是空的
                    self.assertEqual(event.partial.content[0].arguments, {})
                cursor += 1
            last = event

        self.assertEqual(cursor, 2)
        block = last.message.content[0]
        self.assertEqual(block.arguments, {"path": "a.py"})
        self.assertEqual(block.raw, '{"path": "a.py"}')

    async def test_truncated_tool_call_keeps_raw(self):
        done = await self.consume(
            stream(
                [
                    {
                        "name": "truncated",
                        "steps": [
                            {
                                "kind": "tool_call",
                                "id": "call_0",
                                "name": "read_file",
                                "arg_chunks": ['{"path": "a.p'],
                            },
                            {"kind": "finish", "reason": "length"},
                        ],
                    }
                ]
            )
        )

        block = done.message.content[0]
        # 解析不出来，但 raw 能看出收到了什么
        self.assertEqual(block.arguments, {})
        self.assertEqual(block.raw, '{"path": "a.p')

    async def test_usage_accumulates(self):
        done = await self.consume(
            stream(
                [
                    {
                        "name": "usage",
                        "steps": [
                            {"kind": "usage", "input": 10, "output": 5},
                            {"kind": "usage", "input": 2, "output": 3, "cache_read": 7},
                            {"kind": "finish", "reason": "stop"},
                        ],
                    }
                ]
            )
        )

        usage = done.usage
        self.assertEqual(usage.input_tokens, 12)
        self.assertEqual(usage.output_tokens, 8)
        self.assertEqual(usage.cache_read_tokens, 7)


if __name__ == "__main__":
    unittest.main()
