"""多 agent 协作：子 agent 的启停、等待与点对点消息。

派活由模型决定（`spawn`），主 agent 用 `wait` 等结果、用 `close` 收尾。
本模块不扫描任务图、不做调度——图的推进由模型按依赖顺序自己掌握。

生命周期照 Codex 的做法：跑完**不销毁**，转为 idle 并保留会话，只有显式
`close` 才真正关闭；`spawn(..., ephemeral=True)` 可按节点要求一次性。

消息走 `bus`：收件人是 agent 名，投递复用 `Session.notify`——运行中进 steering，
空闲时进收件箱，并立刻起一轮把它读掉（否则没人会再 `prompt` 它）。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..agent.types import AgentError, AgentEvent
from ..ai.types import AssistantMessage, TextBlock
from .bus import LEAD, Bus, Message
from .tasks import Task, TaskError, TaskGraph

if TYPE_CHECKING:  # 只为类型标注；运行时按鸭子类型使用，避免与 session 循环导入
    from .session import Session

STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_FAILED = "failed"
STATE_CLOSED = "closed"

DEFAULT_WAIT_SECONDS = 600.0
#: 派活时拼进提示词的前驱结果上限，避免一个上游把子 agent 的上下文撑爆。
MAX_UPSTREAM_CHARS = 2_000

#: 建子会话：拿到名字与节点，自行决定模型、工作目录与会话文件。
ConnectAgent = Callable[[str, Task], Awaitable["Session"]]
OnEvent = Callable[["Subagent", AgentEvent], None]
#: 主 agent 接收总线消息的入口（通常是 `Session.notify`）。
LeadNotify = Callable[[str], None]


class AgentTeamError(ValueError):
    """派活、等待或收尾不合法。"""


class _Aborted(Exception):
    """子 agent 的运行被中止（不是失败，不计入重试次数）。"""


@dataclass
class Subagent:
    """一个子 agent；`session` 在真正开跑时才建立。"""

    name: str
    state: str = STATE_IDLE
    task_id: str | None = None
    session: Session | None = None
    handle: asyncio.Task[None] | None = None
    ephemeral: bool = False
    #: 这个子 agent 在跑的时候独占的路径（相对工作区）；父 agent 不能再改这些文件。
    scope: tuple[str, ...] = ()
    started_at: float | None = None
    finished_at: float | None = None
    last_error: str | None = None
    #: 最后一次运行产出的文本。叫醒的一轮不写任务图，主 agent 只能从这里读到它。
    last_reply: str | None = None
    #: 协程是否真的跑起来过；`create_task` 之后立刻取消的话它一行都不会执行。
    entered: bool = False

    @property
    def busy(self) -> bool:
        return self.state == STATE_RUNNING

    @property
    def closed(self) -> bool:
        return self.state == STATE_CLOSED


class AgentTeam:
    """子 agent 的注册表与执行者；不含任何调度行为。"""

    def __init__(
        self,
        graph: TaskGraph,
        connect: ConnectAgent,
        *,
        lead_notify: LeadNotify | None = None,
        on_event: OnEvent | None = None,
        history_limit: int = 100,
        max_agents: int = 8,
        require_plan: bool = False,
    ) -> None:
        self._graph = graph
        self._connect = connect
        self._lead_notify = lead_notify
        self._on_event = on_event
        self._max_agents = max(1, max_agents)
        #: 打开后，只有写了 briefing 的节点才能派活——用来逼模型先做任务规划。
        self._require_plan = require_plan
        self._agents: dict[str, Subagent] = {}
        self._counter = 0
        self._closed = False
        self._bus = Bus(self._deliver, limit=history_limit)

    @property
    def graph(self) -> TaskGraph:
        return self._graph

    @property
    def bus(self) -> Bus:
        return self._bus

    # ---- 启停 ----

    async def spawn(
        self,
        task_id: str,
        *,
        ephemeral: bool = False,
        scope: Iterable[str] = (),
    ) -> Subagent:
        """认领节点、建好子会话，然后起跑；返回时它已经在跑，但没跑完。"""
        if self._closed:
            raise AgentTeamError("team is closed")
        # 只数**正在跑**的：非 ephemeral 的 agent 跑完仍留在 `_agents` 里等收消息，
        # 拿 len(self._agents) 当并发上限会让串行派活在第 N 次就撞墙。
        if sum(1 for agent in self._agents.values() if agent.busy) >= self._max_agents:
            raise AgentTeamError(f"too many running subagents (limit {self._max_agents})")

        task = self._graph.get(task_id)
        if self._require_plan and not task.description.strip():
            raise AgentTeamError(
                f"task {task.id} has no description: plan the task first "
                f"(subject + detailed briefing + dependencies), then spawn it"
            )
        # 先占号再 await：并发 spawn 时名字不会撞（原来是在 connect 之后才自增）。
        self._counter += 1
        name = f"sub-{self._counter}"
        # 先认领再建会话：认领失败时不会留下空的会话文件。
        self._graph.claim(task.id, name)
        try:
            session = await self._connect(name, task)
        except BaseException:
            self._graph.release(task.id)
            raise

        if self._closed:
            # aclose 可能发生在 connect 期间：新会话没人接管，自己收掉。
            with suppress(Exception):
                await session.aclose()
            with suppress(TaskError):
                self._graph.release(task.id)
            raise AgentTeamError("team is closed")

        agent = Subagent(
            name=name,
            state=STATE_RUNNING,
            task_id=task.id,
            ephemeral=ephemeral,
            scope=tuple(_normalise_scope(scope)),
            session=session,
            started_at=time.time(),
        )
        self._agents[agent.name] = agent
        agent.handle = asyncio.create_task(self._work(agent, task))
        return agent

    async def wait(
        self,
        names: Iterable[str],
        timeout: float | None = DEFAULT_WAIT_SECONDS,
    ) -> tuple[Subagent, ...]:
        """等这些子 agent 结束；超时不算失败，返回时它们可能还在跑。"""
        agents: list[Subagent] = []
        for name in names:
            agent = self._agents.get(name.strip())
            if agent is None:
                raise AgentTeamError(f"unknown agent: {name.strip()}")
            agents.append(agent)

        pending = [
            agent.handle
            for agent in agents
            if agent.handle is not None and not agent.handle.done()
        ]
        if pending:
            # 这里只等，不取消：取消由调用方（或 aclose/close）负责。
            await asyncio.wait(pending, timeout=timeout)
        return tuple(agents)

    async def close(self, name: str) -> bool:
        """关闭一个子 agent：取消运行中的协程，释放它的会话。"""
        agent = self._agents.get(name.strip())
        if agent is None:
            return False

        await self._stop(agent)
        await self._retire(agent)
        return True

    def interrupt(self, name: str) -> bool:
        """中止但保留会话：节点放回 pending，子 agent 转为 idle。"""
        agent = self._agents.get(name.strip())
        if agent is None or not agent.busy:
            return False

        handle = agent.handle
        if handle is None or handle.done():
            return False

        handle.cancel()
        if not agent.entered:
            # 协程还没跑起来就被取消：它自己的清理逻辑不会执行，这里补上。
            self._abandon(agent)
        return True

    async def aclose(self) -> None:
        """关闭全部子 agent；幂等。"""
        if self._closed:
            return
        self._closed = True

        handles = [agent.handle for agent in self._agents.values() if agent.handle is not None]
        for handle in handles:
            handle.cancel()
        if handles:
            await asyncio.gather(*handles, return_exceptions=True)

        for agent in list(self._agents.values()):
            if agent.task_id is not None:
                with suppress(TaskError):
                    self._graph.release(agent.task_id)
            await self._close_session(agent)
        self._agents.clear()

    # ---- 观察 ----

    def write_blocked(self, path: str | Path) -> str | None:
        """这个路径是否被某个**正在跑**的子 agent 独占；是则返回它的名字。"""
        target = Path(path).as_posix().lstrip("./")
        for agent in self._agents.values():
            if not agent.busy or not agent.scope:
                continue
            for scope in agent.scope:
                if target == scope or target.startswith(scope.rstrip("/") + "/"):
                    return agent.name
        return None

    def list(self) -> tuple[Subagent, ...]:
        return tuple(self._agents.values())

    def get(self, name: str) -> Subagent | None:
        return self._agents.get(name.strip())

    # ---- 消息 ----

    def send(
        self,
        recipient: str,
        text: str,
        *,
        sender: str = LEAD,
        kind: str = "text",
    ) -> Message:
        return self._bus.send(sender, recipient, text, kind=kind)

    def _deliver(self, recipient: str, text: str) -> bool:
        if recipient == LEAD:
            if self._lead_notify is None:
                return False
            self._lead_notify(text)
            return True

        agent = self._agents.get(recipient)
        if agent is None or agent.session is None:
            return False

        # `notify` 返回 True 表示消息只是进了收件箱（没在跑）：得有人再起一轮。
        # 正在跑时它已经是 steering，这一轮就能读到，不必叫醒。
        if agent.session.notify(text):
            self._wake(agent)
        return True

    def _wake(self, agent: Subagent) -> None:
        """给空闲的子 agent 起一轮，把收件箱里的消息真正送进模型。

        已经有一轮在飞时直接返回：那一轮的收尾会再看一眼收件箱。
        """
        if self._closed:
            return
        handle = agent.handle
        if handle is not None and not handle.done():
            return
        agent.entered = False
        agent.state = STATE_RUNNING
        agent.handle = asyncio.create_task(self._resume(agent))

    # ---- 内部 ----

    async def _resume(self, agent: Subagent) -> None:
        """消息触发的一轮：不动任务图，只把收件箱读掉。"""
        agent.entered = True
        session = agent.session
        cancelled = False
        try:
            if session is None:  # 已经被 `close` 收走了
                return
            async for event in session.continue_():
                if self._on_event is not None:
                    with suppress(Exception):
                        self._on_event(agent, event)
                if isinstance(event, AgentError):
                    if event.kind == "aborted":
                        raise _Aborted()
                    raise RuntimeError(event.message)
            agent.last_reply = _last_text(session) or None
        except asyncio.CancelledError:
            cancelled = True
            raise
        except _Aborted:
            pass
        except Exception as exc:
            # 这一轮失败不毁掉 agent：它仍然能被下一条消息叫醒。
            agent.last_error = str(exc)
        finally:
            agent.finished_at = time.time()
            agent.state = STATE_IDLE
            agent.handle = None
            if not cancelled:
                self._wake_if_pending(agent)

    def _wake_if_pending(self, agent: Subagent) -> None:
        """收尾时补一轮：这一轮开始前/结束瞬间到达的消息还躺在收件箱里。"""
        session = agent.session
        if session is not None and session.has_notices:
            self._wake(agent)

    async def _work(self, agent: Subagent, task: Task) -> None:
        agent.entered = True
        session = agent.session
        assert session is not None  # spawn 一定先建好会话
        cancelled = False
        try:
            async for event in session.prompt(_task_prompt(self._graph, task)):
                if self._on_event is not None:
                    with suppress(Exception):
                        self._on_event(agent, event)
                if isinstance(event, AgentError):
                    if event.kind == "aborted":
                        raise _Aborted()
                    raise RuntimeError(event.message)
        except asyncio.CancelledError:
            # 中止：把节点放回 pending，下一次派活还能重来。
            self._graph.release(task.id)
            cancelled = True
            raise
        except _Aborted:
            self._graph.release(task.id)
        except Exception as exc:
            self._graph.release(task.id)
            agent.last_error = str(exc)
            agent.state = STATE_FAILED
            self._notify_lead(agent, task, "failed", None)
        else:
            self._finish(agent, task)
            agent.last_error = None
        finally:
            agent.finished_at = time.time()
            if agent.state != STATE_FAILED:
                agent.state = STATE_IDLE
            # 旧句柄到此为止：`_wake` 才不会被已经结束的这一轮挡住。
            agent.handle = None
            if agent.ephemeral:
                await self._retire(agent)
            elif not cancelled:
                self._wake_if_pending(agent)

    def _finish(self, agent: Subagent, task: Task) -> None:
        result = _last_text(agent.session)
        agent.last_reply = result or None
        with suppress(TaskError):
            self._graph.complete(task.id, agent.name, result)
        self._notify_lead(agent, task, "completed", result)

    def _notify_lead(
        self, agent: Subagent, task: Task, status: str, result: str | None
    ) -> None:
        """子 agent 收尾时主动给 lead 一条结构化消息。

        以前 lead 只能 `wait` 才看得到结果；现在边跑边能收到通知，
        也可以据此决定下一步。
        """
        if self._lead_notify is None:
            return
        head = f"[subagent {agent.name}] task {task.id} ({task.subject}) {status}"
        body = f"\nresult: {result[:MAX_UPSTREAM_CHARS]}" if result else ""
        error = f"\nerror: {agent.last_error}" if agent.last_error else ""
        with suppress(Exception):
            self._lead_notify(head + body + error)

    async def _stop(self, agent: Subagent) -> None:
        handle = agent.handle
        if handle is None or handle.done():
            return

        handle.cancel()
        if not agent.entered:
            self._abandon(agent)
            return
        with suppress(asyncio.CancelledError):
            await handle

    def _abandon(self, agent: Subagent) -> None:
        """取消之后的收尾：把节点放回 pending（协程没跑起来时由这里兜底）。"""
        if agent.task_id is not None:
            with suppress(TaskError):
                self._graph.release(agent.task_id)
        agent.handle = None
        agent.state = STATE_IDLE

    async def _retire(self, agent: Subagent) -> None:
        await self._close_session(agent)
        if self._agents.get(agent.name) is agent:
            self._agents.pop(agent.name, None)
        agent.state = STATE_CLOSED

    async def _close_session(self, agent: Subagent) -> None:
        session = agent.session
        agent.session = None
        if session is None:
            return
        with suppress(Exception):
            await session.aclose()


def _normalise_scope(scope: Iterable[str]) -> list[str]:
    cleaned: list[str] = []
    for item in scope:
        text = str(item).strip().replace("\\", "/").lstrip("./")
        if text:
            cleaned.append(text.rstrip("/"))
    return cleaned


def _task_prompt(graph: TaskGraph, task: Task) -> str:
    """子 agent 收到的唯一一条用户消息：节点内容 + 已完成前驱的结论。"""
    lines = [f"[Task {task.id}] {task.subject}"]
    if task.description:
        lines.extend(("", task.description))

    upstream: list[str] = []
    for dependency in task.blocked_by:
        with suppress(TaskError):
            other = graph.get(dependency)
            if not other.done or not other.result:
                continue
            summary = other.result[:MAX_UPSTREAM_CHARS]
            upstream.append(f"- {other.subject}: {summary}")
    if upstream:
        lines.extend(("", "Upstream results:", *upstream))

    return "\n".join(lines)


def _last_text(session: Session | None) -> str:
    """取子 agent 最后一条有内容的助手消息作为节点结果。"""
    if session is None:
        return ""
    for message in reversed(session.messages):
        if isinstance(message, AssistantMessage):
            text = "".join(
                block.text for block in message.content if isinstance(block, TextBlock)
            )
            if text.strip():
                return text.strip()
    return ""
