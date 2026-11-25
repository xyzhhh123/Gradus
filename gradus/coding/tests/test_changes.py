"""Workspace modification ledger tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gradus.agent.hooks import AfterToolCallContext
from gradus.agent.types import ToolResult
from gradus.ai.types import AssistantMessage, ToolCallBlock
from gradus.coding.changes import WorkspaceChangeLedger
from gradus.coding.permissions import make_after_tool_call
from gradus.coding.session import SESSION_VERSION, SessionFile


class WorkspaceChangeLedgerTest(unittest.TestCase):
    def test_structured_patch_changes_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            written: list[dict] = []
            ledger = WorkspaceChangeLedger(directory, written.append)

            ledger.observe(
                "apply_patch",
                {"patch": "..."},
                ToolResult(
                    "ok",
                    structured_content={
                        "workspace_changes": [
                            {
                                "path": "src/a.py",
                                "action": "update",
                                "additions": 2,
                                "deletions": 1,
                            }
                        ]
                    },
                ),
            )

        self.assertEqual(len(ledger.records), 1)
        self.assertEqual(ledger.records[0].path, "src/a.py")
        self.assertEqual(ledger.records[0].additions, 2)
        self.assertEqual(written[0]["source"], "apply_patch")

    def test_file_and_shell_changes_are_inferred(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = WorkspaceChangeLedger(directory)

            ledger.observe(
                "write_file",
                {"path": "src/a.py"},
                ToolResult("ok"),
            )
            ledger.observe(
                "bash",
                {"command": "sed -i 's/a/b/' src/b.ts"},
                ToolResult("ok"),
            )

        self.assertEqual(
            [(item.action, item.path) for item in ledger.records],
            [
                ("write", "src/a.py"),
                ("shell-write", "src/b.ts"),
            ],
        )
        self.assertFalse(ledger.records[1].confirmed)

    def test_errors_are_not_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = WorkspaceChangeLedger(directory)

            ledger.observe(
                "write_file",
                {"path": "src/a.py"},
                ToolResult("denied", is_error=True),
            )

        self.assertEqual(ledger.records, ())


class ChangePersistenceTest(unittest.TestCase):
    def test_session_file_load_accepts_change_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            rows = [
                {
                    "type": "session",
                    "version": SESSION_VERSION,
                    "id": "s1",
                    "model": "fake",
                    "workspace": directory,
                },
                {
                    "type": "change",
                    "id": "e1",
                    "parentId": None,
                    "change": {
                        "source": "apply_patch",
                        "path": "a.py",
                        "action": "update",
                    },
                },
            ]
            path.write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n",
                encoding="utf-8",
            )

            loaded = SessionFile.load(path)

        self.assertEqual(loaded.messages, [])


class ChangeHookTest(unittest.IsolatedAsyncioTestCase):
    async def test_after_tool_hook_records_before_sanitizing(self):
        with tempfile.TemporaryDirectory() as directory:
            sink: list[dict] = []
            ledger = WorkspaceChangeLedger(directory, sink.append)
            hook = make_after_tool_call(ledger)

            await hook(
                AfterToolCallContext(
                    call=ToolCallBlock(
                        id="c1",
                        name="write_file",
                        arguments={"path": "a.py"},
                    ),
                    arguments={"path": "a.py"},
                    assistant=AssistantMessage(),
                    result=ToolResult("key=sk-abcdefghijklmnop1234"),
                    turn=0,
                )
            )

        self.assertEqual(sink[0]["path"], "a.py")


if __name__ == "__main__":
    unittest.main()
