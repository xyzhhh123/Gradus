"""上下文预算与压缩的离线测试。"""

from __future__ import annotations

import unittest
from collections.abc import Sequence
from unittest.mock import patch

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
from gradus.coding.config import Settings


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

    def test_identical_tool_results_are_deduped(self):
        """同一个输出被读多次时只留一份：这类重复在真实会话里很常见。"""
        messages = [
            ToolResultMessage(tool_call_id="c1", content="same output"),
            ToolResultMessage(tool_call_id="c2", content="same output"),
            ToolResultMessage(tool_call_id="c3", content="different"),
        ]

        text = context.serialize_messages(messages)

        self.assertEqual(text.count("same output"), 1)
        self.assertIn("与第 1 条相同", text)
        self.assertIn("different", text)

    def test_long_assistant_prose_collapses_to_one_line(self):
        messages = [_assistant("第一句。" + "细节" * 500)]

        text = context.serialize_messages(messages)

        self.assertIn("[Assistant]: 第一句。", text)
        self.assertIn("共 1004 字", text)
        self.assertLess(len(text), 400)

    def test_short_assistant_prose_is_kept_whole(self):
        text = context.serialize_messages([_assistant("短说明")])

        self.assertEqual(text, "[Assistant]: 短说明")

    def test_prose_collapse_respects_the_limit(self):
        text = context.serialize_messages(
            [_assistant("x" * 100)], prose_chars=10
        )

        self.assertIn("x" * 10 + "…（共 100 字）", text)

    def test_user_words_are_never_summarized(self):
        """用户原话是意图，压成一行就等于篡改；这条不能省。"""
        long_text = "把这一大段需求" + "细节" * 500

        text = context.serialize_messages([UserMessage(long_text)])

        self.assertIn(long_text, text)

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
    async def test_passes_full_history_and_cut_to_summarizer(self):
        messages = [
            UserMessage("a" * 400),
            _assistant("b" * 400),
            UserMessage("c" * 400),
            _assistant("d" * 400),
        ]
        seen: list[tuple[int, int]] = []

        async def summarizer(history, cut) -> str:
            seen.append((len(history), cut))
            return "summary"

        result = await context.compact(
            messages, summarizer=summarizer, keep_recent_tokens=300
        )

        assert result is not None
        self.assertEqual(result.first_kept, 2)
        self.assertEqual(result.summary, "summary")
        # 重放式摘要需要整段可见历史才能与主请求对齐前缀；切点另行给出。
        self.assertEqual(seen, [(4, 2)])

    async def test_returns_none_when_nothing_to_compact(self):
        messages = [UserMessage("hi")]

        async def summarizer(history, cut) -> str:
            raise AssertionError("should not be called")

        result = await context.compact(messages, summarizer=summarizer)

        self.assertIsNone(result)

    async def test_empty_summary_is_rejected(self):
        messages = [UserMessage("a" * 400), UserMessage("b" * 400)]

        async def summarizer(history, cut) -> str:
            return "   "

        result = await context.compact(
            messages, summarizer=summarizer, keep_recent_tokens=200
        )

        self.assertIsNone(result)


class SummarizerTest(unittest.IsolatedAsyncioTestCase):
    """摘要请求必须与主请求前缀一致：系统提示、工具、历史原样重放，只追加一条指令。"""

    async def test_replay_keeps_prefix_and_appends_instruction(self):
        from gradus.ai.base import ModelSpec, ToolSpec
        from gradus.ai.types import AssistantMessage, StreamDone, TextDelta, Usage

        recorder = _Recorder(
            [
                TextDelta(
                    content_index=0,
                    delta="summary",
                    partial=AssistantMessage(content=[]),
                ),
                StreamDone(
                    stop_reason="stop",
                    message=AssistantMessage(content=[TextBlock("summary")]),
                    usage=Usage(input_tokens=10, output_tokens=2),
                ),
            ]
        )
        settings = Settings(
            spec=ModelSpec(
                provider="anthropic",
                model="m",
                base_url="https://api.anthropic.com",
                context_window=200_000,
                max_output_tokens=8_192,
            ),
            api_key="k",
        )
        tool = ToolSpec(name="read_file", description="读文件", parameters={})

        with patch.object(context, "create_provider", lambda *a, **k: recorder):
            run = context.make_summarizer(settings, system="SYS", tools=[tool])
            summary = await run([UserMessage("hi")], 1)

        request = recorder.requests[0]
        self.assertEqual(summary, "summary")
        self.assertEqual(request.system, "SYS")
        self.assertEqual([item.name for item in request.tools], ["read_file"])
        self.assertEqual(request.messages[0].content, "hi")
        self.assertEqual(request.messages[-1].content, context.SUMMARY_INSTRUCTION)
        # 摘要请求的用量要留下来：它是「前缀缓存有没有命中」的唯一证据
        self.assertIsNotNone(run.last_usage)
        self.assertEqual(run.last_usage.input_tokens, 10)

    def test_instruction_covers_all_eight_sections(self):
        self.assertEqual(len(context.SUMMARY_SECTIONS), 8)
        for section in context.SUMMARY_SECTIONS:
            self.assertIn(section, context.SUMMARY_INSTRUCTION)
            self.assertIn(section, context.SUMMARY_FALLBACK_INSTRUCTION)

    async def test_tool_call_in_summary_is_rejected(self):
        from gradus.ai.base import ModelSpec
        from gradus.ai.types import ToolCallStart

        recorder = _Recorder(
            [
                ToolCallStart(
                    content_index=0,
                    id="c1",
                    name="read_file",
                    partial=AssistantMessage(content=[]),
                )
            ]
        )
        settings = Settings(
            spec=ModelSpec(
                provider="anthropic",
                model="m",
                base_url="https://api.anthropic.com",
                context_window=200_000,
                max_output_tokens=8_192,
            ),
            api_key="k",
        )

        with patch.object(context, "create_provider", lambda *a, **k: recorder):
            run = context.make_summarizer(settings)
            with self.assertRaises(context.CompactionError):
                await run([UserMessage("hi")], 1)


class _Recorder:
    """假的 provider：记录收到的请求，按脚本吐事件。"""

    def __init__(self, events) -> None:
        self.requests = []
        self._events = events

    async def stream(self, request):
        self.requests.append(request)
        for event in self._events:
            yield event


class ActiveTurnTest(unittest.IsolatedAsyncioTestCase):
    """切点不许落进当前轮：这条必须是显式检查，不能靠「活跃轮在尾部」的假设。"""

    def test_turn_start_ignores_harness_messages(self):
        messages = [
            UserMessage("第一轮"),
            _assistant("ok"),
            UserMessage("第二轮"),
            _assistant("好"),
            UserMessage("插话", origin="harness"),
        ]

        self.assertEqual(context.active_turn_start(messages), 2)

    def test_cut_inside_active_turn_is_rejected(self):
        messages = [
            UserMessage("a" * 400),
            _assistant("b" * 400),
            UserMessage("c" * 400),  # 本轮起点
            _assistant("d" * 400),
            UserMessage("插话", origin="harness"),
            _assistant("e" * 400),
        ]

        # 预算只够覆盖尾部三条：朴素算法会切在插话上（第 4 条），把本轮劈开。
        self.assertEqual(context.select_cut(messages, 300, turn_start=2), None)

    def test_cut_at_turn_start_is_allowed(self):
        messages = [
            UserMessage("a" * 400),
            _assistant("b" * 400),
            UserMessage("c" * 400),
            _assistant("d" * 400),
        ]

        # 预算刚好覆盖第 2 条之后的内容：切在轮首是合法的，整轮都保留。
        self.assertEqual(context.select_cut(messages, 300, turn_start=2), 2)

    async def test_compact_refuses_when_turn_exceeds_budget(self):
        messages = [
            UserMessage("a" * 400),
            _assistant("b" * 400),
            UserMessage("c" * 400),  # 本轮起点
            _assistant("d" * 400),
            UserMessage("插话", origin="harness"),
            _assistant("e" * 400),
        ]

        async def summarizer(history, cut) -> str:
            raise AssertionError("不应被调用")

        result = await context.compact(
            messages, summarizer=summarizer, keep_recent_tokens=300
        )

        self.assertIsNone(result)


class WorkProductTest(unittest.TestCase):
    """子 agent 的结果算工作产物：压缩时别把它整段摘要掉。"""

    def _history(self):
        # 子 agent 的结果经 `notify` 注入，origin 是 harness——不是用户自己发的。
        return [
            UserMessage("第一轮提问"),
            _assistant("好的"),
            UserMessage("第二轮提问"),
            _assistant("派个子 agent"),
            UserMessage(
                "<message from=helper>\n审计完了，发现三处问题\n</message>",
                origin="harness",
            ),
            _assistant("收到"),
        ]

    def test_detects_bus_message(self):
        history = self._history()

        self.assertTrue(context.is_work_product(history[4]))
        self.assertFalse(context.is_work_product(history[0]))

    def test_boundary_moves_back_to_turn_start(self):
        history = self._history()

        # 朴素切点在第 5 条：会把子结果连同第 3 条提问一起摘要掉。
        kept = context.keep_recent_work_products(history, first_kept=5, base=0)

        self.assertEqual(kept, 2)  # 退到「第二轮提问」，整轮保留

    def test_boundary_untouched_when_product_still_visible(self):
        history = self._history()

        self.assertEqual(
            context.keep_recent_work_products(history, first_kept=4, base=0), 4
        )

    def test_no_product_means_no_change(self):
        history = [UserMessage("a"), _assistant("b"), UserMessage("c")]

        self.assertEqual(
            context.keep_recent_work_products(history, first_kept=2, base=0), 2
        )


class ProjectViewTest(unittest.TestCase):
    """视图 = 若干份摘要 + 保留段；分层时按从旧到新排列。"""

    def test_projects_every_layer_in_order(self):
        state = context.CompactionState(
            layers=(
                context.SummaryLayer(key="e5", text="旧小结"),
                context.SummaryLayer(key="e9", text="新小结"),
            ),
            first_kept=2,
        )
        messages = [
            UserMessage("a"),
            _assistant("b"),
            UserMessage("c"),
            _assistant("d"),
        ]

        view = context.project_view(messages, state)

        self.assertEqual(len(view), 4)  # 两份摘要 + 两条保留
        self.assertIn("旧小结", view[0].content)
        self.assertIn("新小结", view[1].content)
        self.assertEqual(view[2:], messages[2:])

    def test_state_summary_is_the_newest_layer(self):
        state = context.CompactionState(
            layers=(
                context.SummaryLayer(key="e5", text="旧"),
                context.SummaryLayer(key="e9", text="新"),
            ),
            first_kept=3,
        )

        self.assertEqual(state.summary, "新")


class BudgetTest(unittest.TestCase):
    """保留量必须小于触发线：否则压缩原地打转、每轮重复付费。"""

    def test_retention_scales_with_window(self):
        """保留量按触发线的比例给：窗口越大留得越多，但永远小于触发线的一半。"""
        small = context.retention_budget(30_000)
        medium = context.retention_budget(128_000)
        large = context.retention_budget(1_000_000)

        self.assertEqual(small, 6_808)          # 下限被「触发线一半」截住
        self.assertEqual(medium, 27_904)        # 0.25 × 111,616
        self.assertEqual(large, 64_000)         # 撞上限
        self.assertLess(small, medium)
        self.assertLess(medium, large)
        for window in (30_000, 128_000, 1_000_000):
            self.assertLessEqual(
                context.retention_budget(window), context.compact_threshold(window) // 2
            )

    def test_presets_change_how_much_is_kept(self):
        window = 200_000
        conservative = context.retention_budget(window, preset="conservative")
        balanced = context.retention_budget(window, preset="balanced")
        aggressive = context.retention_budget(window, preset="aggressive")

        self.assertEqual((conservative, balanced, aggressive), (18_361, 45_904, 73_446))
        self.assertLess(conservative, balanced)
        self.assertLess(balanced, aggressive)

    def test_validate_rejects_keep_at_or_above_threshold(self):
        with self.assertRaises(context.ContextBudgetError):
            context.validate_budget(30_000, keep_recent_tokens=20_000)

    def test_validate_accepts_default_budget(self):
        self.assertEqual(context.validate_budget(30_000), 6_808)

    def test_degenerate_window_keeps_default_budget(self):
        # 窗口小于预留时没有余量可言，退回旧的「尽量多留」，不把它当配置错误。
        self.assertEqual(
            context.retention_budget(10), context.DEFAULT_KEEP_RECENT_TOKENS
        )
        self.assertEqual(context.validate_budget(10), context.DEFAULT_KEEP_RECENT_TOKENS)

    def test_reserve_covers_max_output(self):
        # 预留至少要覆盖一次最大输出，否则请求会被自己顶到窗口外。
        self.assertEqual(context.budget_reserve(8_192), context.DEFAULT_RESERVE_TOKENS)
        self.assertEqual(context.budget_reserve(65_536), 65_536)

    def test_bigger_model_reserve_shrinks_threshold(self):
        window = 200_000
        self.assertEqual(context.compact_threshold(window), 183_616)
        self.assertEqual(
            context.compact_threshold(window, context.budget_reserve(65_536)),
            134_464,
        )
        self.assertEqual(
            context.retention_budget(window, context.budget_reserve(65_536)),
            33_616,
        )

    def test_validate_rejects_reserve_that_leaves_no_room(self):
        with self.assertRaises(context.ContextBudgetError):
            context.validate_budget(100_000, reserve_tokens=90_000, keep_recent_tokens=20_000)


class ContextBudgetTest(unittest.TestCase):
    """预算由模型参数推导，不散着算。"""

    def test_default_model(self):
        budget = context.ContextBudget.for_model(200_000, 8_192)

        self.assertEqual(budget.reserve, context.DEFAULT_RESERVE_TOKENS)
        self.assertEqual(budget.threshold, 183_616)
        self.assertEqual(budget.keep_recent, 45_904)

    def test_big_output_shrinks_threshold(self):
        budget = context.ContextBudget.for_model(200_000, 65_536)

        self.assertEqual(budget.reserve, 65_536)
        self.assertEqual(budget.threshold, 134_464)
        self.assertEqual(budget.keep_recent, 33_616)

    def test_small_window_shrinks_keep(self):
        budget = context.ContextBudget.for_model(30_000, 8_192)

        self.assertEqual(budget.keep_recent, 6_808)
        self.assertLess(budget.keep_recent, budget.threshold)

    def test_explicit_keep_above_threshold_is_rejected(self):
        with self.assertRaises(context.ContextBudgetError):
            context.ContextBudget.for_model(30_000, 8_192, keep_tokens=20_000)

    def test_explicit_keep_below_threshold_is_kept(self):
        budget = context.ContextBudget.for_model(200_000, 8_192, keep_tokens=50_000)

        self.assertEqual(budget.keep_recent, 50_000)

    def test_invalid_window_is_rejected(self):
        with self.assertRaises(context.ContextBudgetError):
            context.ContextBudget.for_model(0, 8_192)


class AnchorTest(unittest.TestCase):
    """锚点绑在请求上：只估算锚点之后新增的消息。"""

    def test_counts_only_trailing_messages(self):
        messages = [
            UserMessage("a" * 350),
            _assistant("b" * 350),
            UserMessage("c" * 350),
        ]
        anchor = context.UsageAnchor(tokens=1_000, messages=2)

        usage = context.context_tokens(messages, anchor)

        self.assertEqual(
            usage.tokens, 1_000 + context.estimate_messages_tokens(messages[2:])
        )
        self.assertEqual(usage.usage_tokens, 1_000)
        self.assertEqual(usage.last_usage_index, 2)

    def test_anchor_beyond_history_falls_back_to_estimate(self):
        messages = [UserMessage("hi")]
        anchor = context.UsageAnchor(tokens=1_000, messages=5)

        usage = context.context_tokens(messages, anchor)

        self.assertIsNone(usage.last_usage_index)
        self.assertEqual(usage.tokens, context.estimate_messages_tokens(messages))


class PlanTrimTest(unittest.TestCase):
    """硬裁必须能报告丢了什么，否则「它为什么忘事」无从回答。"""

    def test_reports_dropped_prefix(self):
        messages = [
            UserMessage("a" * 400),
            _assistant("b" * 400),
            UserMessage("c" * 400),
            _assistant("d" * 400),
        ]

        result = context.plan_trim(messages, 100, reserve_tokens=0)

        self.assertEqual(result.dropped, 2)
        self.assertGreater(result.dropped_tokens, 0)
        self.assertEqual(result.kept, messages[2:])

    def test_no_drop_reports_zero(self):
        messages = [UserMessage("hi")]

        result = context.plan_trim(messages, 10_000)

        self.assertEqual((result.dropped, result.dropped_tokens), (0, 0))
        self.assertEqual(result.kept, messages)

    def test_trim_messages_still_returns_list(self):
        messages = [
            UserMessage("a" * 400),
            _assistant("b" * 400),
            UserMessage("c" * 400),
        ]

        self.assertEqual(
            context.trim_messages(messages, 100, reserve_tokens=0), messages[2:]
        )
