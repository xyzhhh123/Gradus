"""有状态 Agent 的离线测试。"""

from __future__ import annotations

import asyncio
import unittest
from collections.abc import AsyncIterator
from typing import Any

from gradus.agent.agent import Agent
from gradus.agent.base import LoopConfig
from gradus.agent.hooks import AgentHooks, NextTurnUpdate
from gradus.agent.tests.fakes import (
    FakeTool,
    RecordingProvider,
    make_provider,
    text_script,
    tool_script,
)
from gradus.agent.types import (
    AgentDone,
    AgentError,
    ToolExecutionStart,
)
from gradus.ai.types import AssistantMessage, ToolResultMessage, UserMessage


async def drain(iterator: AsyncIterator[Any]) -> list[Any]:
    return [event async for event in iterator]


class AgentTest(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_keeps_history_across_runs(self):
        provider = RecordingProvider(
            make_provider(text_script("one"), text_script("two"))
        )
        agent = Agent(provider, LoopConfig(system=""))

        first = await drain(agent.prompt("hi"))
        second = await drain(agent.prompt("again"))

        self.assertIsInstance(first[-1], AgentDone)
        self.assertIsInstance(second[-1], AgentDone)
        self.assertEqual(len(agent.messages), 4)
        self.assertIsInstance(agent.messages[0], UserMessage)
        self.assertEqual(agent.messages[0].content, "hi")
        self.assertIsInstance(agent.messages[2], UserMessage)
        self.assertEqual(agent.messages[2].content, "again")

        request = provider.requests[1].messages
        self.assertEqual(len(request), 3)
        self.assertIsInstance(request[1], AssistantMessage)
        self.assertIsInstance(request[2], UserMessage)
        self.assertEqual(request[2].content, "again")

    async def test_continue_does_not_add_user_message(self):
        provider = RecordingProvider(
            make_provider(text_script("one"), text_script("two"))
        )
        agent = Agent(provider, LoopConfig(system=""))

        await drain(agent.prompt("hi"))
        await drain(agent.continue_())

        self.assertEqual(len(agent.messages), 3)
        self.assertIsInstance(agent.messages[2], AssistantMessage)
        self.assertEqual(len(provider.requests[1].messages), 2)

    async def test_retry_after_error_starts_a_clean_run(self):
        provider = make_provider(
            {"name": "fail", "steps": [{"kind": "error", "message": "boom"}]},
            text_script("recovered"),
        )
        agent = Agent(provider, LoopConfig(system=""))

        events = agent.prompt("hi")
        first: list[Any] = []
        async for event in events:
            first.append(event)
            if isinstance(event, AgentError):
                break

        self.assertIsInstance(first[-1], AgentError)

        # 模拟 session 的重试：丢弃旧迭代器，直接开新一轮
        events = agent.continue_()
        await asyncio.sleep(0.01)
        second = await drain(events)

        self.assertIsInstance(second[-1], AgentDone)
        self.assertEqual(agent.state, "idle")

    async def test_reset_clears_history(self):
        provider = make_provider(text_script("one"))
        agent = Agent(provider, LoopConfig(system=""))

        await drain(agent.prompt("hi"))
        agent.reset()

        self.assertEqual(agent.messages, ())

    async def test_running_agent_rejects_new_runs(self):
        provider = make_provider(text_script("one"))
        agent = Agent(provider, LoopConfig(system=""))

        iterator = agent.prompt("hi")

        with self.assertRaises(RuntimeError):
            agent.prompt("again")
        with self.assertRaises(RuntimeError):
            agent.continue_()
        with self.assertRaises(RuntimeError):
            agent.reset()

        await agent.abort()
        await iterator.aclose()

    async def test_abort_keeps_completed_turns(self):
        fast = FakeTool("fast")
        slow = FakeTool("slow", delay=1.0)
        provider = make_provider(
            tool_script([("call_0", "fast", {})]),
            tool_script([("call_1", "slow", {})]),
        )
        agent = Agent(
            provider,
            LoopConfig(system="", tools=[fast, slow]),
        )
        started = asyncio.Event()
        seen: list[Any] = []

        async def consume() -> None:
            async for event in agent.prompt("hi"):
                seen.append(event)
                if (
                    isinstance(event, ToolExecutionStart)
                    and event.call.name == "slow"
                ):
                    started.set()

        consumer = asyncio.create_task(consume())
        await asyncio.wait_for(started.wait(), timeout=1)
        await agent.abort()
        await consumer

        self.assertEqual(agent.state, "idle")
        self.assertEqual(len(agent.messages), 3)
        self.assertIsInstance(agent.messages[1], AssistantMessage)
        self.assertIsInstance(agent.messages[2], ToolResultMessage)
        self.assertFalse(
            any(isinstance(event, AgentDone) for event in seen)
        )
        errors = [
            event for event in seen if isinstance(event, AgentError)
        ]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].kind, "aborted")

    async def test_abort_drops_unanswered_prompt(self):
        slow = FakeTool("slow", delay=1.0)
        provider = make_provider(tool_script([("call_0", "slow", {})]))
        agent = Agent(provider, LoopConfig(system="", tools=[slow]))
        started = asyncio.Event()

        async def consume() -> None:
            async for event in agent.prompt("hi"):
                if (
                    isinstance(event, ToolExecutionStart)
                    and event.call.name == "slow"
                ):
                    started.set()

        consumer = asyncio.create_task(consume())
        await asyncio.wait_for(started.wait(), timeout=1)
        await agent.abort()
        await consumer

        # 一轮都没完成：问题和半成品都不该留在历史里，否则会串到下一轮。
        self.assertEqual(agent.messages, ())

    async def test_abort_drops_unused_prepare_next_turn_messages(self):
        slow = FakeTool("slow", delay=1.0)
        provider = make_provider(
            text_script("first", reason="deferred"),
            tool_script([("call_0", "slow", {})]),
        )

        async def prepare_next(context):
            if context.turn == 0:
                return NextTurnUpdate(messages=(UserMessage("next"),))
            return None

        agent = Agent(
            provider,
            LoopConfig(
                system="",
                tools=[slow],
                hooks=AgentHooks(prepare_next_turn=prepare_next),
            ),
        )
        started = asyncio.Event()

        async def consume() -> None:
            async for event in agent.prompt("hi"):
                if (
                    isinstance(event, ToolExecutionStart)
                    and event.call.name == "slow"
                ):
                    started.set()

        consumer = asyncio.create_task(consume())
        await asyncio.wait_for(started.wait(), timeout=1)
        await agent.abort()
        await consumer

        # turn 0 已提交；"next" 属于未发生的 turn 1，应丢弃
        self.assertEqual(len(agent.messages), 2)
        self.assertIsInstance(agent.messages[1], AssistantMessage)
        self.assertFalse(
            any(
                isinstance(message, UserMessage)
                and message.content == "next"
                for message in agent.messages
            )
        )

    async def test_steer_is_injected_after_tool_batch(self):
        slow = FakeTool("slow", delay=0.05)
        provider = RecordingProvider(
            make_provider(
                tool_script([("call_0", "slow", {})]),
                text_script("done"),
            )
        )
        agent = Agent(provider, LoopConfig(system="", tools=[slow]))
        started = asyncio.Event()

        async def consume() -> None:
            async for event in agent.prompt("hi"):
                if (
                    isinstance(event, ToolExecutionStart)
                    and event.call.name == "slow"
                ):
                    started.set()

        consumer = asyncio.create_task(consume())
        await asyncio.wait_for(started.wait(), timeout=1)
        agent.steer("look at b.py")
        await consumer

        second = provider.requests[1].messages
        self.assertTrue(
            any(
                isinstance(message, UserMessage)
                and message.content == "look at b.py"
                for message in second
            )
        )

    async def test_follow_up_runs_before_stop(self):
        provider = RecordingProvider(
            make_provider(text_script("one"), text_script("two"))
        )
        agent = Agent(provider, LoopConfig(system=""))

        iterator = agent.prompt("hi")
        agent.follow_up("then this")
        events = await drain(iterator)

        done = events[-1]
        self.assertIsInstance(done, AgentDone)
        self.assertEqual(done.turns, 2)
        self.assertTrue(
            any(
                isinstance(message, UserMessage)
                and message.content == "then this"
                for message in provider.requests[1].messages
            )
        )

    async def test_steer_and_follow_up_require_running(self):
        agent = Agent(make_provider(text_script("one")), LoopConfig(system=""))

        with self.assertRaises(RuntimeError):
            agent.steer("x")
        with self.assertRaises(RuntimeError):
            agent.follow_up("x")

        await drain(agent.prompt("hi"))

        with self.assertRaises(RuntimeError):
            agent.steer("x")
        with self.assertRaises(RuntimeError):
            agent.follow_up("x")

    async def test_unconsumed_follow_up_does_not_leak_into_next_run(self):
        async def finish_turn(context: Any) -> str:
            return "end"

        provider = RecordingProvider(
            make_provider(text_script("one"), text_script("two"))
        )
        agent = Agent(
            provider,
            LoopConfig(
                system="",
                hooks=AgentHooks(finish_turn=finish_turn),
            ),
        )

        iterator = agent.prompt("hi")
        agent.follow_up("stale")
        await drain(iterator)
        await drain(agent.prompt("next"))

        # 第二次运行只应发一次请求；如果有残留，会在停止前被 drain 出去。
        self.assertEqual(len(provider.requests), 2)
        for request in provider.requests:
            self.assertFalse(
                any(
                    isinstance(message, UserMessage)
                    and message.content == "stale"
                    for message in request.messages
                )
            )

    async def test_unconsumed_steering_does_not_leak_into_next_run(self):
        agent: Agent | None = None

        async def before_finish(context: Any) -> None:
            # 这里位于轮次后 drain 之后，消息不会被当次运行消费。
            assert agent is not None
            agent.steer("stale-steer")
            return None

        provider = RecordingProvider(
            make_provider(text_script("one"), text_script("two"))
        )
        agent = Agent(
            provider,
            LoopConfig(system="", hooks=AgentHooks(before_finish=before_finish)),
        )

        await drain(agent.prompt("hi"))
        await drain(agent.prompt("next"))

        self.assertEqual(len(provider.requests), 2)
        for request in provider.requests:
            self.assertFalse(
                any(
                    isinstance(message, UserMessage)
                    and message.content == "stale-steer"
                    for message in request.messages
                )
            )

    async def test_follow_up_one_at_a_time(self):
        provider = RecordingProvider(
            make_provider(
                text_script("a"),
                text_script("b"),
                text_script("c"),
            )
        )
        agent = Agent(
            provider,
            LoopConfig(system=""),
            follow_up_mode="one-at-a-time",
        )

        iterator = agent.prompt("hi")
        agent.follow_up("f1")
        agent.follow_up("f2")
        events = await drain(iterator)

        done = events[-1]
        self.assertIsInstance(done, AgentDone)
        self.assertEqual(done.turns, 3)

        second = provider.requests[1].messages
        third = provider.requests[2].messages
        self.assertTrue(
            any(
                isinstance(message, UserMessage) and message.content == "f1"
                for message in second
            )
        )
        self.assertFalse(
            any(
                isinstance(message, UserMessage) and message.content == "f2"
                for message in second
            )
        )
        self.assertTrue(
            any(
                isinstance(message, UserMessage) and message.content == "f2"
                for message in third
            )
        )

    async def test_wait_for_idle_commits_history(self):
        provider = make_provider(text_script("one"))
        agent = Agent(provider, LoopConfig(system=""))

        iterator = agent.prompt("hi")
        await agent.wait_for_idle()

        self.assertEqual(agent.state, "idle")
        self.assertEqual(len(agent.messages), 2)
        await iterator.aclose()


if __name__ == "__main__":
    unittest.main()
