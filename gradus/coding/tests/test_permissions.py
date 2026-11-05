"""权限策略与 before_tool_call 接线的离线测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gradus.agent.hooks import AfterToolCallContext, BeforeToolCallContext
from gradus.agent.types import ToolResult
from gradus.ai.types import AssistantMessage, ToolCallBlock
from gradus.coding.permissions import (
    MAX_RESULT_CHARS,
    Policy,
    make_after_tool_call,
    make_before_tool_call,
    sanitize_result,
)


class PolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.policy = Policy(workspace=self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_read_tools_are_allowed(self):
        for name in ("read_file", "list_dir", "grep", "glob"):
            with self.subTest(name=name):
                self.assertEqual(self.policy.decide(name, {"path": "a.txt"}).action, "allow")

    def test_writes_ask(self):
        for name in ("write_file", "delete_file"):
            with self.subTest(name=name):
                self.assertEqual(
                    self.policy.decide(name, {"path": "src/a.py"}).action, "ask"
                )

        with tempfile.TemporaryDirectory() as other:
            decision = self.policy.decide(
                "write_file", {"path": str(Path(other) / "a.py")}
            )

        self.assertEqual(decision.action, "ask")
        self.assertIn("workspace", decision.reason)

    def test_protected_paths_are_denied(self):
        for path in (".env", "config/.env.local", "key.pem", ".git/config"):
            with self.subTest(path=path):
                self.assertEqual(
                    self.policy.decide("read_file", {"path": path}).action, "deny"
                )

    def test_dangerous_commands_are_denied(self):
        for command in ("rm -rf /", "sudo ls", "git push --force origin main"):
            with self.subTest(command=command):
                self.assertEqual(
                    self.policy.decide("bash", {"command": command}).action, "deny"
                )

    def test_other_commands_ask(self):
        self.assertEqual(self.policy.decide("bash", {"command": "ls -la"}).action, "ask")

    def test_powershell_commands_ask_and_are_denied(self):
        self.assertEqual(
            self.policy.decide("powershell", {"command": "Get-ChildItem"}).action, "ask"
        )
        self.assertEqual(
            self.policy.decide("powershell", {"command": "sudo x"}).action, "deny"
        )

    def test_unknown_tool_asks(self):
        self.assertEqual(self.policy.decide("mcp_weather", {}).action, "ask")


class HookTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.policy = Policy(workspace=self.root)

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _context(self, name: str, arguments: dict) -> BeforeToolCallContext:
        return BeforeToolCallContext(
            call=ToolCallBlock(id="c1", name=name, arguments=arguments),
            arguments=arguments,
            assistant=AssistantMessage(),
            turn=0,
        )

    async def test_allow_returns_none(self):
        hook = make_before_tool_call(self.policy, None)

        self.assertIsNone(await hook(self._context("read_file", {"path": "a.txt"})))

    async def test_deny_blocks(self):
        hook = make_before_tool_call(self.policy, None)

        result = await hook(self._context("read_file", {"path": ".env"}))

        self.assertTrue(result.block)
        self.assertIn("受保护", result.reason)

    async def test_ask_without_confirm_blocks(self):
        hook = make_before_tool_call(self.policy, None)

        result = await hook(self._context("write_file", {"path": "a.txt"}))

        self.assertTrue(result.block)
        self.assertIn("没有确认回调", result.reason)

    async def test_ask_with_confirm_true_allows(self):
        questions: list[str] = []

        async def confirm(question: str) -> bool:
            questions.append(question)
            return True

        hook = make_before_tool_call(self.policy, confirm)

        self.assertIsNone(await hook(self._context("write_file", {"path": "a.txt"})))
        self.assertIn("write_file", questions[0])

    async def test_ask_with_confirm_false_blocks(self):
        async def confirm(question: str) -> bool:
            return False

        hook = make_before_tool_call(self.policy, confirm)

        result = await hook(self._context("write_file", {"path": "a.txt"}))

        self.assertTrue(result.block)
        self.assertIn("用户拒绝", result.reason)


class SanitizeTest(unittest.IsolatedAsyncioTestCase):
    def test_redacts_api_keys(self):
        text = "key=sk-abcdefghijklmnop1234 token: abcdef"

        sanitized = sanitize_result(text)

        self.assertNotIn("sk-abcdefghijklmnop1234", sanitized)
        self.assertIn("sk-***", sanitized)
        self.assertNotIn("abcdef", sanitized.split("token:")[1])

    def test_redacts_bearer_tokens(self):
        sanitized = sanitize_result("Authorization: Bearer abc.def-123")

        self.assertNotIn("abc.def-123", sanitized)
        self.assertIn("Bearer ***", sanitized)

    def test_leaves_normal_text_unchanged(self):
        text = "def read_file(path):\n    return path"

        self.assertEqual(sanitize_result(text), text)

    def test_truncates_long_content(self):
        sanitized = sanitize_result("x" * (MAX_RESULT_CHARS + 10))

        self.assertTrue(sanitized.endswith(f"[truncated to {MAX_RESULT_CHARS} chars]"))
        self.assertLess(len(sanitized), MAX_RESULT_CHARS + 100)

    async def test_after_hook_skips_unchanged_result(self):
        hook = make_after_tool_call()

        self.assertIsNone(await hook(self._context(ToolResult("plain"))))

    async def test_after_hook_returns_sanitized_content(self):
        hook = make_after_tool_call()

        result = await hook(self._context(ToolResult("sk-abcdefghijklmnop1234")))

        self.assertEqual(result.content, "sk-***")

    def _context(self, result: ToolResult) -> AfterToolCallContext:
        return AfterToolCallContext(
            call=ToolCallBlock(id="c1", name="read_file", arguments={}),
            arguments={},
            assistant=AssistantMessage(),
            result=result,
            turn=0,
        )


if __name__ == "__main__":
    unittest.main()
