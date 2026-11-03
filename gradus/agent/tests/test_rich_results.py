"""富工具结果的离线测试。"""

from __future__ import annotations

import unittest
from typing import Any

from gradus.agent.base import LoopConfig
from gradus.agent.hooks import AfterToolCallResult, AgentHooks
from gradus.agent.loop import run_loop
from gradus.agent.tests.fakes import (
    FakeTool,
    make_provider,
    text_script,
    tool_script,
)
from gradus.agent.types import AgentDone, ToolExecutionEnd, TurnEnd
from gradus.ai.types import Usage, UserMessage


class RichResultsTest(unittest.IsolatedAsyncioTestCase):
    async def test_details_and_usage_reach_event_only(self):
        tool_usage = Usage(input_tokens=1000, output_tokens=500)
        tool = FakeTool(
            "edit",
            result="edited a.py",
            details={"path": "a.py", "added": 3, "removed": 1},
            structured_content={"status": "ok"},
            usage=tool_usage,
        )
        provider = make_provider(
            tool_script([("c1", "edit", {})], usage=(5, 1)),
            text_script("done", usage=(4, 2)),
        )

        events = [
            event
            async for event in run_loop(
                provider,
                [UserMessage("hi")],
                LoopConfig(system="", tools=[tool]),
            )
        ]

        end = next(e for e in events if isinstance(e, ToolExecutionEnd))
        self.assertEqual(
            end.result.details,
            {"path": "a.py", "added": 3, "removed": 1},
        )
        self.assertEqual(end.result.structured_content, {"status": "ok"})
        self.assertEqual(end.result.usage, tool_usage)

        turn_end = next(e for e in events if isinstance(e, TurnEnd))
        self.assertEqual(
            [result.content for result in turn_end.tool_results],
            ["edited a.py"],
        )

        # 工具自身的 usage 不进入整次运行的 token 汇总。
        done = events[-1]
        self.assertIsInstance(done, AgentDone)
        self.assertEqual(
            done.usage,
            Usage(input_tokens=9, output_tokens=3),
        )

    async def test_after_tool_call_keeps_rich_fields(self):
        async def after_tool_call(context: Any) -> AfterToolCallResult:
            return AfterToolCallResult(content="rewritten")

        tool = FakeTool(
            "edit",
            result="edited a.py",
            details={"path": "a.py"},
            structured_content={"status": "ok"},
            usage=Usage(input_tokens=10, output_tokens=2),
        )
        provider = make_provider(
            tool_script([("c1", "edit", {})]),
            text_script("done"),
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

        end = next(e for e in events if isinstance(e, ToolExecutionEnd))
        self.assertEqual(end.result.content, "rewritten")
        self.assertEqual(end.result.details, {"path": "a.py"})
        self.assertEqual(end.result.structured_content, {"status": "ok"})
        self.assertEqual(
            end.result.usage,
            Usage(input_tokens=10, output_tokens=2),
        )


if __name__ == "__main__":
    unittest.main()
