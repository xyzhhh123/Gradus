"""`tools/files.py` 的离线测试。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from gradus.coding.tools import files
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


class PathLockTest(unittest.IsolatedAsyncioTestCase):
    """同一个文件的「读-改-写」要排队：并行批次里不能互相覆盖。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.tools = {tool.spec.name: tool for tool in create_file_tools(self.root)}
        self.path = self.root / "a.txt"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_same_path_shares_one_lock(self):
        locks = files.PathLocks()
        target = self.root / "a.txt"

        self.assertIs(locks.lock_for(target), locks.lock_for(target))
        self.assertIsNot(locks.lock_for(target), locks.lock_for(self.root / "b.txt"))

    async def test_lock_is_mutually_exclusive(self):
        locks = files.PathLocks()
        target = self.root / "a.txt"
        held = locks.lock_for(target)
        await held.acquire()

        async def second_attempt() -> bool:
            async with locks.lock_for(target):
                return True

        task = asyncio.create_task(second_attempt())
        await asyncio.sleep(0)
        # 第一个持有期间，第二个必须拿不到
        self.assertFalse(task.done())

        held.release()
        self.assertTrue(await asyncio.wait_for(task, timeout=1))

    async def test_concurrent_edits_do_not_lose_updates(self):
        self.path.write_text("alpha\nbeta\n", encoding="utf-8")
        reader = self.tools["read_file"]
        editor = self.tools["edit"]
        await reader.execute("c0", {"path": "a.txt"})

        first, second = await asyncio.gather(
            editor.execute(
                "c1",
                {"path": "a.txt", "edits": [{"old_text": "alpha", "new_text": "ALPHA"}]},
            ),
            editor.execute(
                "c1",
                {"path": "a.txt", "edits": [{"old_text": "beta", "new_text": "BETA"}]},
            ),
        )

        self.assertFalse(first.is_error, first.content)
        self.assertFalse(second.is_error, second.content)
        # 不加锁时后写的一方会覆盖前一方：两次替换只剩一个生效
        self.assertEqual(self.path.read_text("utf-8"), "ALPHA\nBETA\n")

    async def test_concurrent_writes_to_one_path_end_up_consistent(self):
        writer = self.tools["write_file"]

        await asyncio.gather(
            writer.execute("c1", {"path": "a.txt", "content": "one"}),
            writer.execute("c1", {"path": "a.txt", "content": "two"}),
        )

        self.assertIn(self.path.read_text("utf-8"), {"one", "two"})


if __name__ == "__main__":
    unittest.main()


class LfWriteTest(unittest.TestCase):
    """写文件必须落 LF：CRLF 会让 git diff 把整棵树算成改动。"""

    def test_write_lf_keeps_lf(self):
        import tempfile
        from pathlib import Path as _Path

        from gradus.coding.tools.files import _write_lf

        with tempfile.TemporaryDirectory() as tmp:
            target = _Path(tmp) / "a.txt"
            _write_lf(target, "one\ntwo\n")
            self.assertEqual(target.read_bytes(), b"one\ntwo\n")
