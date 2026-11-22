"""轨迹写入：两种执行模式共用。

一行一个 JSON 事件，**每条都 flush**：即使中途超时被杀，已经发生的步骤也留得下来。
事件类型：`start` / `turn_start` / `tool_call` / `tool_result` / `turn_end` / `error` / `end`。

设计见 `coding/docs/exec_environment.md`（旧模式在容器内写，新模式在宿主写，格式一致）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class TraceWriter:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._path.open("a", encoding="utf-8", newline="\n")

    def record(self, event_type: str, **fields: Any) -> None:
        # 参数名不要叫 kind：AgentError 事件本身就带 kind 字段，会撞成
        # "got multiple values for argument 'kind'"。
        payload = {"type": event_type, **fields}
        self._handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._handle.flush()

    def close(self, **payload: Any) -> None:
        if payload:
            self.record("end", **payload)
        self._handle.close()

    def __enter__(self) -> "TraceWriter":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if not self._handle.closed:
            self._handle.close()

    @property
    def path(self) -> Path:
        return self._path
