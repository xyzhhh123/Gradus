"""steering / follow-up 在循环中的注入时机测试。"""

from __future__ import annotations

import unittest
from collections.abc import Callable

from gradus.agent.base import LoopConfig
from gradus.agent.hooks import AgentHooks
from gradus.agent.tests.fakes import (
    FakeTool,
    RecordingProvider,
    collect,
    make_provider,
    text_script,
    tool_script,
)
from gradus.agent.types import AgentDone, AgentError
from gradus.ai.types import Message, UserMessage


def scripted(
    batches: list[list[Message]],
) -> Callable[[], list[Message]]:
    """按批次返回消息，用完后返回空列表。"""
    remaining = list(batches)

    def take() -> list[Message]:
        if not remaining:
            return []
        return remaining.pop(0)

    return take


class SteeringLoopTest(unittest.IsolatedAsyncioTestCase):
    async def test_steering_before_first_request_is_committed(self):
        provider = RecordingProvider(make_provider(text_script("ok")))
        config = LoopConfig(
            system="",
            get_steering_messages=scripted(
                [[UserMessage("steer")], []]
            ),
        )

        events = await collect(provider, [], config=config)

        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(
            [message.content for message in provider.requests[0].messages],
            ["hi", "steer"],
        )
        done = events[-1]
        self.assertIsInstance(done, AgentDone)
        self.assertEqual(len(done.new_messages), 2)
        self.assertEqual(done.new_messages[0].content, "steer")

    async def test_steering_after_tool_batch_starts_next_turn(self):
        tool = FakeTool("echo", required=("text",))
        provider = RecordingProvider(
            make_provider(
                tool_script([("call_0", "echo", {"text": "a"})]),
                text_script("done"),
            )
        )
        config = LoopConfig(
            system="",
            tools=[tool],
            get_steering_messages=scripted(
                [[], [UserMessage("steer")], []]
            ),
        )

        events = await collect(provider, [], config=config)

        done = events[-1]
        self.assertIsInstance(done, AgentDone)
        self.assertEqual(done.turns, 2)
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(
            provider.requests[1].messages[-1].content,
            "steer",
        )

    async def test_steering_after_stop_forces_another_turn(self):
        provider = RecordingProvider(
            make_provider(text_script("first"), text_script("second"))
        )
        config = LoopConfig(
            system="",
            get_steering_messages=scripted(
                [[], [UserMessage("steer")], []]
            ),
        )

        events = await collect(provider, [], config=config)

        done = events[-1]
        self.assertIsInstance(done, AgentDone)
        self.assertEqual(done.turns, 2)
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(
            provider.requests[1].messages[-1].content,
            "steer",
        )

    async def test_follow_up_runs_before_stop(self):
        provider = RecordingProvider(
            make_provider(text_script("first"), text_script("second"))
        )
        config = LoopConfig(
            system="",
            get_follow_up_messages=scripted(
                [[UserMessage("follow")], []]
            ),
        )

        events = await collect(provider, [], config=config)

        done = events[-1]
        self.assertIsInstance(done, AgentDone)
        self.assertEqual(done.turns, 2)
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(
            provider.requests[1].messages[-1].content,
            "follow",
        )

    async def test_finish_turn_end_skips_follow_up(self):
        provider = RecordingProvider(make_provider(text_script("first")))

        async def finish(context):
            return "end"

        config = LoopConfig(
            system="",
            hooks=AgentHooks(finish_turn=finish),
            get_follow_up_messages=scripted(
                [[UserMessage("follow")]]
            ),
        )

        events = await collect(provider, [], config=config)

        done = events[-1]
        self.assertIsInstance(done, AgentDone)
        self.assertEqual(done.turns, 1)
        self.assertEqual(len(provider.requests), 1)
        self.assertFalse(
            any(
                isinstance(message, UserMessage)
                and message.content == "follow"
                for message in done.new_messages
            )
        )

    async def test_steering_getter_error_becomes_config_error(self):
        provider = RecordingProvider(make_provider(text_script("never")))

        def bad():
            raise RuntimeError("steering failed")

        config = LoopConfig(system="", get_steering_messages=bad)

        events = await collect(provider, [], config=config)

        self.assertEqual(len(provider.requests), 0)
        error = events[-1]
        self.assertIsInstance(error, AgentError)
        self.assertEqual(error.kind, "config")
        self.assertIn("get_steering_messages failed", error.message)

    async def test_follow_up_getter_error_becomes_config_error(self):
        provider = RecordingProvider(make_provider(text_script("first")))

        def bad():
            raise RuntimeError("follow-up failed")

        config = LoopConfig(system="", get_follow_up_messages=bad)

        events = await collect(provider, [], config=config)

        self.assertEqual(len(provider.requests), 1)
        error = events[-1]
        self.assertIsInstance(error, AgentError)
        self.assertEqual(error.kind, "config")
        self.assertIn("get_follow_up_messages failed", error.message)


if __name__ == "__main__":
    unittest.main()
