"""`codemode` 的离线测试：真实子进程 + 桥转发。"""

from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from gradus.ai.base import ToolSpec
from gradus.agent.types import ToolResult
from gradus.coding.permissions import Policy
from gradus.coding.registry import FunctionTool
from gradus.coding.tools.codemode import create_codemode_tool


def _tool(name: str, handler) -> FunctionTool:
    return FunctionTool(
        spec=ToolSpec(name=name, description="d", parameters={"type": "object"}),
        handler=handler,
        execution_mode="parallel",
    )


class _BadTool:
    spec = ToolSpec(name="bad", description="d", parameters={"type": "object"})
    execution_mode = "parallel"

    async def execute(self, call_id, arguments, on_update=None):
        return ToolResult("boom", is_error=True)


class CodemodeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _codemode(self, tools=(), confirm=None):
        return create_codemode_tool(list(tools), Policy(workspace=self.root), confirm)

    async def test_text_and_return(self):
        result = await self._codemode().execute(
            "c1",
            {"code": 'text("a")\ntext(2)\nreturn {"k": 1}'},
        )

        self.assertFalse(result.is_error)
        self.assertEqual(result.content, 'a\n2\n{"k": 1}')

    async def test_script_calls_tool(self):
        echo = _tool("echo", lambda arguments: arguments["x"])

        async def confirm(question: str) -> bool:
            return True

        result = await self._codemode([echo], confirm).execute(
            "c1", {"code": 'text(tools.echo(x="hi"))'}
        )

        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "hi")

    async def test_tool_error_raises_in_script(self):
        code = textwrap.dedent(
            """
            try:
                tools.bad()
            except RuntimeError as exc:
                text(str(exc))
            """
        )

        async def confirm(question: str) -> bool:
            return True

        result = await self._codemode([_BadTool()], confirm).execute("c1", {"code": code})

        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "boom")

    async def test_write_tools_ask_once_per_script(self):
        write = _tool("write_file", lambda arguments: "written")
        calls: list[str] = []

        async def confirm(question: str) -> bool:
            calls.append(question)
            return True

        code = textwrap.dedent(
            """
            tools.write_file(path="a.txt")
            tools.write_file(path="b.txt")
            text("done")
            """
        )
        result = await self._codemode([write], confirm).execute("c1", {"code": code})

        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "done")
        self.assertEqual(len(calls), 1)

    async def test_denied_confirmation_blocks_call(self):
        write = _tool("write_file", lambda arguments: "written")

        async def confirm(question: str) -> bool:
            return False

        code = textwrap.dedent(
            """
            try:
                tools.write_file(path="a.txt")
            except RuntimeError as exc:
                text(str(exc))
            """
        )
        result = await self._codemode([write], confirm).execute("c1", {"code": code})

        self.assertIn("permission denied", result.content)

    async def test_no_confirm_means_denied(self):
        write = _tool("write_file", lambda arguments: "written")
        code = textwrap.dedent(
            """
            try:
                tools.write_file(path="a.txt")
            except RuntimeError as exc:
                text(str(exc))
            """
        )

        result = await self._codemode([write]).execute("c1", {"code": code})

        self.assertIn("permission denied", result.content)

    async def test_import_is_rejected(self):
        result = await self._codemode().execute("c1", {"code": "import os"})

        self.assertTrue(result.is_error)
        self.assertIn("import is not allowed", result.content)

    async def test_dunder_attribute_is_rejected(self):
        result = await self._codemode().execute("c1", {"code": "x = ().__class__"})

        self.assertTrue(result.is_error)
        self.assertIn("__class__", result.content)

    async def test_unknown_tool_raises(self):
        code = textwrap.dedent(
            """
            try:
                tools.nope()
            except RuntimeError as exc:
                text(str(exc))
            """
        )
        result = await self._codemode().execute("c1", {"code": code})

        self.assertIn("unknown tool: nope", result.content)

    async def test_timeout_kills_script(self):
        result = await self._codemode().execute(
            "c1", {"code": "while True:\n    pass", "timeout": 0.2}
        )

        self.assertTrue(result.is_error)
        self.assertIn("timed out", result.content)

    async def test_empty_code_is_rejected(self):
        result = await self._codemode().execute("c1", {"code": "   "})

        self.assertTrue(result.is_error)
        self.assertEqual(result.content, "code is required")
