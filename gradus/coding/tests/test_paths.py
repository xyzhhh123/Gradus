"""Workspace-relative path contract tests."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from gradus.coding.paths import (
    WorkspacePath,
    WorkspacePathError,
    workspace_path,
    workspace_relative,
)


class WorkspacePathTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_relative_path_resolves_inside_root(self):
        parsed = WorkspacePath.parse(self.root, "src/pkg/a.py")

        self.assertEqual(parsed.relative.as_posix(), "src/pkg/a.py")
        self.assertEqual(parsed.resolved, self.root / "src" / "pkg" / "a.py")

    def test_backslashes_are_normalized(self):
        self.assertEqual(
            workspace_path(self.root, r"src\pkg\a.py"),
            self.root / "src" / "pkg" / "a.py",
        )

    def test_rejects_absolute_drive_unc_and_parent_paths(self):
        for value in (
            "/etc/passwd",
            r"C:\Windows\win.ini",
            r"\\server\share\a.py",
            "../outside.py",
            "src/../../outside.py",
        ):
            with self.subTest(value=value):
                with self.assertRaises(WorkspacePathError):
                    workspace_path(self.root, value)

    def test_symlink_escape_is_rejected(self):
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        link = self.root / "escape"
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable")

        with self.assertRaises(WorkspacePathError):
            workspace_path(self.root, "escape/secret.txt")

    def test_workspace_relative_rejects_external_target(self):
        with tempfile.TemporaryDirectory() as other:
            with self.assertRaises(WorkspacePathError):
                workspace_relative(self.root, other)

