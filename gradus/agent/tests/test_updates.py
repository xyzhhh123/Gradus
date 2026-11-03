"""工具执行更新事件的离线测试。"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from gradus.agent.base import LoopConfig
from gradus.agent.loop import run_loop
from gradus.agent.tests.fakes import (
    FakeTool,
    make_provider,
    text_script,
    tool_script,
)
from gradus.agent.types import (
    AgentDone,
    ToolExecutionEnd,
    ToolExecutionStart,
    ToolExecutionUpdate,
    ToolResult,
    TurnEnd,
)
from gradus.ai.base import ToolSpec
from gradus.ai.types import UserMessage


class StreamingTool:
    """按顺序发出若干片段，再返回最终结果。"""

    def __init__(
        self,
        name: str,
        chunks: tuple[str, ...],
        *,
        execution_mode: str = "serial",
        delay: float = 0.0,
    ) -> None:
        self.spec = ToolSpec(
            name=name,
            description="",
            parameters={"type": "object", "properties": {}},
        )
        self.execution_mode = execution_mode
        self.chunks = chunks
        self.delay = delay

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: Any = None,
    ) -> ToolResult:
        for chunk in self.chunks:
            if on_update is not None:
                on_update(chunk)
            if self.delay:
                await asyncio.sleep(self.delay)
        return ToolResult("done")


def indices(events: list[Any], event_type: type, call_id: str) -> list[int]:
    return [
        index
        for index, event in enumerate(events)
        if isinstance(event, event_type) and event.call.id == call_id
    ]


class ToolUpdatesTest(unittest.IsolatedAsyncioTestCase):
    async def test_updates_arrive_between_start_and_end(self):
        tool = StreamingTool("stream", ("a", "b"))
        provider = make_provider(
            tool_script([("c1", "stream", {})]),
            text_script("done"),
        )

        events = [
            event
            async for event in run_loop(
                provider,
                [UserMessage("hi")],
                LoopConfig(system="", tools=[tool]),
            )
        ]

        updates = [e for e in events if isinstance(e, ToolExecutionUpdate)]
        self.assertEqual([e.partial for e in updates], ["a", "b"])
        self.assertTrue(all(e.call.id == "c1" for e in updates))

        start = indices(events, ToolExecutionStart, "c1")[0]
        end = indices(events, ToolExecutionEnd, "c1")[0]
        self.assertLess(start, events.index(updates[0]))
        self.assertLess(events.index(updates[-1]), end)

    async def test_parallel_updates_stay_ordered_per_call(self):
        first = StreamingTool(
            "first", ("a1", "a2"), execution_mode="parallel", delay=0.01
        )
        second = StreamingTool(
            "second", ("b1", "b2"), execution_mode="parallel", delay=0.005
        )
        provider = make_provider(
            tool_script([("c1", "first", {}), ("c2", "second", {})]),
            text_script("done"),
        )

        events = [
            event
            async for event in run_loop(
                provider,
                [UserMessage("hi")],
                LoopConfig(system="", tools=[first, second]),
            )
        ]

        updates = [e for e in events if isinstance(e, ToolExecutionUpdate)]
        self.assertEqual(
            [e.partial for e in updates if e.call.id == "c1"],
            ["a1", "a2"],
        )
        self.assertEqual(
            [e.partial for e in updates if e.call.id == "c2"],
            ["b1", "b2"],
        )

        # 两个调用的更新确实交错，但仍能按 call.id 归属。
        a1 = events.index(
            next(e for e in updates if e.partial == "a1")
        )
        a2 = events.index(
            next(e for e in updates if e.partial == "a2")
        )
        b1 = events.index(
            next(e for e in updates if e.partial == "b1")
        )
        self.assertLess(a1, b1)
        self.assertLess(b1, a2)

        # 每个调用的 End 都在它最后一条 Update 之后。
        for call_id in ("c1", "c2"):
            last_update = max(indices(events, ToolExecutionUpdate, call_id))
            end = indices(events, ToolExecutionEnd, call_id)[0]
            self.assertLess(last_update, end)

    async def test_updates_do_not_enter_history(self):
        tool = StreamingTool("stream", ("partial-1", "partial-2"))
        provider = make_provider(
            tool_script([("c1", "stream", {})]),
            text_script("done"),
        )

        events = [
            event
            async for event in run_loop(
                provider,
                [UserMessage("hi")],
                LoopConfig(system="", tools=[tool]),
            )
        ]

        turn_end = next(e for e in events if isinstance(e, TurnEnd))
        self.assertEqual(
            [result.content for result in turn_end.tool_results],
            ["done"],
        )

        done = events[-1]
        self.assertIsInstance(done, AgentDone)
        self.assertFalse(
            any(
                "partial-" in getattr(message, "content", "")
                for message in done.new_messages
                if isinstance(getattr(message, "content", ""), str)
            )
        )

    async def test_tool_without_updates_emits_none(self):
        tool = FakeTool()
        provider = make_provider(
            tool_script([("c1", "echo", {})]),
            text_script("done"),
        )

        events = [
            event
            async for event in run_loop(
                provider,
                [UserMessage("hi")],
                LoopConfig(system="", tools=[tool]),
            )
        ]

        self.assertFalse(
            any(isinstance(e, ToolExecutionUpdate) for e in events)
        )


if __name__ == "__main__":
    unittest.main()
