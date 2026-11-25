"""Spill ownership and cleanup tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gradus.coding.sandbox import cleanup_temp
from gradus.coding.tools.spill import SpillStore


class SpillStoreTest(unittest.TestCase):
    def test_pointer_and_reference_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            store = SpillStore(root / ".gradus" / "spill", display_root=root)

            pointer = store.with_pointer(
                "x" * 500,
                100,
                preview_chars=20,
            )
            path = next(iter(store.references))

            self.assertIn(".gradus/spill/", pointer)
            self.assertNotIn(str(root), pointer)
            self.assertEqual(store.retain(path), 2)
            self.assertEqual(store.release(path), 1)
            self.assertTrue(path.exists())
            self.assertEqual(store.release(path), 0)
            self.assertFalse(path.exists())

    def test_cleanup_removes_referenced_files(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SpillStore(Path(directory) / "owned")
            path = store.spill("payload")

            store.cleanup()

            self.assertFalse(path.exists())
            self.assertFalse(store.directory.exists())

    def test_cleanup_temp_reclaims_dead_spill_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            owned = root / "gradus-spill-dead"
            owned.mkdir()
            (owned / ".owner").write_text("99999999", encoding="utf-8")
            (owned / "payload.txt").write_text("x", encoding="utf-8")

            removed = cleanup_temp(root=root)

            self.assertIn("gradus-spill-dead", removed)
            self.assertFalse(owned.exists())


if __name__ == "__main__":
    unittest.main()
