"""`multiagent` 的离线测试：派活、等待、收尾、生命周期与消息总线。"""

from __future__ import annotations

import asyncio
import unittest

from gradus.agent.types import AgentDone, AgentError
from gradus.ai.types import AssistantMessage, TextBlock, Usage
from gradus.coding.bus import BusError
from gradus.coding.multiagent import AgentTeam, AgentTeamError
from gradus.coding.tasks import STATUS_COMPLETED, STATUS_PENDING, TaskError, TaskGraph
from gradus.coding.tools.multiagent import create_multiagent_tools


class _Recorder:
    def __init__(self) -> None:
        self.connects = 0
        self.closed = 0


class _FakeSession:
    """按脚本产事件的替身，只实现团队用到的那几件事。"""

    def __init__(
        self,
        recorder: _Recorder,
        task_id: str,
        *,
        delay: float = 0.02,
        fail: bool = False,
    ) -> None:
        self._recorder = recorder
        self._task_id = task_id
        self._delay = delay
        self._fail = fail
        self.messages: list = []
        self.prompts: list[str] = []
        self.notified: list[str] = []
        self.buffered: list[str] = []
        self.resumed = 0
        #: 是否有一轮正在跑；`notify` 据此回答「消息是插话还是进了收件箱」。
        self.running = False
        self.closed = False

    def prompt(self, text: str):
        self.prompts.append(text)
        self.running = True

        async def stream():
            try:
                await asyncio.sleep(self._delay)
                if self._fail:
                    yield AgentError("provider", "boom", None, [], Usage())
                    return
                self.messages.append(AssistantMessage([TextBlock(f"done {self._task_id}")]))
                yield AgentDone([], Usage(), 1, "stop")
            finally:
                self.running = False

        return stream()

    def continue_(self):
        """被消息叫醒的一轮：不追加用户消息，只把收件箱读掉。"""
        self.resumed += 1
        self.buffered.clear()
        self.running = True

        async def stream():
            try:
                await asyncio.sleep(self._delay)
                self.messages.append(AssistantMessage([TextBlock("read inbox")]))
                yield AgentDone([], Usage(), 1, "stop")
            finally:
                self.running = False

        return stream()

    def notify(self, text: str) -> bool:
        self.notified.append(text)
        if self.running:
            return False
        self.buffered.append(text)
        return True

    @property
    def has_notices(self) -> bool:
        return bool(self.buffered)

    async def aclose(self) -> None:
        self.closed = True
        self._recorder.closed += 1


def _factory(recorder: _Recorder, *, delay: float = 0.02, fail_ids: frozenset[str] = frozenset()):
    sessions: dict[str, _FakeSession] = {}

    async def connect(name: str, task):
        recorder.connects += 1
        session = _FakeSession(recorder, task.id, delay=delay, fail=task.id in fail_ids)
        sessions[name] = session
        return session

    return connect, sessions


class SpawnAndWaitTest(unittest.IsolatedAsyncioTestCase):
    async def test_spawn_claims_and_wait_collects_the_result(self):
        graph = TaskGraph()
        task = graph.add("only")
        recorder = _Recorder()
        connect, sessions = _factory(recorder)
        team = AgentTeam(graph, connect)

        agent = await team.spawn(task.id)
        self.assertEqual(agent.state, "running")
        self.assertEqual(graph.get(task.id).status, "in_progress")

        await team.wait([agent.name])

        self.assertEqual(graph.get(task.id).status, STATUS_COMPLETED)
        self.assertEqual(graph.get(task.id).result, f"done {task.id}")
        self.assertEqual(agent.state, "idle")
        # 默认保留会话，这样之后还能继续给它发消息
        self.assertFalse(sessions[agent.name].closed)
        self.assertIs(team.get(agent.name), agent)

    async def test_spawn_rejects_a_blocked_task(self):
        graph = TaskGraph()
        first = graph.add("first")
        second = graph.add("second")
        graph.update_dependencies(second.id, add=(first.id,))
        team = AgentTeam(graph, _factory(_Recorder())[0])

        with self.assertRaises(TaskError):
            await team.spawn(second.id)
        self.assertEqual(team.list(), ())

    async def test_spawn_rejects_an_unknown_task(self):
        team = AgentTeam(TaskGraph(), _factory(_Recorder())[0])

        with self.assertRaises(TaskError):
            await team.spawn("task_nope")

    async def test_spawn_releases_the_task_when_connecting_fails(self):
        graph = TaskGraph()
        task = graph.add("only")

        async def broken(name, wanted):
            raise RuntimeError("sandbox unavailable")

        team = AgentTeam(graph, broken)

        with self.assertRaises(RuntimeError):
            await team.spawn(task.id)
        self.assertEqual(graph.get(task.id).status, STATUS_PENDING)
        self.assertEqual(team.list(), ())

    async def test_wait_on_unknown_agent_raises(self):
        team = AgentTeam(TaskGraph(), _factory(_Recorder())[0])

        with self.assertRaises(AgentTeamError):
            await team.wait(["sub-9"])

    async def test_wait_timeout_returns_while_running(self):
        graph = TaskGraph()
        task = graph.add("slow")
        connect, _ = _factory(_Recorder(), delay=10.0)
        team = AgentTeam(graph, connect)
        agent = await team.spawn(task.id)

        waited = await team.wait([agent.name], timeout=0.05)

        self.assertEqual(waited[0].state, "running")

        await team.close(agent.name)
        self.assertEqual(graph.get(task.id).status, STATUS_PENDING)

    async def test_upstream_results_are_passed_downstream(self):
        graph = TaskGraph()
        first = graph.add("schema")
        second = graph.add("endpoints")
        graph.update_dependencies(second.id, add=(first.id,))
        recorder = _Recorder()
        connect, sessions = _factory(recorder)
        team = AgentTeam(graph, connect)

        first_agent = await team.spawn(first.id)
        await team.wait([first_agent.name])
        second_agent = await team.spawn(second.id)
        await team.wait([second_agent.name])

        prompt = sessions[second_agent.name].prompts[0]
        self.assertIn("Upstream results:", prompt)
        self.assertIn(f"done {first.id}", prompt)


class LifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_ephemeral_closes_the_session_and_leaves_the_roster(self):
        graph = TaskGraph()
        task = graph.add("only")
        recorder = _Recorder()
        connect, sessions = _factory(recorder)
        team = AgentTeam(graph, connect)

        agent = await team.spawn(task.id, ephemeral=True)
        await team.wait([agent.name])

        self.assertEqual(graph.get(task.id).status, STATUS_COMPLETED)
        self.assertTrue(sessions[agent.name].closed)
        self.assertEqual(team.list(), ())

    async def test_interrupt_returns_the_task_to_pending(self):
        graph = TaskGraph()
        task = graph.add("slow")
        connect, _ = _factory(_Recorder(), delay=10.0)
        team = AgentTeam(graph, connect)
        agent = await team.spawn(task.id)
        await asyncio.sleep(0)  # 让协程真正进入运行

        self.assertTrue(team.interrupt(agent.name))
        await asyncio.gather(agent.handle, return_exceptions=True)

        self.assertEqual(graph.get(task.id).status, STATUS_PENDING)
        self.assertIsNone(graph.get(task.id).owner)
        self.assertEqual(agent.state, "idle")
        # 中止不销毁：会话还在，可以再派活
        self.assertIs(team.get(agent.name), agent)
        await team.close(agent.name)

    async def test_close_releases_the_session(self):
        graph = TaskGraph()
        task = graph.add("only")
        recorder = _Recorder()
        connect, sessions = _factory(recorder)
        team = AgentTeam(graph, connect)
        agent = await team.spawn(task.id)
        await team.wait([agent.name])

        self.assertTrue(await team.close(agent.name))

        self.assertTrue(sessions[agent.name].closed)
        self.assertIsNone(team.get(agent.name))
        self.assertEqual(agent.state, "closed")

    async def test_close_returns_false_for_an_unknown_agent(self):
        team = AgentTeam(TaskGraph(), _factory(_Recorder())[0])

        self.assertFalse(await team.close("sub-404"))

    async def test_failure_marks_the_agent_and_releases_the_task(self):
        graph = TaskGraph()
        task = graph.add("flaky")
        connect, _ = _factory(_Recorder(), fail_ids=frozenset({task.id}))
        team = AgentTeam(graph, connect)

        agent = await team.spawn(task.id)
        await team.wait([agent.name])

        self.assertEqual(agent.state, "failed")
        self.assertIn("boom", agent.last_error or "")
        self.assertEqual(graph.get(task.id).status, STATUS_PENDING)

    async def test_aclose_cancels_everything(self):
        graph = TaskGraph()
        first = graph.add("a")
        second = graph.add("b")
        connect, _ = _factory(_Recorder(), delay=10.0)
        team = AgentTeam(graph, connect)
        await team.spawn(first.id)
        await team.spawn(second.id)

        await team.aclose()

        self.assertEqual(team.list(), ())
        self.assertEqual(graph.get(first.id).status, STATUS_PENDING)
        self.assertEqual(graph.get(second.id).status, STATUS_PENDING)

    async def test_spawn_after_aclose_raises(self):
        graph = TaskGraph()
        task = graph.add("only")
        team = AgentTeam(graph, _factory(_Recorder())[0])
        await team.aclose()

        with self.assertRaises(AgentTeamError):
            await team.spawn(task.id)


class BusTest(unittest.IsolatedAsyncioTestCase):
    async def test_message_reaches_a_running_subagent(self):
        graph = TaskGraph()
        task = graph.add("slow")
        connect, sessions = _factory(_Recorder(), delay=10.0)
        team = AgentTeam(graph, connect)
        agent = await team.spawn(task.id)

        team.send(agent.name, "先用 json 方案")

        self.assertEqual(
            sessions[agent.name].notified,
            ["<message from=lead>\n先用 json 方案\n</message>"],
        )
        # 正在跑：消息是插话，不该另起一轮。
        self.assertEqual(sessions[agent.name].resumed, 0)
        await team.close(agent.name)

    async def test_message_wakes_an_idle_subagent(self):
        graph = TaskGraph()
        task = graph.add("only")
        connect, sessions = _factory(_Recorder())
        team = AgentTeam(graph, connect)
        agent = await team.spawn(task.id)
        await team.wait([agent.name])
        self.assertEqual(agent.state, "idle")

        team.send(agent.name, "再补一段测试")
        await team.wait([agent.name])  # 等被叫醒的这一轮跑完

        self.assertEqual(sessions[agent.name].resumed, 1)
        self.assertEqual(agent.state, "idle")
        self.assertEqual(len(sessions[agent.name].notified), 1)
        await team.close(agent.name)

    async def test_messages_while_waking_up_share_one_turn(self):
        graph = TaskGraph()
        task = graph.add("only")
        connect, sessions = _factory(_Recorder())
        team = AgentTeam(graph, connect)
        agent = await team.spawn(task.id)
        await team.wait([agent.name])

        team.send(agent.name, "第一件事")
        team.send(agent.name, "第二件事")
        await team.wait([agent.name])

        # 第二条在叫醒的那一轮里到达，属于插话，不该再开一轮。
        self.assertEqual(sessions[agent.name].resumed, 1)
        self.assertEqual(len(sessions[agent.name].notified), 2)
        await team.close(agent.name)

    async def test_message_reaches_the_lead(self):
        leads: list[str] = []
        team = AgentTeam(TaskGraph(), _factory(_Recorder())[0], lead_notify=leads.append)

        team.send("lead", "建表完成了")

        self.assertEqual(leads, ["<message from=lead>\n建表完成了\n</message>"])

    async def test_message_to_an_unknown_agent_is_rejected(self):
        team = AgentTeam(TaskGraph(), _factory(_Recorder())[0])

        with self.assertRaises(BusError):
            team.send("sub-404", "hi")
        self.assertEqual(team.bus.history(), ())

    async def test_empty_message_is_rejected(self):
        leads: list[str] = []
        team = AgentTeam(TaskGraph(), _factory(_Recorder())[0], lead_notify=leads.append)

        with self.assertRaises(BusError):
            team.send("lead", "   ")

    async def test_history_keeps_recent_messages(self):
        team = AgentTeam(TaskGraph(), _factory(_Recorder())[0], lead_notify=lambda _: None)

        for index in range(3):
            team.send("lead", f"m{index}")

        messages = team.bus.history(limit=2)
        self.assertEqual([m.text for m in messages], ["m1", "m2"])
        self.assertEqual(messages[0].sender, "lead")


class ToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.graph = TaskGraph()
        self.leads: list[str] = []
        connect, self.sessions = _factory(_Recorder())
        self.team = AgentTeam(self.graph, connect, lead_notify=self.leads.append)
        self.holder: list[AgentTeam | None] = [self.team]

    def _tools(self):
        return create_multiagent_tools(lambda: self.holder[0])

    async def test_spawn_agent_reports_the_new_name(self):
        task = self.graph.add("write docs")

        result = await self._tools()[0].execute("c1", {"task_id": task.id})

        self.assertFalse(result.is_error)
        self.assertIn("sub-1", result.content)
        self.assertEqual(self.graph.get(task.id).status, "in_progress")

    async def test_spawn_agent_reports_a_blocked_task(self):
        first = self.graph.add("first")
        second = self.graph.add("second")
        self.graph.update_dependencies(second.id, add=(first.id,))

        result = await self._tools()[0].execute("c1", {"task_id": second.id})

        self.assertTrue(result.is_error)

    async def test_wait_reports_state_and_result(self):
        task = self.graph.add("only")
        await self._tools()[0].execute("c1", {"task_id": task.id})

        result = await self._tools()[1].execute(
            "c1", {"agents": ["sub-1"], "timeout_seconds": 5}
        )

        self.assertIn("idle", result.content)
        self.assertIn(f"done {task.id}", result.content)

    async def test_wait_with_zero_timeout_does_not_block(self):
        graph = TaskGraph()
        task = graph.add("slow")
        connect, _ = _factory(_Recorder(), delay=10.0)
        self.holder[0] = AgentTeam(graph, connect)
        await self._tools()[0].execute("c1", {"task_id": task.id})

        result = await self._tools()[1].execute(
            "c1", {"agents": ["sub-1"], "timeout_seconds": 0}
        )

        self.assertIn("running", result.content)
        assert self.holder[0] is not None
        await self.holder[0].aclose()

    async def test_list_agents_is_empty_initially(self):
        result = await self._tools()[2].execute("c1", {})

        self.assertEqual(result.content, "no subagents")

    async def test_send_message_reaches_the_lead(self):
        result = await self._tools()[3].execute("c1", {"to": "lead", "text": "hi"})

        self.assertFalse(result.is_error)
        self.assertEqual(len(self.leads), 1)

    async def test_send_message_rejects_an_unknown_recipient(self):
        result = await self._tools()[3].execute("c1", {"to": "sub-9", "text": "hi"})

        self.assertTrue(result.is_error)

    async def test_wait_reports_the_reply_from_a_wake_up_round(self):
        task = self.graph.add("only")
        await self._tools()[0].execute("c1", {"task_id": task.id})
        await self._tools()[1].execute("c1", {"agents": ["sub-1"], "timeout_seconds": 5})

        await self._tools()[3].execute("c1", {"to": "sub-1", "text": "补一句"})
        result = await self._tools()[1].execute(
            "c1", {"agents": ["sub-1"], "timeout_seconds": 5}
        )

        # 节点结果还在，被叫醒那一轮的产出另起一行
        self.assertIn(f"result: done {task.id}", result.content)
        self.assertIn("reply: read inbox", result.content)

    async def test_close_agent_removes_it(self):
        task = self.graph.add("only")
        await self._tools()[0].execute("c1", {"task_id": task.id})
        await self.team.wait(["sub-1"])

        result = await self._tools()[4].execute("c1", {"name": "sub-1"})

        self.assertFalse(result.is_error)
        self.assertIsNone(self.team.get("sub-1"))

    async def test_interrupt_reports_a_non_running_agent(self):
        task = self.graph.add("only")
        await self._tools()[0].execute("c1", {"task_id": task.id})
        await self.team.wait(["sub-1"])

        result = await self._tools()[5].execute("c1", {"name": "sub-1"})

        self.assertTrue(result.is_error)

    async def test_tools_report_a_missing_team(self):
        self.holder[0] = None

        result = await self._tools()[2].execute("c1", {})

        self.assertTrue(result.is_error)


if __name__ == "__main__":
    unittest.main()

class _FakeTeam:
    """只实现 WaitTool 用到的两个方法，用来检查传下去的超时。"""

    def __init__(self) -> None:
        self.timeouts: list[float | None] = []
        self.graph = TaskGraph()

    def get(self, name: str):
        return None

    async def wait(self, names, timeout=None):
        self.timeouts.append(timeout)
        return ()


class MultiagentRegressionTest(unittest.IsolatedAsyncioTestCase):
    async def test_wait_tool_defaults_to_600_seconds(self):
        """不传 timeout_seconds 时不能把 None 透给 asyncio.wait（那会无限等）。"""
        fake = _FakeTeam()
        tools = create_multiagent_tools(lambda: fake)
        wait_tool = next(tool for tool in tools if tool.spec.name == "wait")

        await wait_tool.execute("call", {"agents": ["sub-1"]}, None)

        self.assertEqual(fake.timeouts, [600.0])

    async def test_parallel_spawn_gets_unique_names(self):
        graph = TaskGraph()
        first = graph.add("first")
        second = graph.add("second")
        gate = asyncio.Event()
        entered = asyncio.Event()

        async def connect(name, task):
            entered.set()
            await gate.wait()
            return _FakeSession(_Recorder(), task.id)

        team = AgentTeam(graph, connect)
        spawns = [asyncio.create_task(team.spawn(first.id)), asyncio.create_task(team.spawn(second.id))]
        await entered.wait()
        await asyncio.sleep(0)
        gate.set()
        agents = await asyncio.gather(*spawns)

        self.assertEqual({agent.name for agent in agents}, {"sub-1", "sub-2"})
        self.assertEqual(len(team.list()), 2)

    async def test_aclose_during_spawn_closes_the_new_session(self):
        graph = TaskGraph()
        task = graph.add("only")
        gate = asyncio.Event()
        entered = asyncio.Event()
        recorder = _Recorder()
        sessions: dict[str, _FakeSession] = {}

        async def connect(name, wanted):
            entered.set()
            await gate.wait()
            session = _FakeSession(recorder, wanted.id)
            sessions[name] = session
            return session

        team = AgentTeam(graph, connect)
        spawner = asyncio.create_task(team.spawn(task.id))
        await entered.wait()
        await team.aclose()
        gate.set()

        with self.assertRaises(AgentTeamError):
            await spawner
        self.assertEqual(team.list(), ())
        self.assertEqual(graph.get(task.id).status, STATUS_PENDING)
        self.assertTrue(all(session.closed for session in sessions.values()))

    async def test_spawn_is_capped_by_max_agents(self):
        graph = TaskGraph()
        first = graph.add("first")
        second = graph.add("second")
        team = AgentTeam(graph, _factory(_Recorder(), delay=10.0)[0], max_agents=1)

        await team.spawn(first.id)
        with self.assertRaises(AgentTeamError):
            await team.spawn(second.id)
        await team.aclose()

    async def test_attach_team_without_slot_is_a_noop(self):
        """`multiagent=False` 的会话没有槽位，宿主回填团队时不能炸。"""
        from gradus.coding.session import Session

        session = object.__new__(Session)
        session._team_cell = None
        Session.attach_team(session, object())

        self.assertIsNone(session.team)

    async def test_require_plan_rejects_a_task_without_a_briefing(self):
        graph = TaskGraph()
        task = graph.add("only")
        team = AgentTeam(graph, _factory(_Recorder())[0], require_plan=True)

        with self.assertRaises(AgentTeamError):
            await team.spawn(task.id)
        self.assertEqual(graph.get(task.id).status, STATUS_PENDING)

    async def test_lead_is_notified_when_a_subagent_finishes(self):
        graph = TaskGraph()
        task = graph.add("only", "do the thing")
        notes: list[str] = []
        team = AgentTeam(
            graph,
            _factory(_Recorder())[0],
            lead_notify=notes.append,
            require_plan=True,
        )

        agent = await team.spawn(task.id, scope=("pkg/",))
        await team.wait([agent.name])

        self.assertTrue(any("completed" in note and agent.name in note for note in notes))

    async def test_scope_blocks_parent_writes_while_the_subagent_runs(self):
        graph = TaskGraph()
        task = graph.add("only", "do the thing")
        connect, _ = _factory(_Recorder(), delay=10.0)
        team = AgentTeam(graph, connect)

        agent = await team.spawn(task.id, scope=("pkg/",))

        self.assertEqual(team.write_blocked("pkg/module.py"), agent.name)
        self.assertIsNone(team.write_blocked("docs/readme.md"))

        await team.close(agent.name)
        self.assertIsNone(team.write_blocked("pkg/module.py"))

    async def test_write_tool_refuses_a_path_owned_by_a_subagent(self):
        import tempfile
        from pathlib import Path as _Path

        from gradus.coding.tools.files import create_file_tools

        with tempfile.TemporaryDirectory() as tmp:
            tools = create_file_tools(_Path(tmp), guard=lambda path: "sub-1")
            write = next(tool for tool in tools if tool.spec.name == "write_file")

            result = await write.execute(
                "call", {"path": "pkg/a.py", "content": "x"}, None
            )

            self.assertTrue(result.is_error)
            self.assertIn("owned by sub-1", result.content)
            self.assertFalse((_Path(tmp) / "pkg" / "a.py").exists())

    async def test_create_task_builds_the_dag_and_unlocks_spawning(self):
        from gradus.coding.tools.tasks import create_task_tools

        graph = TaskGraph()
        team = AgentTeam(graph, _factory(_Recorder(), delay=10.0)[0], require_plan=True)
        task_tools = {tool.spec.name: tool for tool in create_task_tools(graph)}
        team_tools = {tool.spec.name: tool for tool in create_multiagent_tools(lambda: team)}

        # 没有描述的任务不能派活：执行者看不到这段对话，没 brief 等于空手
        early = graph.add("early", "")
        blocked = await team_tools["spawn_agent"].execute("c", {"task_id": early.id}, None)
        self.assertTrue(blocked.is_error)
        self.assertIn("no description", blocked.content)

        # create_task 一次把节点和依赖都建好（等价于 s10 的 create_task + update_task）
        import re

        first = await task_tools["create_task"].execute(
            "c", {"subject": "collect", "description": "read the code"}, None
        )
        self.assertFalse(first.is_error, first.content)
        first_id = re.search(r"task_[0-9a-f]{8}", first.content).group(0)

        second = await task_tools["create_task"].execute(
            "c",
            {"subject": "fix", "description": "apply fix", "blocked_by": [first_id]},
            None,
        )
        self.assertFalse(second.is_error, second.content)
        second_id = re.search(r"task_[0-9a-f]{8}", second.content).group(0)
        self.assertEqual(graph.get(second_id).blocked_by, (first_id,))

        # 依赖没完成前派活被拒；完成后可以派
        self.assertTrue(graph.blockers(second_id))
        claimed = await task_tools["claim_task"].execute("c", {"task_id": first_id}, None)
        self.assertFalse(claimed.is_error, claimed.content)
        done = await task_tools["complete_task"].execute("c", {"task_id": first_id}, None)
        self.assertFalse(done.is_error, done.content)
        self.assertFalse(graph.blockers(second_id))

        agent = await team.spawn(second_id)
        self.assertTrue(agent.name)
        await team.aclose()

    async def test_update_task_wires_dependencies_after_the_fact(self):
        from gradus.coding.tools.tasks import create_task_tools

        graph = TaskGraph()
        tools = {tool.spec.name: tool for tool in create_task_tools(graph)}
        import re

        a = await tools["create_task"].execute("c", {"subject": "a", "description": "x"}, None)
        aid = re.search(r"task_[0-9a-f]{8}", a.content).group(0)
        b = await tools["create_task"].execute("c", {"subject": "b", "description": "y"}, None)
        bid = re.search(r"task_[0-9a-f]{8}", b.content).group(0)

        linked = await tools["update_task"].execute(
            "c", {"task_id": bid, "add_blocked_by": [aid]}, None
        )
        self.assertFalse(linked.is_error, linked.content)
        self.assertEqual(graph.get(bid).blocked_by, (aid,))

        unknown = await tools["update_task"].execute(
            "c", {"task_id": bid, "add_blocked_by": ["task_zzzzzzzz"]}, None
        )
        self.assertTrue(unknown.is_error)
        self.assertIn("unknown task", unknown.content)


    async def test_agent_limit_counts_running_not_total(self):
        """串行派活不该撞上限：跑完的 agent 仍留在字典里等收消息。"""
        graph = TaskGraph()
        first = graph.add("one", "brief one")
        team = AgentTeam(
            graph, _factory(_Recorder(), delay=10.0)[0], max_agents=1
        )

        one = await team.spawn(first.id)
        await team.close(one.name)
        self.assertFalse(one.busy)

        # 上限是 1，但上一个已经跑完了，再派一个必须成功
        second = graph.add("two", "brief two")
        two = await team.spawn(second.id)
        self.assertTrue(two.name)
        await team.aclose()

    async def test_agent_limit_still_blocks_concurrent_overrun(self):
        graph = TaskGraph()
        graph.add("warm", "brief warm")
        team = AgentTeam(graph, _factory(_Recorder(), delay=10.0)[0], max_agents=1)

        running = graph.add("running", "brief running")
        agent = await team.spawn(running.id)
        self.assertTrue(agent.busy)

        extra = graph.add("extra", "brief extra")
        with self.assertRaises(AgentTeamError):
            await team.spawn(extra.id)
        await team.aclose()

    async def test_messages_carry_a_kind(self):
        graph = TaskGraph()
        task = graph.add("only", "brief")
        notes: list[str] = []
        team = AgentTeam(graph, _factory(_Recorder(), delay=10.0)[0], lead_notify=notes.append)

        message = team.send("lead", "why did the build fail?", kind="question")
        self.assertEqual(message.kind, "question")
        self.assertIn("kind=question", notes[-1])

        with self.assertRaises(BusError):
            team.send("lead", "hi", kind="nonsense")
