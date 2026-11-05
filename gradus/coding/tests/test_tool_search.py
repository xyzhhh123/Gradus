"""`tool_search` 与 deferred 加载的离线测试。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from gradus.agent.base import LoopConfig
from gradus.ai.base import ToolSpec
from gradus.coding import session
from gradus.coding.registry import FunctionTool
from gradus.coding.tools.tool_search import ToolSearchTool


def _tool(name: str, description: str = "") -> FunctionTool:
    return FunctionTool(
        spec=ToolSpec(name=name, description=description, parameters={"type": "object"}),
        handler=lambda arguments: "ok",
    )


class ToolSearchTest(unittest.IsolatedAsyncioTestCase):
    async def test_ranks_by_name_and_description(self):
        pool = [
            _tool("mcp__github__search_code", "Search code in repositories"),
            _tool("mcp__github__list_issues", "List issues"),
            _tool("mcp__docs__read", "Read documentation pages"),
        ]
        search = ToolSearchTool(pool)

        result = await search.execute("c1", {"query": "search code"})

        self.assertFalse(result.is_error)
        self.assertTrue(result.content.startswith("mcp__github__search_code"))
        self.assertNotIn("mcp__docs__read", result.content)

    async def test_limit_caps_results(self):
        pool = [_tool(f"tool_{index}", "match me") for index in range(5)]
        search = ToolSearchTool(pool)

        result = await search.execute("c1", {"query": "match", "limit": 2})

        names = [line.split(":")[0] for line in result.content.splitlines() if ":" in line]
        self.assertEqual(len([name for name in names if name.startswith("tool_")]), 2)

    async def test_no_matches(self):
        search = ToolSearchTool([_tool("a", "alpha")])

        result = await search.execute("c1", {"query": "zzz"})

        self.assertEqual(result.content, "(no matching tools)")

    async def test_empty_query(self):
        search = ToolSearchTool([_tool("a", "alpha")])

        result = await search.execute("c1", {})

        self.assertTrue(result.is_error)

    async def test_pending_tools_are_deduplicated_and_cleared(self):
        pool = [_tool("mcp__x__run", "run things")]
        search = ToolSearchTool(pool)

        await search.execute("c1", {"query": "run"})
        await search.execute("c2", {"query": "run"})

        self.assertEqual([tool.spec.name for tool in search.take_pending()], ["mcp__x__run"])
        self.assertEqual(search.take_pending(), [])


class PrepareNextTurnTest(unittest.IsolatedAsyncioTestCase):
    async def test_adds_loaded_tools_to_next_turn(self):
        direct = _tool("read_file", "read")
        loaded = _tool("mcp__x__run", "run things")
        search = ToolSearchTool([loaded])
        hook = session.make_prepare_next_turn(search)
        config = LoopConfig(system="s", tools=(direct,))

        await search.execute("c1", {"query": "run"})
        update = await hook(SimpleNamespace(config=config))

        assert update is not None
        self.assertEqual(
            [tool.spec.name for tool in update.config.tools], ["read_file", "mcp__x__run"]
        )

    async def test_returns_none_when_nothing_pending(self):
        hook = session.make_prepare_next_turn(ToolSearchTool([]))
        config = LoopConfig(system="s", tools=())

        self.assertIsNone(await hook(SimpleNamespace(config=config)))

    async def test_already_declared_tools_are_skipped(self):
        loaded = _tool("mcp__x__run", "run things")
        search = ToolSearchTool([loaded])
        hook = session.make_prepare_next_turn(search)
        config = LoopConfig(system="s", tools=(loaded,))

        await search.execute("c1", {"query": "run"})

        self.assertIsNone(await hook(SimpleNamespace(config=config)))
