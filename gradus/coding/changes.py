"""Structured workspace modification ledger.

The ledger records what a mutating tool claims to have changed. It is
intentionally a provenance layer, not a Git replacement: records may contain
path/action counts inferred from tool arguments when a command cannot provide a
full diff.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..agent.types import ToolResult
from .permissions import detect_shell_source_writes


@dataclass(frozen=True)
class WorkspaceChange:
    """One observed workspace modification."""

    source: str
    action: str
    path: str
    additions: int = 0
    deletions: int = 0
    confirmed: bool = True

    def payload(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "action": self.action,
            "path": self.path,
            "additions": self.additions,
            "deletions": self.deletions,
            "confirmed": self.confirmed,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


class WorkspaceChangeLedger:
    """Collect workspace changes from successful tool calls."""

    def __init__(
        self,
        workspace: str | Path,
        sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self._sink = sink
        self._records: list[WorkspaceChange] = []

    @property
    def records(self) -> tuple[WorkspaceChange, ...]:
        return tuple(self._records)

    def observe(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        result: ToolResult,
    ) -> None:
        if result.is_error:
            return

        changes = _structured_changes(result)
        if changes:
            for item in changes:
                self._record(
                    WorkspaceChange(
                        source=tool_name,
                        action=str(item.get("action") or "update"),
                        path=str(item.get("path") or ""),
                        additions=_number(item.get("additions")),
                        deletions=_number(item.get("deletions")),
                    )
                )
            return

        path = arguments.get("path")
        if isinstance(path, str) and path:
            action = {
                "write_file": "write",
                "edit": "edit",
                "delete_file": "delete",
            }.get(tool_name)
            if action is not None:
                self._record(
                    WorkspaceChange(
                        source=tool_name,
                        action=action,
                        path=self._relative(path),
                        confirmed=True,
                    )
                )
                return

        if tool_name in {"bash", "powershell"}:
            command = str(arguments.get("command") or "")
            for path in detect_shell_source_writes(command):
                self._record(
                    WorkspaceChange(
                        source=tool_name,
                        action="shell-write",
                        path=path,
                        confirmed=False,
                    )
                )

    def _record(self, change: WorkspaceChange) -> None:
        if not change.path:
            return
        self._records.append(change)
        if self._sink is not None:
            self._sink(change.payload())

    def _relative(self, path: str) -> str:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        try:
            return candidate.resolve().relative_to(self.workspace).as_posix()
        except ValueError:
            return candidate.resolve().as_posix()


def _structured_changes(result: ToolResult) -> list[dict[str, Any]]:
    value = result.structured_content
    if not isinstance(value, dict):
        return []
    changes = value.get("workspace_changes")
    if not isinstance(changes, list):
        return []
    return [item for item in changes if isinstance(item, dict)]


def _number(value: Any) -> int:
    return value if isinstance(value, int) and value >= 0 else 0

