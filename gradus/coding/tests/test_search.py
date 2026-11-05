"""`grep` / `glob` 的离线测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gradus.coding.tools.search import GlobTool, GrepTool, create_search_tools


class SearchTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "src").mkdir()
        (self.root / "node_modules").mkdir()
        (self.root / "a.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
        (self.root / "src" / "c.py").write_text("foo bar\n", encoding="utf-8")
        (self.root / "b.txt").write_text("Hello World\nhello again\n", encoding="utf-8")
        (self.root / "node_modules" / "x.js").write_text("foo\n", encoding="utf-8")
        (self.root / "binary.dat").write_bytes(b"\x00foo\n")
        self.grep = GrepTool(self.root)
        self.glob = GlobTool(self.root)

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_grep_labels_matches_and_skips_ignored(self):
        result = await self.grep.execute("c1", {"pattern": "foo"})

        self.assertFalse(result.is_error)
        self.assertIn("a.py:1: def foo():", result.content)
        self.assertIn("src/c.py:1: foo bar", result.content)
        self.assertNotIn("node_modules", result.content)
        self.assertNotIn("binary.dat", result.content)

    async def test_grep_literal(self):
        result = await self.grep.execute(
            "c1", {"pattern": "foo()", "literal": True}
        )

        self.assertIn("def foo():", result.content)
        self.assertNotIn("foo bar", result.content)

    async def test_grep_ignore_case(self):
        result = await self.grep.execute(
            "c1", {"pattern": "hello", "ignoreCase": True}
        )

        self.assertIn("b.txt:1: Hello World", result.content)
        self.assertIn("b.txt:2: hello again", result.content)

    async def test_grep_glob_filter(self):
        result = await self.grep.execute("c1", {"pattern": "hello", "glob": "*.txt"})

        self.assertIn("b.txt", result.content)
        self.assertNotIn("a.py", result.content)

    async def test_grep_context_lines(self):
        result = await self.grep.execute(
            "c1", {"pattern": "return 1", "context": 1}
        )

        self.assertIn("a.py:1- def foo():", result.content)
        self.assertIn("a.py:2:     return 1", result.content)

    async def test_grep_limit_is_reported(self):
        result = await self.grep.execute("c1", {"pattern": "foo", "limit": 1})

        self.assertIn("results truncated", result.content)

    async def test_grep_invalid_regex(self):
        result = await self.grep.execute("c1", {"pattern": "("})

        self.assertTrue(result.is_error)
        self.assertIn("Invalid regex", result.content)

    async def test_grep_missing_path(self):
        result = await self.grep.execute("c1", {"pattern": "x", "path": "nope"})

        self.assertTrue(result.is_error)
        self.assertIn("not found", result.content)

    async def test_grep_no_matches(self):
        result = await self.grep.execute("c1", {"pattern": "zzz"})

        self.assertEqual(result.content, "(no matches)")

    async def test_glob_matches_and_skips_ignored(self):
        result = await self.glob.execute("c1", {"pattern": "**/*.py"})

        self.assertEqual(result.content, "a.py\nsrc/c.py")
        self.assertNotIn("node_modules", result.content)

    async def test_glob_limit_is_reported(self):
        result = await self.glob.execute("c1", {"pattern": "**/*.py", "limit": 1})

        self.assertEqual(result.content.splitlines()[0], "a.py")
        self.assertIn("results truncated", result.content)

    async def test_glob_rejects_file(self):
        result = await self.glob.execute("c1", {"pattern": "*", "path": "a.py"})

        self.assertTrue(result.is_error)
        self.assertIn("Not a directory", result.content)

    def test_factory_names(self):
        names = [tool.spec.name for tool in create_search_tools(self.root)]

        self.assertEqual(names, ["grep", "glob"])
