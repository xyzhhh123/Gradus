"""Generic completion gate for workspace-changing tasks."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ..agent.hooks import (
    FinishTurnContext,
    NextTurnUpdate,
    TurnAction,
)
from ..agent.types import ToolResult
from ..ai.types import UserMessage

_CHANGE_TOOLS = frozenset(
    {
        "write_file",
        "edit",
        "delete_file",
        "apply_patch",
    }
)
_VERIFY_COMMAND = re.compile(
    r"(?i)(?:"
    r"\b(?:pytest|py\.test|tox|nox)\b|"
    r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:test|lint|check|build|typecheck)\b|"
    r"\b(?:cargo|go|dotnet|mvn|gradle)\s+(?:test|check|build)\b|"
    r"\b(?:ruff|mypy|pyright|tsc|eslint|prettier)\b|"
    r"\bmake\s+(?:test|check|build)\b"
    r")"
)

COMPLETION_REVIEW = (
    "Before finishing, verify the workspace changes you just made. Derive the "
    "requirements from the user's request, run the most relevant test/build/lint "
    "or inspection command, and compare the evidence against each requirement. "
    "If some requirement cannot be verified, state the exact remaining gap "
    "instead of treating the change as complete."
)


class CompletionGate:
    """Require one evidence-oriented review after the latest workspace change."""

    def __init__(self) -> None:
        self.change_version = 0
        self.verified_version = 0
        self.prompted_version = 0
        self._pending = False

    def observe(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        result: ToolResult,
    ) -> None:
        if result.is_error:
            return
        if self._is_change(tool_name, result):
            self.change_version += 1
            self._pending = False
            return
        if tool_name in {"bash", "powershell"}:
            command = str(arguments.get("command") or "")
            if _VERIFY_COMMAND.search(command):
                self.verified_version = self.change_version

    async def finish_turn(self, context: FinishTurnContext) -> TurnAction | None:
        if context.assistant.stop_reason != "stop":
            return None
        if self.change_version <= self.verified_version:
            return None
        if self.change_version <= self.prompted_version:
            return None
        self.prompted_version = self.change_version
        self._pending = True
        return "continue"

    async def prepare_next_turn(
        self,
        context: Any,
    ) -> NextTurnUpdate | None:
        if not self._pending:
            return None
        self._pending = False
        return NextTurnUpdate(
            messages=(UserMessage(COMPLETION_REVIEW, origin="harness"),)
        )

    @staticmethod
    def _is_change(tool_name: str, result: ToolResult) -> bool:
        if tool_name in _CHANGE_TOOLS:
            return True
        value = result.structured_content
        return isinstance(value, dict) and bool(value.get("workspace_changes"))

