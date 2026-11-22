"""技能：双根目录、用户级优先、`use_skill` 与使用计数。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gradus.ai.base import ModelSpec
from gradus.coding import extensions, session
from gradus.coding.config import Settings
from gradus.coding.tools.skill import UseSkillTool


def _settings() -> Settings:
    return Settings(
        spec=ModelSpec(
            provider="anthropic",
            model="m",
            base_url="https://api.anthropic.com",
            context_window=200_000,
            max_output_tokens=8_192,
        ),
        api_key="k",
        sandbox="local",
    )


def _write_skill(
    root: Path, name: str, description: str, body: str, extra: str = ""
) -> None:
    path = root / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n{extra}---\n\n{body}\n",
        encoding="utf-8",
    )


class LoadTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.user = base / "user"
        self.project = base / "project"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _load(self, project_enabled: bool = True) -> extensions.Extensions:
        return extensions.load(
            self.project,
            project_enabled=project_enabled,
            project_dir=self.project,
            user_dir=self.user,
        )

    def test_user_level_needs_no_trust(self):
        _write_skill(self.user, "personal", "my own flow", "do it this way")

        loaded = self._load()

        self.assertEqual([item.name for item in loaded.skills], ["personal"])

    def test_untrusted_project_keeps_user_skills(self):
        _write_skill(self.user, "personal", "my own flow", "do it")
        _write_skill(self.project, "project-only", "repo flow", "do that")

        loaded = self._load(project_enabled=False)

        self.assertEqual([item.name for item in loaded.skills], ["personal"])

    def test_user_level_wins_on_name_conflict(self):
        _write_skill(self.user, "release", "my release flow", "mine")
        _write_skill(self.project, "release", "repo release flow", "theirs")

        loaded = self._load()

        self.assertEqual(len(loaded.skills), 1)
        self.assertIn("mine", loaded.skill("release").body())
        self.assertTrue(
            any("shadowed" in warning for warning in loaded.warnings), loaded.warnings
        )

    def test_user_level_tools_are_ignored(self):
        tool = self.user / "tools" / "evil.py"
        tool.parent.mkdir(parents=True)
        tool.write_text("raise RuntimeError('should never load')\n", encoding="utf-8")

        loaded = self._load()

        self.assertEqual(loaded.tools, ())

    def test_project_tools_still_load(self):
        tool = self.project / "tools" / "echo.py"
        tool.parent.mkdir(parents=True)
        tool.write_text(
            "from gradus.coding.registry import FunctionTool\n"
            "from gradus.ai.base import ToolSpec\n"
            "def create_tool():\n"
            "    return FunctionTool(spec=ToolSpec(name='echo', description='e', parameters={'type': 'object'}), handler=lambda a: 'ok')\n",
            encoding="utf-8",
        )

        loaded = self._load()

        self.assertEqual([item.spec.name for item in loaded.tools], ["echo"])


class UseSkillToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _write_skill(self.root, "lint", "check style", "run the linter")
        self.skills = extensions.load(
            self.root, project_dir=self.root, user_dir=self.root / "none"
        ).skills
        self.usage: dict[str, int] = {}
        self.tool = UseSkillTool(lambda: self.skills, self.usage)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def test_returns_body_without_frontmatter(self):
        result = await self.tool.execute("c1", {"name": "lint"})

        self.assertFalse(result.is_error)
        self.assertIn("run the linter", result.content)
        self.assertNotIn("description:", result.content)
        self.assertEqual(self.usage, {"lint": 1})

    async def test_unknown_name_lists_available(self):
        result = await self.tool.execute("c1", {"name": "nope"})

        self.assertTrue(result.is_error)
        self.assertIn("lint", result.content)

    async def test_long_body_is_chunked_and_offset_continues(self):
        _write_skill(self.root, "long", "long one", "a" * (extensions.BODY_LIMIT + 100))
        skills = extensions.load(
            self.root, project_dir=self.root, user_dir=self.root / "none"
        ).skills
        tool = UseSkillTool(lambda: skills, {})

        first = await tool.execute("c1", {"name": "long"})
        second = await tool.execute(
            "c2", {"name": "long", "offset": extensions.BODY_LIMIT}
        )

        self.assertIn("continue with use_skill", first.content)
        self.assertIn("100 chars left", "100 chars left")  # 只为可读性，真正断言在下一行
        self.assertIn("offset=", first.content)
        self.assertIn("a", second.content)
        self.assertNotIn("continue with use_skill", second.content)


class SessionSkillsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.user = base / "user"
        self.workspace = base / "workspace"
        self.workspace.mkdir()
        _write_skill(self.user, "personal", "my own flow", "do it this way")

    async def asyncTearDown(self) -> None:
        await session.close_pool()
        self._tmp.cleanup()

    async def _connect(self):
        with patch.object(extensions, "default_user_dir", lambda: self.user), patch.object(
            session, "is_trusted", lambda *a, **k: True
        ):
            return await session.Session.connect(_settings(), workspace=self.workspace)

    async def test_user_skill_is_declared_and_callable(self):
        one = await self._connect()
        try:
            self.assertIn("call use_skill", one.system)
            self.assertIn("personal", one.system)
            self.assertIn("use_skill", [tool.spec.name for tool in one.tools])

            tool = next(t for t in one.tools if t.spec.name == "use_skill")
            result = await tool.execute("c1", {"name": "personal"})

            self.assertIn("do it this way", result.content)
            self.assertEqual(one.skills_used, {"personal": 1})
        finally:
            await one.aclose()

    async def test_slash_skill_counts_as_usage(self):
        one = await self._connect()
        try:
            expanded = one._expand("/skill:personal 参数")

            self.assertIn("do it this way", expanded)
            self.assertIn("参数", expanded)
            self.assertEqual(one.skills_used, {"personal": 1})
        finally:
            await one.aclose()


class ModelInvocationOptOutTest(unittest.IsolatedAsyncioTestCase):
    """`disable-model-invocation: true`：不进索引、`use_skill` 拒绝、`/skill:` 仍可用。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _write_skill(
            self.root,
            "deploy",
            "生产部署流程",
            "部署步骤",
            extra="disable-model-invocation: true\n",
        )
        _write_skill(self.root, "lint", "check style", "run the linter")
        self.loaded = extensions.load(
            self.root, project_dir=self.root, user_dir=self.root / "none"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_disabled_skill_is_not_in_the_index(self):
        index = extensions.skills_section(self.loaded.skills)

        self.assertIn("lint", index)
        self.assertNotIn("deploy", index)

    def test_disabled_skill_still_loads_and_expands(self):
        self.assertIsNotNone(self.loaded.skill("deploy"))
        expanded = self.loaded.expand("/skill:deploy")

        self.assertIn("部署步骤", expanded)

    async def test_use_skill_refuses_a_user_only_skill(self):
        tool = UseSkillTool(lambda: self.loaded.skills, {})

        result = await tool.execute("c1", {"name": "deploy"})

        self.assertTrue(result.is_error)
        self.assertIn("/skill:deploy", result.content)


if __name__ == "__main__":
    unittest.main()
