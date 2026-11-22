"""`BackgroundTasks` 的启动、通知、上限与生命周期测试。"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

from gradus.ai.base import ModelSpec
from gradus.coding import session
from gradus.coding.background import BackgroundLimit, BackgroundTasks
from gradus.coding.config import Settings
from gradus.coding.permissions import Policy
from gradus.coding.sandbox import LocalEnvironment
from gradus.coding.tools.files import FileTracker

SLOW = "import time; time.sleep(30)"
QUICK = "print('hello')"
FAILING = "import sys; sys.exit(3)"


async def wait_until(predicate, timeout: float = 5.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


class BackgroundTasksTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def make(self, **kwargs):
        notices: list[str] = []
        tasks = BackgroundTasks(
            LocalEnvironment(self.root),
            self.root / ".gradus" / "background",
            **kwargs,
        )
        tasks.bind(notices.append)
        return tasks, notices

    async def start(self, tasks: BackgroundTasks, code: str, name: str = "python"):
        return await tasks.start(
            tool=name,
            command=code,
            argv=[sys.executable, "-c", code],
            cwd=self.root,
        )

    async def test_start_returns_immediately_and_notifies_on_completion(self):
        tasks, notices = self.make()

        task = await self.start(tasks, QUICK)

        self.assertEqual(task.id, "bg_0001")
        self.assertEqual(task.status, "running")
        self.assertTrue(await wait_until(lambda: bool(notices)))
        self.assertEqual(task.status, "completed")
        self.assertEqual(task.exit_code, 0)
        self.assertEqual(task.log_path.read_text(encoding="utf-8").strip(), "hello")
        self.assertIn("<task_id>bg_0001</task_id>", notices[0])
        self.assertIn("<status>completed</status>", notices[0])
        self.assertIn("hello", notices[0])
        await tasks.aclose()

    async def test_nonzero_exit_is_reported_as_failed(self):
        tasks, notices = self.make()

        task = await self.start(tasks, FAILING)
        self.assertTrue(await wait_until(lambda: bool(notices)))

        self.assertEqual(task.status, "failed")
        self.assertEqual(task.exit_code, 3)
        self.assertIn("<exit_code>3</exit_code>", notices[0])
        await tasks.aclose()

    async def test_concurrency_limit_refuses_instead_of_queueing(self):
        tasks, _ = self.make(max_running=1)

        first = await self.start(tasks, SLOW)
        with self.assertRaises(BackgroundLimit):
            await self.start(tasks, SLOW)

        await tasks.aclose()
        self.assertEqual(first.status, "killed")

    async def test_timeout_marks_task_killed_and_still_notifies(self):
        tasks, notices = self.make(max_seconds=0.2)

        task = await self.start(tasks, SLOW)
        self.assertTrue(await wait_until(lambda: bool(notices), timeout=10))

        self.assertEqual(task.status, "killed")
        self.assertIn("<status>killed</status>", notices[0])
        await tasks.aclose()

    async def test_aclose_kills_running_tasks_without_notice(self):
        tasks, notices = self.make()

        task = await self.start(tasks, SLOW)
        await tasks.aclose()

        self.assertEqual(task.status, "killed")
        self.assertEqual(notices, [])

    async def test_large_output_is_streamed_and_log_stays_bounded(self):
        tasks, notices = self.make()
        code = (
            "import sys\n"
            "line = 'x' * 100 + '\\n'\n"
            "for _ in range(20000):\n"
            "    sys.stdout.write(line)\n"
        )

        task = await self.start(tasks, code)
        self.assertTrue(await wait_until(lambda: bool(notices), timeout=20))

        text = task.log_path.read_text(encoding="utf-8")
        self.assertIn("chars omitted", text)
        self.assertLess(len(text), 64 * 1024 + 256 * 1024 + 1024)
        self.assertTrue(text.startswith("x"))
        self.assertTrue(text.rstrip().endswith("x"))
        self.assertLess(len(notices[0]), 10_000)
        await tasks.aclose()

    async def test_snapshot_lists_tasks_and_describes_one(self):
        tasks, notices = self.make()

        task = await self.start(tasks, QUICK)
        self.assertTrue(await wait_until(lambda: bool(notices)))

        listing = tasks.snapshot()
        self.assertIn("bg_0001", listing)
        self.assertIn("completed", listing)

        detail = tasks.snapshot("bg_0001")
        self.assertIn("status: completed", detail)
        self.assertIn("hello", detail)

        self.assertIn("unknown background task", tasks.snapshot("bg_9999"))
        self.assertEqual(tasks.snapshot(None), tasks.snapshot())
        await tasks.aclose()


class _FakeAgent:
    """只实现 `Session._notify` 用到的两件事。"""

    def __init__(self, state: str = "idle") -> None:
        self.state = state
        self.notified: list[str] = []

    def notify(self, text: str) -> None:
        self.notified.append(text)


class SessionNoticeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def make(self, agent: _FakeAgent):
        settings = Settings(
            spec=ModelSpec(
                provider="anthropic",
                model="claude-test",
                base_url="https://api.anthropic.com",
                context_window=200_000,
                max_output_tokens=8_192,
            ),
            api_key="sk-test",
        )
        return session.Session(
            agent,
            settings,
            "system",
            self.root,
            Policy(workspace=self.root),
            FileTracker(),
            None,
            (),
            None,
            None,
            (),
            None,
        )

    async def test_notice_during_run_goes_straight_to_the_agent(self):
        agent = _FakeAgent("running")
        one = self.make(agent)

        one._notify("<task_notification/>")

        self.assertEqual(agent.notified, ["<task_notification/>"])

    async def test_notice_while_idle_is_buffered_until_the_next_run(self):
        agent = _FakeAgent("idle")
        one = self.make(agent)

        one._notify("one")
        one._notify("two")
        self.assertEqual(agent.notified, [])

        agent.state = "running"
        one._flush_notices()

        self.assertEqual(agent.notified, ["one\ntwo"])


if __name__ == "__main__":
    unittest.main()
