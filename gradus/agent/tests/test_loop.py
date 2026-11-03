"""agent 循环的离线测试。

覆盖：
- 纯文本、工具调用后再回答、并行工具顺序
- 未知工具、工具抛异常、参数缺失
- length 不执行、stop 带工具调用不执行、tool_use 无调用
- deferred 续跑、max_turns、StreamError、配置错误
- usage 汇总、new_messages、输入历史不被修改

使用异步的 FakeProvider + 本地异步假工具，不联网。
运行（在 Gradus 目录下）：
    uv run pytest gradus/agent/tests
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from gradus.agent.base import LoopConfig
from gradus.agent.loop import run_loop
from gradus.agent.tests.fakes import (
    BadResultTool,
    CancelOnSecondTool,
    FakeTool,
    NoDoneProvider,
    RaisingProvider,
    RecordingProvider,
    collect,
    make_provider,
    names_of,
    text_script,
    tool_script,
)
from gradus.agent.types import (
    AgentDone,
    AgentError,
    AgentStart,
    ToolExecutionEnd,
    ToolExecutionStart,
    TurnEnd,
)
from gradus.ai.types import (
    AssistantMessage,
    TextBlock,
    ToolResultMessage,
    UserMessage,
    Usage,
)


class AgentLoopTest(unittest.IsolatedAsyncioTestCase):
    async def test_text_only_run(self):
        messages = [UserMessage("hi")]
        events = await collect(
            make_provider(text_script("你好", usage=(5, 2))),
            [],
            messages=messages,
        )

        self.assertEqual(
            names_of(events),
            [
                "AgentStart",
                "TurnStart",
                "StreamStart",
                "TextStart",
                "TextDelta",
                "TextEnd",
                "StreamDone",
                "TurnEnd",
                "AgentDone",
            ],
        )
        self.assertIsInstance(events[0], AgentStart)

        done = events[-1]
        self.assertIsInstance(done, AgentDone)
        self.assertEqual(done.turns, 1)
        self.assertEqual(done.stop_reason, "stop")
        self.assertEqual(done.usage, Usage(input_tokens=5, output_tokens=2))
        self.assertEqual(len(done.new_messages), 1)

        assistant = done.new_messages[0]
        self.assertIsInstance(assistant, AssistantMessage)
        self.assertIsInstance(assistant.content[0], TextBlock)
        self.assertEqual(assistant.content[0].text, "你好")

        # 输入历史只读
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].content, "hi")

    async def test_tool_call_then_answer(self):
        tool = FakeTool("echo", required=("text",))
        provider = make_provider(
            tool_script([("call_0", "echo", {"text": "a"})], usage=(10, 2)),
            text_script("done", usage=(3, 1)),
        )

        events = await collect(provider, [tool])

        self.assertEqual(tool.calls, [("call_0", {"text": "a"})])
        self.assertEqual(
            sum(isinstance(event, ToolExecutionStart) for event in events), 1
        )
        self.assertEqual(
            sum(isinstance(event, ToolExecutionEnd) for event in events), 1
        )
        self.assertEqual(sum(isinstance(event, TurnEnd) for event in events), 2)

        done = events[-1]
        self.assertIsInstance(done, AgentDone)
        self.assertEqual(done.turns, 2)
        self.assertEqual(done.usage, Usage(input_tokens=13, output_tokens=3))
        self.assertEqual(len(done.new_messages), 3)

        result = done.new_messages[1]
        self.assertIsInstance(result, ToolResultMessage)
        self.assertEqual(result.tool_call_id, "call_0")
        self.assertEqual(result.content, "a")
        self.assertFalse(result.is_error)

    async def test_parallel_tool_calls_keep_order(self):
        tool = FakeTool("echo", required=("text",))
        provider = make_provider(
            tool_script(
                [
                    ("call_0", "echo", {"text": "a"}),
                    ("call_1", "echo", {"text": "b"}),
                ]
            ),
            text_script("done"),
        )

        events = await collect(provider, [tool])
        done = events[-1]

        self.assertIsInstance(done, AgentDone)
        self.assertEqual(
            tool.calls,
            [("call_0", {"text": "a"}), ("call_1", {"text": "b"})],
        )

        results = [
            message
            for message in done.new_messages
            if isinstance(message, ToolResultMessage)
        ]
        self.assertEqual([message.content for message in results], ["a", "b"])

    async def test_unknown_tool_becomes_error_result(self):
        tool = FakeTool("echo", required=("text",))
        provider = make_provider(
            tool_script([("call_0", "missing", {"text": "a"})]),
            text_script("done"),
        )

        events = await collect(provider, [tool])

        end = next(event for event in events if isinstance(event, ToolExecutionEnd))
        self.assertTrue(end.result.is_error)
        self.assertEqual(end.result.content, "unknown tool: missing")

        done = events[-1]
        self.assertIsInstance(done, AgentDone)
        result = done.new_messages[1]
        self.assertIsInstance(result, ToolResultMessage)
        self.assertTrue(result.is_error)

    async def test_tool_exception_becomes_error_result(self):
        tool = FakeTool("boom", error=ValueError("boom"))
        provider = make_provider(
            tool_script([("call_0", "boom", {})]),
            text_script("done"),
        )

        events = await collect(provider, [tool])

        end = next(event for event in events if isinstance(event, ToolExecutionEnd))
        self.assertTrue(end.result.is_error)
        self.assertEqual(end.result.content, "ValueError: boom")
        self.assertIsInstance(events[-1], AgentDone)

    async def test_missing_required_arguments(self):
        tool = FakeTool("read_file", required=("path",), result="file")
        provider = make_provider(
            tool_script([("call_0", "read_file", {})]),
            text_script("done"),
        )

        events = await collect(provider, [tool])

        self.assertEqual(tool.calls, [])
        end = next(event for event in events if isinstance(event, ToolExecutionEnd))
        self.assertTrue(end.result.is_error)
        self.assertEqual(
            end.result.content,
            "missing required arguments for read_file: path",
        )

    async def test_length_with_tool_calls_returns_errors_and_continues(self):
        tool = FakeTool("echo", required=("text",))
        provider = make_provider(
            {
                "name": "truncated",
                "steps": [
                    {
                        "kind": "tool_call",
                        "id": "call_0",
                        "name": "echo",
                        "arg_chunks": ['{"text": "a'],
                    },
                    {"kind": "finish", "reason": "length"},
                ],
            },
            text_script("done"),
        )

        events = await collect(provider, [tool])

        self.assertEqual(tool.calls, [])
        start = next(
            event for event in events if isinstance(event, ToolExecutionStart)
        )
        self.assertEqual(start.call.name, "echo")

        end = next(
            event for event in events if isinstance(event, ToolExecutionEnd)
        )
        self.assertTrue(end.result.is_error)
        self.assertIn("response was truncated", end.result.content)
        self.assertEqual(
            sum(isinstance(event, TurnEnd) for event in events),
            2,
        )

        done = events[-1]
        self.assertIsInstance(done, AgentDone)
        self.assertEqual(done.turns, 2)
        self.assertEqual(done.stop_reason, "stop")
        self.assertEqual(len(done.new_messages), 3)

        result = done.new_messages[1]
        self.assertIsInstance(result, ToolResultMessage)
        self.assertTrue(result.is_error)

    async def test_length_without_tool_calls_finishes(self):
        provider = make_provider(text_script("partial", reason="length"))

        events = await collect(provider, [])

        self.assertFalse(
            any(isinstance(event, ToolExecutionStart) for event in events)
        )
        done = events[-1]
        self.assertIsInstance(done, AgentDone)
        self.assertEqual(done.stop_reason, "length")
        self.assertEqual(done.turns, 1)
        self.assertEqual(len(done.new_messages), 1)

    async def test_stop_with_tool_calls_does_not_execute(self):
        tool = FakeTool("echo", required=("text",))
        provider = make_provider(
            tool_script([("call_0", "echo", {"text": "a"})], reason="stop")
        )

        events = await collect(provider, [tool])

        self.assertEqual(tool.calls, [])
        self.assertFalse(
            any(isinstance(event, ToolExecutionStart) for event in events)
        )
        done = events[-1]
        self.assertIsInstance(done, AgentDone)
        self.assertEqual(done.stop_reason, "stop")
        self.assertEqual(len(done.new_messages), 1)

    async def test_tool_use_without_calls_becomes_agent_error(self):
        provider = make_provider(
            {"name": "empty", "steps": [{"kind": "finish", "reason": "tool_use"}]}
        )

        events = await collect(provider, [])
        error = events[-1]

        self.assertIsInstance(error, AgentError)
        self.assertEqual(error.kind, "provider")
        self.assertEqual(error.message, "stop_reason=tool_use but no tool calls")
        self.assertIsInstance(error.partial, AssistantMessage)
        self.assertEqual(error.new_messages, [])

        self.assertFalse(any(isinstance(event, AgentDone) for event in events))
        self.assertFalse(any(isinstance(event, TurnEnd) for event in events))

    async def test_provider_exception_becomes_agent_error(self):
        events = await collect(RaisingProvider(), [])
        error = events[-1]

        self.assertIsInstance(error, AgentError)
        self.assertEqual(error.kind, "provider")
        self.assertEqual(error.message, "RuntimeError: boom")
        self.assertEqual(error.new_messages, [])
        self.assertFalse(any(isinstance(event, AgentDone) for event in events))

    async def test_deferred_requests_again(self):
        provider = make_provider(
            text_script("等待", reason="deferred"),
            text_script("完成", reason="stop"),
        )

        events = await collect(provider, [])
        done = events[-1]

        self.assertIsInstance(done, AgentDone)
        self.assertEqual(done.turns, 2)
        self.assertEqual(done.stop_reason, "stop")
        self.assertEqual(len(done.new_messages), 2)
        self.assertEqual(done.new_messages[0].stop_reason, "deferred")

    async def test_usage_accumulates_across_turns(self):
        provider = make_provider(
            text_script("等待", reason="deferred", usage=(10, 2)),
            text_script("完成", reason="stop", usage=(3, 4)),
        )

        events = await collect(provider, [])
        turn_endings = [event for event in events if isinstance(event, TurnEnd)]

        self.assertEqual(
            [event.usage for event in turn_endings],
            [
                Usage(input_tokens=10, output_tokens=2),
                Usage(input_tokens=3, output_tokens=4),
            ],
        )
        done = events[-1]
        self.assertIsInstance(done, AgentDone)
        self.assertEqual(done.usage, Usage(input_tokens=13, output_tokens=6))

    async def test_stream_error_becomes_agent_error(self):
        provider = make_provider(
            {
                "name": "aborted",
                "steps": [
                    {"kind": "text", "chunks": ["写到一半"]},
                    {"kind": "error", "error_kind": "aborted", "message": "用户中断"},
                ],
            }
        )

        events = await collect(provider, [])

        error = events[-1]
        self.assertIsInstance(error, AgentError)
        self.assertEqual(error.kind, "aborted")
        self.assertEqual(error.message, "用户中断")
        self.assertEqual(error.new_messages, [])
        self.assertEqual(error.partial.content[0].text, "写到一半")

        self.assertFalse(any(isinstance(event, AgentDone) for event in events))
        self.assertFalse(any(isinstance(event, TurnEnd) for event in events))

    async def test_error_kind_maps_to_provider(self):
        provider = make_provider(
            {
                "name": "failed",
                "steps": [{"kind": "error", "error_kind": "error", "message": "boom"}],
            }
        )

        events = await collect(provider, [])
        error = events[-1]

        self.assertIsInstance(error, AgentError)
        self.assertEqual(error.kind, "provider")

    async def test_max_turns(self):
        tool = FakeTool("echo", required=("text",))
        provider = make_provider(
            tool_script([("call_0", "echo", {"text": "a"})]),
            tool_script([("call_1", "echo", {"text": "b"})]),
        )

        events = await collect(provider, [tool], max_turns=2)

        self.assertEqual(len(tool.calls), 2)
        error = events[-1]
        self.assertIsInstance(error, AgentError)
        self.assertEqual(error.kind, "max_turns")
        self.assertIn("max_turns=2", error.message)
        self.assertEqual(len(error.new_messages), 4)

    async def test_duplicate_tool_names_is_config_error(self):
        first = FakeTool("echo")
        second = FakeTool("echo")

        events = await collect(make_provider(), [first, second])

        self.assertEqual(names_of(events), ["AgentStart", "AgentError"])
        error = events[-1]
        self.assertIsInstance(error, AgentError)
        self.assertEqual(error.kind, "config")
        self.assertIn("duplicate tool name: echo", error.message)

    async def test_cancellation_propagates(self):
        tool = FakeTool("cancel", error=asyncio.CancelledError())
        provider = make_provider(tool_script([("call_0", "cancel", {})]))
        seen: list[Any] = []

        with self.assertRaises(asyncio.CancelledError):
            async for event in run_loop(
                provider,
                [UserMessage("hi")],
                LoopConfig(system="", tools=[tool]),
            ):
                seen.append(event)

        error = seen[-1]
        self.assertIsInstance(error, AgentError)
        self.assertEqual(error.kind, "aborted")
        self.assertEqual(error.new_messages, [])

    async def test_cancellation_mid_tool_batch_discards_turn(self):
        tool = CancelOnSecondTool()
        provider = make_provider(
            tool_script(
                [
                    ("call_0", "cancel", {}),
                    ("call_1", "cancel", {}),
                ]
            )
        )

        seen: list[Any] = []
        with self.assertRaises(asyncio.CancelledError):
            async for event in run_loop(
                provider,
                [UserMessage("hi")],
                LoopConfig(system="", tools=[tool]),
            ):
                seen.append(event)

        self.assertEqual(tool.calls, 2)
        # 整轮没有提交：没有 TurnEnd、没有 AgentDone
        self.assertFalse(any(isinstance(event, TurnEnd) for event in seen))
        self.assertFalse(any(isinstance(event, AgentDone) for event in seen))
        errors = [
            event for event in seen if isinstance(event, AgentError)
        ]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].kind, "aborted")
        self.assertEqual(errors[0].new_messages, [])

    async def test_max_turns_zero_terminates_without_request(self):
        provider = RecordingProvider(make_provider(text_script("never")))

        events = await collect(provider, [], max_turns=0)

        self.assertEqual(names_of(events), ["AgentStart", "AgentError"])
        error = events[-1]
        self.assertIsInstance(error, AgentError)
        self.assertEqual(error.kind, "max_turns")
        self.assertIn("max_turns=0", error.message)
        self.assertEqual(len(provider.requests), 0)

    async def test_stream_without_done_becomes_provider_error(self):
        events = await collect(NoDoneProvider(), [])

        error = events[-1]
        self.assertIsInstance(error, AgentError)
        self.assertEqual(error.kind, "provider")
        self.assertIn("without StreamDone", error.message)

    async def test_invalid_tool_result_becomes_error(self):
        tool = BadResultTool()
        provider = make_provider(
            tool_script([("call_0", "bad", {})]),
            text_script("done"),
        )

        events = await collect(provider, [tool])

        end = next(
            event for event in events if isinstance(event, ToolExecutionEnd)
        )
        self.assertTrue(end.result.is_error)
        self.assertIn("returned invalid result", end.result.content)
        self.assertIsInstance(events[-1], AgentDone)

    async def test_stream_error_partial_usage_is_counted(self):
        provider = make_provider(
            {
                "name": "aborted",
                "steps": [
                    {"kind": "usage", "input": 7, "output": 3},
                    {
                        "kind": "error",
                        "error_kind": "aborted",
                        "message": "中断",
                    },
                ],
            }
        )

        events = await collect(provider, [])

        error = events[-1]
        self.assertIsInstance(error, AgentError)
        self.assertEqual(error.kind, "aborted")
        self.assertEqual(
            error.usage,
            Usage(input_tokens=7, output_tokens=3),
        )


if __name__ == "__main__":
    unittest.main()
