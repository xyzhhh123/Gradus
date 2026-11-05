"""`tool_search`：按关键词从 deferred 工具里挑出要加载的工具。

只负责检索与登记，不执行选中的工具。真正把工具加进下一次请求由 `session.py`
的 `prepare_next_turn` 钩子完成，所以加载只在本次运行内有效。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ...agent.base import AgentTool, ToolUpdateCallback
from ...agent.types import ToolResult
from ...ai.base import ToolSpec

DEFAULT_LIMIT = 8

TOOL_SEARCH_SPEC = ToolSpec(
    name="tool_search",
    description=(
        "Search MCP tools that are not declared yet, and load the best matches for "
        "your next call. Use this when you need a capability that is not currently "
        "in the tool list. Results become callable on the next turn."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What you want to do, in a few words",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "description": f"Maximum tools to load (default: {DEFAULT_LIMIT})",
            },
        },
        "required": ["query"],
    },
)


class ToolSearchTool:
    """在 deferred 工具池里按关键词打分，登记命中的工具。"""

    spec = TOOL_SEARCH_SPEC
    execution_mode = "parallel"

    def __init__(self, pool: Sequence[AgentTool]) -> None:
        self._pool = list(pool)
        self._pending: list[AgentTool] = []
        self._pending_names: set[str] = set()

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        query = str(arguments.get("query", "")).strip()
        if not query:
            return ToolResult("query is required", is_error=True)

        limit = max(1, int(arguments.get("limit") or DEFAULT_LIMIT))
        matches = _rank(self._pool, query)[:limit]

        if not matches:
            return ToolResult("(no matching tools)")

        for tool in matches:
            name = tool.spec.name
            if name not in self._pending_names:
                self._pending.append(tool)
                self._pending_names.add(name)

        lines = [f"{tool.spec.name}: {tool.spec.description}" for tool in matches]
        lines.append("These tools will be available on your next call.")
        return ToolResult("\n".join(lines))

    def take_pending(self) -> list[AgentTool]:
        """取走本次待加载的工具，清空登记。"""
        pending, self._pending = self._pending, []
        self._pending_names.clear()
        return pending


def _rank(pool: Sequence[AgentTool], query: str) -> list[AgentTool]:
    tokens = [token for token in query.lower().split() if token]
    scored: list[tuple[int, int, AgentTool]] = []

    for index, tool in enumerate(pool):
        name = tool.spec.name.lower()
        description = tool.spec.description.lower()
        score = 0
        for token in tokens:
            if token in name:
                score += 3
            if token in description:
                score += 1
        if score > 0:
            scored.append((score, index, tool))

    scored.sort(key=lambda item: (-item[0], item[1]))
    return [tool for _, _, tool in scored]
