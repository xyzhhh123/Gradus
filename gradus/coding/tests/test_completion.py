"""Completion gate tests."""

from __future__ import annotations

import unittest

from gradus.agent.hooks import FinishTurnContext
from gradus.agent.types import ToolResult
from gradus.ai.types import AssistantMessage
from gradus.coding.completion import COMPLETION_REVIEW, CompletionGate


def _finish(gate: CompletionGate):
    return gate.finish_turn(
        FinishTurnContext(
            assistant=AssistantMessage([], stop_reason="stop"),
            tool_results=(),
            new_messages=(),
            turn=1,
        )
    )


class CompletionGateTest(unittest.IsolatedAsyncioTestCase):
    async def test_change_requires_one_review_before_finishing(self):
        gate = CompletionGate()
        gate.observe("apply_patch", {}, ToolResult("ok"))

        self.assertEqual(await _finish(gate), "continue")
        update = await gate.prepare_next_turn(None)
        self.assertEqual(update.messages[0].content, COMPLETION_REVIEW)
        self.assertIsNone(await _finish(gate))

    async def test_successful_verification_removes_gate(self):
        gate = CompletionGate()
        gate.observe("edit", {"path": "a.py"}, ToolResult("ok"))
        gate.observe(
            "bash",
            {"command": "uv run pytest -q"},
            ToolResult("1 passed"),
        )

        self.assertIsNone(await _finish(gate))

    async def test_failed_verification_does_not_remove_gate(self):
        gate = CompletionGate()
        gate.observe("edit", {"path": "a.py"}, ToolResult("ok"))
        gate.observe(
            "bash",
            {"command": "uv run pytest -q"},
            ToolResult("failed", is_error=True),
        )

        self.assertEqual(await _finish(gate), "continue")

    async def test_new_change_after_verification_requires_another_review(self):
        gate = CompletionGate()
        gate.observe("edit", {"path": "a.py"}, ToolResult("ok"))
        gate.observe(
            "bash",
            {"command": "uv run pytest -q"},
            ToolResult("1 passed"),
        )
        gate.observe("edit", {"path": "a.py"}, ToolResult("ok"))

        self.assertEqual(await _finish(gate), "continue")


if __name__ == "__main__":
    unittest.main()
