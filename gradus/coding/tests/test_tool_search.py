"""`tool_search` 与 deferred 加载的离线测试。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from gradus.agent.base import LoopConfig
from gradus.ai.base import ToolSpec
from gradus.coding import session
from gradus.coding.registry import FunctionTool
from gradus.coding.tools.tool_search import ToolSearchTool, tokenize


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

    async def test_chinese_query_reaches_english_metadata(self):
        """中文没有空格：靠 2-gram + 同义词表落到英文工具名上，不能整句当 token。"""
        pool = [
            _tool("complete_task", "Mark a task as completed and unblock dependents"),
            _tool("spawn_agent", "Spawn a sub-agent to run one node"),
        ]
        search = ToolSearchTool(pool)

        result = await search.execute("c1", {"query": "标记任务完成"})

        self.assertTrue(result.content.startswith("complete_task"), result.content)

    async def test_query_with_underscore_matches_snake_case_name(self):
        pool = [_tool("list_agents", "List running agents")]
        search = ToolSearchTool(pool)

        result = await search.execute("c1", {"query": "list agents"})

        self.assertTrue(result.content.startswith("list_agents"), result.content)

    async def test_parameter_names_are_searchable(self):
        """schema 里的字段名也算检索文本（Pi 的做法）：按 task_id 能搜到相关工具。"""
        pool = [
            FunctionTool(
                spec=ToolSpec(
                    name="claim_task",
                    description="Claim a task",
                    parameters={
                        "type": "object",
                        "properties": {"task_id": {"type": "string", "description": "任务编号"}},
                    },
                ),
                handler=lambda arguments: "ok",
            ),
            _tool("close_agent", "Close an agent"),
        ]
        search = ToolSearchTool(pool)

        result = await search.execute("c1", {"query": "task_id"})

        self.assertTrue(result.content.startswith("claim_task"), result.content)

    async def test_unrelated_query_finds_nothing(self):
        search = ToolSearchTool([_tool("complete_task", "Mark a task as completed")])

        result = await search.execute("c1", {"query": "天气怎么样"})

        self.assertIn("没有匹配到", result.content)
        self.assertEqual(search.take_pending(), [])

    async def test_tokenize_splits_snake_case_and_chinese(self):
        self.assertEqual(tokenize("complete_task"), ["complete", "task"])
        self.assertIn("task", tokenize("标记任务完成"))
        self.assertIn("complete", tokenize("标记任务完成"))
        self.assertIn("issue", tokenize("issues"))

    async def test_limit_caps_results(self):
        pool = [_tool(f"tool_{index}", "match me") for index in range(5)]
        search = ToolSearchTool(pool)

        result = await search.execute("c1", {"query": "match", "limit": 2})

        names = [line.split(":")[0] for line in result.content.splitlines() if ":" in line]
        self.assertEqual(len([name for name in names if name.startswith("tool_")]), 2)

    async def test_no_matches_lists_the_index_without_loading(self):
        """零命中不能只说「没找到」：给目录，让模型按名加载。"""
        search = ToolSearchTool([_tool("a", "alpha"), _tool("b", "beta")])

        result = await search.execute("c1", {"query": "zzz"})

        self.assertIn("可加载的工具（2 个）", result.content)
        self.assertIn("- a: alpha", result.content)
        self.assertIn('load=["名字"]', result.content)
        # 只是列目录，不能顺手把工具加载进来
        self.assertEqual(search.take_pending(), [])

    async def test_load_by_name_bypasses_search(self):
        """按名加载不看语言也不看分词：名字是稳定标识。"""
        pool = [_tool("complete_task", "Mark a task as completed"), _tool("spawn_agent", "Spawn")]
        search = ToolSearchTool(pool)

        result = await search.execute("c1", {"load": ["complete_task"]})

        self.assertTrue(result.content.startswith("complete_task"), result.content)
        self.assertEqual([tool.spec.name for tool in search.take_pending()], ["complete_task"])

    async def test_load_reports_unknown_names(self):
        search = ToolSearchTool([_tool("complete_task", "Mark a task as completed")])

        result = await search.execute("c1", {"load": ["nope"]})

        self.assertIn("没有这些按需工具", result.content)
        self.assertIn("nope", result.content)
        self.assertEqual(search.take_pending(), [])

    async def test_load_and_query_combine(self):
        pool = [_tool("complete_task", "Mark a task as completed"), _tool("spawn_agent", "Spawn one")]
        search = ToolSearchTool(pool)

        await search.execute("c1", {"load": ["spawn_agent"], "query": "complete task"})

        names = {tool.spec.name for tool in search.take_pending()}
        self.assertEqual(names, {"spawn_agent", "complete_task"})

    async def test_index_is_capped_for_big_pools(self):
        pool = [_tool(f"tool_{index}", "does something") for index in range(40)]
        search = ToolSearchTool(pool)

        result = await search.execute("c1", {"query": "zzz"})

        self.assertIn("可加载的工具（40 个）", result.content)
        self.assertIn("先列前 30 个", result.content)
        self.assertEqual(result.content.count("\n- "), 30)

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

    async def test_successful_outcomes_raise_future_ranking(self):
        pool = [
            _tool("tool_a", "match me"),
            _tool("tool_b", "match me"),
        ]
        search = ToolSearchTool(pool)
        search.note_outcome("tool_b", False)
        search.note_outcome("tool_b", False)
        search.note_outcome("tool_a", True)

        result = await search.execute("c1", {"query": "match"})

        self.assertTrue(result.content.startswith("tool_b"), result.content)
        self.assertGreater(search.feedback["tool_b"], search.feedback["tool_a"])

    async def test_search_counts_are_visible_in_session_feedback_state(self):
        search = ToolSearchTool([_tool("run", "run things")])

        await search.execute("c1", {"query": "run"})
        search.note_outcome("run", False)

        self.assertEqual(search.feedback["run"], 2.05)


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
