"""`check_background`：查询后台命令的状态与输出。

后台任务完成时会自动送达一条 `<task_notification>`，因此本工具不是必需路径；
它的用途是“任务还在跑，我想看看进度或中间输出”，以及跨运行（上一次运行留下的
任务）时主动取回结果。

只读：不启动、不取消、不改变任务状态。
"""

from __future__ import annotations

from typing import Any

from ...agent.base import AgentTool, ToolUpdateCallback
from ...agent.types import ToolResult
from ...ai.base import ToolSpec
from ..background import BackgroundTasks

DEFAULT_TAIL_LINES = 60
MAX_TAIL_LINES = 500

CHECK_BACKGROUND_SPEC = ToolSpec(
    name="check_background",
    description=(
        "Check background commands started in this session. Omit task_id to list "
        "tasks with their status. Pass task_id to get status, exit code, "
        "and the tail of its output. Completed tasks are also reported "
        "automatically as a task notification."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Task id such as bg_0001; omit to list all tasks",
            },
            "tail_lines": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_TAIL_LINES,
                "description": (
                    f"Output lines to return for a single task "
                    f"(default: {DEFAULT_TAIL_LINES})"
                ),
            },
        },
    },
)


class CheckBackgroundTool:
    """读 `BackgroundTasks` 的任务表。"""

    spec = CHECK_BACKGROUND_SPEC
    execution_mode = "parallel"

    def __init__(self, background: BackgroundTasks) -> None:
        self._background = background

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        raw_id = arguments.get("task_id")
        task_id = raw_id.strip() if isinstance(raw_id, str) and raw_id.strip() else None
        return ToolResult(self._background.snapshot(task_id, _tail_lines(arguments)))


def create_check_background_tool(background: BackgroundTasks) -> AgentTool:
    return CheckBackgroundTool(background)


def _tail_lines(arguments: dict[str, Any]) -> int:
    value = arguments.get("tail_lines")
    if isinstance(value, bool) or not isinstance(value, int):
        return DEFAULT_TAIL_LINES
    return min(max(value, 1), MAX_TAIL_LINES)
