"""`bench/` 的离线测试：不联网、不跑模型，只测记账与汇总的数学与格式。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bench import common
from bench.common import Task, TaskResult
from bench.run import expired, pending


class TaskListTest(unittest.TestCase):
    def test_parses_minimal_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tasks.jsonl"
            path.write_text(
                json.dumps({"id": "t1", "instruction": "修 bug", "repo": "a/b"}) + "\n",
                encoding="utf-8",
            )

            tasks = common.load_tasks(path)

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].id, "t1")
        self.assertEqual(tasks[0].instruction, "修 bug")
        self.assertEqual(tasks[0].repo, "a/b")

    def test_rejects_missing_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tasks.jsonl"
            path.write_text(json.dumps({"id": "t1"}) + "\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                common.load_tasks(path)


class ResultStoreTest(unittest.TestCase):
    def test_last_write_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tasks.jsonl"
            common.append_result(path, TaskResult(id="t1", resolved=False, input=10))
            common.append_result(path, TaskResult(id="t1", resolved=True, input=20))

            results = common.read_results(path)

        self.assertEqual(len(results), 1)
        self.assertTrue(results["t1"].resolved)
        self.assertEqual(results["t1"].input, 20)

    def test_skips_broken_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tasks.jsonl"
            path.write_text('{"id": "t1", "resolved": true}\nnot json\n', encoding="utf-8")

            results = common.read_results(path)

        self.assertEqual(list(results), ["t1"])


class WilsonTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(common.wilson(0, 0), (0.0, 0.0))

    def test_bounds_are_sane(self):
        low, high = common.wilson(10, 10)

        self.assertLess(low, 1.0)
        self.assertAlmostEqual(high, 1.0)

    def test_twenty_task_interval_is_wide(self):
        """20 题里过 7 个，区间必须很宽——这正是“切片要攒数据”的理由。"""
        low, high = common.wilson(7, 20)

        self.assertAlmostEqual(low, 0.18, places=2)
        self.assertAlmostEqual(high, 0.57, places=2)


class SummarizeTest(unittest.TestCase):
    def _results(self):
        return [
            TaskResult(id="t1", resolved=True, input=100, output=10, turns=3, tool_calls=5),
            TaskResult(id="t2", resolved=False, input=200, output=20, turns=5, tool_calls=8,
                       failure_class="测试未过"),
            TaskResult(id="t3", status="timeout", input=50, failure_class="超时"),
        ]

    def test_counts_and_failures(self):
        summary = common.summarize(self._results(), total_tasks=5)

        self.assertEqual(summary["finished"], 3)
        self.assertEqual(summary["judged"], 2)
        self.assertEqual(summary["solved"], 1)
        self.assertEqual(summary["tokens_in"], 350)
        self.assertEqual(summary["failures"], {"测试未过": 1, "超时": 1})
        self.assertAlmostEqual(summary["cache_ratio"], 0.0)

    def test_paid_tokens_excludes_cache(self):
        summary = common.summarize(
            [TaskResult(id="t1", resolved=True, input=1000, output=10, cache_read=800)],
            total_tasks=1,
        )

        self.assertEqual(summary["paid_tokens"], 200)
        self.assertEqual(summary["paid_per_solved"], 200)

    def test_paid_line_in_report(self):
        summary = common.summarize(
            [TaskResult(id="t1", resolved=True, input=1000, output=10, cache_read=800)],
            total_tasks=1,
        )

        text = common.format_report(summary, {"commit": "c", "model": "m", "dataset": "d"})

        self.assertIn("paid", text)
        self.assertIn("0.2k", text)  # compact() 把 200 写成 0.2k

    def test_report_has_the_fixed_shape(self):
        summary = common.summarize(self._results(), total_tasks=5)
        plan = {"commit": "abc1234", "model": "m", "dataset": "swe-bench-verified"}

        text = common.format_report(summary, plan)

        for marker in ("resolved", "Wilson", "tokens", "per tool", "failures", "wall"):
            self.assertIn(marker.split()[0], text)
        self.assertIn("1/2", text)


class SlicePolicyTest(unittest.TestCase):
    def test_pending_skips_done(self):
        tasks = [Task(id="t1", instruction="a", workspace=""), Task(id="t2", instruction="b", workspace="")]

        self.assertEqual([task.id for task in pending(tasks, {"t1": object()})], ["t2"])

    def test_expired(self):
        self.assertTrue(expired(10.0, now=11.0))
        self.assertFalse(expired(10.0, now=9.0))


if __name__ == "__main__":
    unittest.main()

class PatchNormalizeTest(unittest.TestCase):
    def test_crlf_becomes_lf(self):
        from bench.agent import to_lf

        self.assertEqual(to_lf("a\r\nb\r\n"), "a\nb\n")

    def test_lf_is_untouched(self):
        from bench.agent import to_lf

        self.assertEqual(to_lf("a\nb\n"), "a\nb\n")


class RunManyTest(unittest.IsolatedAsyncioTestCase):
    """并发调度：同时进行的题数不超过 workers，结果不丢。"""

    async def test_parallelism_is_capped(self):
        import asyncio

        from bench.common import Task, TaskResult
        from bench.run import run_many

        active = 0
        peak = 0

        async def run_one(task):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.05)
            active -= 1
            return TaskResult(id=task.id)

        tasks = [Task(id=f"t{i}", instruction="x", workspace="") for i in range(6)]
        results = await run_many(
            tasks, run_one, workers=2, deadline=float("inf"), max_tasks=0
        )

        self.assertEqual(len(results), 6)
        self.assertLessEqual(peak, 2)

    async def test_max_tasks_limits_batch(self):
        from bench.common import Task, TaskResult
        from bench.run import run_many

        async def run_one(task):
            return TaskResult(id=task.id)

        tasks = [Task(id=f"t{i}", instruction="x", workspace="") for i in range(5)]
        results = await run_many(
            tasks, run_one, workers=3, deadline=float("inf"), max_tasks=2
        )

        self.assertEqual(len(results), 2)
