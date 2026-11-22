"""多 agent 工具：派活、等待、收尾与点对点消息。

派活是模型的显式动作：先建好任务图，再对就绪节点 `spawn_agent`，然后用
`wait` 等结果。图不会自己往前走——这是与"运行时调度器"方案的根本区别。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ...agent.base import AgentTool, ToolUpdateCallback
from ...agent.types import ToolResult
from ...ai.base import ToolSpec
from ..bus import BusError
from ..multiagent import DEFAULT_WAIT_SECONDS, AgentTeam, AgentTeamError, Subagent
from ..tasks import TaskError, TaskGraph

MAX_RESULT_CHARS = 2_000

SPAWN_AGENT_SPEC = ToolSpec(
    name="spawn_agent",
    description=(
        "Hand a ready task off to a subagent and return immediately. The subagent gets "
        "its own conversation, the task description, and the results of the tasks it "
        "depends on; it does not see this conversation, so the task description must "
        "carry the findings (files, commands, what you already tried). After spawning, "
        "this task belongs to the subagent: do not edit the same files yourself, just "
        "wait and integrate the result. It keeps its conversation after finishing, so "
        "you can send it more messages or close it later."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "A task that is ready to start"},
            "ephemeral": {
                "type": "boolean",
                "description": "Close the subagent as soon as it finishes",
            },
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Paths (files or directories, relative to the workspace) this subagent "
                    "owns while it runs. You (the lead) will be blocked from editing them "
                    "until it finishes, so the two of you cannot do the same work twice."
                ),
            },
        },
        "required": ["task_id"],
    },
)

WAIT_SPEC = ToolSpec(
    name="wait",
    description=(
        "Wait until the given subagents finish, then report their final state, the result "
        "recorded on their tasks, and the reply from their latest turn (for example the "
        "turn started by send_message). A timeout is not an error: it returns the agents "
        "that are still running."
    ),
    parameters={
        "type": "object",
        "properties": {
            "agents": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Subagent names, e.g. sub-1",
            },
            "timeout_seconds": {
                "type": "number",
                "description": "How long to wait; 0 returns immediately. Default 600",
            },
        },
        "required": ["agents"],
    },
)

LIST_AGENTS_SPEC = ToolSpec(
    name="list_agents",
    description="List every live subagent with its state, current or last task, and last error.",
    parameters={"type": "object", "properties": {}},
)

SEND_MESSAGE_SPEC = ToolSpec(
    name="send_message",
    description=(
        "Send a message to another agent by name ('lead' or a subagent such as 'sub-1'). "
        "A running recipient sees it before its next request. An idle subagent starts a "
        "new turn to read it — call wait afterwards to get its reply. Sending to a "
        "closed agent fails."
    ),
    parameters={
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "Recipient agent name"},
            "text": {"type": "string", "description": "Message body"},
            "kind": {
                "type": "string",
                "enum": ["text", "question", "result", "status"],
                "description": "Intent of the message; the recipient sees it in the tag",
            },
        },
        "required": ["to", "text"],
    },
)

CLOSE_AGENT_SPEC = ToolSpec(
    name="close_agent",
    description=(
        "Close a subagent and release its conversation. A running subagent is stopped "
        "first, and its task goes back to pending."
    ),
    parameters={
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    },
)

INTERRUPT_AGENT_SPEC = ToolSpec(
    name="interrupt_agent",
    description=(
        "Stop a running subagent but keep its conversation. Its task goes back to "
        "pending, so it can be claimed and started again."
    ),
    parameters={
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    },
)

#: 团队在工具之后才创建，所以这里拿的是间接引用。
TeamGetter = Callable[[], AgentTeam | None]


class _TeamTool:
    spec: ToolSpec
    execution_mode = "serial"

    def __init__(self, team: TeamGetter) -> None:
        self._get_team = team

    def _team(self) -> AgentTeam | ToolResult:
        team = self._get_team()
        if team is None:
            return ToolResult("multi-agent is not available in this session", is_error=True)
        return team


class SpawnAgentTool(_TeamTool):
    spec = SPAWN_AGENT_SPEC

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        team = self._team()
        if isinstance(team, ToolResult):
            return team

        task_id = _text(arguments, "task_id")
        try:
            scope = _string_list(arguments.get("files"))
            agent = await team.spawn(
                task_id,
                ephemeral=bool(arguments.get("ephemeral")),
                scope=scope,
            )
        except (TaskError, AgentTeamError) as exc:
            return ToolResult(f"cannot spawn: {exc}", is_error=True)

        task = team.graph.get(task_id)
        return ToolResult(
            f"spawned {agent.name} on {task.id} ({task.subject}).\n"
            f"Handoff: {task.id} now belongs to {agent.name} — do not work on it "
            f"yourself; call wait to collect the result and then integrate it."
            + (
                f"\nOwned paths: {', '.join(agent.scope)}"
                if agent.scope
                else ""
            )
        )


class WaitTool(_TeamTool):
    spec = WAIT_SPEC

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        team = self._team()
        if isinstance(team, ToolResult):
            return team

        names = _string_list(arguments.get("agents"))
        if not names:
            return ToolResult("agents is required", is_error=True)

        seconds = _number(arguments.get("timeout_seconds"))
        if seconds is None:
            # 工具描述写的默认值是 600；不传时不能把 None 透给 asyncio.wait（那是无限等）。
            seconds = DEFAULT_WAIT_SECONDS
        try:
            if seconds is not None and seconds <= 0:
                agents = tuple(team.get(name) for name in names)
                if any(agent is None for agent in agents):
                    raise AgentTeamError("unknown agent in list")
                waited = tuple(agent for agent in agents if agent is not None)
            else:
                waited = await team.wait(names, timeout=seconds)
        except AgentTeamError as exc:
            return ToolResult(str(exc), is_error=True)

        return ToolResult("\n".join(_describe(agent, team.graph) for agent in waited))


class ListAgentsTool(_TeamTool):
    spec = LIST_AGENTS_SPEC

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        team = self._team()
        if isinstance(team, ToolResult):
            return team

        agents = team.list()
        if not agents:
            return ToolResult("no subagents")
        return ToolResult("\n".join(_describe(agent, team.graph) for agent in agents))


class SendMessageTool(_TeamTool):
    spec = SEND_MESSAGE_SPEC

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        team = self._team()
        if isinstance(team, ToolResult):
            return team

        recipient = _text(arguments, "to")
        kind = _text(arguments, "kind") or "text"
        try:
            message = team.send(recipient, _text(arguments, "text"), kind=kind)
        except BusError as exc:
            return ToolResult(f"cannot send: {exc}", is_error=True)

        return ToolResult(
            f"delivered {message.id} ({message.kind}) to {message.recipient}"
        )


class CloseAgentTool(_TeamTool):
    spec = CLOSE_AGENT_SPEC

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        team = self._team()
        if isinstance(team, ToolResult):
            return team

        name = _text(arguments, "name")
        if not await team.close(name):
            return ToolResult(f"unknown agent: {name}", is_error=True)
        return ToolResult(f"closed {name}")


class InterruptAgentTool(_TeamTool):
    spec = INTERRUPT_AGENT_SPEC

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        team = self._team()
        if isinstance(team, ToolResult):
            return team

        name = _text(arguments, "name")
        if not team.interrupt(name):
            return ToolResult(f"{name} is not running", is_error=True)
        return ToolResult(f"interrupted {name}; its task is back to pending")


def create_multiagent_tools(team: TeamGetter) -> list[AgentTool]:
    return [
        SpawnAgentTool(team),
        WaitTool(team),
        ListAgentsTool(team),
        SendMessageTool(team),
        CloseAgentTool(team),
        InterruptAgentTool(team),
    ]


def _describe(agent: Subagent, graph: TaskGraph) -> str:
    parts = [agent.name, agent.state]
    result: str | None = None
    if agent.task_id:
        try:
            task = graph.get(agent.task_id)
        except TaskError:
            task = None
        if task is not None:
            parts.append(f"{task.id} ({task.subject})")
            if task.result:
                result = task.result
                parts.append(f"result: {result[:MAX_RESULT_CHARS]}")
    # 叫醒的一轮不写任务图：它的产出只在 `last_reply` 里，和节点结果不同才值得显示。
    if agent.last_reply and agent.last_reply != result:
        parts.append(f"reply: {agent.last_reply[:MAX_RESULT_CHARS]}")
    if agent.last_error:
        parts.append(f"error: {agent.last_error}")
    return " · ".join(parts)


def _text(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    return value.strip() if isinstance(value, str) else ""


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None
