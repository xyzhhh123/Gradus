"""`terminate` 批次提前结束的离线测试。"""

from __future__ import annotations

import unittest
from typing import Any

from gradus.agent.base import LoopConfig
from gradus.agent.hooks import (
    AfterToolCallResult,
    AgentHooks,
    BeforeToolCallResult,
)
from gradus.agent.loop import run_loop
from gradus.agent.tests.fakes import (
    FakeTool,
    RecordingProvider,
    make_provider,
    text_script,
    tool_script,
)
from gradus.agent.types import AgentDone, ToolExecutionEnd
from gradus.ai.types import UserMessage


async def drain(iterator: Any) -> list[Any]:
    return [event async for event in iterator]


class TerminateTest(unittest.IsolatedAsyncioTestCase):
    async def test_all_results_terminate_stops_before_next_request(self):
        tool = FakeTool(terminate=True)
        provider = RecordingProvider(
            make_provider(
                tool_script([("c1", "echo", {})]),
                text_script("unused"),
            )
        )

        events = [
            event
            async for event in run_loop(
                provider,
                [UserMessage("hi")],
                LoopConfig(system="", tools=[tool]),
            )
        ]

        self.assertEqual(len(provider.requests), 1)
        done = events[-1]
        self.assertIsInstance(done, AgentDone)
        self.assertEqual(done.turns, 1)
        self.assertEqual(done.stop_reason, "tool_use")

        ends = [e for e in events if isinstance(e, ToolExecutionEnd)]
        self.assertEqual(len(ends), 1)
        self.assertTrue(ends[0].result.terminate)

    async def test_mixed_batch_continues(self):
        stop = FakeTool("stop", terminate=True)
        go = FakeTool("go")
        provider = RecordingProvider(
            make_provider(
                tool_script([("c1", "stop", {}), ("c2", "go", {})]),
                text_script("done"),
            )
        )

        events = [
            event
            async for event in run_loop(
                provider,
                [UserMessage("hi")],
                LoopConfig(system="", tools=[stop, go]),
            )
        ]

        self.assertEqual(len(provider.requests), 2)
        self.assertIsInstance(events[-1], AgentDone)
        self.assertEqual(events[-1].turns, 2)

    async def test_blocked_before_tool_call_can_terminate(self):
        async def before_tool_call(context: Any) -> BeforeToolCallResult:
            return BeforeToolCallResult(
                block=True,
                reason="blocked by policy",
                terminate=True,
            )

        tool = FakeTool()
        provider = RecordingProvider(
            make_provider(
                tool_script([("c1", "echo", {})]),
                text_script("unused"),
            )
        )

        events = [
            event
            async for event in run_loop(
                provider,
                [UserMessage("hi")],
                LoopConfig(
                    system="",
                    tools=[tool],
                    hooks=AgentHooks(before_tool_call=before_tool_call),
                ),
            )
        ]

        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(tool.calls, [])

        ends = [e for e in events if isinstance(e, ToolExecutionEnd)]
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0].result.content, "blocked by policy")
        self.assertTrue(ends[0].result.is_error)
        self.assertTrue(ends[0].result.terminate)

    async def test_after_tool_call_can_terminate(self):
        async def after_tool_call(context: Any) -> AfterToolCallResult:
            return AfterToolCallResult(terminate=True)

        tool = FakeTool()
        provider = RecordingProvider(
            make_provider(
                tool_script([("c1", "echo", {})]),
                text_script("unused"),
            )
        )

        events = [
            event
            async for event in run_loop(
                provider,
                [UserMessage("hi")],
                LoopConfig(
                    system="",
                    tools=[tool],
                    hooks=AgentHooks(after_tool_call=after_tool_call),
                ),
            )
        ]

        self.assertEqual(len(provider.requests), 1)
        self.assertIsInstance(events[-1], AgentDone)

    async def test_terminate_still_runs_follow_up(self):
        tool = FakeTool(terminate=True)
        provider = RecordingProvider(
            make_provider(
                tool_script([("c1", "echo", {})]),
                text_script("done"),
            )
        )
        pending = [UserMessage("next task")]

        def get_follow_up():
            if not pending:
                return []
            messages = list(pending)
            pending.clear()
            return messages

        await drain(
            run_loop(
                provider,
                [UserMessage("hi")],
                LoopConfig(
                    system="",
                    tools=[tool],
                    get_follow_up_messages=get_follow_up,
                ),
            )
        )

        self.assertEqual(len(provider.requests), 2)
        self.assertTrue(
            any(
                isinstance(message, UserMessage)
                and message.content == "next task"
                for message in provider.requests[1].messages
            )
        )

    async def test_steering_overrides_terminate(self):
        tool = FakeTool(terminate=True)
        provider = RecordingProvider(
            make_provider(
                tool_script([("c1", "echo", {})]),
                text_script("done"),
            )
        )
        pending: list[UserMessage] = []

        def get_steering():
            if not pending:
                return []
            messages = list(pending)
            pending.clear()
            return messages

        pushed = False

        async def finish_turn(context: Any) -> None:
            nonlocal pushed
            if not pushed:
                pushed = True
                pending.append(UserMessage("wait"))
            return None

        await drain(
            run_loop(
                provider,
                [UserMessage("hi")],
                LoopConfig(
                    system="",
                    tools=[tool],
                    hooks=AgentHooks(finish_turn=finish_turn),
                    get_steering_messages=get_steering,
                ),
            )
        )

        self.assertEqual(len(provider.requests), 2)
        self.assertTrue(
            any(
                isinstance(message, UserMessage)
                and message.content == "wait"
                for message in provider.requests[1].messages
            )
        )


if __name__ == "__main__":
    unittest.main()
