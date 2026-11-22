"""recall 的离线测试：按条目 id 读回、按关键词检索、越界与可见区拒绝。"""

from __future__ import annotations

import unittest

from gradus.agent.types import ToolResult
from gradus.ai.types import AssistantMessage, TextBlock, UserMessage
from gradus.coding import history as recall
from gradus.coding.tools.recall_history import RecallHistoryTool, RecallHooks

IDS = ("e1", "e2", "e3", "e4")


def _history():
    return [
        UserMessage("把 retryWithBackoff 的默认次数改成 5"),
        AssistantMessage(content=[TextBlock("好的，先看 auth.ts")]),
        UserMessage("另外注意别动 token 刷新逻辑"),
        AssistantMessage(content=[TextBlock("已改完，测试通过")]),
    ]


class ReadIdsTest(unittest.TestCase):
    def test_reads_folded_message(self):
        text = recall.read_ids(_history(), IDS, covered=3, wanted=["e1"])

        self.assertIn("#e1", text)
        self.assertIn("retryWithBackoff", text)

    def test_rejects_entry_still_visible(self):
        with self.assertRaises(recall.RecallError) as ctx:
            recall.read_ids(_history(), IDS, covered=2, wanted=["e4"])

        self.assertIn("还在上下文里", str(ctx.exception))

    def test_rejects_unknown_entry(self):
        with self.assertRaises(recall.RecallError) as ctx:
            recall.read_ids(_history(), IDS, covered=2, wanted=["e99"])

        self.assertIn("找不到条目", str(ctx.exception))

    def test_requires_at_least_one_id(self):
        with self.assertRaises(recall.RecallError):
            recall.read_ids(_history(), IDS, covered=2, wanted=[])


class SearchTest(unittest.TestCase):
    def test_returns_ids_and_skips_visible(self):
        text = recall.search(_history(), IDS, covered=3, query="retryWithBackoff")

        self.assertIn("#e1", text)
        self.assertNotIn("#e4", text)  # 可见区不参与检索

    def test_reports_no_hit_without_raising(self):
        text = recall.search(_history(), IDS, covered=3, query="不存在的词")

        self.assertIn("没有命中", text)

    def test_chinese_query_finds_english_text(self):
        """中文查询靠 2-gram + 同义词落到英文原文上（重试 → retry）。"""
        text = recall.search(_history(), IDS, covered=3, query="重试次数")

        self.assertIn("#e1", text)

    def test_respects_limit(self):
        history = [
            UserMessage("token 刷新逻辑在 auth.ts"),
            UserMessage("token 过期要重新登录"),
            UserMessage("token 长度校验"),
        ]
        text = recall.search(history, ("e1", "e2", "e3"), covered=3, query="token", limit=1)

        self.assertEqual(text.count("#"), 1)

    def test_snippet_anchors_on_literal_words_not_synonyms(self):
        """「重试」经同义词映射是 retry：片段定位必须用原文字面词，否则退回开头。"""
        filler = "这是一段很长的记录，" * 40
        history = [UserMessage(filler + "最后我们决定重试定为 5。" + filler)]

        text = recall.search(history, ("e1",), 1, "重试")

        self.assertIn("重试定为 5", text)
        # 片段要锚在命中处：从开头截的话「重试定为」会出现在 400 字符之后
        self.assertLess(text.index("重试定为"), 200)

    def test_tokenize_is_cached_between_searches(self):
        """区段很长时不该每次重新分词：同一个 (id, 长度) 只算一次。"""
        cache: dict[tuple[str, int], list[str]] = {}
        history = _history()

        recall.search(history, IDS, covered=3, query="retry", cache=cache)
        first = dict(cache)
        recall.search(history, IDS, covered=3, query="token", cache=cache)

        self.assertEqual(len(cache), 3)  # 覆盖 3 条
        self.assertEqual(first, cache)  # 第二次没有新增条目

    def test_requires_query(self):
        with self.assertRaises(recall.RecallError):
            recall.search(_history(), IDS, covered=2, query="   ")


class SelectorFallbackTest(unittest.IsolatedAsyncioTestCase):
    """关键词零命中时，用一次独立的小窗口让模型挑；正常命中不调它。"""

    async def test_zero_hit_falls_back_to_selector(self):
        history = [
            UserMessage("把 retryWithBackoff 的默认次数从 3 改成 5"),
            UserMessage("缓存层用进程内 LRU"),
        ]
        ids = ("e1", "e2")
        calls: list[tuple[str, int]] = []

        async def pick(query, cands):
            calls.append((query, len(cands)))
            return ["e2"]

        text, used = await recall.search_with_selector(
            history, ids, 2, "收尾掉这个活", select=pick
        )

        self.assertTrue(used)
        self.assertEqual(len(calls), 1)
        self.assertIn("#e2", text)
        self.assertIn("进程内 LRU", text)

    async def test_hit_does_not_call_selector(self):
        history = [UserMessage("缓存层用进程内 LRU")]
        calls: list[str] = []

        async def pick(query, cands):
            calls.append(query)
            return []

        text, used = await recall.search_with_selector(
            history, ("e1",), 1, "缓存", select=pick
        )

        self.assertFalse(used)
        self.assertEqual(calls, [])
        self.assertIn("#e1", text)

    async def test_selector_finding_nothing_reports_miss(self):
        history = [UserMessage("缓存层用进程内 LRU")]

        async def pick(query, cands):
            return []

        text, used = await recall.search_with_selector(
            history, ("e1",), 1, "天气", select=pick
        )

        self.assertFalse(used)
        self.assertIn("没有命中", text)


class ToolTest(unittest.IsolatedAsyncioTestCase):
    async def test_tool_requires_ids_or_query(self):
        tool = RecallHistoryTool(RecallHooks())

        result = await tool.execute("c1", {})

        self.assertTrue(result.is_error)

    async def test_tool_rejects_ids_and_query_together(self):
        tool = RecallHistoryTool(RecallHooks())

        result = await tool.execute("c1", {"ids": ["e1"], "query": "abc"})

        self.assertTrue(result.is_error)

    async def test_tool_forwards_to_hook(self):
        seen = []

        async def runner(ids, query, limit):
            seen.append((ids, query, limit))
            return ToolResult("ok")

        tool = RecallHistoryTool(RecallHooks(runner=runner))

        result = await tool.execute("c1", {"ids": ["e3", "e4"], "limit": 3})

        self.assertEqual(seen, [(["e3", "e4"], "", 3)])
        self.assertFalse(result.is_error)
