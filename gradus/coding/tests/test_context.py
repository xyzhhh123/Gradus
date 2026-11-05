"""上下文预算与压缩的离线测试。"""

from __future__ import annotations

import unittest
from collections.abc import Sequence

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
from gradus.coding import context


def _assistant(text: str, *, usage: Usage | None = None, stop: str | None = "stop"):
    return AssistantMessage(
        content=[TextBlock(text)],
        stop_reason=stop,  # type: ignore[arg-type]
        usage=usage,
    )


class EstimateTest(unittest.TestCase):
    def test_empty_text_is_zero(self):
        self.assertEqual(context.estimate_text_tokens(""), 0)

    def test_ascii_uses_chars_per_token(self):
        self.assertEqual(context.estimate_text_tokens("a" * 7), 2)

    def test_cjk_counts_one_token_per_char(self):
        self.assertEqual(context.estimate_text_tokens("你好世界"), 4)

    def test_mixed_text_combines_both_rules(self):
        # 2 个汉字 = 2，7 个 ASCII = 2
        self.assertEqual(context.estimate_text_tokens("你好" + "a" * 7), 4)

    def test_user_message_uses_content(self):
        self.assertEqual(context.estimate_message_tokens(UserMessage("abcd")), 2)

    def test_tool_result_uses_content(self):
        message = ToolResultMessage(tool_call_id="c1", content="a" * 7)

        self.assertEqual(context.estimate_message_tokens(message), 2)

    def test_assistant_counts_text_thinking_and_tool_arguments(self):
        message = AssistantMessage(
            content=[
                TextBlock("a" * 7),
                ThinkingBlock("你好"),
                ToolCallBlock(id="c1", name="read_file", arguments={"path": "a"}),
            ]
        )

        # 2（文本）+ 2（思考）+ 3（工具名）+ 4（参数 JSON，含引号与括号）
        self.assertEqual(context.estimate_message_tokens(message), 11)


class ContextTokensTest(unittest.TestCase):
    def test_uses_last_successful_usage_as_anchor(self):
        messages = [
            UserMessage("old " * 100),
            _assistant("done", usage=Usage(input_tokens=1000, output_tokens=50)),
            UserMessage("new question"),
        ]

        usage = context.context_tokens(messages)

        self.assertEqual(usage.usage_tokens, 1050)
        self.assertEqual(usage.last_usage_index, 1)
        self.assertEqual(usage.tokens, 1050 + usage.trailing_tokens)
        self.assertGreater(usage.trailing_tokens, 0)

    def test_error_and_aborted_usage_are_ignored(self):
        messages = [
            UserMessage("hi"),
            _assistant("failed", usage=Usage(input_tokens=900), stop="error"),
        ]

        usage = context.context_tokens(messages)

        self.assertIsNone(usage.last_usage_index)
        self.assertEqual(usage.usage_tokens, 0)
        self.assertEqual(usage.tokens, usage.trailing_tokens)

    def test_zero_usage_is_ignored(self):
        messages = [UserMessage("hi"), _assistant("ok", usage=Usage())]

        self.assertIsNone(context.context_tokens(messages).last_usage_index)

    def test_without_usage_estimates_everything(self):
        messages = [UserMessage("a" * 7), _assistant("a" * 7)]

        self.assertEqual(context.context_tokens(messages).tokens, 4)


class ShouldCompactTest(unittest.TestCase):
    def test_below_threshold(self):
        messages = [UserMessage("a" * 70)]

        self.assertFalse(
            context.should_compact(messages, 10_000, reserve_tokens=0)
        )

    def test_above_threshold(self):
        messages = [UserMessage("a" * 700)]

        self.assertTrue(context.should_compact(messages, 100, reserve_tokens=0))

    def test_reserve_larger_than_window_still_compacts(self):
        self.assertTrue(context.should_compact([UserMessage("hello")], 100))


class SelectCutTest(unittest.TestCase):
    def test_keeps_everything_when_under_budget(self):
        messages = [UserMessage("a"), _assistant("b")]

        self.assertIsNone(context.select_cut(messages, 10_000))

    def test_never_cuts_at_first_message(self):
        messages = [UserMessage("a" * 700), _assistant("b" * 700)]

        self.assertIsNone(context.select_cut(messages, 1))

    def test_cuts_at_user_message(self):
        messages = [
            UserMessage("a" * 400),
            _assistant("b" * 400),
            UserMessage("c" * 400),
            _assistant("d" * 400),
        ]

        self.assertEqual(context.select_cut(messages, 300), 2)

    def test_does_not_cut_at_tool_result(self):
        messages = [
            UserMessage("a" * 400),
            AssistantMessage(
                content=[ToolCallBlock(id="c1", name="read_file", arguments={})],
                stop_reason="tool_use",
            ),
            ToolResultMessage(tool_call_id="c1", content="b" * 400),
            UserMessage("c" * 400),
        ]

        self.assertEqual(context.select_cut(messages, 200), 3)


class TrimTest(unittest.TestCase):
    def test_keeps_everything_under_budget(self):
        messages = [UserMessage("hi"), _assistant("ok")]

        self.assertEqual(context.trim_messages(messages, 10_000), messages)

    def test_drops_leading_turns(self):
        messages = [
            UserMessage("a" * 400),
            _assistant("b" * 400),
            UserMessage("c" * 400),
            _assistant("d" * 400),
        ]

        kept = context.trim_messages(messages, 100, reserve_tokens=0)

        self.assertEqual(kept, messages[2:])

    def test_never_splits_tool_pairs(self):
        messages = [
            UserMessage("a" * 400),
            AssistantMessage(
                content=[ToolCallBlock(id="c1", name="read_file", arguments={})],
                stop_reason="tool_use",
            ),
            ToolResultMessage(tool_call_id="c1", content="b" * 400),
        ]

        kept = context.trim_messages(messages, 100, reserve_tokens=0)

        self.assertEqual(kept, messages)


class SerializeTest(unittest.TestCase):
    def test_labels_each_role(self):
        messages: Sequence[Message] = [
            UserMessage("hello"),
            _assistant("hi"),
            ToolResultMessage(tool_call_id="c1", content="out"),
        ]

        text = context.serialize_messages(messages)

        self.assertIn("[User]: hello", text)
        self.assertIn("[Assistant]: hi", text)
        self.assertIn("[Tool result]: out", text)

    def test_tool_calls_are_flattened(self):
        messages = [
            AssistantMessage(
                content=[ToolCallBlock(id="c1", name="read_file", arguments={"path": "a"})],
                stop_reason="tool_use",
            )
        ]

        self.assertIn('[Assistant tool call]: read_file({"path": "a"})', context.serialize_messages(messages))

    def test_thinking_is_omitted(self):
        messages = [
            AssistantMessage(content=[ThinkingBlock("secret"), TextBlock("answer")])
        ]

        text = context.serialize_messages(messages)

        self.assertNotIn("secret", text)
        self.assertIn("answer", text)

    def test_long_tool_result_is_clipped(self):
        messages = [ToolResultMessage(tool_call_id="c1", content="a" * 100)]

        text = context.serialize_messages(messages, tool_result_chars=10)

        self.assertIn("90 chars truncated", text)

    def test_summary_message_has_marker(self):
        message = context.summary_message("s")

        self.assertTrue(message.content.startswith(context.SUMMARY_MARKER))


class CompactTest(unittest.IsolatedAsyncioTestCase):
    async def test_compacts_prefix_only(self):
        messages = [
            UserMessage("a" * 400),
            _assistant("b" * 400),
            UserMessage("c" * 400),
            _assistant("d" * 400),
        ]
        seen: list[str] = []

        async def summarizer(text: str) -> str:
            seen.append(text)
            return "summary"

        result = await context.compact(messages, summarizer=summarizer, keep_recent_tokens=300)

        assert result is not None
        self.assertEqual(result.first_kept, 2)
        self.assertEqual(result.summary, "summary")
        self.assertIn("a" * 10, seen[0])
        self.assertNotIn("c" * 10, seen[0])

    async def test_returns_none_when_nothing_to_compact(self):
        messages = [UserMessage("hi")]

        async def summarizer(text: str) -> str:
            raise AssertionError("should not be called")

        result = await context.compact(messages, summarizer=summarizer)

        self.assertIsNone(result)

    async def test_empty_summary_is_rejected(self):
        messages = [UserMessage("a" * 400), UserMessage("b" * 400)]

        async def summarizer(text: str) -> str:
            return "   "

        result = await context.compact(messages, summarizer=summarizer, keep_recent_tokens=200)

        self.assertIsNone(result)
