"""`tools/files.py` 的离线测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gradus.coding.tools.files import (
    MAX_LINES,
    create_file_tools,
    create_read_only_tools,
)


class FileToolsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.tools = {tool.spec.name: tool for tool in create_read_only_tools(self.root)}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def _read(self, **arguments):
        return await self.tools["read_file"].execute("call_1", arguments)

    async def _list(self, **arguments):
        return await self.tools["list_dir"].execute("call_1", arguments)

    def _write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    async def test_read_returns_content(self):
        self._write("a.txt", "alpha\nbeta")

        result = await self._read(path="a.txt")

        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "alpha\nbeta")

    async def test_read_honours_offset_and_limit(self):
        self._write("a.txt", "alpha\nbeta\ngamma\ndelta")

        result = await self._read(path="a.txt", offset=2, limit=2)

        self.assertFalse(result.is_error)
        self.assertEqual(
            result.content,
            "beta\ngamma\n\n[1 more lines in file. Use offset=4 to continue.]",
        )

    async def test_read_truncates_by_lines(self):
        self._write("big.txt", "\n".join(f"line {i}" for i in range(1, 2101)))

        result = await self._read(path="big.txt")

        self.assertIn(f"line {MAX_LINES}", result.content)
        self.assertNotIn(f"line {MAX_LINES + 1}", result.content)
        self.assertIn(f"Showing lines 1-{MAX_LINES} of 2100", result.content)
        self.assertIn(f"Use offset={MAX_LINES + 1}", result.content)

    async def test_read_truncates_by_bytes(self):
        self._write("wide.txt", "\n".join("x" * 1000 for _ in range(100)))

        result = await self._read(path="wide.txt")

        self.assertIn("Use offset=", result.content)
        self.assertLess(len(result.content.encode("utf-8")), 50 * 1024 + 200)

    async def test_read_handles_oversized_first_line(self):
        self._write("one-line.txt", "x" * 60_000)

        result = await self._read(path="one-line.txt")

        self.assertFalse(result.is_error)
        self.assertIn("[Line 1 truncated to 50KB]", result.content)

    async def test_read_reports_missing_file(self):
        result = await self._read(path="missing.txt")

        self.assertTrue(result.is_error)
        self.assertIn("File not found", result.content)

    async def test_read_rejects_directory(self):
        (self.root / "docs").mkdir()

        result = await self._read(path="docs")

        self.assertTrue(result.is_error)
        self.assertIn("Not a file", result.content)

    async def test_read_reports_offset_beyond_end(self):
        self._write("a.txt", "alpha")

        result = await self._read(path="a.txt", offset=10)

        self.assertTrue(result.is_error)
        self.assertIn("beyond end of file", result.content)

    async def test_read_accepts_absolute_path_outside_root(self):
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other) / "outside.txt"
            outside.write_text("outside", encoding="utf-8")

            result = await self._read(path=str(outside))

        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "outside")

    async def test_list_sorts_entries_and_marks_directories(self):
        self._write("b.txt", "")
        self._write("a.txt", "")
        self._write(".env", "")
        (self.root / "sub").mkdir()

        result = await self._list()

        self.assertFalse(result.is_error)
        self.assertEqual(result.content, ".env\na.txt\nb.txt\nsub/")

    async def test_list_reports_empty_directory(self):
        result = await self._list()

        self.assertEqual(result.content, "(empty directory)")

    async def test_list_honours_limit(self):
        for name in ("a.txt", "b.txt", "c.txt"):
            self._write(name, "")

        result = await self._list(limit=2)

        self.assertEqual(
            result.content,
            "a.txt\nb.txt\n\n[2 entries limit reached. Use limit=4 for more]",
        )

    async def test_list_reports_missing_path(self):
        result = await self._list(path="missing")

        self.assertTrue(result.is_error)
        self.assertIn("Path not found", result.content)

    async def test_list_rejects_file(self):
        self._write("a.txt", "")

        result = await self._list(path="a.txt")

        self.assertTrue(result.is_error)
        self.assertIn("Not a directory", result.content)


class WriteToolsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.tools = {tool.spec.name: tool for tool in create_file_tools(self.root)}
        self.path = self.root / "a.txt"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def _write(self, **arguments):
        return await self.tools["write_file"].execute("call_1", arguments)

    async def _edit(self, **arguments):
        return await self.tools["edit"].execute("call_1", arguments)

    async def _read(self, **arguments):
        return await self.tools["read_file"].execute("call_1", arguments)

    async def test_write_creates_file_and_parents(self):
        result = await self._write(path="nested/dir/a.txt", content="alpha")

        self.assertFalse(result.is_error)
        self.assertEqual((self.root / "nested/dir/a.txt").read_text("utf-8"), "alpha")
        self.assertIn("Wrote 5 bytes", result.content)

    async def test_write_overwrites_existing_file(self):
        self.path.write_text("old", encoding="utf-8")

        result = await self._write(path="a.txt", content="new")

        self.assertFalse(result.is_error)
        self.assertEqual(self.path.read_text("utf-8"), "new")

    async def test_write_rejects_directory(self):
        (self.root / "docs").mkdir()

        result = await self._write(path="docs", content="x")

        self.assertTrue(result.is_error)
        self.assertIn("Not a file", result.content)

    async def test_edit_requires_prior_read(self):
        self.path.write_text("alpha", encoding="utf-8")

        result = await self._edit(
            path="a.txt", edits=[{"old_text": "alpha", "new_text": "beta"}]
        )

        self.assertTrue(result.is_error)
        self.assertIn("Read a.txt before editing", result.content)

    async def test_edit_replaces_after_read(self):
        self.path.write_text("alpha", encoding="utf-8")
        await self._read(path="a.txt")

        result = await self._edit(
            path="a.txt", edits=[{"old_text": "alpha", "new_text": "beta"}]
        )

        self.assertFalse(result.is_error)
        self.assertEqual(self.path.read_text("utf-8"), "beta")

    async def test_edit_applies_multiple_disjoint_edits(self):
        self.path.write_text("alpha beta gamma", encoding="utf-8")
        await self._read(path="a.txt")

        result = await self._edit(
            path="a.txt",
            edits=[
                {"old_text": "beta", "new_text": "B"},
                {"old_text": "gamma", "new_text": "G"},
            ],
        )

        self.assertFalse(result.is_error)
        self.assertEqual(self.path.read_text("utf-8"), "alpha B G")

    async def test_edit_rejects_non_unique_old_text(self):
        self.path.write_text("foo foo", encoding="utf-8")
        await self._read(path="a.txt")

        result = await self._edit(
            path="a.txt", edits=[{"old_text": "foo", "new_text": "bar"}]
        )

        self.assertTrue(result.is_error)
        self.assertIn("appears 2 times", result.content)

    async def test_edit_rejects_missing_old_text(self):
        self.path.write_text("alpha", encoding="utf-8")
        await self._read(path="a.txt")

        result = await self._edit(
            path="a.txt", edits=[{"old_text": "zzz", "new_text": "x"}]
        )

        self.assertTrue(result.is_error)
        self.assertIn("not found", result.content)

    async def test_edit_rejects_overlapping_edits(self):
        self.path.write_text("abcdef", encoding="utf-8")
        await self._read(path="a.txt")

        result = await self._edit(
            path="a.txt",
            edits=[
                {"old_text": "abc", "new_text": "X"},
                {"old_text": "cde", "new_text": "Y"},
            ],
        )

        self.assertTrue(result.is_error)
        self.assertIn("overlap", result.content)

    async def test_edit_rejects_empty_edits(self):
        self.path.write_text("alpha", encoding="utf-8")
        await self._read(path="a.txt")

        result = await self._edit(path="a.txt", edits=[])

        self.assertTrue(result.is_error)
        self.assertIn("non-empty array", result.content)

    async def test_write_marks_file_as_read(self):
        await self._write(path="a.txt", content="alpha")

        result = await self._edit(
            path="a.txt", edits=[{"old_text": "alpha", "new_text": "beta"}]
        )

        self.assertFalse(result.is_error)
        self.assertEqual(self.path.read_text("utf-8"), "beta")

    async def test_delete_removes_file(self):
        self.path.write_text("x", encoding="utf-8")

        result = await self.tools["delete_file"].execute(
            "call_1", {"path": "a.txt"}
        )

        self.assertFalse(result.is_error)
        self.assertIn("Deleted a.txt", result.content)
        self.assertFalse(self.path.exists())

    async def test_delete_rejects_directory(self):
        (self.root / "docs").mkdir()

        result = await self.tools["delete_file"].execute(
            "call_1", {"path": "docs"}
        )

        self.assertTrue(result.is_error)
        self.assertIn("Not a file", result.content)

    async def test_delete_reports_missing_file(self):
        result = await self.tools["delete_file"].execute(
            "call_1", {"path": "nope.txt"}
        )

        self.assertTrue(result.is_error)
        self.assertIn("File not found", result.content)


if __name__ == "__main__":
    unittest.main()
