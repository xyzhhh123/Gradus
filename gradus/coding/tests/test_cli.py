"""CLI 参数与事件渲染的离线测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gradus.agent.types import (
    AgentDone,
    AgentError,
    ToolExecutionEnd,
    ToolExecutionStart,
    ToolResult,
    TurnStart,
)
from gradus.ai.types import AssistantMessage, TextDelta, ToolCallBlock, Usage
from gradus.coding import cli, mcp
from gradus.coding.cli import overrides_from_args, parse_args
from gradus.coding.render import render, render_log


class RenderTest(unittest.TestCase):
    def test_text_delta(self):
        event = TextDelta(content_index=0, delta="hello", partial=AssistantMessage())

        self.assertEqual(render(event), "hello")

    def test_tool_start(self):
        call = ToolCallBlock(id="c1", name="read_file", arguments={"path": "a.py"})

        text = render(ToolExecutionStart(call=call))

        self.assertIn("[tool] read_file", text)
        self.assertIn('"path": "a.py"', text)

    def test_tool_end_ok_shows_status_duration_and_first_line(self):
        call = ToolCallBlock(id="c1", name="read_file", arguments={})
        event = ToolExecutionEnd(
            call=call,
            result=ToolResult("alpha\nbeta"),
            duration_ms=12,
        )

        text = render(event)

        self.assertIn("ok", text)
        self.assertIn("12ms", text)
        self.assertIn("alpha", text)
        self.assertNotIn("beta", text)

    def test_tool_end_error(self):
        call = ToolCallBlock(id="c1", name="read_file", arguments={})
        event = ToolExecutionEnd(
            call=call,
            result=ToolResult("missing", is_error=True),
        )

        self.assertIn("error", render(event))

    def test_agent_done(self):
        event = AgentDone(
            new_messages=[],
            usage=Usage(input_tokens=10, output_tokens=5),
            turns=2,
            stop_reason="stop",
        )

        text = render(event)

        self.assertIn("[done] 2 turns", text)
        self.assertIn("in 10", text)
        self.assertIn("out 5", text)

    def test_agent_error(self):
        event = AgentError(
            kind="provider",
            message="boom",
            partial=None,
            new_messages=[],
            usage=Usage(),
        )

        self.assertIn("[error] provider: boom", render(event))

    def test_unhandled_event_is_silent(self):
        self.assertIsNone(render(TurnStart(turn=0)))


class LogTest(unittest.TestCase):
    def test_tool_end_is_logged_as_json(self):
        call = ToolCallBlock(id="c1", name="read_file", arguments={})
        event = ToolExecutionEnd(
            call=call,
            result=ToolResult("alpha"),
            duration_ms=12,
        )

        payload = json.loads(render_log(event))

        self.assertEqual(payload["event"], "tool_end")
        self.assertEqual(payload["tool"], "read_file")
        self.assertEqual(payload["call_id"], "c1")
        self.assertEqual(payload["duration_ms"], 12)
        self.assertFalse(payload["is_error"])
        self.assertIn("ts", payload)

    def test_agent_error_is_logged_as_json(self):
        event = AgentError(
            kind="provider",
            message="boom",
            partial=None,
            new_messages=[],
            usage=Usage(),
        )

        payload = json.loads(render_log(event))

        self.assertEqual(payload["event"], "agent_error")
        self.assertEqual(payload["kind"], "provider")
        self.assertEqual(payload["message"], "boom")

    def test_other_events_are_not_logged(self):
        self.assertIsNone(render_log(TurnStart(turn=0)))


class ArgsTest(unittest.TestCase):
    def test_overrides_map_only_given_flags(self):
        args = parse_args(["--model", "kimi", "--base-url", "https://example.test"])

        self.assertEqual(
            overrides_from_args(args),
            {"GRADUS_MODEL": "kimi", "GRADUS_BASE_URL": "https://example.test"},
        )

    def test_service_flag_maps_to_env_key(self):
        args = parse_args(["--service", "deepseek"])

        self.assertEqual(overrides_from_args(args), {"GRADUS_SERVICE": "deepseek"})

    def test_sandbox_flag_maps_to_env_key(self):
        args = parse_args(["--sandbox", "docker"])

        self.assertEqual(overrides_from_args(args), {"GRADUS_SANDBOX": "docker"})

    def test_no_flags_means_no_overrides(self):
        self.assertEqual(overrides_from_args(parse_args([])), {})


class UntrustTest(unittest.IsolatedAsyncioTestCase):
    async def test_untrust_removes_trusted_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "trusted.json"
            project = Path(tmp) / "proj"
            project.mkdir()
            with (
                patch.object(mcp, "trust_file", lambda: store),
                patch.dict("os.environ", {}, clear=True),
            ):
                mcp.remember_trust(project)
                code = await cli.main(["--untrust", str(project)])

                self.assertEqual(code, 0)
                self.assertFalse(mcp.is_trusted(project))

    async def test_bare_untrust_uses_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "trusted.json"
            project = Path(tmp) / "proj"
            project.mkdir()
            with (
                patch.object(mcp, "trust_file", lambda: store),
                patch.dict("os.environ", {}, clear=True),
            ):
                mcp.remember_trust(project)
                code = await cli.main(["--untrust", "--workspace", str(project)])

                self.assertEqual(code, 0)
                self.assertFalse(mcp.is_trusted(project))

    async def test_untrust_unknown_project_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "trusted.json"
            project = Path(tmp) / "proj"
            project.mkdir()
            with patch.object(mcp, "trust_file", lambda: store):
                code = await cli.main(["--untrust", str(project)])

                self.assertEqual(code, 0)
                self.assertFalse(store.exists())


if __name__ == "__main__":
    unittest.main()
