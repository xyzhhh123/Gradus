"""Offline tests for the path-contract A/B canary harness."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bench.path_contract_ab import (
    _legacy_workspace_path,
    _variant_mode,
    tasks,
)
from gradus.coding.paths import WorkspacePathError, workspace_path
from gradus.coding.session import BASE_PROMPT


class LegacyPathTest(unittest.TestCase):
    def test_legacy_accepts_absolute_and_parent_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            outside = Path(directory) / "outside.txt"

            self.assertEqual(
                _legacy_workspace_path(root, str(outside)),
                outside.resolve(),
            )
            self.assertEqual(
                _legacy_workspace_path(root, "../outside.txt"),
                outside.resolve(),
            )
            with self.assertRaises(WorkspacePathError):
                workspace_path(root, "../outside.txt")


class VariantModeTest(unittest.TestCase):
    def test_legacy_mode_is_process_local_and_restores_prompt(self):
        original = BASE_PROMPT

        with _variant_mode("legacy"):
            from gradus.coding import session

            self.assertNotEqual(session.BASE_PROMPT, original)
            self.assertIn("paths outside it are refused", session.BASE_PROMPT)

        from gradus.coding import session

        self.assertEqual(session.BASE_PROMPT, original)

    def test_far_case_has_ten_unique_tasks(self):
        names = [task.name for task in tasks()]

        self.assertEqual(len(names), 10)
        self.assertEqual(len(set(names)), 10)


if __name__ == "__main__":
    unittest.main()
