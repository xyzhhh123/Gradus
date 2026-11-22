"""`schedule` 的纯逻辑测试：表达式解析、触发判定、状态流转与落盘。"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from gradus.agent.types import AgentDone, AgentError, TurnEnd
from gradus.ai.base import ModelSpec
from gradus.ai.types import AssistantMessage, TextBlock, Usage
from gradus.coding import session
from gradus.coding.config import Settings
from gradus.coding.permissions import Policy
from gradus.coding.schedule import (
    CronExpression,
    Schedule,
    ScheduleError,
)
from gradus.coding.tools.files import FileTracker
from gradus.coding.tools.schedule import (
    create_manage_schedules_tool,
    create_schedule_task_tool,
)

MONDAY = datetime(2024, 1, 1, 9, 0)  # 2024-01-01 是周一


class CronParseTest(unittest.TestCase):
    def test_accepts_the_supported_syntax(self):
        for text in (
            "* * * * *",
            "*/5 * * * *",
            "0 9 * * *",
            "0 9 * * 1-5",
            "0 0 1,15 * *",
            "30 8-18 * * *",
        ):
            with self.subTest(text=text):
                CronExpression.parse(text)

    def test_rejects_bad_expressions(self):
        for text in (
            "* * * *",
            "* * * * * *",
            "60 * * * *",
            "* 24 * * *",
            "* * 0 * *",
            "*/0 * * * *",
            "*/x * * * *",
            "5-1 * * * *",
            "1-70 * * * *",
            "abc * * * *",
        ):
            with self.subTest(text=text):
                with self.assertRaises(ScheduleError):
                    CronExpression.parse(text)

    def test_matches_minute_and_hour(self):
        expression = CronExpression.parse("0 9 * * *")

        self.assertTrue(expression.matches(datetime(2024, 1, 1, 9, 0)))
        self.assertFalse(expression.matches(datetime(2024, 1, 1, 9, 1)))
        self.assertFalse(expression.matches(datetime(2024, 1, 1, 10, 0)))

    def test_step_field(self):
        expression = CronExpression.parse("*/15 * * * *")

        self.assertTrue(expression.matches(datetime(2024, 1, 1, 9, 30)))
        self.assertFalse(expression.matches(datetime(2024, 1, 1, 9, 31)))

    def test_weekday_uses_sunday_zero(self):
        monday = CronExpression.parse("0 9 * * 1")
        sunday = CronExpression.parse("0 9 * * 0")

        self.assertTrue(monday.matches(MONDAY))
        self.assertFalse(sunday.matches(MONDAY))

    def test_day_and_weekday_are_ored_when_both_restricted(self):
        expression = CronExpression.parse("0 9 1 * 1")

        self.assertTrue(expression.matches(MONDAY))          # 日=1 且 周一
        self.assertTrue(expression.matches(datetime(2024, 1, 8, 9, 0)))  # 周一
        self.assertFalse(expression.matches(datetime(2024, 1, 2, 9, 0)))


class ScheduleStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.schedule = Schedule()

    def test_add_validates_and_returns_job(self):
        job = self.schedule.add("0 9 * * *", "  run tests  ")

        self.assertTrue(job.id.startswith("job_"))
        self.assertEqual(job.prompt, "run tests")
        self.assertFalse(job.pending_delivery)
        self.assertEqual(len(self.schedule.jobs()), 1)

        with self.assertRaises(ScheduleError):
            self.schedule.add("bad", "x")
        with self.assertRaises(ScheduleError):
            self.schedule.add("0 9 * * *", "   ")

    def test_cancel(self):
        job = self.schedule.add("0 9 * * *", "run tests")

        self.assertTrue(self.schedule.cancel(job.id))
        self.assertFalse(self.schedule.cancel(job.id))
        self.assertEqual(self.schedule.jobs(), ())

    def test_due_marks_pending_once_per_minute(self):
        job = self.schedule.add("0 9 * * *", "run tests")

        fired = self.schedule.due(MONDAY)

        self.assertEqual([item.id for item in fired], [job.id])
        self.assertTrue(job.pending_delivery)
        self.assertEqual(job.last_fired, "2024-01-01 09:00")

        self.assertEqual(self.schedule.due(MONDAY), ())
        self.assertEqual([item.id for item in self.schedule.pending()], [job.id])

    def test_due_ignores_minutes_that_do_not_match(self):
        self.schedule.add("0 9 * * *", "run tests")

        self.assertEqual(self.schedule.due(datetime(2024, 1, 1, 9, 5)), ())

    def test_ack_keeps_recurring_and_removes_one_shot(self):
        recurring = self.schedule.add("0 9 * * *", "daily")
        one_shot = self.schedule.add("0 9 * * *", "once", recurring=False)
        self.schedule.due(MONDAY)

        self.schedule.ack(recurring.id)
        self.schedule.ack(one_shot.id)

        self.assertFalse(recurring.pending_delivery)
        self.assertEqual([item.id for item in self.schedule.jobs()], [recurring.id])

    def test_fail_retries_then_gives_up(self):
        schedule = Schedule(max_attempts=2)
        job = schedule.add("0 9 * * *", "daily")
        schedule.due(MONDAY)

        schedule.fail(job.id, "model down")
        self.assertTrue(job.pending_delivery)
        self.assertEqual(job.attempts, 1)

        schedule.fail(job.id, "model down")
        self.assertFalse(job.pending_delivery)
        self.assertEqual(job.attempts, 2)
        self.assertEqual(schedule.pending(), ())

    def test_fail_drops_one_shot_after_the_limit(self):
        schedule = Schedule(max_attempts=1)
        job = schedule.add("0 9 * * *", "once", recurring=False)
        schedule.due(MONDAY)

        schedule.fail(job.id, "boom")

        self.assertEqual(schedule.jobs(), ())


class SchedulePersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "schedules.json"

    def test_durable_jobs_survive_reload(self):
        first = Schedule(self.path)
        job = first.add("0 9 * * *", "run tests")
        first.due(MONDAY)

        second = Schedule(self.path)
        loaded = second.jobs()

        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].id, job.id)
        self.assertEqual(loaded[0].cron, "0 9 * * *")
        self.assertEqual(loaded[0].prompt, "run tests")
        self.assertTrue(loaded[0].pending_delivery)
        self.assertEqual(loaded[0].last_fired, "2024-01-01 09:00")

    def test_non_durable_jobs_are_not_written(self):
        first = Schedule(self.path)
        first.add("0 9 * * *", "memory only", durable=False)

        self.assertEqual(Schedule(self.path).jobs(), ())

    def test_cancel_is_persisted(self):
        first = Schedule(self.path)
        job = first.add("0 9 * * *", "run tests")
        first.cancel(job.id)

        self.assertEqual(Schedule(self.path).jobs(), ())

    def test_corrupted_file_is_reported_not_ignored(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not json", encoding="utf-8")

        schedule = Schedule(self.path)

        self.assertEqual(schedule.jobs(), ())
        self.assertTrue(schedule.warnings)
        self.assertIn("cannot load", schedule.warnings[0])

    def test_invalid_entries_are_skipped(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                [
                    {"id": "job_ok", "cron": "0 9 * * *", "prompt": "keep"},
                    {"id": "nope", "cron": "0 9 * * *", "prompt": "drop"},
                    {"id": "job_bad", "cron": "bad", "prompt": "drop"},
                ]
            ),
            encoding="utf-8",
        )

        schedule = Schedule(self.path)

        self.assertEqual([job.id for job in schedule.jobs()], ["job_ok"])
        self.assertEqual(len(schedule.warnings), 2)

    def test_payload_round_trip_shape(self):
        schedule = Schedule(self.path)
        schedule.add("0 9 * * *", "run tests")

        payload = json.loads(self.path.read_text(encoding="utf-8"))

        self.assertEqual(len(payload), 1)
        self.assertEqual(
            set(payload[0]),
            {
                "id",
                "cron",
                "prompt",
                "recurring",
                "durable",
                "pending_delivery",
                "last_fired",
                "attempts",
                "last_error",
            },
        )

    def test_job_from_payload_rejects_bad_shapes(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps([{"id": "job_x", "cron": "0 9 * * *", "prompt": ""}]),
            encoding="utf-8",
        )

        schedule = Schedule(self.path)

        self.assertEqual(schedule.jobs(), ())
        self.assertTrue(schedule.warnings)


class _ScriptedAgent:
    """只实现 `Session._run` 用到的那几件事。"""

    def __init__(self, events) -> None:
        self.state = "idle"
        self.messages: tuple = ()
        self.prompts: list[tuple[str, str]] = []
        self.notified: list[str] = []
        self._events = list(events)

    def prompt(self, text: str, *, origin: str = "user"):
        self.prompts.append((text, origin))

        async def stream():
            for event in self._events:
                yield event

        return stream()

    def continue_(self, prompt=None):
        return self.prompt("", origin="harness")

    def notify(self, text: str) -> None:
        self.notified.append(text)


def _settings() -> Settings:
    return Settings(
        spec=ModelSpec(
            provider="anthropic",
            model="claude-test",
            base_url="https://api.anthropic.com",
            context_window=200_000,
            max_output_tokens=8_192,
        ),
        api_key="sk-test",
    )


class SessionScheduledTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def make(self, agent, schedule):
        return session.Session(
            agent,
            _settings(),
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
            schedule=schedule,
        )

    async def test_take_due_delegates_and_marks(self):
        schedule = Schedule()
        job = schedule.add("0 9 * * *", "run tests")
        one = self.make(_ScriptedAgent([]), schedule)

        self.assertEqual([item.id for item in one.take_due(MONDAY)], [job.id])
        self.assertEqual(one.take_due(MONDAY), ())

    async def test_take_due_without_schedule_returns_empty(self):
        one = self.make(_ScriptedAgent([]), None)

        self.assertEqual(one.take_due(MONDAY), ())

    async def test_scheduled_run_is_tagged_and_acked_after_the_first_turn(self):
        schedule = Schedule()
        job = schedule.add("0 9 * * *", "run tests")
        schedule.due(MONDAY)
        agent = _ScriptedAgent(
            [
                TurnEnd(AssistantMessage([TextBlock("ok")]), [], Usage()),
                AgentDone([], Usage(), 1, "stop"),
            ]
        )
        one = self.make(agent, schedule)

        events = [event async for event in one.prompt_scheduled(job)]

        self.assertEqual(agent.prompts, [("[Scheduled] run tests", "harness")])
        self.assertFalse(job.pending_delivery)
        self.assertEqual(job.attempts, 0)
        self.assertIsInstance(events[-1], AgentDone)

    async def test_failed_scheduled_run_counts_an_attempt(self):
        schedule = Schedule(max_attempts=2)
        job = schedule.add("0 9 * * *", "run tests")
        schedule.due(MONDAY)
        agent = _ScriptedAgent([AgentError("config", "boom", None, [], Usage())])
        one = self.make(agent, schedule)

        events = [event async for event in one.prompt_scheduled(job)]

        self.assertIsInstance(events[-1], AgentError)
        self.assertEqual(job.attempts, 1)
        self.assertEqual(job.last_error, "boom")
        self.assertTrue(job.pending_delivery)

    async def test_one_shot_job_is_removed_after_a_successful_run(self):
        schedule = Schedule()
        job = schedule.add("0 9 * * *", "once", recurring=False)
        schedule.due(MONDAY)
        agent = _ScriptedAgent(
            [AgentDone([], Usage(), 1, "stop")]
        )
        one = self.make(agent, schedule)

        async for _ in one.prompt_scheduled(job):
            pass

        self.assertEqual(schedule.jobs(), ())

    async def test_cancelled_run_leaves_the_job_pending(self):
        schedule = Schedule(max_attempts=1)
        job = schedule.add("0 9 * * *", "run tests")
        schedule.due(MONDAY)
        agent = _ScriptedAgent([AgentError("aborted", "cancelled", None, [], Usage())])
        one = self.make(agent, schedule)

        async for _ in one.prompt_scheduled(job):
            pass

        # 取消既不算成功也不算失败：下一次 tick 还能重新点火。
        self.assertTrue(job.pending_delivery)
        self.assertEqual(job.attempts, 0)
        self.assertIsNone(job.last_error)

    async def test_pending_jobs_lists_marked_jobs(self):
        schedule = Schedule()
        job = schedule.add("0 9 * * *", "run tests")
        one = self.make(_ScriptedAgent([]), schedule)

        self.assertEqual(one.pending_jobs(), ())
        schedule.due(MONDAY)
        self.assertEqual([item.id for item in one.pending_jobs()], [job.id])


class ScheduleToolsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.schedule = Schedule()

    async def test_schedule_task_registers_a_job(self):
        tool = create_schedule_task_tool(self.schedule)

        result = await tool.execute("c1", {"cron": "0 9 * * 1-5", "prompt": "run tests"})

        self.assertFalse(result.is_error)
        self.assertEqual(len(self.schedule.jobs()), 1)
        self.assertIn("0 9 * * 1-5", result.content)

    async def test_schedule_task_rejects_a_bad_cron(self):
        tool = create_schedule_task_tool(self.schedule)

        result = await tool.execute("c1", {"cron": "0 9 * *", "prompt": "run tests"})

        self.assertTrue(result.is_error)
        self.assertEqual(self.schedule.jobs(), ())

    async def test_schedule_task_requires_a_prompt(self):
        tool = create_schedule_task_tool(self.schedule)

        result = await tool.execute("c1", {"cron": "0 9 * * *", "prompt": "  "})

        self.assertTrue(result.is_error)
        self.assertEqual(self.schedule.jobs(), ())

    async def test_schedule_task_can_create_a_one_shot_job(self):
        tool = create_schedule_task_tool(self.schedule)

        result = await tool.execute(
            "c1", {"cron": "0 9 * * *", "prompt": "once", "recurring": False}
        )

        self.assertIn("one-shot", result.content)
        self.assertFalse(self.schedule.jobs()[0].recurring)

    async def test_manage_schedules_lists_jobs(self):
        job = self.schedule.add("0 9 * * *", "run tests")
        tool = create_manage_schedules_tool(self.schedule)

        result = await tool.execute("c1", {"action": "list"})

        self.assertIn(job.id, result.content)
        self.assertIn("0 9 * * *", result.content)

    async def test_manage_schedules_lists_an_empty_table(self):
        tool = create_manage_schedules_tool(self.schedule)

        result = await tool.execute("c1", {})

        self.assertEqual(result.content, "no scheduled tasks")

    async def test_manage_schedules_cancels_a_job(self):
        job = self.schedule.add("0 9 * * *", "run tests")
        tool = create_manage_schedules_tool(self.schedule)

        result = await tool.execute("c1", {"action": "cancel", "job_id": job.id})

        self.assertFalse(result.is_error)
        self.assertEqual(self.schedule.jobs(), ())

    async def test_manage_schedules_rejects_an_unknown_job(self):
        tool = create_manage_schedules_tool(self.schedule)

        result = await tool.execute("c1", {"action": "cancel", "job_id": "job_nope"})

        self.assertTrue(result.is_error)


if __name__ == "__main__":
    unittest.main()
