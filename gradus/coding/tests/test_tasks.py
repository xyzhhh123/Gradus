"""`tasks` 的纯逻辑测试：建图、依赖判定、认领解锁与落盘。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gradus.coding.tasks import (
    STATUS_COMPLETED,
    STATUS_PENDING,
    STATUS_RUNNING,
    TaskError,
    TaskGraph,
)
from gradus.coding.tools.tasks import (
    ClaimTaskTool,
    CompleteTaskTool,
    CreateTaskTool,
    GetTaskTool,
    ListTasksTool,
    UpdateTaskTool,
)


class TaskGraphBuildTest(unittest.TestCase):
    def setUp(self) -> None:
        self.graph = TaskGraph()

    def test_add_returns_a_pending_task(self):
        task = self.graph.add("write docs", "把 README 补完", role="writer", model="m1")

        self.assertTrue(task.id.startswith("task_"))
        self.assertEqual(task.status, STATUS_PENDING)
        self.assertEqual(task.role, "writer")
        self.assertEqual(task.model, "m1")
        self.assertIsNone(task.owner)

    def test_add_rejects_an_empty_subject(self):
        with self.assertRaises(TaskError):
            self.graph.add("   ")

    def test_get_rejects_an_unknown_id(self):
        with self.assertRaises(TaskError):
            self.graph.get("task_nope")

    def test_update_dependencies_adds_edges(self):
        schema = self.graph.add("schema")
        endpoints = self.graph.add("endpoints")

        updated = self.graph.update_dependencies(endpoints.id, add=(schema.id,))

        self.assertEqual(updated.blocked_by, (schema.id,))
        # 重复添加同一条边不出错，也不产生重复
        again = self.graph.update_dependencies(endpoints.id, add=(schema.id,))
        self.assertEqual(again.blocked_by, (schema.id,))

    def test_update_dependencies_rejects_a_missing_dependency(self):
        task = self.graph.add("endpoints")

        with self.assertRaises(TaskError):
            self.graph.update_dependencies(task.id, add=("task_missing",))

    def test_update_dependencies_rejects_self_dependency(self):
        task = self.graph.add("schema")

        with self.assertRaises(TaskError):
            self.graph.update_dependencies(task.id, add=(task.id,))

    def test_update_dependencies_rejects_a_cycle(self):
        first = self.graph.add("first")
        second = self.graph.add("second")
        third = self.graph.add("third")
        self.graph.update_dependencies(second.id, add=(first.id,))
        self.graph.update_dependencies(third.id, add=(second.id,))

        # 让 first 依赖 third 会形成 first → third → second → first
        with self.assertRaises(TaskError):
            self.graph.update_dependencies(first.id, add=(third.id,))

    def test_update_dependencies_can_remove_edges(self):
        schema = self.graph.add("schema")
        endpoints = self.graph.add("endpoints")
        self.graph.update_dependencies(endpoints.id, add=(schema.id,))

        updated = self.graph.update_dependencies(endpoints.id, remove=(schema.id,))

        self.assertEqual(updated.blocked_by, ())

    def test_dependencies_cannot_change_after_claim(self):
        schema = self.graph.add("schema")
        endpoints = self.graph.add("endpoints")
        self.graph.claim(schema.id, "sub-1")

        with self.assertRaises(TaskError):
            self.graph.update_dependencies(schema.id, add=(endpoints.id,))


class TaskGraphReadyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.graph = TaskGraph()

    def test_chain_exposes_one_node_at_a_time(self):
        first = self.graph.add("first")
        second = self.graph.add("second")
        self.graph.update_dependencies(second.id, add=(first.id,))

        self.assertEqual([t.id for t in self.graph.ready()], [first.id])
        self.assertFalse(self.graph.can_start(second.id))

        self.graph.claim(first.id, "a")
        self.graph.complete(first.id, "a")

        self.assertEqual([t.id for t in self.graph.ready()], [second.id])

    def test_diamond_exposes_both_middle_nodes(self):
        root = self.graph.add("root")
        left = self.graph.add("left")
        right = self.graph.add("right")
        last = self.graph.add("last")
        self.graph.update_dependencies(left.id, add=(root.id,))
        self.graph.update_dependencies(right.id, add=(root.id,))
        self.graph.update_dependencies(last.id, add=(left.id, right.id))

        self.graph.claim(root.id, "a")
        _, unblocked = self.graph.complete(root.id, "a")

        self.assertEqual({t.id for t in unblocked}, {left.id, right.id})
        self.assertEqual({t.id for t in self.graph.ready()}, {left.id, right.id})
        # 汇聚点要等两个前驱都完成
        self.assertFalse(self.graph.can_start(last.id))

    def test_blockers_reports_missing_dependencies(self):
        task = self.graph.add("left")
        other = self.graph.add("right")
        self.graph.update_dependencies(task.id, add=(other.id,))

        self.assertEqual(self.graph.blockers(task.id), (other.id,))

    def test_finished_is_false_until_every_node_completes(self):
        task = self.graph.add("only")

        self.assertFalse(self.graph.finished)
        self.graph.claim(task.id, "a")
        self.graph.complete(task.id, "a")
        self.assertTrue(self.graph.finished)


class TaskGraphClaimTest(unittest.TestCase):
    def setUp(self) -> None:
        self.graph = TaskGraph()

    def test_claim_moves_to_in_progress(self):
        task = self.graph.add("schema")

        claimed = self.graph.claim(task.id, "sub-1")

        self.assertEqual(claimed.status, STATUS_RUNNING)
        self.assertEqual(claimed.owner, "sub-1")

    def test_claim_rejects_a_blocked_task(self):
        first = self.graph.add("first")
        second = self.graph.add("second")
        self.graph.update_dependencies(second.id, add=(first.id,))

        with self.assertRaises(TaskError):
            self.graph.claim(second.id, "sub-1")

    def test_claim_rejects_an_already_claimed_task(self):
        task = self.graph.add("schema")
        self.graph.claim(task.id, "sub-1")

        with self.assertRaises(TaskError):
            self.graph.claim(task.id, "sub-2")

    def test_claim_rejects_an_empty_owner(self):
        task = self.graph.add("schema")

        with self.assertRaises(TaskError):
            self.graph.claim(task.id, "  ")

    def test_release_returns_the_task_to_ready(self):
        task = self.graph.add("schema")
        self.graph.claim(task.id, "sub-1")

        self.assertTrue(self.graph.release(task.id))
        self.assertEqual(task.status, STATUS_PENDING)
        self.assertIsNone(task.owner)
        self.assertEqual([t.id for t in self.graph.ready()], [task.id])

    def test_release_ignores_a_pending_task(self):
        task = self.graph.add("schema")

        self.assertFalse(self.graph.release(task.id))


class TaskGraphCompleteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.graph = TaskGraph()

    def test_complete_stores_the_result(self):
        task = self.graph.add("schema")
        self.graph.claim(task.id, "a")

        done, _ = self.graph.complete(task.id, "a", "建好了三张表")

        self.assertEqual(done.status, STATUS_COMPLETED)
        self.assertEqual(done.result, "建好了三张表")

    def test_complete_rejects_a_foreign_owner(self):
        task = self.graph.add("schema")
        self.graph.claim(task.id, "a")

        with self.assertRaises(TaskError):
            self.graph.complete(task.id, "b")

    def test_complete_rejects_a_pending_task(self):
        task = self.graph.add("schema")

        with self.assertRaises(TaskError):
            self.graph.complete(task.id, "a")

    def test_complete_only_reports_newly_unblocked(self):
        root = self.graph.add("root")
        left = self.graph.add("left")
        right = self.graph.add("right")
        last = self.graph.add("last")
        self.graph.update_dependencies(left.id, add=(root.id,))
        self.graph.update_dependencies(right.id, add=(root.id,))
        self.graph.update_dependencies(last.id, add=(left.id, right.id))

        self.graph.claim(root.id, "a")
        self.graph.complete(root.id, "a")
        self.graph.claim(left.id, "a")
        self.graph.claim(right.id, "a")

        _, unlocked = self.graph.complete(left.id, "a")
        self.assertEqual(unlocked, ())
        _, unlocked = self.graph.complete(right.id, "a")
        self.assertEqual([t.id for t in unlocked], [last.id])


class TaskGraphRemoveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.graph = TaskGraph()

    def test_remove_cleans_dangling_dependencies(self):
        schema = self.graph.add("schema")
        endpoints = self.graph.add("endpoints")
        self.graph.update_dependencies(endpoints.id, add=(schema.id,))

        self.assertTrue(self.graph.remove(schema.id))

        self.assertEqual(self.graph.get(endpoints.id).blocked_by, ())
        self.assertTrue(self.graph.can_start(endpoints.id))

    def test_remove_rejects_a_running_task(self):
        task = self.graph.add("schema")
        self.graph.claim(task.id, "a")

        with self.assertRaises(TaskError):
            self.graph.remove(task.id)

    def test_remove_returns_false_for_an_unknown_id(self):
        self.assertFalse(self.graph.remove("task_nope"))


class TaskGraphPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "tasks.json"

    def test_roundtrip_keeps_edges_and_results(self):
        graph = TaskGraph(self.path)
        schema = graph.add("schema")
        endpoints = graph.add("endpoints", "写接口", role="coder")
        graph.update_dependencies(endpoints.id, add=(schema.id,))
        graph.claim(schema.id, "sub-1")
        graph.complete(schema.id, "sub-1", "建表完成")

        reloaded = TaskGraph(self.path)

        self.assertEqual(len(reloaded.tasks()), 2)
        self.assertEqual(reloaded.get(schema.id).result, "建表完成")
        self.assertEqual(reloaded.get(endpoints.id).blocked_by, (schema.id,))
        self.assertEqual(
            [t.id for t in reloaded.ready()], [endpoints.id]
        )

    def test_interrupted_tasks_are_released_on_load(self):
        graph = TaskGraph(self.path)
        task = graph.add("schema")
        graph.claim(task.id, "sub-1")

        reloaded = TaskGraph(self.path)

        recovered = reloaded.get(task.id)
        self.assertEqual(recovered.status, STATUS_PENDING)
        self.assertIsNone(recovered.owner)
        self.assertTrue(any("interrupted" in item for item in reloaded.warnings))

    def test_corrupt_file_only_warns(self):
        self.path.write_text("{not json", encoding="utf-8")

        graph = TaskGraph(self.path)

        self.assertEqual(graph.tasks(), ())
        self.assertTrue(graph.warnings)

    def test_invalid_entries_are_skipped(self):
        self.path.write_text(
            json.dumps(
                [
                    {"id": "task_ok01", "subject": "keep me"},
                    {"id": "bad", "subject": "no prefix"},
                    {"id": "task_bad2", "subject": ""},
                    {"id": "task_bad3", "subject": "x", "status": "weird"},
                ]
            ),
            encoding="utf-8",
        )

        graph = TaskGraph(self.path)

        self.assertEqual([t.id for t in graph.tasks()], ["task_ok01"])
        self.assertEqual(len(graph.warnings), 3)

    def test_missing_dependency_is_reported(self):
        self.path.write_text(
            json.dumps(
                [{"id": "task_a001", "subject": "a", "blocked_by": ["task_gone"]}]
            ),
            encoding="utf-8",
        )

        graph = TaskGraph(self.path)

        self.assertEqual(graph.ready(), ())
        self.assertTrue(any("missing task" in item for item in graph.warnings))

    def test_write_failure_rolls_back(self):
        directory = Path(self._tmp.name) / "blocked"
        directory.mkdir()
        graph = TaskGraph()

        # 把落盘路径指到一个目录上，写入必然失败
        graph._path = directory  # type: ignore[assignment]
        with self.assertRaises(TaskError):
            graph.add("schema")

        self.assertEqual(graph.tasks(), ())


class TaskToolsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.graph = TaskGraph()

    async def test_create_task_returns_the_new_id(self):
        tool = CreateTaskTool(self.graph)

        result = await tool.execute("c1", {"subject": "write docs"})

        self.assertFalse(result.is_error)
        self.assertIn("write docs", result.content)
        self.assertEqual(len(self.graph.tasks()), 1)

    async def test_create_task_rejects_an_empty_subject(self):
        tool = CreateTaskTool(self.graph)

        result = await tool.execute("c1", {"subject": ""})

        self.assertTrue(result.is_error)
        self.assertEqual(self.graph.tasks(), ())

    async def test_update_task_connects_nodes(self):
        first = self.graph.add("first")
        second = self.graph.add("second")
        tool = UpdateTaskTool(self.graph)

        result = await tool.execute(
            "c1", {"task_id": second.id, "add_blocked_by": [first.id]}
        )

        self.assertFalse(result.is_error)
        self.assertIn(first.id, result.content)
        self.assertEqual(self.graph.get(second.id).blocked_by, (first.id,))

    async def test_update_task_reports_a_cycle(self):
        first = self.graph.add("first")
        second = self.graph.add("second")
        self.graph.update_dependencies(second.id, add=(first.id,))
        tool = UpdateTaskTool(self.graph)

        result = await tool.execute(
            "c1", {"task_id": first.id, "add_blocked_by": [second.id]}
        )

        self.assertTrue(result.is_error)

    async def test_list_tasks_shows_ready_and_blocked(self):
        first = self.graph.add("first")
        second = self.graph.add("second")
        self.graph.update_dependencies(second.id, add=(first.id,))
        tool = ListTasksTool(self.graph)

        result = await tool.execute("c1", {})

        self.assertIn("ready", result.content)
        self.assertIn(f"blocked by {first.id}", result.content)

    async def test_list_tasks_handles_an_empty_graph(self):
        tool = ListTasksTool(self.graph)

        result = await tool.execute("c1", {})

        self.assertEqual(result.content, "no tasks")

    async def test_get_task_returns_json(self):
        task = self.graph.add("schema", "建三张表")
        tool = GetTaskTool(self.graph)

        result = await tool.execute("c1", {"task_id": task.id})

        payload = json.loads(result.content)
        self.assertEqual(payload["id"], task.id)
        self.assertEqual(payload["description"], "建三张表")
        self.assertEqual(payload["unfinished"], [])

    async def test_get_task_rejects_an_unknown_id(self):
        tool = GetTaskTool(self.graph)

        result = await tool.execute("c1", {"task_id": "task_nope"})

        self.assertTrue(result.is_error)

    async def test_claim_and_complete_use_lead_by_default(self):
        first = self.graph.add("first")
        second = self.graph.add("second")
        self.graph.update_dependencies(second.id, add=(first.id,))
        claim = ClaimTaskTool(self.graph)
        complete = CompleteTaskTool(self.graph)

        claimed = await claim.execute("c1", {"task_id": first.id})
        done = await complete.execute(
            "c1", {"task_id": first.id, "result": "建表完成"}
        )

        self.assertIn("lead", claimed.content)
        self.assertIn("unblocked", done.content)
        self.assertEqual(self.graph.get(second.id).blocked_by, (first.id,))
        self.assertTrue(self.graph.can_start(second.id))

    async def test_complete_reports_the_finished_graph(self):
        task = self.graph.add("only")
        self.graph.claim(task.id, "lead")
        tool = CompleteTaskTool(self.graph)

        result = await tool.execute("c1", {"task_id": task.id})

        self.assertIn("finished", result.content)

    async def test_claim_rejects_a_blocked_task(self):
        first = self.graph.add("first")
        second = self.graph.add("second")
        self.graph.update_dependencies(second.id, add=(first.id,))
        tool = ClaimTaskTool(self.graph)

        result = await tool.execute("c1", {"task_id": second.id})

        self.assertTrue(result.is_error)


class TaskToolsWiringTest(unittest.TestCase):
    def test_build_tools_registers_task_tools_only_when_a_graph_is_given(self):
        from gradus.coding import session
        from gradus.coding.permissions import Policy
        from gradus.coding.tools.files import FileTracker

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = Policy(workspace=root)
            without, _, _, _ = session._build_tools(
                root, FileTracker(), None, policy, None
            )
            with_graph, search, _, _ = session._build_tools(
                root, FileTracker(), None, policy, None, tasks=TaskGraph()
            )

        direct = [tool.spec.name for tool in with_graph]
        self.assertNotIn("create_task", [tool.spec.name for tool in without])
        # 建任务要常驻（「这件事得拆开做」的第一个动作），其余任务工具按需加载
        self.assertIn("create_task", direct)
        self.assertNotIn("complete_task", direct)
        assert search is not None
        deferred = sorted(tool.spec.name for tool in search._pool)
        self.assertEqual(
            deferred,
            [
                "claim_task",
                "complete_task",
                "get_task",
                "list_tasks",
                "plan_tasks",
                "update_task",
            ],
        )
        # 有按需工具时 tool_search 自己必须常驻，否则模型没有入口去加载它们
        self.assertIn("tool_search", direct)

    def test_task_tools_are_allowed_by_the_default_policy(self):
        from gradus.coding.permissions import Policy

        policy = Policy(workspace=Path("."))
        for name in (
            "create_task",
            "update_task",
            "list_tasks",
            "get_task",
            "claim_task",
            "complete_task",
        ):
            with self.subTest(name=name):
                self.assertEqual(policy.decide(name, {}).action, "allow")


if __name__ == "__main__":
    unittest.main()
