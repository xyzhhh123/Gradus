"""`schedule_task` 与 `manage_schedules`：增删查定时任务。

工具只读写任务表，从不执行任务：真正「到点跑一轮」由宿主的时钟协程负责，
见 `coding/docs/design.md` 5.7。
"""

from __future__ import annotations

from typing import Any

from ...agent.base import AgentTool, ToolUpdateCallback
from ...agent.types import ToolResult
from ...ai.base import ToolSpec
from ..schedule import Schedule, ScheduleError, ScheduledJob

SCHEDULE_TASK_SPEC = ToolSpec(
    name="schedule_task",
    description=(
        "Schedule a prompt to run as a new turn when a cron expression matches "
        "(local time, standard 5 fields: minute hour day-of-month month "
        "day-of-week). Examples: '0 9 * * 1-5' is 09:00 on weekdays, "
        "'30 18 * * *' is 18:30 every day. Set recurring=false for a one-shot "
        "task. It runs later without the user watching, so write a "
        "self-contained prompt."
    ),
    parameters={
        "type": "object",
        "properties": {
            "cron": {
                "type": "string",
                "description": "5-field cron expression, e.g. '0 9 * * 1-5'",
            },
            "prompt": {
                "type": "string",
                "description": "Prompt to run when due; make it self-contained",
            },
            "recurring": {
                "type": "boolean",
                "description": (
                    "true (default) keeps firing on every match; "
                    "false removes the task after it runs once"
                ),
            },
        },
        "required": ["cron", "prompt"],
    },
)

MANAGE_SCHEDULES_SPEC = ToolSpec(
    name="manage_schedules",
    description=(
        "List or cancel scheduled tasks. action='list' (default) shows every task "
        "with its id, cron expression and state; action='cancel' deletes the task "
        "with the given job_id."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "cancel"],
                "description": "list (default) or cancel",
            },
            "job_id": {
                "type": "string",
                "description": "Task id from a previous list; required for cancel",
            },
        },
    },
)


class ScheduleTaskTool:
    """把一条提示词登记进任务表，`due()` 之后由宿主点火。"""

    spec = SCHEDULE_TASK_SPEC
    execution_mode = "serial"

    def __init__(self, schedule: Schedule) -> None:
        self._schedule = schedule

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        cron = _text(arguments, "cron")
        prompt = _text(arguments, "prompt")
        if not cron or not prompt:
            return ToolResult("cron and prompt are required", is_error=True)

        recurring = arguments.get("recurring")
        try:
            job = self._schedule.add(
                cron,
                prompt,
                recurring=recurring if isinstance(recurring, bool) else True,
            )
        except ScheduleError as exc:
            return ToolResult(f"cannot schedule: {exc}", is_error=True)

        return ToolResult(
            f"scheduled {job.id} ({_scope(job)}): {job.cron}\n{job.prompt}"
        )


class ManageSchedulesTool:
    """列出或取消定时任务；取消只是从任务表里删掉，不影响正在跑的那一轮。"""

    spec = MANAGE_SCHEDULES_SPEC
    execution_mode = "serial"

    def __init__(self, schedule: Schedule) -> None:
        self._schedule = schedule

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        action = str(arguments.get("action") or "list").strip().lower()

        if action == "cancel":
            job_id = _text(arguments, "job_id")
            if not job_id:
                return ToolResult("job_id is required for cancel", is_error=True)
            if not self._schedule.cancel(job_id):
                return ToolResult(f"no such task: {job_id}", is_error=True)
            return ToolResult(f"cancelled {job_id}")

        if action != "list":
            return ToolResult(f"unknown action: {action}", is_error=True)

        jobs = self._schedule.jobs()
        if not jobs:
            return ToolResult("no scheduled tasks")
        return ToolResult("\n".join(_describe(job) for job in jobs))


def create_schedule_task_tool(schedule: Schedule) -> AgentTool:
    return ScheduleTaskTool(schedule)


def create_manage_schedules_tool(schedule: Schedule) -> AgentTool:
    return ManageSchedulesTool(schedule)


def _text(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    return value.strip() if isinstance(value, str) else ""


def _scope(job: ScheduledJob) -> str:
    return "recurring" if job.recurring else "one-shot"


def _describe(job: ScheduledJob) -> str:
    parts = [job.id, job.cron, _scope(job)]
    if job.pending_delivery:
        parts.append(f"pending (attempts {job.attempts})")
    if job.last_fired:
        parts.append(f"last fired {job.last_fired}")
    head = " · ".join(parts)
    first_line = job.prompt.splitlines()[0][:120]
    return f"{head}\n  {first_line}"
