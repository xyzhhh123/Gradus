"""`recall_history`：把被压缩掉的历史读回模型。

纯逻辑在 `coding/history.py`（渲染、按 id 读回、按关键词检索、选择器兜底）；
这里只负责声明工具、把参数转给会话注入的 `RecallHooks.runner`。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ...agent.base import AgentTool, ToolUpdateCallback
from ...agent.types import ToolResult
from ...ai.base import ToolSpec
from ..history import DEFAULT_LIMIT, MAX_LIMIT


RECALL_HISTORY_SPEC = ToolSpec(
    name="recall_history",
    description=(
        "读回已经被压缩移出视野的历史。给 ids 读回指定条目的原文；"
        "给 query 在已压缩区段里按关键词检索。只能访问已被压缩的部分——"
        "还在上下文里的内容直接看上下文即可。id 见检索结果里的 # 前缀。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "条目 id，例如 [\"e42\"]；来自之前检索结果里的 #e42",
            },
            "query": {
                "type": "string",
                "description": "关键词；命中按出现次数排序",
            },
            "limit": {
                "type": "integer",
                "description": f"检索返回条数上限，默认 {DEFAULT_LIMIT}",
            },
        },
        "additionalProperties": False,
    },
)


@dataclass
class RecallHooks:
    """工具与会话之间的间接引用：会话在工具装配之后才构造出来。"""

    runner: Callable[
        [Sequence[str] | None, str, int], Awaitable[ToolResult]
    ] | None = None


class RecallHistoryTool:
    """把 `recall_history` 声明给模型；真正的检索与会话状态在 `RecallHooks.runner` 里。"""

    spec = RECALL_HISTORY_SPEC
    execution_mode = "parallel"

    def __init__(self, hooks: RecallHooks) -> None:
        self._hooks = hooks

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        runner = self._hooks.runner
        if runner is None:
            return ToolResult("recall_history 尚未就绪", is_error=True)

        raw_ids = arguments.get("ids")
        if raw_ids is not None and not isinstance(raw_ids, list):
            return ToolResult("ids 必须是字符串数组", is_error=True)
        ids = [str(item).strip() for item in raw_ids or [] if str(item).strip()]
        query = str(arguments.get("query") or "").strip()
        try:
            limit = max(1, min(int(arguments.get("limit") or DEFAULT_LIMIT), MAX_LIMIT))
        except (TypeError, ValueError):
            return ToolResult("limit 必须是整数", is_error=True)

        if ids and query:
            return ToolResult("ids 与 query 只能给一个", is_error=True)
        if not ids and not query:
            return ToolResult("需要 ids 或 query", is_error=True)
        return await runner(ids or None, query, limit)


