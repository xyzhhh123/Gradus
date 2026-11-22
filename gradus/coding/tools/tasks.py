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
        },
        "required": ["subject"],
    },
)

PLAN_TASKS_SPEC = ToolSpec(
    name="plan_tasks",
    description=(
        "Create the whole task DAG in one call and mark the plan ready. Call this "
        "BEFORE spawning any subagent: spawn_agent refuses tasks until the graph is "
        "planned. Each node needs a key, a one-line subject and a detailed briefing "
        "(the executor never sees this conversation). blocked_by refers to keys in the "
        "same call, so forward references are fine."
    ),
    parameters={
        "type": "object",
        "properties": {
            "nodes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "description": "Short id used by blocked_by"},
                        "subject": {"type": "string"},
                        "description": {
                            "type": "string",
                            "description": "Briefing: files, commands, findings, done-when",
                        },
                        "blocked_by": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Keys that must finish first",
                        },
                        "role": {"type": "string"},
                        "model": {"type": "string"},
                    },
                    "required": ["key", "subject", "description"],
                },
            }
        },
        "required": ["nodes"],
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


class PlanTasksTool:
    """一次性建整张 DAG：先全量校验，再建节点、连依赖、置计划标记。"""

    spec = PLAN_TASKS_SPEC
    execution_mode = "serial"

    def __init__(self, graph: TaskGraph) -> None:
        self._graph = graph

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        raw = arguments.get("nodes")
        if not isinstance(raw, list) or not raw:
            return ToolResult("nodes must be a non-empty array", is_error=True)

        nodes: list[dict[str, Any]] = []
        keys: dict[str, str] = {}
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                return ToolResult(f"nodes[{index}] must be an object", is_error=True)
            key = str(item.get("key", "")).strip()
            subject = str(item.get("subject", "")).strip()
            description = str(item.get("description", "")).strip()
            if not key or not subject:
                return ToolResult(f"nodes[{index}] needs key and subject", is_error=True)
            if not description:
                return ToolResult(
                    f"nodes[{index}] ({key}) needs a description: the executor cannot "
                    f"see this conversation, so the briefing must be self-contained",
                    is_error=True,
                )
            if key in keys:
                return ToolResult(f"duplicate key: {key}", is_error=True)
            keys[key] = key
            nodes.append(item)

        edges: dict[str, tuple[str, ...]] = {}
        for item in nodes:
            key = str(item["key"]).strip()
            blocked = item.get("blocked_by") or []
            if not isinstance(blocked, list) or any(not isinstance(x, str) for x in blocked):
                return ToolResult(f"blocked_by of {key} must be a string array", is_error=True)
            unknown = [str(x) for x in blocked if str(x).strip() not in keys]
            if unknown:
                return ToolResult(
                    f"blocked_by of {key} refers to unknown keys: {', '.join(unknown)}",
                    is_error=True,
                )
            edges[key] = tuple(str(x).strip() for x in blocked)

        cycle = _find_cycle(edges)
        if cycle is not None:
            return ToolResult(f"dependency cycle: {' -> '.join(cycle)}", is_error=True)

        created: dict[str, str] = {}
        for item in nodes:
            key = str(item["key"]).strip()
            task = self._graph.add(
                str(item["subject"]).strip(),
                str(item["description"]).strip(),
                role=(str(item["role"]).strip() or None) if item.get("role") else None,
                model=(str(item["model"]).strip() or None) if item.get("model") else None,
            )
            created[key] = task.id

        for key, blocked in edges.items():
            if not blocked:
                continue
            self._graph.update_dependencies(
                created[key], add=tuple(created[other] for other in blocked)
            )

        self._graph.mark_planned()
        lines = [f"planned {len(nodes)} tasks (ready to spawn):"]
        for item in nodes:
            key = str(item["key"]).strip()
            deps = ", ".join(edges[key]) or "-"
            lines.append(f"- {created[key]} [{key}] {item['subject']} (after: {deps})")
        return ToolResult("\n".join(lines))


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
        PlanTasksTool(graph),
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
