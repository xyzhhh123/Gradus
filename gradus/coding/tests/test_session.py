"""session 的离线测试（不联网）。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gradus.ai.base import ModelSpec
from gradus.ai.types import (
    AssistantMessage,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from gradus.coding import context, session, workspace
from gradus.coding.config import Settings
from gradus.coding.mcp import McpServer

_user_config_dir: tempfile.TemporaryDirectory | None = None
_user_config_patch = None


def setUpModule() -> None:
    """把用户级 MCP 配置指到空目录，避免读到开发机上的 ~/.gradus/mcp.json。"""
    global _user_config_dir, _user_config_patch
    _user_config_dir = tempfile.TemporaryDirectory()
    missing = Path(_user_config_dir.name) / "user-mcp.json"
    _user_config_patch = patch.object(session, "default_user_config_path", lambda: missing)
    _user_config_patch.start()


def tearDownModule() -> None:
    if _user_config_patch is not None:
        _user_config_patch.stop()
    if _user_config_dir is not None:
        _user_config_dir.cleanup()


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


def _small_settings() -> Settings:
    return Settings(
        spec=ModelSpec(
            provider="anthropic",
            model="claude-test",
            base_url="https://api.anthropic.com",
            context_window=10,
            max_output_tokens=100,
        ),
        api_key="sk-test",
    )


def _history() -> list[Message]:
    return [
        UserMessage("a" * 400),
        AssistantMessage([TextBlock("ok")], stop_reason="stop"),
        UserMessage("b" * 400),
        AssistantMessage([TextBlock("ok")], stop_reason="stop"),
    ]


class SessionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        await session.close_pool()

    async def test_sessions_share_pool_but_not_agent(self):
        first = await session.Session.connect(_settings())
        second = await session.Session.connect(_settings())
        try:
            self.assertIs(session.get_pool(), session.get_pool())
            self.assertIsNot(first, second)
            self.assertEqual(first.messages, ())
            self.assertEqual(second.messages, ())
        finally:
            await first.aclose()
            await second.aclose()

    async def test_aclose_keeps_shared_pool_open(self):
        pool = session.get_pool()
        one = await session.Session.connect(_settings())

        await one.aclose()

        self.assertFalse(pool.is_closed)
        self.assertIs(session.get_pool(), pool)

    async def test_close_pool_rebuilds_next_time(self):
        pool = session.get_pool()

        await session.close_pool()

        self.assertTrue(pool.is_closed)
        self.assertIsNot(session.get_pool(), pool)

    async def test_closed_session_rejects_prompt(self):
        one = await session.Session.connect(_settings())
        await one.aclose()

        with self.assertRaises(RuntimeError):
            one.prompt("hi")

    async def test_system_prompt_contains_environment(self):
        text = session.build_system()

        self.assertIn("Gradus", text)
        self.assertIn(os.getcwd(), text)

    async def test_workspace_defaults_to_cwd(self):
        one = await session.Session.connect(_settings())
        try:
            self.assertEqual(one.workspace, Path.cwd().resolve())
        finally:
            await one.aclose()

    async def test_workspace_can_be_overridden(self):
        with tempfile.TemporaryDirectory() as tmp:
            one = await session.Session.connect(_settings(), workspace=tmp)
            try:
                self.assertEqual(one.workspace, Path(tmp).resolve())
            finally:
                await one.aclose()

    async def test_custom_system_is_kept(self):
        one = await session.Session.connect(_settings(), system="custom")

        self.assertEqual(one.system, "custom")
        await one.aclose()


class SessionFileTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.dir = self.root / "sessions"

    async def asyncTearDown(self) -> None:
        await session.close_pool()
        self._tmp.cleanup()

    def test_create_writes_header(self):
        log = session.SessionFile.create(self.dir, _settings(), self.root)

        header = json.loads(log.path.read_text("utf-8").splitlines()[0])

        self.assertEqual(header["type"], "session")
        self.assertEqual(header["version"], 1)
        self.assertEqual(header["model"], "claude-test")
        self.assertEqual(header["workspace"], str(self.root.resolve()))
        self.assertTrue(log.path.name.endswith(".jsonl"))

    def test_append_chains_entries(self):
        log = session.SessionFile.create(self.dir, _settings(), self.root)

        log.append([UserMessage("hi"), AssistantMessage([TextBlock("hello")])])

        lines = [
            json.loads(line) for line in log.path.read_text("utf-8").splitlines()
        ]
        self.assertEqual(lines[1]["id"], "e1")
        self.assertIsNone(lines[1]["parentId"])
        self.assertEqual(lines[2]["id"], "e2")
        self.assertEqual(lines[2]["parentId"], "e1")

    def test_round_trip_messages(self):
        log = session.SessionFile.create(self.dir, _settings(), self.root)
        messages = [
            UserMessage("hi"),
            AssistantMessage(
                content=[
                    TextBlock("answer"),
                    ThinkingBlock("because", "sig"),
                    ToolCallBlock(
                        id="c1",
                        name="read_file",
                        arguments={"path": "a.txt"},
                        raw='{"path": "a.txt"}',
                    ),
                ],
                stop_reason="tool_use",
                usage=Usage(1, 2, 3, 4),
            ),
            ToolResultMessage(tool_call_id="c1", content="alpha"),
        ]

        log.append(messages)
        loaded = session.SessionFile.load(log.path)

        self.assertEqual(loaded.messages, messages)
        self.assertEqual(loaded.meta.model, "claude-test")

    def test_load_from_directory_picks_a_file(self):
        log = session.SessionFile.create(self.dir, _settings(), self.root)
        log.append([UserMessage("hi")])

        loaded = session.SessionFile.load(self.dir)

        self.assertEqual(loaded.messages, [UserMessage("hi")])
        self.assertEqual(loaded.path, log.path)

    def test_unpaired_tool_call_is_rejected(self):
        log = session.SessionFile.create(self.dir, _settings(), self.root)
        log.append(
            [
                AssistantMessage(
                    content=[ToolCallBlock(id="c1", name="read_file")],
                    stop_reason="tool_use",
                )
            ]
        )

        with self.assertRaises(session.SessionError):
            session.SessionFile.load(log.path)

    def test_orphan_tool_result_is_rejected(self):
        log = session.SessionFile.create(self.dir, _settings(), self.root)
        log.append([ToolResultMessage(tool_call_id="c1", content="x")])

        with self.assertRaises(session.SessionError):
            session.SessionFile.load(log.path)

    def test_load_rejects_missing_header(self):
        path = self.root / "bad.jsonl"
        path.write_text('{"type":"message","id":"e1","parentId":null,"message":{}}\n')

        with self.assertRaises(session.SessionError):
            session.SessionFile.load(path)

    async def test_connect_creates_session_file(self):
        one = await session.Session.connect(
            _settings(), workspace=self.root, session_dir=self.dir
        )
        try:
            self.assertIsNotNone(one.session_path)
            self.assertTrue(one.session_path.exists())
        finally:
            await one.aclose()

    async def test_resume_restores_messages_and_workspace(self):
        log = session.SessionFile.create(self.dir, _settings(), self.root)
        log.append(
            [
                UserMessage("hi"),
                AssistantMessage([TextBlock("hello")], stop_reason="stop"),
            ]
        )

        one = await session.Session.connect(_settings(), resume=log.path)
        try:
            self.assertEqual(one.workspace, self.root.resolve())
            self.assertEqual(len(one.messages), 2)
            self.assertEqual(one.session_path, log.path)
        finally:
            await one.aclose()


class CompactionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.dir = self.root / "sessions"

    async def asyncTearDown(self) -> None:
        await session.close_pool()
        self._tmp.cleanup()

    def test_record_compaction_appends_entry(self):
        log = session.SessionFile.create(self.dir, _settings(), self.root)
        log.append(_history())

        log.record_compaction("summary", 2)

        entries = [
            json.loads(line) for line in log.path.read_text("utf-8").splitlines()
        ]
        compaction = entries[-1]
        self.assertEqual(compaction["type"], "compaction")
        self.assertEqual(compaction["firstKeptEntryId"], "e3")
        self.assertEqual(compaction["parentId"], "e4")
        self.assertEqual(len(log.messages), 3)
        self.assertTrue(log.messages[0].content.startswith(context.SUMMARY_MARKER))

    def test_load_replays_last_compaction(self):
        log = session.SessionFile.create(self.dir, _settings(), self.root)
        log.append(_history())
        log.record_compaction("summary", 2)

        loaded = session.SessionFile.load(log.path)

        self.assertEqual(len(loaded.messages), 3)
        self.assertEqual(loaded.messages[0], context.summary_message("summary"))
        self.assertEqual(loaded.messages[1], _history()[2])

    def test_append_after_compaction_keeps_chain(self):
        log = session.SessionFile.create(self.dir, _settings(), self.root)
        log.append(_history())
        log.record_compaction("summary", 2)

        log.append([UserMessage("next")])
        loaded = session.SessionFile.load(log.path)

        entries = [
            json.loads(line) for line in log.path.read_text("utf-8").splitlines()
        ]
        self.assertEqual(entries[-1]["parentId"], entries[-2]["id"])
        self.assertEqual(loaded.messages[-1], UserMessage("next"))

    def test_load_rejects_unknown_kept_entry(self):
        log = session.SessionFile.create(self.dir, _settings(), self.root)
        log.append([UserMessage("hi")])
        with log.path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "compaction",
                        "id": "e9",
                        "parentId": "e1",
                        "summary": "s",
                        "firstKeptEntryId": "e8",
                    }
                )
                + "\n"
            )

        with self.assertRaises(session.SessionError):
            session.SessionFile.load(log.path)

    async def test_maybe_compact_rebuilds_and_records(self):
        log = session.SessionFile.create(self.dir, _settings(), self.root)
        log.append(_history())
        one = await session.Session.connect(
            _small_settings(), workspace=self.root, resume=log.path
        )

        async def summarizer(text: str) -> str:
            return "summary"

        try:
            with patch.object(session, "make_summarizer", lambda *a, **k: summarizer):
                await one._maybe_compact()

            self.assertEqual(len(one.messages), 3)
            self.assertEqual(one.messages[0], context.summary_message("summary"))
            entries = [
                json.loads(line) for line in log.path.read_text("utf-8").splitlines()
            ]
            self.assertEqual(entries[-1]["type"], "compaction")
        finally:
            await one.aclose()

    async def test_failed_summary_keeps_history(self):
        log = session.SessionFile.create(self.dir, _settings(), self.root)
        log.append(_history())
        one = await session.Session.connect(
            _small_settings(), workspace=self.root, resume=log.path
        )

        async def summarizer(text: str) -> str:
            raise context.CompactionError("boom")

        try:
            with patch.object(session, "make_summarizer", lambda *a, **k: summarizer):
                await one._maybe_compact()

            self.assertEqual(len(one.messages), 4)
            entries = [
                json.loads(line) for line in log.path.read_text("utf-8").splitlines()
            ]
            self.assertEqual(entries[-1]["type"], "message")
        finally:
            await one.aclose()


class McpTrustTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        folder = self.root / ".gradus"
        folder.mkdir()
        self.config = folder / "mcp.json"
        self.config.write_text(
            json.dumps({"servers": {"demo": {"command": "nope", "enabled": False}}}),
            encoding="utf-8",
        )

    async def asyncTearDown(self) -> None:
        await session.close_pool()
        self._tmp.cleanup()

    async def test_untrusted_config_is_skipped_without_confirm(self):
        one = await session.Session.connect(_settings(), workspace=self.root)
        try:
            self.assertEqual(one.mcp_servers, ())
            self.assertTrue(any("untrusted" in item for item in one.mcp_warnings))
        finally:
            await one.aclose()

    async def test_declining_skips_mcp(self):
        async def confirm(question: str) -> bool:
            return False

        one = await session.Session.connect(
            _settings(), workspace=self.root, confirm=confirm
        )
        try:
            self.assertEqual(one.mcp_servers, ())
            self.assertTrue(any("declined" in item for item in one.mcp_warnings))
        finally:
            await one.aclose()

    async def test_accepting_remembers_trust(self):
        async def confirm(question: str) -> bool:
            return True

        with (
            patch.object(session, "is_trusted", lambda root: False),
            patch.object(session, "remember_trust") as remember,
        ):
            one = await session.Session.connect(
                _settings(), workspace=self.root, confirm=confirm
            )
        try:
            remember.assert_called_once_with(self.root.resolve())
            self.assertEqual(one.mcp_warnings, ())
        finally:
            await one.aclose()

    async def test_explicit_config_skips_trust_prompt(self):
        one = await session.Session.connect(
            _settings(), workspace=self.root, mcp_config=self.config
        )
        try:
            self.assertEqual(one.mcp_warnings, ())
        finally:
            await one.aclose()


class ContextFilesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.nested = self.root / "pkg"
        self.nested.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_reads_ancestors_from_root_to_leaf(self):
        (self.root / "AGENTS.md").write_text("root rules", encoding="utf-8")
        (self.nested / "AGENTS.md").write_text("leaf rules", encoding="utf-8")

        files = session.read_project_context(self.nested)

        self.assertEqual(
            [path for path, _ in files][-2:],
            [self.root / "AGENTS.md", self.nested / "AGENTS.md"],
        )
        self.assertEqual([text for _, text in files][-2:], ["root rules", "leaf rules"])

    def test_override_wins_in_the_same_directory(self):
        (self.root / "AGENTS.md").write_text("plain", encoding="utf-8")
        (self.root / "AGENTS.override.md").write_text("override", encoding="utf-8")

        files = session.read_project_context(self.root)

        local = [path.name for path, _ in files if path.parent == self.root]
        self.assertEqual(local, ["AGENTS.override.md"])

    def test_missing_files_mean_empty_context(self):
        files = session.read_project_context(self.nested)

        self.assertEqual(
            [path for path, _ in files if path.parent in (self.root, self.nested)], []
        )


class ContextSystemTest(unittest.IsolatedAsyncioTestCase):
    async def test_connect_injects_project_instructions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ws"
            root.mkdir()
            (root / "AGENTS.md").write_text("Always use tabs.", encoding="utf-8")

            one = await session.Session.connect(_settings(), workspace=root)
            try:
                self.assertIn("<project_context>", one.system)
                self.assertIn("Always use tabs.", one.system)
                self.assertIn(root / "AGENTS.md", one.context_files)
            finally:
                await one.aclose()

    async def test_reload_rereads_context_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ws"
            root.mkdir()
            target = root / "AGENTS.md"
            target.write_text("old rule", encoding="utf-8")

            one = await session.Session.connect(_settings(), workspace=root)
            try:
                target.write_text("new rule", encoding="utf-8")
                await one.reload()

                self.assertIn("new rule", one.system)
                self.assertNotIn("old rule", one.system)
            finally:
                await one.aclose()

    async def test_custom_system_is_not_merged_with_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ws"
            root.mkdir()
            (root / "AGENTS.md").write_text("project rule", encoding="utf-8")

            one = await session.Session.connect(
                _settings(), system="custom", workspace=root
            )
            try:
                self.assertEqual(one.system, "custom")
                self.assertEqual(one.context_files, ())
            finally:
                await one.aclose()


class McpSandboxCwdTest(unittest.IsolatedAsyncioTestCase):
    """沙箱会话里，stdio MCP server 缺省 cwd 指向副本。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.root = base / "host"
        self.root.mkdir()
        self.copy = base / "copy"
        self.copy.mkdir()
        self.config = base / "mcp.json"
        self.config.write_text(
            json.dumps({"servers": {"demo": {"command": "nope"}}}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_helper_only_touches_stdio_servers_without_cwd(self):
        copy = Path("C:/tmp/gradus-ws-test")
        servers = (
            McpServer(name="stdio", command="x"),
            McpServer(name="explicit", command="x", cwd="C:/host"),
            McpServer(name="http", url="http://127.0.0.1:9/mcp"),
        )

        localized = session._mcp_servers_in(copy, servers)

        self.assertEqual(localized[0].cwd, str(copy))
        self.assertEqual(localized[1].cwd, "C:/host")
        self.assertIsNone(localized[2].cwd)
        # 原对象不能被改掉，刷新时还要按原配置重连。
        self.assertIsNone(servers[0].cwd)

    async def test_sandbox_copy_becomes_the_default_cwd(self):
        captured: list[tuple[McpServer, ...]] = []

        async def fake_connect(servers, *, interactive=True):
            captured.append(tuple(servers))
            return None

        async def fake_env(settings, root):
            return (
                None,
                None,
                (),
                workspace.WorkspaceCopy(self.root, self.copy, worktree=False),
            )

        with (
            patch.object(session, "_build_environment", fake_env),
            patch.object(session, "connect_mcp", fake_connect),
        ):
            one = await session.Session.connect(
                _settings(),
                system="custom",
                workspace=self.root,
                mcp_config=self.config,
            )
        try:
            self.assertEqual(captured[0][0].cwd, str(self.copy))
            self.assertEqual(one.sandbox_workspace, self.copy)
        finally:
            await one.aclose()


if __name__ == "__main__":
    unittest.main()
