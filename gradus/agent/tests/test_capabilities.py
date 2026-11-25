"""Tool capability contract tests."""

from __future__ import annotations

import unittest

from gradus.agent.base import tool_capability
from gradus.agent.types import ToolCapability
from gradus.ai.base import ToolSpec
from gradus.coding.registry import FunctionTool


class ToolCapabilityTest(unittest.TestCase):
    def test_read_only_capability(self):
        capability = ToolCapability(effects=("read", "search"))

        self.assertTrue(capability.read_only)
        self.assertFalse(capability.executes)
        self.assertFalse(capability.delegates)

    def test_write_is_not_read_only(self):
        capability = ToolCapability(
            effects=("write",),
            mutates_workspace=True,
            idempotent=False,
        )

        self.assertFalse(capability.read_only)

    def test_unknown_capability_is_optional(self):
        tool = FunctionTool(
            spec=ToolSpec(name="x", description="", parameters={"type": "object"}),
            handler=lambda arguments: "ok",
        )

        self.assertIsNone(tool_capability(tool))

    def test_function_tool_carries_capability(self):
        capability = ToolCapability(effects=("read",))
        tool = FunctionTool(
            spec=ToolSpec(name="x", description="", parameters={"type": "object"}),
            handler=lambda arguments: "ok",
            capability=capability,
        )

        self.assertIs(tool_capability(tool), capability)


if __name__ == "__main__":
    unittest.main()
