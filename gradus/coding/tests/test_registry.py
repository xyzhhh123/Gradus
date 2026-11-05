"""工具注册表与自定义工具适配的离线测试。"""

from __future__ import annotations

import unittest

from gradus.ai.base import ToolSpec
from gradus.coding.registry import (
    FunctionTool,
    ToolCatalog,
    ToolError,
    ToolRegistry,
    merge_tools,
)


def _tool(name: str) -> FunctionTool:
    return FunctionTool(
        spec=ToolSpec(name=name, description="d", parameters={"type": "object"}),
        handler=lambda arguments: "ok",
    )


class ToolRegistryTest(unittest.TestCase):
    def test_keeps_registration_order(self):
        registry = ToolRegistry([_tool("a"), _tool("b")])

        self.assertEqual([tool.spec.name for tool in registry.tools], ["a", "b"])
        self.assertEqual(len(registry), 2)

    def test_duplicate_name_is_rejected(self):
        registry = ToolRegistry([_tool("a")])

        with self.assertRaises(ToolError):
            registry.add(_tool("a"))

    def test_empty_name_is_rejected(self):
        with self.assertRaises(ToolError):
            ToolRegistry([_tool("")])

    def test_merge_across_groups(self):
        merged = merge_tools((_tool("a"),), (_tool("b"),))

        self.assertEqual([tool.spec.name for tool in merged], ["a", "b"])

    def test_merge_rejects_duplicates(self):
        with self.assertRaises(ToolError):
            merge_tools((_tool("a"),), (_tool("a"),))


class ToolCatalogTest(unittest.TestCase):
    def test_groups_by_exposure(self):
        catalog = ToolCatalog()
        catalog.add(_tool("a"))
        catalog.add(_tool("b"), "deferred")
        catalog.add(_tool("c"), "hidden")

        self.assertEqual([tool.spec.name for tool in catalog.direct], ["a"])
        self.assertEqual([tool.spec.name for tool in catalog.deferred], ["b"])
        self.assertEqual([tool.spec.name for tool in catalog.hidden], ["c"])
        self.assertEqual(len(catalog), 3)

    def test_add_all_keeps_order(self):
        catalog = ToolCatalog()
        catalog.add_all((_tool("x"), _tool("y")), "deferred")

        self.assertEqual([tool.spec.name for tool in catalog.deferred], ["x", "y"])

    def test_duplicate_across_groups_is_rejected(self):
        catalog = ToolCatalog()
        catalog.add(_tool("a"))

        with self.assertRaises(ToolError):
            catalog.add(_tool("a"), "deferred")

    def test_empty_name_is_rejected(self):
        with self.assertRaises(ToolError):
            ToolCatalog().add(_tool(""), "direct")

    def test_unknown_exposure_is_rejected(self):
        with self.assertRaises(ToolError):
            ToolCatalog().add(_tool("a"), "sometimes")


class FunctionToolTest(unittest.IsolatedAsyncioTestCase):
    async def test_sync_handler(self):
        tool = FunctionTool(
            spec=ToolSpec(name="echo", description="d", parameters={"type": "object"}),
            handler=lambda arguments: arguments["text"],
        )

        result = await tool.execute("c1", {"text": "hi"})

        self.assertEqual(result.content, "hi")
        self.assertFalse(result.is_error)
        self.assertEqual(tool.execution_mode, "serial")

    async def test_async_handler(self):
        async def handler(arguments):
            return arguments["text"] * 2

        tool = FunctionTool(
            spec=ToolSpec(name="echo", description="d", parameters={"type": "object"}),
            handler=handler,
            execution_mode="parallel",
        )

        result = await tool.execute("c1", {"text": "ab"})

        self.assertEqual(result.content, "abab")

    async def test_handler_error_propagates(self):
        def handler(arguments):
            raise ValueError("boom")

        tool = FunctionTool(
            spec=ToolSpec(name="bad", description="d", parameters={"type": "object"}),
            handler=handler,
        )

        with self.assertRaises(ValueError):
            await tool.execute("c1", {})
