"""扩展加载的离线测试（不联网）。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gradus.ai.base import ModelSpec
from gradus.coding import extensions, session
from gradus.coding.config import Settings
from gradus.coding.permissions import Policy
from gradus.coding.tools.files import FileTracker

TOOL_TEMPLATE = """
from gradus.ai.base import ToolSpec
from gradus.coding.registry import FunctionTool


def create_tool():
    return FunctionTool(
        ToolSpec(
            name="{name}",
            description="{description}",
            parameters={{"type": "object"}},
        ),
        lambda arguments: "ok",
    )
"""


def _settings() -> Settings:
    return Settings(
        spec=ModelSpec(
            provider="anthropic",
            model="claude-test",
            base_url="https://api.anthropic.com",
            context_window=200_000,
            max_output_tokens=8_192,
        ),
        api_key="sk-test",
    )


class ExtensionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.folder = self.root / "extensions"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _tool(
        self,
        name: str,
        filename: str | None = None,
        description: str = "echo",
    ) -> Path:
        path = self.folder / "tools" / (filename or f"{name}.py")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            TOOL_TEMPLATE.format(name=name, description=description), encoding="utf-8"
        )
        return path

    def _command(self, name: str, body: str) -> None:
        path = self.folder / "commands" / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def _skill(self, name: str, description: str, body: str) -> None:
        path = self.folder / "skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n{body}",
            encoding="utf-8",
        )

    def _load(self, *, enabled: bool = True) -> extensions.Extensions:
        return extensions.load(self.root, enabled=enabled, project_dir=self.folder)

    def test_missing_directory_is_empty(self):
        loaded = self._load()

        self.assertEqual(loaded.tools, ())
        self.assertEqual(loaded.commands, ())
        self.assertEqual(loaded.skills, ())
        self.assertEqual(loaded.warnings, ())

    def test_disabled_returns_empty(self):
        self._tool("echo")

        self.assertEqual(self._load(enabled=False).tools, ())

    def test_loads_tools_commands_and_skills(self):
        self._tool("echo")
        self._command("review", "look at $ARGUMENTS")
        self._skill("lint", "check style", "run the linter")

        loaded = self._load()

        self.assertEqual([tool.spec.name for tool in loaded.tools], ["echo"])
        self.assertEqual([item.name for item in loaded.commands], ["review"])
        self.assertEqual([item.name for item in loaded.skills], ["lint"])

    def test_broken_tool_only_warns(self):
        self._tool("good")
        bad = self.folder / "tools" / "bad.py"
        bad.write_text("def create_tool(:\n", encoding="utf-8")

        loaded = self._load()

        self.assertEqual([tool.spec.name for tool in loaded.tools], ["good"])
        self.assertEqual(len(loaded.warnings), 1)
        self.assertIn("bad.py", loaded.warnings[0])

    def test_missing_factory_only_warns(self):
        path = self.folder / "tools" / "empty.py"
        path.parent.mkdir(parents=True)
        path.write_text("value = 1\n", encoding="utf-8")

        loaded = self._load()

        self.assertEqual(loaded.tools, ())
        self.assertIn("create_tool", loaded.warnings[0])

    def test_factory_must_return_agent_tool(self):
        path = self.folder / "tools" / "wrong.py"
        path.parent.mkdir(parents=True)
        path.write_text("def create_tool():\n    return 42\n", encoding="utf-8")

        loaded = self._load()

        self.assertEqual(loaded.tools, ())
        self.assertIn("AgentTool", loaded.warnings[0])

    def test_duplicate_tool_name_keeps_last_file(self):
        self._tool("dup", "a.py", "first")
        self._tool("dup", "b.py", "second")

        loaded = self._load()

        self.assertEqual(len(loaded.tools), 1)
        self.assertEqual(loaded.tools[0].spec.description, "second")

    def test_command_substitutes_arguments(self):
        self._command("review", "check $ARGUMENTS now")

        self.assertEqual(self._load().expand("/review src/a.py"), "check src/a.py now")

    def test_command_appends_arguments_without_placeholder(self):
        self._command("review", "check code")

        self.assertEqual(self._load().expand("/review src/a.py"), "check code\n\nsrc/a.py")
        self.assertEqual(self._load().expand("/review"), "check code")

    def test_plain_text_is_returned_unchanged(self):
        self.assertEqual(self._load().expand("hello"), "hello")

    def test_unknown_command_raises(self):
        with self.assertRaises(extensions.UnknownCommand):
            self._load().expand("/nope")

    def test_skill_command_uses_body(self):
        self._skill("lint", "check style", "run the linter")

        self.assertEqual(self._load().expand("/skill:lint"), "run the linter")

    def test_skill_requires_description(self):
        path = self.folder / "skills" / "bad" / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text("---\nname: bad\n---\nbody", encoding="utf-8")

        loaded = self._load()

        self.assertEqual(loaded.skills, ())
        self.assertIn("description", loaded.warnings[0])

    def test_skill_body_excludes_frontmatter(self):
        self._skill("lint", "check style", "run the linter")

        self.assertEqual(self._load().skill("lint").body(), "run the linter")

    def test_skills_section_lists_name_description_and_path(self):
        self._skill("lint", "check style", "run the linter")

        text = extensions.skills_section(self._load().skills)

        self.assertIn("- lint: check style", text)
        self.assertIn("SKILL.md", text)
        self.assertEqual(extensions.skills_section(()), "")

    def test_build_tools_skips_conflicting_extension_tool(self):
        self._tool("read_file")
        tools, _, _, warnings = session._build_tools(
            self.root,
            FileTracker(),
            None,
            Policy(workspace=self.root),
            None,
            self._load().tools,
        )

        self.assertEqual(len(warnings), 1)
        self.assertIn("read_file", warnings[0])
        self.assertEqual(sum(1 for tool in tools if tool.spec.name == "read_file"), 1)


class SessionExtensionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.workspace = self.root / "workspace"
        self.folder = self.workspace / ".gradus" / "extensions"
        self.folder.mkdir(parents=True)

    async def asyncTearDown(self) -> None:
        await session.close_pool()
        self._tmp.cleanup()

    async def test_session_exposes_commands_skills_and_extension_tools(self):
        command = self.folder / "commands" / "review.md"
        command.parent.mkdir()
        command.write_text("check $ARGUMENTS", encoding="utf-8")
        skill = self.folder / "skills" / "lint" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(
            "---\ndescription: check style\n---\nrun the linter", encoding="utf-8"
        )
        tool_file = self.folder / "tools" / "echo.py"
        tool_file.parent.mkdir()
        tool_file.write_text(
            TOOL_TEMPLATE.format(name="echo", description="echo"), encoding="utf-8"
        )

        with patch.object(session, "is_trusted", lambda root: True):
            one = await session.Session.connect(_settings(), workspace=self.workspace)
        try:
            self.assertEqual([item.name for item in one.commands], ["review"])
            self.assertEqual([item.name for item in one.skills], ["lint"])
            self.assertIn("Skills (read the file", one.system)
            self.assertIn("check style", one.system)
            self.assertIn("echo", [tool.spec.name for tool in one._tools])
            with self.assertRaises(session.SessionError):
                one.prompt("/nope")
        finally:
            await one.aclose()

    async def test_untrusted_extensions_are_skipped(self):
        command = self.folder / "commands" / "secret.md"
        command.parent.mkdir()
        command.write_text("do it", encoding="utf-8")

        with patch.object(session, "is_trusted", lambda root: False):
            one = await session.Session.connect(_settings(), workspace=self.workspace)
        try:
            self.assertEqual(one.commands, ())
            self.assertTrue(
                any("untrusted extensions" in item for item in one.extension_warnings)
            )
        finally:
            await one.aclose()

    async def test_reload_picks_up_new_files(self):
        with patch.object(session, "is_trusted", lambda root: True):
            one = await session.Session.connect(_settings(), workspace=self.workspace)
        try:
            command = self.folder / "commands" / "later.md"
            command.parent.mkdir()
            command.write_text("new body", encoding="utf-8")
            skill = self.folder / "skills" / "docs" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("---\ndescription: write docs\n---\nbody", encoding="utf-8")
            tool_file = self.folder / "tools" / "echo.py"
            tool_file.parent.mkdir()
            tool_file.write_text(
                TOOL_TEMPLATE.format(name="echo", description="echo"), encoding="utf-8"
            )

            await one.reload()

            self.assertEqual([item.name for item in one.commands], ["later"])
            self.assertEqual([item.name for item in one.skills], ["docs"])
            self.assertIn("write docs", one.system)
            self.assertIn("echo", [tool.spec.name for tool in one.tools])
        finally:
            await one.aclose()

    async def test_reload_without_trust_only_warns(self):
        with patch.object(session, "is_trusted", lambda root: False):
            one = await session.Session.connect(_settings(), workspace=self.workspace)
        try:
            command = self.folder / "commands" / "late.md"
            command.parent.mkdir()
            command.write_text("body", encoding="utf-8")

            warnings = await one.reload()

            self.assertTrue(any("not trusted" in item for item in warnings))
            self.assertEqual(one.commands, ())
        finally:
            await one.aclose()


if __name__ == "__main__":
    unittest.main()
