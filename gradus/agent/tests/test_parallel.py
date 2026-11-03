"""并行工具批次的离线测试。"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from gradus.agent.base import LoopConfig
from gradus.agent.loop import run_loop
from gradus.agent.tests.fakes import make_provider, text_script, tool_script
from gradus.agent.types import (
    AgentDone,
    AgentError,
    ToolExecutionEnd,
    ToolResult,
    TurnEnd,
)
from gradus.ai.base import ToolSpec
from gradus.ai.types import UserMessage


class Probe:
    """记录同一批工具的最大并发数。"""

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0

    def enter(self) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)

    def leave(self) -> None:
        self.active -= 1


class ProbeTool:
    def __init__(
        self,
        name: str,
        probe: Probe,
        *,
        execution_mode: str = "parallel",
        delay: float = 0.05,
    ) -> None:
        self.spec = ToolSpec(
            name=name,
            description="",
            parameters={"type": "object", "properties": {}},
        )
        self.execution_mode = execution_mode
        self.probe = probe
        self.delay = delay
        self.calls: list[str] = []

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: Any = None,
    ) -> ToolResult:
        self.probe.enter()
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.probe.leave()
        self.calls.append(call_id)
        return ToolResult(call_id)


class ParallelToolsTest(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_tools_overlap(self):
        probe = Probe()
        first = ProbeTool("first", probe)
        second = ProbeTool("second", probe)
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

        self.assertEqual(probe.max_active, 2)
        self.assertEqual(first.calls, ["c1"])
        self.assertEqual(second.calls, ["c2"])
        self.assertIsInstance(events[-1], AgentDone)

    async def test_results_keep_source_order(self):
        probe = Probe()
        slow = ProbeTool("slow", probe, delay=0.05)
        fast = ProbeTool("fast", probe, delay=0.0)
        provider = make_provider(
            tool_script([("slow-id", "slow", {}), ("fast-id", "fast", {})]),
            text_script("done"),
        )

        events = [
            event
            async for event in run_loop(
                provider,
                [UserMessage("hi")],
                LoopConfig(system="", tools=[slow, fast]),
            )
        ]

        ends = [e for e in events if isinstance(e, ToolExecutionEnd)]
        self.assertEqual([e.call.id for e in ends], ["fast-id", "slow-id"])

        turn_end = next(e for e in events if isinstance(e, TurnEnd))
        self.assertEqual(
            [result.tool_call_id for result in turn_end.tool_results],
            ["slow-id", "fast-id"],
        )
        self.assertEqual(
            [result.content for result in turn_end.tool_results],
            ["slow-id", "fast-id"],
        )

    async def test_one_serial_tool_makes_batch_serial(self):
        probe = Probe()
        first = ProbeTool("first", probe)
        second = ProbeTool("second", probe)
        writer = ProbeTool("writer", probe, execution_mode="serial")
        provider = make_provider(
            tool_script(
                [
                    ("c1", "first", {}),
                    ("c2", "writer", {}),
                    ("c3", "second", {}),
                ]
            ),
            text_script("done"),
        )

        events = [
            event
            async for event in run_loop(
                provider,
                [UserMessage("hi")],
                LoopConfig(system="", tools=[first, writer, second]),
            )
        ]

        self.assertEqual(probe.max_active, 1)
        self.assertEqual(first.calls, ["c1"])
        self.assertEqual(writer.calls, ["c2"])
        self.assertEqual(second.calls, ["c3"])

        turn_end = next(e for e in events if isinstance(e, TurnEnd))
        self.assertEqual(
            [result.tool_call_id for result in turn_end.tool_results],
            ["c1", "c2", "c3"],
        )

    async def test_cancellation_discards_parallel_turn(self):
        class SlowCancelProbe:
            execution_mode = "parallel"
            spec = ToolSpec(
                name="slow",
                description="",
                parameters={"type": "object", "properties": {}},
            )

            def __init__(self) -> None:
                self.started = False
                self.cancelled = False

            async def execute(
                self,
                call_id: str,
                arguments: dict[str, Any],
                on_update: Any = None,
            ) -> ToolResult:
                self.started = True
                try:
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise
                return ToolResult("slow")

        class CancelTool:
            execution_mode = "parallel"
            spec = ToolSpec(
                name="cancel",
                description="",
                parameters={"type": "object", "properties": {}},
            )

            async def execute(
                self,
                call_id: str,
                arguments: dict[str, Any],
                on_update: Any = None,
            ) -> ToolResult:
                raise asyncio.CancelledError()

        slow = SlowCancelProbe()
        provider = make_provider(
            tool_script([("c1", "slow", {}), ("c2", "cancel", {})]),
            text_script("done"),
        )
        events: list[Any] = []

        with self.assertRaises(asyncio.CancelledError):
            async for event in run_loop(
                provider,
                [UserMessage("hi")],
                LoopConfig(system="", tools=[slow, CancelTool()]),
            ):
                events.append(event)

        self.assertTrue(slow.started)
        self.assertTrue(slow.cancelled)
        self.assertIsInstance(events[-1], AgentError)
        self.assertEqual(events[-1].kind, "aborted")
        self.assertFalse(any(isinstance(e, TurnEnd) for e in events))


if __name__ == "__main__":
    unittest.main()
