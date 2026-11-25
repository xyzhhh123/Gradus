"""`tool_search`：按关键词或名字从 deferred 工具里挑出要加载的工具。

只负责检索与登记，不执行选中的工具。真正把工具加进下一次请求由 `session.py`
的 `prepare_next_turn` 钩子完成，所以加载只在本次运行内有效。

检索形态照 Pi 的 `extensions/tool-search/tool.ts`：**BM25**、拆驼峰与下划线、
去停用词、简单复数词干化、把参数 schema 里的字段名与描述也算进检索文档。
分词与打分在 `coding/retrieval.py`（`recall` 共用）。

两处偏离 Pi（见 `docs/design.md` §5.9）：

1. 中文按 **2-gram** 切 + 小型双语同义词表——Pi 的分词器只认 `[a-z0-9]`；
2. **零命中给出目录、`load` 按名加载**——检索猜不中时不该让模型以为「没有这个能力」。
   目录只列名字与一句话，**不加载**；点名的才加载。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ...agent.base import AgentTool, ToolUpdateCallback
from ...agent.types import ToolCapability, ToolResult
from ...ai.base import ToolSpec
from ..retrieval import bm25, tokenize

DEFAULT_LIMIT = 8

#: 零命中时最多列多少个工具。MCP 池子可能上百个，全列会把上下文撑爆。
MAX_INDEX = 30
SEARCH_CAPABILITY = ToolCapability(effects=("search",), result_kind="diagnostic")

TOOL_SEARCH_SPEC = ToolSpec(
    name="tool_search",
    description=(
        "Find tools that are not declared yet (lazily-loaded MCP servers) and load them "
        "for the next call. Give `query` to search, or `load` with exact tool names. If "
        "the query matches nothing you get the full index — then load by name."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What you want to do, in a few words (English works best)",
            },
            "load": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Exact tool names to load, bypassing search, e.g. [\"complete_task\"]"
                ),
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "description": f"Maximum tools to load (default: {DEFAULT_LIMIT})",
            },
        },
    },
)


def _schema_text(schema: Any, parts: list[str]) -> None:
    """参数 schema 里的字段名与描述也进检索文档（字段名常是关键词）。"""
    if not isinstance(schema, dict):
        return
    description = schema.get("description")
    if isinstance(description, str):
        parts.append(description)
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, value in properties.items():
            parts.append(str(name))
            _schema_text(value, parts)
    _schema_text(schema.get("items"), parts)
    for key in ("anyOf", "oneOf", "allOf"):
        variants = schema.get(key)
        if isinstance(variants, list):
            for variant in variants:
                _schema_text(variant, parts)


def tool_search_document(spec: ToolSpec) -> str:
    """Searchable text for one tool."""
    parts = [spec.name, spec.name.replace("_", " "), spec.description]
    _schema_text(spec.parameters, parts)
    return " ".join(part for part in parts if part and part.strip())


def _first_line(description: str, limit: int = 110) -> str:
    """目录里每个工具只占一行：看得出是干什么的，又不至于占满上下文。"""
    line = " ".join(description.split())
    return line[:limit] + "…" if len(line) > limit else line


def rank_tools(
    pool: Sequence[AgentTool],
    query: str,
    feedback: Mapping[str, float] | None = None,
) -> list[AgentTool]:
    """Rank tools by BM25 relevance; callers apply their own result limit."""
    query_terms = tokenize(query)
    if not query_terms:
        return []

    corpus = [tokenize(tool_search_document(tool.spec)) for tool in pool]
    scores = bm25(query_terms, corpus)
    ranked = [
        (score + (feedback or {}).get(tool.spec.name, 0.0), index, tool)
        for index, (score, tool) in enumerate(zip(scores, pool))
        if score > 0
    ]
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [tool for _, _, tool in ranked]


def _index_text(pool: Sequence[AgentTool]) -> str:
    """零命中时的目录：名字 + 一句话，**不加载**任何工具。"""
    shown = list(pool)[:MAX_INDEX]
    header = f"没有匹配到。可加载的工具（{len(pool)} 个）"
    if len(pool) > MAX_INDEX:
        header += f"，先列前 {MAX_INDEX} 个；用更具体的英文词再搜"
    lines = [f"{header}："]
    lines.extend(
        f"- {tool.spec.name}: {_first_line(tool.spec.description)}" for tool in shown
    )
    lines.append('用 tool_search(load=["名字"]) 加载其中一个；加载后下一轮就能调用。')
    return "\n".join(lines)


class ToolSearchTool:
    """在 deferred 工具池里按 BM25 挑工具；也可以按名直接加载。"""

    spec = TOOL_SEARCH_SPEC
    execution_mode = "parallel"
    capability = SEARCH_CAPABILITY

    def __init__(self, pool: Sequence[AgentTool]) -> None:
        self._pool = list(pool)
        self._pending: list[AgentTool] = []
        self._pending_names: set[str] = set()
        self._stats: dict[str, dict[str, int]] = {}

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        query = str(arguments.get("query") or "").strip()
        raw_load = arguments.get("load")
        if raw_load is not None and not isinstance(raw_load, list):
            return ToolResult("load must be an array of tool names", is_error=True)
        wanted = [str(item).strip() for item in raw_load or [] if str(item).strip()]

        if not query and not wanted:
            return ToolResult("query or load is required", is_error=True)

        try:
            limit = max(1, int(arguments.get("limit") or DEFAULT_LIMIT))
        except (TypeError, ValueError):
            return ToolResult("limit must be an integer", is_error=True)

        by_name = {tool.spec.name: tool for tool in self._pool}
        matches: list[AgentTool] = []
        unknown: list[str] = []
        for name in wanted:
            tool = by_name.get(name)
            if tool is None:
                unknown.append(name)
            elif tool not in matches:
                matches.append(tool)

        # 名字加载优先：它不经过分词，任何语言都能用。
        if query:
            for tool in rank_tools(self._pool, query, self.feedback):
                if tool not in matches:
                    matches.append(tool)
        matches = matches[:limit]

        if matches:
            for tool in matches:
                name = tool.spec.name
                if name not in self._pending_names:
                    self._pending.append(tool)
                    self._pending_names.add(name)
                stats = self._stats.setdefault(
                    name,
                    {"searches": 0, "calls": 0, "successes": 0, "errors": 0},
                )
                stats["searches"] += 1

        lines = [f"{tool.spec.name}: {tool.spec.description}" for tool in matches]
        if matches:
            lines.append("These tools will be available on your next call.")
        if unknown:
            lines.append(
                f"没有这些按需工具：{', '.join(unknown)}（可能已经可用，或名字拼错）"
            )
        if not matches and not unknown:
            # 零命中：列目录而不是「没找到」——绝不能让它以为「没有这个能力」。
            return ToolResult(_index_text(self._pool))
        return ToolResult("\n".join(lines))

    def take_pending(self) -> list[AgentTool]:
        """取走本次待加载的工具，清空登记。"""
        pending, self._pending = self._pending, []
        self._pending_names.clear()
        return pending

    @property
    def feedback(self) -> dict[str, float]:
        """In-session ranking bonus derived from real tool outcomes."""
        result: dict[str, float] = {}
        for name, stats in self._stats.items():
            calls = stats["calls"]
            if not calls:
                continue
            success_rate = stats["successes"] / calls
            result[name] = success_rate * 2.0 + min(calls, 5) * 0.05
        return result

    def note_outcome(self, tool_name: str, is_error: bool) -> None:
        if tool_name == self.spec.name:
            return
        stats = self._stats.setdefault(
            tool_name,
            {"searches": 0, "calls": 0, "successes": 0, "errors": 0},
        )
        stats["calls"] += 1
        stats["successes" if not is_error else "errors"] += 1
