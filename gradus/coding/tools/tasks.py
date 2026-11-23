"""任务图工具：建节点、连边、查看、认领与完成。

这组工具把 `coding/tasks.py` 暴露给模型。它们只改任务表，不启动任何运行；
"谁去跑这个节点"由 `AgentTeam` 决定。
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from ...agent.base import AgentTool, ToolUpdateCallback
from ...agent.types import ToolResult
from ...ai.base import ToolSpec
from ..tasks import Task, TaskError, TaskGraph

CREATE_TASK_SPEC = ToolSpec(
    name="create_task",
    description=(
        "Create a node in the task graph. Tasks are the unit of work: they can "
        "depend on each other, and a task only becomes startable once every task "
        "it is blocked by is completed. Create all nodes first, then connect them "
        "with update_task using the returned ids."
    ),
    parameters={
        "type": "object",
        "properties": {
            "subject": {"type": "string", "description": "One-line goal of the task"},
            "description": {
                "type": "string",
                "description": "Details the executor needs; it does not see this conversation",
            },
            "role": {
                "type": "string",
                "description": "Optional role hint, e.g. coder / reviewer / tester",
            },
            "model": {
                "type": "string",
                "description": "Optional model id from the configured candidate list",
            },
            "blocked_by": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Task ids this task waits for. Same as calling update_task afterwards."
                ),
            },
        },
        "required": ["subject"],
    },
)



UPDATE_TASK_SPEC = ToolSpec(
    name="update_task",
    description=(
        "Add or remove dependencies on an existing task. A dependency means 'that "
        "task must be completed before this one can start'. Rejects cycles, "
        "self-dependencies and unknown ids. Only pending tasks can be edited."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Task to edit"},
            "add_blocked_by": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Task ids this task must wait for",
            },
            "remove_blocked_by": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Task ids to drop from the dependency list",
            },
        },
        "required": ["task_id"],
    },
)

LIST_TASKS_SPEC = ToolSpec(
    name="list_tasks",
    description=(
        "List every task with its status, owner and unfinished dependencies. "
        "Tasks marked ready can be claimed right now."
    ),
    parameters={"type": "object", "properties": {}},
)

GET_TASK_SPEC = ToolSpec(
    name="get_task",
    description=(
        "Show one task in full: description, dependencies, owner, status and the "
        "result recorded when it completed."
    ),
    parameters={
        "type": "object",
        "properties": {"task_id": {"type": "string"}},
        "required": ["task_id"],
    },
)

CLAIM_TASK_SPEC = ToolSpec(
    name="claim_task",
    description=(
        "Claim a ready task before working on it. Fails when the task is blocked, "
        "already claimed or already completed."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "owner": {
                "type": "string",
                "description": "Who is taking it; defaults to 'lead'",
            },
        },
        "required": ["task_id"],
    },
)

COMPLETE_TASK_SPEC = ToolSpec(
    name="complete_task",
    description=(
        "Mark a claimed task as completed and report which tasks it just "
        "unblocked. Record a short result: downstream tasks receive it as context."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "owner": {
                "type": "string",
                "description": "Must match the owner that claimed it; defaults to 'lead'",
            },
            "result": {
                "type": "string",
                "description": "Short summary handed to downstream tasks",
            },
        },
        "required": ["task_id"],
    },
)


class CreateTaskTool:
    spec = CREATE_TASK_SPEC
    execution_mode = "serial"

    def __init__(self, graph: TaskGraph) -> None:
        self._graph = graph

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        try:
            task = self._graph.add(
                _text(arguments, "subject"),
                _text(arguments, "description"),
                role=_text(arguments, "role") or None,
                model=_text(arguments, "model") or None,
                blocked_by=tuple(_string_list(arguments.get("blocked_by"))),
            )
        except TaskError as exc:
            return ToolResult(f"cannot create task: {exc}", is_error=True)

        return ToolResult(f"created {task.id}: {task.subject}")


class UpdateTaskTool:
    spec = UPDATE_TASK_SPEC
    execution_mode = "serial"

    def __init__(self, graph: TaskGraph) -> None:
        self._graph = graph

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        task_id = _text(arguments, "task_id")
        if not task_id:
            return ToolResult("task_id is required", is_error=True)

        try:
            task = self._graph.update_dependencies(
                task_id,
                add=tuple(_string_list(arguments.get("add_blocked_by"))),
                remove=tuple(_string_list(arguments.get("remove_blocked_by"))),
            )
        except TaskError as exc:
            return ToolResult(f"cannot update {task_id}: {exc}", is_error=True)

        return ToolResult(_describe(task, self._graph))


class ListTasksTool:
    spec = LIST_TASKS_SPEC
    execution_mode = "serial"

    def __init__(self, graph: TaskGraph) -> None:
        self._graph = graph

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        tasks = self._graph.tasks()
        if not tasks:
            return ToolResult("no tasks")

        lines = [_describe(task, self._graph) for task in tasks]
        return ToolResult("\n".join(lines))


class GetTaskTool:
    spec = GET_TASK_SPEC
    execution_mode = "serial"

    def __init__(self, graph: TaskGraph) -> None:
        self._graph = graph

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        task_id = _text(arguments, "task_id")
        try:
            task = self._graph.get(task_id)
        except TaskError as exc:
            return ToolResult(str(exc), is_error=True)

        payload = asdict(task)
        payload["unfinished"] = list(self._graph.blockers(task_id))
        return ToolResult(json.dumps(payload, ensure_ascii=False, indent=2))


class ClaimTaskTool:
    spec = CLAIM_TASK_SPEC
    execution_mode = "serial"

    def __init__(self, graph: TaskGraph) -> None:
        self._graph = graph

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        task_id = _text(arguments, "task_id")
        owner = _text(arguments, "owner") or "lead"
        try:
            task = self._graph.claim(task_id, owner)
        except TaskError as exc:
            return ToolResult(f"cannot claim {task_id}: {exc}", is_error=True)

        return ToolResult(f"claimed {task.id} ({task.subject}) by {task.owner}")


class CompleteTaskTool:
    spec = COMPLETE_TASK_SPEC
    execution_mode = "serial"

    def __init__(self, graph: TaskGraph) -> None:
        self._graph = graph

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        task_id = _text(arguments, "task_id")
        owner = _text(arguments, "owner") or "lead"
        try:
            task, unblocked = self._graph.complete(
                task_id, owner, _text(arguments, "result")
            )
        except TaskError as exc:
            return ToolResult(f"cannot complete {task_id}: {exc}", is_error=True)

        message = f"completed {task.id} ({task.subject})"
        if unblocked:
            names = ", ".join(f"{item.id} ({item.subject})" for item in unblocked)
            message += f"\nunblocked: {names}"
        if not self._graph.finished:
            return ToolResult(message)
        return ToolResult(f"{message}\nthe task graph is finished")




def _find_cycle(edges: dict[str, tuple[str, ...]]) -> list[str] | None:
    """规划阶段的环检测：返回一条环，没有则 None。"""
    visiting: set[str] = set()
    done: set[str] = set()
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        if node in done:
            return None
        if node in visiting:
            return stack[stack.index(node) :] + [node]
        visiting.add(node)
        stack.append(node)
        for nxt in edges.get(node, ()):
            found = visit(nxt)
            if found is not None:
                return found
        stack.pop()
        visiting.discard(node)
        done.add(node)
        return None

    for node in edges:
        found = visit(node)
        if found is not None:
            return found
    return None


def create_task_tools(graph: TaskGraph) -> list[AgentTool]:
    return [
        CreateTaskTool(graph),
        UpdateTaskTool(graph),
        ListTasksTool(graph),
        GetTaskTool(graph),
        ClaimTaskTool(graph),
        CompleteTaskTool(graph),
    ]


def _describe(task: Task, graph: TaskGraph) -> str:
    parts = [task.id, task.status]
    if task.owner:
        parts.append(f"owner={task.owner}")
    blocked = graph.blockers(task.id)
    if blocked:
        parts.append(f"blocked by {', '.join(blocked)}")
    elif task.status == "pending":
        parts.append("ready")
    head = " · ".join(parts)
    return f"{head}\n  {task.subject}"


def _text(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    return value.strip() if isinstance(value, str) else ""


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]
