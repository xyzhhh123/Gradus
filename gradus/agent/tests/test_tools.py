"""工具参数校验、超时与执行耗时的离线测试。"""

from __future__ import annotations

import asyncio
import unittest

from gradus.agent.base import LoopConfig
from gradus.agent.tests.fakes import (
    CancelOnSecondTool,
    FakeTool,
    collect,
    make_provider,
    text_script,
    tool_script,
)
from gradus.agent.types import AgentDone, ToolExecutionEnd
from gradus.ai.types import ToolResultMessage


class ToolRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_argument_type_is_rejected(self):
        tool = FakeTool("echo", required=("text",))
        provider = make_provider(
            tool_script([("call_0", "echo", {"text": 123})]),
            text_script("done"),
        )
        config = LoopConfig(system="", tools=[tool])

        events = await collect(provider, [], config=config)

        self.assertEqual(tool.calls, [])
        end = next(event for event in events if isinstance(event, ToolExecutionEnd))
        self.assertTrue(end.result.is_error)
        self.assertIn("invalid arguments for echo", end.result.content)
        self.assertIn("text", end.result.content)

        done = events[-1]
        self.assertIsInstance(done, AgentDone)
        result = done.new_messages[1]
        self.assertIsInstance(result, ToolResultMessage)
        self.assertTrue(result.is_error)

    async def test_tool_timeout_becomes_error_result(self):
        tool = FakeTool("slow", delay=0.1)
        provider = make_provider(
            tool_script([("call_0", "slow", {})]),
            text_script("done"),
        )
        config = LoopConfig(system="", tools=[tool], tool_timeout=0.01)

        events = await collect(provider, [], config=config)

        end = next(event for event in events if isinstance(event, ToolExecutionEnd))
        self.assertTrue(end.result.is_error)
        self.assertIn("timed out", end.result.content)
        self.assertIsInstance(events[-1], AgentDone)

    async def test_tool_execution_end_records_duration(self):
        tool = FakeTool("echo")
        provider = make_provider(
            tool_script([("call_0", "echo", {})]),
            text_script("done"),
        )
        config = LoopConfig(system="", tools=[tool])

        events = await collect(provider, [], config=config)

        end = next(event for event in events if isinstance(event, ToolExecutionEnd))
        self.assertIsNotNone(end.duration_ms)
        self.assertGreaterEqual(end.duration_ms, 0)

    async def test_cancellation_still_propagates_with_timeout(self):
        tool = CancelOnSecondTool()
        provider = make_provider(
            tool_script(
                [
                    ("call_0", "cancel", {}),
                    ("call_1", "cancel", {}),
                ]
            )
        )
        config = LoopConfig(system="", tools=[tool], tool_timeout=1.0)

        with self.assertRaises(asyncio.CancelledError):
            await collect(provider, [], config=config)


if __name__ == "__main__":
    unittest.main()
