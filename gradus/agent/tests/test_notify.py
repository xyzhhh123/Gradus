"""`Agent.notify` 的注入语义测试。"""

from __future__ import annotations

import unittest

from gradus.agent.agent import Agent
from gradus.agent.base import LoopConfig
from gradus.agent.tests.fakes import RecordingProvider, make_provider, text_script
from gradus.agent.types import AgentDone
from gradus.ai.types import UserMessage


class NotifyTest(unittest.IsolatedAsyncioTestCase):
    async def test_notify_while_idle_is_dropped(self):
        provider = RecordingProvider(make_provider(text_script("ok")))
        agent = Agent(provider, LoopConfig(system=""))

        agent.notify("<task_notification/>")

        events = [event async for event in agent.prompt("hi")]
        done = events[-1]

        self.assertIsInstance(done, AgentDone)
        self.assertEqual(
            [message.content for message in provider.requests[0].messages], ["hi"]
        )

    async def test_notify_during_run_reaches_the_first_request(self):
        provider = RecordingProvider(make_provider(text_script("ok")))
        agent = Agent(provider, LoopConfig(system=""))

        events = agent.prompt("hi")
        agent.notify("<task_notification/>")
        collected = [event async for event in events]

        self.assertIsInstance(collected[-1], AgentDone)
        injected = [
            message
            for message in provider.requests[0].messages
            if isinstance(message, UserMessage) and message.origin == "harness"
        ]
        self.assertEqual(len(injected), 1)
        self.assertEqual(injected[0].content, "<task_notification/>")

    async def test_notify_is_not_leaked_to_the_next_run(self):
        provider = RecordingProvider(
            make_provider(text_script("first"), text_script("second"))
        )
        agent = Agent(provider, LoopConfig(system=""))

        async for _ in agent.prompt("one"):
            pass

        agent.notify("<task_notification/>")
        async for _ in agent.prompt("two"):
            pass

        self.assertFalse(
            any(
                isinstance(message, UserMessage) and message.origin == "harness"
                for message in provider.requests[1].messages
            )
        )


    async def test_prompt_can_be_tagged_as_harness(self):
        provider = RecordingProvider(make_provider(text_script("ok")))
        agent = Agent(provider, LoopConfig(system=""))

        async for _ in agent.prompt("tick", origin="harness"):
            pass

        first = provider.requests[0].messages[0]
        self.assertIsInstance(first, UserMessage)
        self.assertEqual(first.origin, "harness")

    async def test_follow_up_can_be_tagged_as_harness(self):
        provider = RecordingProvider(
            make_provider(text_script("first"), text_script("second"))
        )
        agent = Agent(provider, LoopConfig(system=""))

        events = agent.prompt("go")
        agent.follow_up("[Scheduled] tick", origin="harness")
        collected = [event async for event in events]

        self.assertIsInstance(collected[-1], AgentDone)
        injected = [
            message
            for message in provider.requests[1].messages
            if isinstance(message, UserMessage) and message.origin == "harness"
        ]
        self.assertEqual([message.content for message in injected], ["[Scheduled] tick"])


if __name__ == "__main__":
    unittest.main()
