"""`bash` / `powershell` 命令工具的离线测试。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gradus.coding.tools import shell
from gradus.coding.tools.shell import BASH_SPEC, ShellTool, create_shell_tools


class ShellToolTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        # 用当前解释器当执行器，参数前缀换成 python 的 -c，测试不依赖 bash。
        self.tool = ShellTool(self.root, sys.executable, ("-c",), BASH_SPEC)

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_runs_and_captures_output(self):
        result = await self.tool.execute("c1", {"command": "print('hi')"})

        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "hi\n")

    async def test_runs_in_workspace(self):
        result = await self.tool.execute("c1", {"command": "import os; print(os.getcwd())"})

        self.assertEqual(Path(result.content.strip()), self.root.resolve())

    async def test_exit_code_marks_error(self):
        result = await self.tool.execute(
            "c1", {"command": "import sys; sys.exit(3)"}
        )

        self.assertTrue(result.is_error)
        self.assertIn("[exit code 3]", result.content)

    async def test_timeout_kills_command(self):
        with patch.object(shell, "_timeout", lambda value: 0.2):
            result = await self.tool.execute(
                "c1", {"command": "import time; time.sleep(5)"}
            )

        self.assertTrue(result.is_error)
        self.assertIn("timed out", result.content)

    async def test_missing_command_and_bad_executable(self):
        missing = await self.tool.execute("c1", {})
        self.assertTrue(missing.is_error)
        self.assertIn("command is required", missing.content)

        broken = ShellTool(
            self.root, "definitely-not-a-real-command-xyz", ("-c",), BASH_SPEC
        )
        started = await broken.execute("c1", {"command": "x"})
        self.assertTrue(started.is_error)
        self.assertIn("Cannot start", started.content)

    async def test_long_output_is_truncated(self):
        result = await self.tool.execute("c1", {"command": "print('x' * 40000)"})

        self.assertIn("chars truncated", result.content)


class CreateShellToolsTest(unittest.TestCase):
    def test_no_shell_available(self):
        with (
            patch.object(shell.platform, "system", lambda: "Windows"),
            patch.object(shell.shutil, "which", lambda name: None),
        ):
            self.assertEqual(create_shell_tools("."), [])

    def test_windows_registers_both(self):
        def which(name: str) -> str | None:
            return {"bash": "/bin/bash", "pwsh": "/bin/pwsh"}.get(name)

        with (
            patch.object(shell.platform, "system", lambda: "Windows"),
            patch.object(shell.shutil, "which", which),
        ):
            names = [tool.spec.name for tool in create_shell_tools(".")]

        self.assertEqual(names, ["bash", "powershell"])

    def test_windows_skips_wsl_launcher(self):
        def which(name: str) -> str | None:
            return {
                "bash": r"C:\WINDOWS\system32\bash.exe",
                "pwsh": r"C:\Program Files\PowerShell\7\pwsh.exe",
            }.get(name)

        with (
            patch.object(shell.platform, "system", lambda: "Windows"),
            patch.object(shell.shutil, "which", which),
        ):
            names = [tool.spec.name for tool in create_shell_tools(".")]

        self.assertEqual(names, ["powershell"])

    def test_non_windows_has_no_powershell(self):
        def which(name: str) -> str | None:
            return {"bash": "/bin/bash", "pwsh": "/bin/pwsh"}.get(name)

        with (
            patch.object(shell.platform, "system", lambda: "Linux"),
            patch.object(shell.shutil, "which", which),
        ):
            names = [tool.spec.name for tool in create_shell_tools(".")]

        self.assertEqual(names, ["bash"])
