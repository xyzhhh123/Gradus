"""session 的离线测试（不联网）。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from gradus.agent.types import AgentDone
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
from gradus.coding.config import ModelChoice, Settings
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

    async def test_switch_model_rebuilds_agent(self):
        one = await session.Session.connect(_settings())
        try:
            target = Settings(
                spec=ModelSpec(
                    provider="openai_compat",
                    model="kimi-test",
                    base_url="https://api.moonshot.cn/v1",
                    context_window=128_000,
                    max_output_tokens=4_096,
                ),
                api_key="sk-other",
            )
            await one.switch_model(target)

            self.assertEqual(one.settings.spec.provider, "openai_compat")
            self.assertEqual(one.settings.spec.model, "kimi-test")
            self.assertEqual(one.messages, ())
        finally:
            await one.aclose()

    async def test_switch_model_rejects_closed_session(self):
        one = await session.Session.connect(_settings())
        await one.aclose()

        with self.assertRaises(RuntimeError):
            await one.switch_model(_settings())


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

    def test_create_uses_the_given_name_and_never_overwrites(self):
        first = session.SessionFile.create(self.dir, _settings(), self.root, name="sub-1")
        second = session.SessionFile.create(self.dir, _settings(), self.root, name="sub-1")

        self.assertEqual(first.path.name, "sub-1.jsonl")
        self.assertEqual(second.path.name, "sub-1-2.jsonl")

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

    def test_harness_origin_survives_round_trip(self):
        log = session.SessionFile.create(self.dir, _settings(), self.root)
        messages = [
            UserMessage("hi"),
            UserMessage("<task_notification/>", origin="harness"),
        ]

        log.append(messages)
        loaded = session.SessionFile.load(log.path)

        self.assertEqual(
            [message.origin for message in loaded.messages], ["user", "harness"]
        )
        first = json.loads(log.path.read_text("utf-8").splitlines()[1])
        self.assertNotIn("origin", first["message"])

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
        # 历史不因压缩而改写：完整历史仍是 4 条，模型看到的视图由状态派生。
        self.assertEqual(len(log.messages), len(_history()))
        self.assertIsNotNone(log.compaction)
        assert log.compaction is not None
        self.assertEqual(log.compaction.first_kept, 2)
        view = context.project_view(log.messages, log.compaction)
        self.assertEqual(len(view), 3)
        self.assertTrue(view[0].content.startswith(context.SUMMARY_MARKER))

    def test_record_merge_collapses_oldest_layers(self):
        log = session.SessionFile.create(self.dir, _settings(), self.root)
        log.append(_history())
        log.record_compaction("one", 2)
        log.record_compaction("two", 3)

        keys = [layer.key for layer in log.compaction.layers]
        log.record_merge("merged", keys[:2])

        self.assertEqual([layer.text for layer in log.compaction.layers], ["merged"])
        self.assertEqual(log.compaction.first_kept, 3)

        loaded = session.SessionFile.load(log.path)
        self.assertEqual(
            [layer.text for layer in loaded.compaction.layers], ["merged"]
        )

    async def test_merge_uses_configured_layer_cap(self):
        settings = replace(_settings(), summary_layers=1)
        one = await session.Session.connect(settings, workspace=self.root)
        one._compaction_slot[0] = context.CompactionState(
            layers=(
                context.SummaryLayer("a", "first"),
                context.SummaryLayer("b", "second"),
            ),
            first_kept=0,
            version=1,
        )
        try:
            with patch.object(
                session, "merge_summaries", AsyncMock(return_value="both")
            ) as merge:
                await one._merge_layers_if_needed()
            self.assertEqual(merge.await_count, 1)
            self.assertEqual([layer.text for layer in one.compaction.layers], ["both"])
        finally:
            await one.aclose()

    async def test_default_layer_cap_leaves_two_layers_alone(self):
        one = await session.Session.connect(_settings(), workspace=self.root)
        one._compaction_slot[0] = context.CompactionState(
            layers=(
                context.SummaryLayer("a", "first"),
                context.SummaryLayer("b", "second"),
            ),
            first_kept=0,
            version=1,
        )
        try:
            with patch.object(
                session, "merge_summaries", AsyncMock(return_value="both")
            ) as merge:
                await one._merge_layers_if_needed()
            self.assertEqual(merge.await_count, 0)
            self.assertEqual(len(one.compaction.layers), 2)
        finally:
            await one.aclose()

    async def test_merge_is_triggered_by_total_summary_length(self):
        """份数没超、但两份加起来太长时也要合并。"""
        one = await session.Session.connect(_settings(), workspace=self.root)
        one._compaction_slot[0] = context.CompactionState(
            layers=(
                context.SummaryLayer("a", "x" * 20_000),
                context.SummaryLayer("b", "second"),
            ),
            first_kept=0,
            version=1,
        )
        try:
            with patch.object(
                session, "merge_summaries", AsyncMock(return_value="both")
            ) as merge:
                await one._merge_layers_if_needed()
            self.assertEqual(merge.await_count, 1)
            self.assertEqual([layer.text for layer in one.compaction.layers], ["both"])
        finally:
            await one.aclose()

    async def test_configured_summary_tokens_gives_more_room(self):
        settings = replace(_settings(), summary_tokens=10_000_000)
        one = await session.Session.connect(settings, workspace=self.root)
        one._compaction_slot[0] = context.CompactionState(
            layers=(
                context.SummaryLayer("a", "x" * 20_000),
                context.SummaryLayer("b", "second"),
            ),
            first_kept=0,
            version=1,
        )
        try:
            with patch.object(
                session, "merge_summaries", AsyncMock(return_value="both")
            ) as merge:
                await one._merge_layers_if_needed()
            self.assertEqual(merge.await_count, 0)
            self.assertEqual(len(one.compaction.layers), 2)
        finally:
            await one.aclose()

    async def test_recall_budget_comes_from_config(self):
        log = session.SessionFile.create(self.dir, _settings(), self.root)
        log.append(_history())
        log.record_compaction("summary", 2)
        settings = replace(_settings(), recall_tokens=1)
        one = await session.Session.connect(
            settings, workspace=self.root, resume=log.path
        )
        try:
            result = await one.recall(["e1"], "", 8)
            self.assertTrue(result.is_error)
            self.assertIn("本代只剩", result.content)
        finally:
            await one.aclose()

    def test_record_trim_appends_once_per_prefix(self):
        log = session.SessionFile.create(self.dir, _settings(), self.root)
        log.append(_history())

        log.record_trim(2, 500)
        # 同一个前缀会在每次请求里重复触发，只记一次。
        log.record_trim(2, 500)

        entries = [
            json.loads(line) for line in log.path.read_text("utf-8").splitlines()
        ]
        trims = [entry for entry in entries if entry["type"] == "trim"]
        self.assertEqual(len(trims), 1)
        self.assertEqual(trims[0]["dropped"], 2)
        self.assertEqual(trims[0]["tokens"], 500)

    def test_record_trim_tracks_growing_prefix(self):
        log = session.SessionFile.create(self.dir, _settings(), self.root)
        log.append(_history())

        log.record_trim(1, 100)
        log.record_trim(3, 400)

        entries = [
            json.loads(line) for line in log.path.read_text("utf-8").splitlines()
        ]
        trims = [entry for entry in entries if entry["type"] == "trim"]
        self.assertEqual([entry["dropped"] for entry in trims], [1, 3])

    def test_load_skips_trim_entries(self):
        log = session.SessionFile.create(self.dir, _settings(), self.root)
        log.append(_history())
        log.record_trim(1, 200)

        loaded = session.SessionFile.load(log.path)

        self.assertEqual(len(loaded.messages), len(_history()))

    def test_record_leaf_rewinds_and_chains_from_target(self):
        log = session.SessionFile.create(self.dir, _settings(), self.root)
        log.append(_history())

        log.record_leaf("e2")

        # 被放弃的尾巴立刻从视图里消失，但历史仍在文件里。
        self.assertEqual(len(log.messages), 2)

        log.append([UserMessage("again")])
        entries = [
            json.loads(line) for line in log.path.read_text("utf-8").splitlines()
        ]
        leaf_event = next(entry for entry in entries if entry["type"] == "leaf")
        self.assertEqual(leaf_event["leaf"], "e2")
        self.assertEqual(leaf_event["parentId"], "e2")
        # 新消息挂在 leaf 事件后面，链路仍然从目标叶子出发。
        self.assertEqual(entries[-1]["parentId"], leaf_event["id"])

        loaded = session.SessionFile.load(log.path)
        self.assertEqual(
            loaded.messages, [*_history()[:2], UserMessage("again")]
        )

    def test_record_leaf_rejects_entry_outside_branch(self):
        log = session.SessionFile.create(self.dir, _settings(), self.root)
        log.append(_history())

        with self.assertRaises(session.SessionError):
            log.record_leaf("e99")

    def test_record_leaf_drops_state_pointing_past_rewind(self):
        log = session.SessionFile.create(self.dir, _settings(), self.root)
        log.append(_history())
        log.record_compaction("summary", 2)  # 保留 e3、e4

        log.record_leaf("e1")  # 退到压缩点之前

        self.assertIsNone(log.compaction)
        self.assertEqual(len(log.messages), 1)

    def test_load_keeps_full_history_and_installs_state(self):
        log = session.SessionFile.create(self.dir, _settings(), self.root)
        log.append(_history())
        log.record_compaction("summary", 2)

        loaded = session.SessionFile.load(log.path)

        # 历史保持完整；被摘要掉的原文仍在文件里，只是不进视图。
        self.assertEqual(len(loaded.messages), len(_history()))
        self.assertIsNotNone(loaded.compaction)
        assert loaded.compaction is not None
        self.assertEqual(loaded.compaction.summary, "summary")
        self.assertEqual(loaded.compaction.first_kept, 2)

        view = context.project_view(loaded.messages, loaded.compaction)
        self.assertEqual(len(view), 3)
        self.assertEqual(view[0], context.summary_message("summary"))
        self.assertEqual(view[1], _history()[2])

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

        async def summarizer(history, cut) -> str:
            return "summary"

        try:
            with patch.object(session, "make_summarizer", lambda *a, **k: summarizer):
                await one._maybe_compact()

            self.assertEqual(len(one.messages), 3)
            self.assertEqual(one.messages[0], context.summary_message("summary"))
            # 压缩只安装状态，不替换历史：完整历史仍是 4 条。
            self.assertEqual(len(one.history), 4)
            entries = [
                json.loads(line) for line in log.path.read_text("utf-8").splitlines()
            ]
            self.assertEqual(entries[-1]["type"], "compaction")
        finally:
            await one.aclose()

    async def test_compaction_failure_backs_off_before_retrying(self):
        """摘要失败后要跳过几轮再试，不能每轮都重付一次注定失败的请求。"""
        log = session.SessionFile.create(self.dir, _settings(), self.root)
        log.append(_history())
        one = await session.Session.connect(
            _small_settings(), workspace=self.root, resume=log.path
        )
        calls: list[int] = []

        async def failing(history, cut) -> str:
            calls.append(cut)
            raise context.CompactionError("boom")

        try:
            with patch.object(session, "make_summarizer", lambda *a, **k: failing):
                await one._maybe_compact()  # 第一次：真去试，失败
                self.assertEqual(len(calls), 1)
                self.assertEqual(one._compact_skips, 1)

                await one._maybe_compact()  # 第二次：退避中，不再调用
                self.assertEqual(len(calls), 1)
                self.assertEqual(one._compact_skips, 0)

                await one._maybe_compact()  # 第三次：退避用完，再试一次
                self.assertEqual(len(calls), 2)
                self.assertEqual(one._compact_skips, 2)
        finally:
            await one.aclose()

    async def test_successful_compaction_clears_the_backoff(self):
        log = session.SessionFile.create(self.dir, _settings(), self.root)
        log.append(_history())
        one = await session.Session.connect(
            _small_settings(), workspace=self.root, resume=log.path
        )
        one._compact_failures = 3
        one._compact_skips = 8

        async def summarizer(history, cut) -> str:
            return "summary"

        try:
            with patch.object(session, "make_summarizer", lambda *a, **k: summarizer):
                one._compact_skips = 0  # 模拟退避已用完
                await one._maybe_compact()

            self.assertIsNotNone(one.compaction)
            self.assertEqual(one._compact_failures, 0)
            self.assertEqual(one._compact_skips, 0)
        finally:
            await one.aclose()

    async def test_active_turn_too_big_is_reported_once(self):
        """当前这一轮自己就超预算时压缩切不动：要说一次原因，并且不装状态。"""
        log = session.SessionFile.create(self.dir, _settings(), self.root)
        # 一条消息就超过保留量（20,000），`select_cut` 因此拿不到合法切点
        log.append([UserMessage("x" * 200_000)])
        one = await session.Session.connect(
            _small_settings(), workspace=self.root, resume=log.path
        )
        notes: list[str] = []

        try:
            with patch.object(one, "notify", lambda text: notes.append(text) or True):
                await one._maybe_compact()
                await one._maybe_compact()

            self.assertEqual(len(notes), 1)
            self.assertIn("这一轮本身太大", notes[0])
            self.assertIsNone(one.compaction)
        finally:
            await one.aclose()

    async def test_compaction_is_not_refused_by_the_last_request_usage(self):
        """真机上「每次都判成没变小」的回归：最后一条 assistant 带 usage 时也要能压。

        接受条件曾经用 `context_tokens(...)` 算压前压后，而它会退回最后一条带
        usage 的 assistant —— 那个数描述的是压缩**前**发出的请求，两边相等，
        于是摘要算完也被丢掉，现象是「跑很久、不压缩、历史被兜底裁剪悄悄裁短」。
        """

        def big_user(tag: str) -> UserMessage:
            return UserMessage(f"[{tag}] " + "x" * 30_000)

        def reply(usage: Usage | None = None) -> AssistantMessage:
            return AssistantMessage([TextBlock("ok")], stop_reason="stop", usage=usage)

        history = [
            big_user("a"),
            reply(),
            big_user("b"),
            reply(),
            big_user("c"),
            reply(),
            # 最后这次请求的用量已经越过触发线（200,000 - 16,384 = 183,616）
            reply(Usage(input_tokens=190_000, output_tokens=10)),
        ]
        log = session.SessionFile.create(self.dir, _settings(), self.root)
        log.append(history)
        one = await session.Session.connect(
            _settings(), workspace=self.root, resume=log.path
        )

        async def summarizer(messages, cut) -> str:
            return "summary"

        try:
            with patch.object(session, "make_summarizer", lambda *a, **k: summarizer):
                await one._maybe_compact()

            self.assertIsNotNone(one.compaction, "compaction refused by usage-based before")
            self.assertLess(len(one.messages), len(one.history))
        finally:
            await one.aclose()

    async def test_switch_branch_returns_to_abandoned_branch(self):
        log = session.SessionFile.create(self.dir, _settings(), self.root)
        log.append(_history())          # e1..e4
        one = await session.Session.connect(
            _settings(), workspace=self.root, resume=log.path
        )

        try:
            await one.switch_branch("e2")
            self.assertEqual(one.history, tuple(_history()[:2]))

            await one.switch_branch("e4")
            self.assertEqual(one.history, tuple(_history()))
        finally:
            await one.aclose()

    async def test_switch_branch_requires_session_file(self):
        one = await session.Session.connect(_settings(), workspace=self.root)
        try:
            with self.assertRaises(session.SessionError):
                await one.switch_branch("e1")
        finally:
            await one.aclose()

    async def test_recall_reads_folded_history(self):
        log = session.SessionFile.create(self.dir, _settings(), self.root)
        log.append(_history())
        log.record_compaction("summary", 2)
        one = await session.Session.connect(
            _settings(), workspace=self.root, resume=log.path
        )

        try:
            folded = await one.recall(["e1"], "", 8)
            self.assertFalse(folded.is_error)
            self.assertIn("#e1", folded.content)

            visible = await one.recall(["e4"], "", 8)
            self.assertTrue(visible.is_error)
            self.assertIn("还在上下文里", visible.content)
        finally:
            await one.aclose()

    async def test_recall_without_compaction_is_refused(self):
        one = await session.Session.connect(_settings(), workspace=self.root)
        try:
            result = await one.recall(None, "", 8)
            self.assertTrue(result.is_error)
            self.assertIn("没有已压缩", result.content)
        finally:
            await one.aclose()

    async def test_runtime_view_matches_restored_view(self):
        """运行中看到的视图，必须与「重新加载后投影出来的视图」逐条相同。"""
        log = session.SessionFile.create(self.dir, _settings(), self.root)
        log.append(_history())
        log.record_compaction("summary", 2)
        log.append([UserMessage("next")])

        one = await session.Session.connect(
            _settings(), workspace=self.root, resume=log.path
        )
        try:
            runtime = list(one.messages)
            history = list(one.history)
        finally:
            await one.aclose()

        loaded = session.SessionFile.load(log.path)
        restored = context.project_view(loaded.messages, loaded.compaction)

        self.assertEqual(runtime, restored)
        self.assertEqual(history, loaded.messages)
        self.assertEqual(len(runtime), 4)  # 摘要 + 保留 2 条 + next
        self.assertTrue(runtime[0].content.startswith(context.SUMMARY_MARKER))

    async def test_failed_summary_keeps_history(self):
        log = session.SessionFile.create(self.dir, _settings(), self.root)
        log.append(_history())
        one = await session.Session.connect(
            _small_settings(), workspace=self.root, resume=log.path
        )

        async def summarizer(history, cut) -> str:
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
        # read_project_context 返回归一化后的路径；期望值也要归一化，否则在
        # 启用 8.3 短名的系统上（CI runner）拿到的是短路径，比较必然失败。
        self.root = Path(self._tmp.name).resolve()
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
            root = (Path(tmp) / "ws").resolve()
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


class SpawnSettingsTest(unittest.TestCase):
    """子 agent 的模型选择：节点的 `model` 提示怎么落到 `Settings` 上。"""

    @staticmethod
    def _session(settings: Settings) -> session.Session:
        one = session.Session.__new__(session.Session)
        one._settings = settings
        return one

    def test_follows_the_main_model_without_a_hint(self):
        settings = _settings()

        resolved = self._session(settings)._settings_for(None)

        self.assertIs(resolved, settings)

    def test_picks_a_candidate_by_name(self):
        small = ModelChoice(
            name="small",
            spec=ModelSpec(
                provider="anthropic",
                model="claude-small",
                base_url="https://api.anthropic.com",
                context_window=100_000,
                max_output_tokens=4_096,
            ),
        )
        settings = replace(_settings(), models=(small,))

        resolved = self._session(settings)._settings_for("small")

        self.assertEqual(resolved.spec.model, "claude-small")
        self.assertEqual(resolved.active, "small")

    def test_picks_a_candidate_by_model_id(self):
        small = ModelChoice(
            name="small",
            spec=ModelSpec(
                provider="anthropic",
                model="claude-small",
                base_url="https://api.anthropic.com",
                context_window=100_000,
                max_output_tokens=4_096,
            ),
        )
        settings = replace(_settings(), models=(small,))

        resolved = self._session(settings)._settings_for("claude-small")

        self.assertEqual(resolved.spec.model, "claude-small")

    def test_ignores_an_unknown_hint(self):
        settings = _settings()

        resolved = self._session(settings)._settings_for("does-not-exist")

        self.assertIs(resolved, settings)


class _FakeAgent:
    """只实现 `_run` 用到的几件事：状态、历史、两种起跑方式与通知。"""

    def __init__(self) -> None:
        self.messages: list[Message] = []
        self.state = "idle"
        self.prompts: list[str] = []
        self.continued = 0
        self.notified: list[str] = []

    def prompt(self, text: str, *, origin: str = "user"):
        self.state = "running"
        self.prompts.append(text)
        return self._stream()

    def continue_(self, prompt=None):
        self.state = "running"
        self.continued += 1
        return self._stream()

    def notify(self, text: str) -> None:
        self.notified.append(text)

    async def _stream(self):
        yield AgentDone([], Usage(), 1, "stop")


class ContinueTest(unittest.IsolatedAsyncioTestCase):
    """`continue_` 不造用户消息，只把收件箱里的通知补送进这一轮。"""

    async def asyncTearDown(self) -> None:
        await session.close_pool()

    async def test_continue_sends_notices_without_a_new_user_message(self):
        one = await session.Session.connect(_settings())
        fake = _FakeAgent()
        try:
            with patch.object(one, "_agent", fake):
                # 空闲时投递只是暂存，返回 True 让调用方决定要不要叫醒
                self.assertTrue(one.notify("sub-1 说：接口改好了"))
                async for _ in one.continue_():
                    pass
        finally:
            await one.aclose()

        self.assertEqual(fake.prompts, [])
        self.assertEqual(fake.continued, 1)
        self.assertEqual(fake.notified, ["sub-1 说：接口改好了"])

    async def test_continue_rejects_a_closed_session(self):
        one = await session.Session.connect(_settings())
        await one.aclose()

        with self.assertRaises(RuntimeError):
            one.continue_()


class TurnEndBudgetTest(unittest.IsolatedAsyncioTestCase):
    """预算在两处检查：轮次开始前，与轮次收尾。"""

    async def asyncTearDown(self) -> None:
        await session.close_pool()

    async def test_budget_is_checked_at_turn_start_and_end(self):
        """收尾那次不能省。

        只在开头检查的话，本轮新增的内容要等下一次请求才算进预算；而那时兜底裁剪
        （`transform_messages`）会先把旧消息丢掉——现象是「没报错、也没压缩」，
        历史却在悄悄变短。
        """
        one = await session.Session.connect(_settings())
        fake = _FakeAgent()
        calls: list[str] = []

        async def counting() -> None:
            calls.append("compact")

        try:
            with (
                patch.object(one, "_agent", fake),
                patch.object(one, "_maybe_compact", counting),
            ):
                async for _event in one.prompt("hi"):
                    pass
        finally:
            await one.aclose()

        self.assertEqual(calls, ["compact", "compact"])


class SessionHeaderTest(unittest.IsolatedAsyncioTestCase):
    """`{run}` 一次会话一个值：主对话与摘要必须带同一个，否则缓存不命中。"""

    async def asyncTearDown(self) -> None:
        await session.close_pool()

    async def test_run_placeholder_is_frozen_per_session(self):
        settings = replace(_settings(), headers={"x-opencode-session": "{run}"})

        first = await session.Session.connect(settings)
        second = await session.Session.connect(settings)
        try:
            frozen = first.settings.headers["x-opencode-session"]
            other = second.settings.headers["x-opencode-session"]
        finally:
            await first.aclose()
            await second.aclose()

        self.assertNotIn("{run}", frozen)
        self.assertTrue(frozen)
        # 换会话换一个：不同会话本来就不该共享路由
        self.assertNotEqual(frozen, other)

    async def test_summarizer_gets_the_frozen_header(self):
        """摘要必须带会话级的头：换一个 id 就会被网关路由到没缓存的机器。"""
        settings = replace(_settings(), headers={"x-opencode-session": "{run}"})
        one = await session.Session.connect(settings)
        captured: list[dict[str, str]] = []

        async def summarizer(history, cut) -> str:
            return "summary"

        def factory(settings_arg, **kwargs):
            captured.append(dict(settings_arg.headers))
            return summarizer

        try:
            with patch.object(session, "make_summarizer", factory):
                await one._summarize([UserMessage("x" * 400), UserMessage("y" * 400)], 10_000)
        finally:
            await one.aclose()

        self.assertEqual(len(captured), 1)
        self.assertNotIn("{run}", captured[0]["x-opencode-session"])
        self.assertEqual(captured[0], dict(one.settings.headers))


class UsageRecordTest(unittest.TestCase):
    """每轮用量落进会话文件，能加总；坏行不影响审计。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.root = self.dir / "workspace"
        self.root.mkdir()
        self.log = session.SessionFile.create(self.dir, _settings(), self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_records_one_line_per_turn(self):
        usage = Usage(input_tokens=100, output_tokens=20, cache_read_tokens=60)
        self.log.record_usage(usage, model="m")

        lines = [
            json.loads(line)
            for line in self.log.path.read_text("utf-8").splitlines()
            if line.strip()
        ]
        record = [item for item in lines if item["type"] == "usage"][-1]
        self.assertEqual(record["input"], 100)
        self.assertEqual(record["output"], 20)
        self.assertEqual(record["cacheRead"], 60)
        self.assertEqual(record["model"], "m")

    def test_totals_sum_across_turns(self):
        self.log.record_usage(
            Usage(input_tokens=100, output_tokens=20, cache_read_tokens=60), model="m"
        )
        self.log.record_usage(Usage(input_tokens=50, output_tokens=5), model="m")

        totals = self.log.total_usage()

        self.assertEqual(totals["turns"], 2)
        self.assertEqual(totals["input"], 150)
        self.assertEqual(totals["output"], 25)
        self.assertEqual(totals["cacheRead"], 60)

    def test_usage_records_do_not_break_reload(self):
        """`usage` 是账本，不是消息：读回会话时必须被跳过，而不是报 unknown type。"""
        self.log.append(_history())
        self.log.record_usage(Usage(input_tokens=10, output_tokens=1), model="m")

        fresh = session.SessionFile.load(self.log.path)

        self.assertEqual(len(fresh.messages), 4)
        self.assertEqual(fresh.total_usage()["turns"], 1)

    def test_bad_line_is_skipped(self):
        self.log.record_usage(Usage(input_tokens=10, output_tokens=1), model="m")
        with self.log.path.open("a", encoding="utf-8") as handle:
            handle.write("{not json\n")

        totals = self.log.total_usage()

        self.assertEqual(totals["turns"], 1)
        self.assertEqual(totals["input"], 10)


if __name__ == "__main__":
    unittest.main()


class BudgetReminderTest(unittest.IsolatedAsyncioTestCase):
    """轮数预算提醒：75% 与 90% 各一次，只追加不重写。"""

    async def test_injects_at_threshold(self):
        from gradus.coding.session import make_budget_reminder

        hook = make_budget_reminder(20)
        context = type("C", (), {"turn": 14, "config": None})()

        update = await hook(context)

        self.assertIsNotNone(update)
        self.assertIn("15/20", update.messages[0].content)

    async def test_only_once_per_threshold(self):
        from gradus.coding.session import make_budget_reminder

        hook = make_budget_reminder(20)
        context = type("C", (), {"turn": 14, "config": None})()

        self.assertIsNotNone(await hook(context))
        self.assertIsNone(await hook(context))

    async def test_silent_early_and_small_budget(self):
        from gradus.coding.session import make_budget_reminder

        early = type("C", (), {"turn": 3, "config": None})()
        small = type("C", (), {"turn": 3, "config": None})()

        self.assertIsNone(await make_budget_reminder(20)(early))
        self.assertIsNone(await make_budget_reminder(4)(small))
