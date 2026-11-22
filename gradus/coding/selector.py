"""检索兜底：让模型在一个**独立的小窗口**里从候选清单里挑。

为什么需要它：`tool_search` / `recall` 的关键词检索是字面匹配，靠同义词表把中文桥到
英文。表覆盖不到的说法会落空——这时开一次独立请求，只给它「候选清单 + 用户意图」，
让它挑出相关的条目。代价小：候选清单几百 token，而主对话一轮是上万 token。

护栏（缺一不可，否则会变成看不见的费用黑洞）：

1. 只在**零命中**时调用，正常命中不动它；
2. **一次调用最多一次**，不允许递归（挑不出来就返回原文目录，结束）；
3. 输出必须是 JSON，且 id 必须存在于候选里，否则整条丢弃；
4. 独立超时与输出上限；
5. 用量回传（`SelectorResult.usage`），否则以后说不清这笔钱。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

from ..ai.base import Request, expand_headers
from ..ai.types import (
    StreamDone,
    StreamError,
    TextDelta,
    UserMessage,
    Usage,
)
from ..ai.providers import create_provider
from .config import Settings

#: 候选清单的条数上限：太多会把小窗口本身撑大，太少又挑不出来。
MAX_CANDIDATES = 30

#: 每条候选给多少字符做判据。
GIST_CHARS = 80

#: 一次最多挑几条：挑太多会把 recall 的读取预算吃掉（`recall_tokens` 每代一份）。
MAX_PICKS = 4

SELECTOR_SYSTEM = (
    "You are a retrieval selector. You get a list of entries (id: gist) and a query. "
    "Pick at most 4 entries whose gist most likely contains the answer, most relevant "
    'first. Reply with JSON only: {"ids": ["e1"]}. If nothing fits, reply {"ids": []}. '
    "Do not explain and do not show reasoning."
)


@dataclass(frozen=True)
class SelectorResult:
    """选择器的结果：挑中的 id（已校验）与这次调用的用量。"""

    ids: tuple[str, ...]
    usage: Usage | None = None


def _parse_ids(
    text: str, allowed: Sequence[str], limit: int = MAX_PICKS
) -> tuple[str, ...]:
    """从模型输出里取出 id：容忍代码围栏与多余文字，只留候选里存在的。"""
    match = re.search(r"\{.*\}", text, re.S)
    if match is None:
        return ()
    try:
        payload = json.loads(match.group(0))
    except (TypeError, ValueError):
        return ()
    raw = payload.get("ids") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return ()

    seen: dict[str, None] = {}
    for item in raw:
        name = str(item).strip()
        if name in allowed and name not in seen:
            seen[name] = None
        if len(seen) >= limit:
            break
    return tuple(seen)


async def select_ids(
    settings: Settings,
    query: str,
    candidates: Sequence[tuple[str, str]],
    *,
    client=None,
    max_output_tokens: int = 600,
) -> SelectorResult:
    """在小窗口里挑条目；失败一律返回空结果，绝不抛给调用方。"""
    if not candidates:
        return SelectorResult(())

    lines = [f"- {entry_id}: {gist}" for entry_id, gist in candidates[:MAX_CANDIDATES]]
    prompt = f"查询：{query}\n\n候选：\n" + "\n".join(lines)

    parts: list[str] = []
    usage: Usage | None = None
    try:
        provider = create_provider(
            settings.spec,
            settings.api_key,
            client=client,
            headers=expand_headers(settings.headers),
        )
        request = Request(
            system=SELECTOR_SYSTEM,
            messages=[UserMessage(content=prompt)],
            max_output_tokens=max_output_tokens,
            temperature=0.0,
        )
        async for event in provider.stream(request):
            if isinstance(event, TextDelta):
                parts.append(event.delta)
            elif isinstance(event, StreamDone):
                usage = event.usage
            elif isinstance(event, StreamError):
                return SelectorResult((), usage)
    except Exception:  # noqa: BLE001 - 兜底路径不允许把异常抛出去
        return SelectorResult((), usage)

    allowed = [entry_id for entry_id, _ in candidates]
    return SelectorResult(_parse_ids("".join(parts), allowed), usage)
