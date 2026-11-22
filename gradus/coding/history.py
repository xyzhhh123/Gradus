"""召回历史的纯逻辑：把被压缩掉的消息渲染、按 id 读回、按关键词检索。

工具适配器在 `tools/recall_history.py`；这里不依赖 agent 的工具类型，只用消息与
`retrieval`，所以会话层可以放心 import（`tools/` 只放"包成 AgentTool"的适配器）。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from ..ai.types import (
    AssistantMessage,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultMessage,
    UserMessage,
)

from .retrieval import bm25, surface_terms, tokenize

DEFAULT_LIMIT = 8
MAX_LIMIT = 20
SNIPPET_CHARS = 240


class RecallError(ValueError):
    """召回请求本身不合法（id 不存在、落在可见区、参数冲突）。"""


def render(message: Message) -> str:
    """把一条消息拍成可读文本；与压缩时的序列化口径一致。"""
    if isinstance(message, UserMessage):
        label = "用户" if message.origin == "user" else "系统通知"
        return f"[{label}] {message.content}"
    if isinstance(message, ToolResultMessage):
        return f"[工具结果] {message.content}"

    parts: list[str] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            parts.append(block.text)
        elif isinstance(block, ToolCallBlock):
            parts.append(f"[调用 {block.name}] {_dump(block.arguments)}")
    return "[助手] " + "\n".join(parts)


def read_ids(
    history: Sequence[Message],
    ids: Sequence[str | None],
    covered: int,
    wanted: Sequence[str],
) -> str:
    """按条目 id 读回原文；id 不存在或落在可见区时抛 `RecallError`。"""
    if not wanted:
        raise RecallError("recall: 没有给出 ids")

    index_by_id = {entry_id: position for position, entry_id in enumerate(ids) if entry_id}
    chunks: list[str] = []
    for entry_id in wanted:
        position = index_by_id.get(entry_id)
        if position is None:
            raise RecallError(f"recall: 找不到条目 {entry_id!r}")
        if position >= covered:
            raise RecallError(
                f"recall: {entry_id} 还在上下文里，直接读上下文即可"
            )
        chunks.append(f"#{entry_id}\n{render(history[position])}")
    return "\n\n".join(chunks)


def search_positions(
    history: Sequence[Message],
    ids: Sequence[str | None],
    covered: int,
    query: str,
    limit: int = DEFAULT_LIMIT,
    cache: dict[tuple[str, int], list[str]] | None = None,
) -> list[int] | None:
    """��关键词检索已压缩区段，按分数返回位置；没有任何命中返回 `None`。

    分词与打分在 `retrieval`：中文按 2-gram 切、同义词映射到英文词，
    所以「重试次数」这类查询不会因为原文是英文而整句落空。
    `cache` 存每条的**分词结果**（键是 `(条目 id, 正文长度)`），已压缩的消息不会再变。
    """
    query = query.strip()
    if not query:
        raise RecallError("recall: 没有给出 query")

    terms = tokenize(query)
    if not terms:
        return None

    corpus: list[list[str]] = []
    for position in range(min(covered, len(history))):
        text = render(history[position])
        if cache is None:
            corpus.append(tokenize(text))
            continue
        key = (ids[position] or str(position), len(text))
        cached = cache.get(key)
        if cached is None:
            cached = tokenize(text)
            cache[key] = cached
        corpus.append(cached)
    scores = bm25(terms, corpus)

    scores_scored = [
        (score, position) for position, score in enumerate(scores) if score > 0
    ]
    if not scores_scored:
        return None
    scores_scored.sort(key=lambda item: (-item[0], item[1]))
    return [position for _, position in scores_scored[: max(1, min(limit, MAX_LIMIT))]]


def render_hits(
    history: Sequence[Message],
    ids: Sequence[str | None],
    query: str,
    positions: Sequence[int],
) -> str:
    """把命中的位置排版成工具结果：`#id` + 命中处的片段。"""
    terms = tokenize(query)
    # 定位片段要用「原文字面可能出现的词」：`terms` 经过同义词映射（重试 → retry），
    # 拿去中文原文里找不到，片段会退回开头。
    anchors = list(dict.fromkeys([*terms, *surface_terms(query)]))

    lines = [f"已压缩区段里匹配 {query!r} 的位置："]
    for position in positions:
        entry_id = ids[position] or str(position)
        snippet = _snippet(render(history[position]), anchors)
        lines.append(f"\n#{entry_id}\n{snippet}")
    return "\n".join(lines)


def search(
    history: Sequence[Message],
    ids: Sequence[str | None],
    covered: int,
    query: str,
    limit: int = DEFAULT_LIMIT,
    cache: dict[tuple[str, int], list[str]] | None = None,
) -> str:
    """在已压缩区段里按关键词检索；没有任何命中时返回说明而不是错误。"""
    positions = search_positions(history, ids, covered, query, limit, cache)
    if not positions:
        return f"没有命中 {query!r}；已压缩的 {covered} 条都搜过了。"
    return render_hits(history, ids, query, positions)


def candidates(
    history: Sequence[Message],
    ids: Sequence[str | None],
    covered: int,
    *,
    gist_chars: int = 80,
    limit: int = 30,
) -> list[tuple[str, str]]:
    """给选择器用的候选清单：`(条目 id, 前 80 字)`。不需要改压缩状态格式。"""
    out: list[tuple[str, str]] = []
    for position in range(min(covered, len(history))):
        text = " ".join(render(history[position]).split())
        gist = text[:gist_chars] + ("…" if len(text) > gist_chars else "")
        out.append((ids[position] or str(position), gist))
        if len(out) >= limit:
            break
    return out


async def search_with_selector(
    history: Sequence[Message],
    ids: Sequence[str | None],
    covered: int,
    query: str,
    limit: int = DEFAULT_LIMIT,
    *,
    cache: dict[tuple[str, int], list[str]] | None = None,
    select: Callable[[str, list[tuple[str, str]]], Awaitable[Sequence[str]]] | None = None,
) -> tuple[str, bool]:
    """先走关键词；零命中且有 `select` 时，再让模型在小窗口里挑。

    返回 `(文本, 是否用了选择器)`——调用方据此记录用量与预算。
    """
    positions = search_positions(history, ids, covered, query, limit, cache)
    if positions:
        return render_hits(history, ids, query, positions), False

    if select is not None:
        picked = list(await select(query, candidates(history, ids, covered)))
        if picked:
            try:
                text = read_ids(history, ids, covered, picked)
            except RecallError:
                text = ""
            if text:
                return f"关键词没有命中 {query!r}，按选择器挑的条目取回：\n\n{text}", True

    return f"没有命中 {query!r}；已压缩的 {covered} 条都搜过了。", False


def _snippet(text: str, terms: Sequence[str], width: int = SNIPPET_CHARS) -> str:
    """截一段命中最密集的片段；找不到就取开头。"""
    lowered = text.lower()
    anchor = min(
        (lowered.find(term) for term in terms if term in lowered),
        default=0,
    )
    start = max(0, anchor - width // 3)
    return text[start : start + width]


def _dump(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)