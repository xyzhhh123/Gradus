"""时钟的决策逻辑：只判断「这一秒该跑哪条」，不真的发请求。

`Connection` 需要 WebSocket 才能构造，测试只借它的两个方法，
所以用 `__new__` 跳过 `__init__`。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from gradus.coding.schedule import Schedule, ScheduledJob
from gradus.host.serve import Connection


class _FakeSession:
    """只提供 `_next_job` 用到的三件事。"""

    def __init__(self, pending: tuple[ScheduledJob, ...] = (), state: str = "idle") -> None:
        self.state = state
        self._pending = pending
        self.due_calls = 0

    def take_due(self):
        self.due_calls += 1
        return ()

    def pending_jobs(self):
        return self._pending


class _Running:
    """占位运行中的任务：只要 `done()` 为假就够。"""

    def done(self) -> bool:
        return False


def _connection(session, run_task=None) -> Connection:
    connection = Connection.__new__(Connection)
    connection.session = session
    connection.run_task = run_task
    return connection


def _job() -> ScheduledJob:
    schedule = Schedule()
    return schedule.add("0 9 * * *", "run tests")


class NextJobTest(unittest.TestCase):
    def test_fires_the_first_pending_job_when_idle(self):
        job = _job()
        connection = _connection(_FakeSession((job,)))

        self.assertIs(connection._next_job(), job)

    def test_marks_due_jobs_even_when_busy(self):
        session = _FakeSession()
        connection = _connection(session, run_task=_Running())

        self.assertIsNone(connection._next_job())
        self.assertEqual(session.due_calls, 1)

    def test_does_not_fire_while_a_turn_is_running(self):
        connection = _connection(_FakeSession((_job(),), state="running"))

        self.assertIsNone(connection._next_job())

    def test_does_not_fire_without_pending_jobs(self):
        connection = _connection(_FakeSession())

        self.assertIsNone(connection._next_job())

    def test_does_not_fire_without_a_session(self):
        connection = _connection(None)

        self.assertIsNone(connection._next_job())


class _FakeTeam:
    """只记录 `aclose()` 被调了几次。"""

    def __init__(self) -> None:
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1


class StopTeamTest(unittest.IsolatedAsyncioTestCase):
    async def test_stop_team_closes_the_team_and_clears_the_reference(self):
        team = _FakeTeam()
        session = SimpleNamespace(attached="stale")

        def attach(value) -> None:
            session.attached = value

        session.attach_team = attach
        connection = Connection.__new__(Connection)
        connection.session = session
        connection.team = team

        await connection.stop_team()

        self.assertEqual(team.closed, 1)
        self.assertIsNone(connection.team)
        self.assertIsNone(session.attached)

    async def test_stop_team_without_a_team_only_clears_the_reference(self):
        session = SimpleNamespace(attached="stale")

        def attach(value) -> None:
            session.attached = value

        session.attach_team = attach
        connection = Connection.__new__(Connection)
        connection.session = session
        connection.team = None

        await connection.stop_team()

        self.assertIsNone(connection.team)
        self.assertIsNone(session.attached)


if __name__ == "__main__":
    unittest.main()
