"""agent hook 的离线测试。"""

from __future__ import annotations

import unittest
from dataclasses import replace

from gradus.agent.base import LoopConfig
from gradus.agent.hooks import (
    AfterToolCallResult,
    AgentHooks,
    BeforeFinishUpdate,
    BeforeRunUpdate,
    BeforeToolCallResult,
    MaxTurnsDecision,
    NextTurnUpdate,
)
from gradus.agent.tests.fakes import (
    FakeTool,
    RecordingProvider,
    collect,
    make_provider,
    text_script,
    tool_script,
)
from gradus.agent.types import (
    AgentDone,
    AgentError,
    ToolExecutionEnd,
    TurnEnd,
)
from gradus.ai.types import ToolResultMessage, Usage, UserMessage


class AgentHooksTest(unittest.IsolatedAsyncioTestCase):
    async def test_transform_messages_only_affects_request(self):
        provider = RecordingProvider(make_provider(text_script("ok")))

        async def transform(context):
            return [*context.messages, UserMessage("injected")]

        config = LoopConfig(
            system="",
            hooks=AgentHooks(transform_messages=transform),
        )
        events = await collect(provider, [], config=config)
        done = events[-1]

        self.assertIsInstance(done, AgentDone)
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(
            provider.requests[0].messages[-1].content,
            "injected",
        )
        # 只影响本次请求，不写回 new_messages
        self.assertEqual(len(done.new_messages), 1)

    async def test_prepare_request_can_replace_request(self):
        provider = RecordingProvider(make_provider(text_script("ok")))

        async def prepare(context):
            return replace(
                context.request,
                system="changed",
                temperature=0.2,
            )

        config = LoopConfig(
            system="original",
            hooks=AgentHooks(prepare_request=prepare),
        )
        events = await collect(provider, [], config=config)

        self.assertIsInstance(events[-1], AgentDone)
        self.assertEqual(provider.requests[0].system, "changed")
        self.assertEqual(provider.requests[0].temperature, 0.2)

    async def test_before_tool_call_can_block(self):
        tool = FakeTool("echo", required=("text",))
        provider = make_provider(
            tool_script([("call_0", "echo", {"text": "a"})]),
            text_script("done"),
        )

        async def before(context):
            return BeforeToolCallResult(block=True, reason="not allowed")

        config = LoopConfig(
            system="",
            tools=[tool],
            hooks=AgentHooks(before_tool_call=before),
        )
        events = await collect(provider, [], config=config)

        self.assertEqual(tool.calls, [])
        end = next(event for event in events if isinstance(event, ToolExecutionEnd))
        self.assertTrue(end.result.is_error)
        self.assertEqual(end.result.content, "not allowed")

        done = events[-1]
        self.assertIsInstance(done, AgentDone)
        result = done.new_messages[1]
        self.assertIsInstance(result, ToolResultMessage)
        self.assertTrue(result.is_error)
        self.assertEqual(result.content, "not allowed")

    async def test_after_tool_call_can_rewrite_result(self):
        tool = FakeTool("echo", required=("text",), result="raw")
        provider = make_provider(
            tool_script([("call_0", "echo", {"text": "a"})]),
            text_script("done"),
        )

        async def after(context):
            return AfterToolCallResult(content="trimmed", is_error=True)

        config = LoopConfig(
            system="",
            tools=[tool],
            hooks=AgentHooks(after_tool_call=after),
        )
        events = await collect(provider, [], config=config)

        end = next(event for event in events if isinstance(event, ToolExecutionEnd))
        self.assertTrue(end.result.is_error)
        self.assertEqual(end.result.content, "trimmed")

        done = events[-1]
        result = done.new_messages[1]
        self.assertIsInstance(result, ToolResultMessage)
        self.assertTrue(result.is_error)
        self.assertEqual(result.content, "trimmed")

    async def test_finish_turn_can_end_after_tool_batch(self):
        tool = FakeTool("echo", required=("text",))
        provider = RecordingProvider(
            make_provider(
                tool_script([("call_0", "echo", {"text": "a"})]),
                text_script("should not run"),
            )
        )

        async def finish(context):
            self.assertEqual(len(context.tool_results), 1)
            return "end"

        config = LoopConfig(
            system="",
            tools=[tool],
            hooks=AgentHooks(finish_turn=finish),
        )
        events = await collect(provider, [], config=config)
        done = events[-1]

        self.assertIsInstance(done, AgentDone)
        self.assertEqual(done.turns, 1)
        self.assertEqual(done.stop_reason, "tool_use")
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(
            sum(isinstance(event, TurnEnd) for event in events),
            1,
        )

    async def test_finish_turn_can_continue_after_stop(self):
        provider = RecordingProvider(
            make_provider(
                text_script("first", reason="stop"),
                text_script("second", reason="stop"),
            )
        )

        async def finish(context):
            return "continue" if context.turn == 0 else None

        config = LoopConfig(
            system="",
            hooks=AgentHooks(finish_turn=finish),
        )
        events = await collect(provider, [], config=config)
        done = events[-1]

        self.assertIsInstance(done, AgentDone)
        self.assertEqual(done.turns, 2)
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(len(done.new_messages), 2)

    async def test_prepare_next_turn_can_add_message_and_change_config(self):
        provider = RecordingProvider(
            make_provider(
                text_script("first", reason="deferred"),
                text_script("second", reason="stop"),
            )
        )

        async def prepare_next(context):
            if context.turn != 0:
                return None
            return NextTurnUpdate(
                messages=(UserMessage("next"),),
                config=replace(context.config, system="changed"),
            )

        config = LoopConfig(
            system="original",
            hooks=AgentHooks(prepare_next_turn=prepare_next),
        )
        events = await collect(provider, [], config=config)
        done = events[-1]

        self.assertIsInstance(done, AgentDone)
        self.assertEqual(done.turns, 2)
        self.assertEqual(provider.requests[1].system, "changed")
        self.assertEqual(provider.requests[1].messages[-1].content, "next")
        self.assertEqual(len(done.new_messages), 3)
        self.assertIsInstance(done.new_messages[1], UserMessage)

    async def test_prepare_next_turn_can_change_tools(self):
        first_tool = FakeTool("first")
        second_tool = FakeTool("second")
        provider = RecordingProvider(
            make_provider(
                tool_script([("call_0", "first", {})]),
                text_script("done"),
            )
        )

        async def prepare_next(context):
            if context.turn != 0:
                return None
            return NextTurnUpdate(
                config=replace(context.config, tools=[second_tool]),
            )

        config = LoopConfig(
            system="",
            tools=[first_tool],
            hooks=AgentHooks(prepare_next_turn=prepare_next),
        )
        events = await collect(provider, [], config=config)

        self.assertIsInstance(events[-1], AgentDone)
        self.assertEqual(
            [tool.name for tool in provider.requests[1].tools],
            ["second"],
        )

    async def test_before_tool_call_error_becomes_tool_error(self):
        tool = FakeTool("echo", required=("text",))
        provider = make_provider(
            tool_script([("call_0", "echo", {"text": "a"})]),
            text_script("done"),
        )

        async def before(context):
            raise RuntimeError("policy failed")

        config = LoopConfig(
            system="",
            tools=[tool],
            hooks=AgentHooks(before_tool_call=before),
        )
        events = await collect(provider, [], config=config)

        self.assertEqual(tool.calls, [])
        end = next(event for event in events if isinstance(event, ToolExecutionEnd))
        self.assertTrue(end.result.is_error)
        self.assertEqual(
            end.result.content,
            "before_tool_call failed: RuntimeError: policy failed",
        )
        self.assertIsInstance(events[-1], AgentDone)

    async def test_transform_messages_error_becomes_config_error(self):
        provider = RecordingProvider(make_provider(text_script("ok")))

        async def transform(context):
            raise ValueError("bad history")

        config = LoopConfig(
            system="",
            hooks=AgentHooks(transform_messages=transform),
        )
        events = await collect(provider, [], config=config)

        self.assertEqual(len(provider.requests), 0)
        error = events[-1]
        self.assertIsInstance(error, AgentError)
        self.assertEqual(error.kind, "config")
        self.assertIn(
            "transform_messages failed: ValueError: bad history",
            error.message,
        )


    async def test_before_run_can_replace_messages_and_config(self):
        provider = RecordingProvider(make_provider(text_script("ok")))

        async def before_run(context):
            return BeforeRunUpdate(
                messages=(UserMessage("replaced"),),
                config=replace(context.config, system="changed"),
            )

        config = LoopConfig(
            system="original",
            hooks=AgentHooks(before_run=before_run),
        )
        events = await collect(provider, [], config=config)

        self.assertIsInstance(events[-1], AgentDone)
        self.assertEqual(provider.requests[0].system, "changed")
        self.assertEqual(len(provider.requests[0].messages), 1)
        self.assertEqual(provider.requests[0].messages[0].content, "replaced")

    async def test_before_run_error_becomes_config_error(self):
        provider = RecordingProvider(make_provider(text_script("ok")))

        async def before_run(context):
            raise ValueError("bad config")

        config = LoopConfig(
            system="",
            hooks=AgentHooks(before_run=before_run),
        )
        events = await collect(provider, [], config=config)

        self.assertEqual(len(provider.requests), 0)
        error = events[-1]
        self.assertIsInstance(error, AgentError)
        self.assertEqual(error.kind, "config")
        self.assertIn(
            "before_run failed: ValueError: bad config",
            error.message,
        )

    async def test_before_finish_can_rewrite_result(self):
        provider = make_provider(text_script("ok", usage=(5, 2)))

        async def before_finish(context):
            return BeforeFinishUpdate(
                new_messages=(UserMessage("summary"),),
                usage=Usage(input_tokens=100, output_tokens=7),
                stop_reason="length",
            )

        config = LoopConfig(
            system="",
            hooks=AgentHooks(before_finish=before_finish),
        )
        events = await collect(provider, [], config=config)
        done = events[-1]

        self.assertIsInstance(done, AgentDone)
        self.assertEqual(len(done.new_messages), 1)
        self.assertEqual(done.new_messages[0].content, "summary")
        self.assertEqual(done.usage.input_tokens, 100)
        self.assertEqual(done.usage.output_tokens, 7)
        self.assertEqual(done.stop_reason, "length")

    async def test_before_finish_error_becomes_config_error(self):
        provider = make_provider(text_script("ok"))

        async def before_finish(context):
            raise ValueError("cannot finalize")

        config = LoopConfig(
            system="",
            hooks=AgentHooks(before_finish=before_finish),
        )
        events = await collect(provider, [], config=config)
        error = events[-1]

        self.assertIsInstance(error, AgentError)
        self.assertEqual(error.kind, "config")
        self.assertIn(
            "before_finish failed: ValueError: cannot finalize",
            error.message,
        )

    async def test_on_max_turns_can_extend_limit(self):
        tool = FakeTool("echo", required=("text",))
        provider = RecordingProvider(
            make_provider(
                tool_script([("call_0", "echo", {"text": "a"})]),
                text_script("done"),
            )
        )
        calls: list[int] = []

        async def on_max_turns(context):
            calls.append(context.turns)
            if len(calls) == 1:
                return MaxTurnsDecision(extend_by=1)
            return None

        config = LoopConfig(
            system="",
            tools=[tool],
            max_turns=1,
            hooks=AgentHooks(on_max_turns=on_max_turns),
        )
        events = await collect(provider, [], config=config)
        done = events[-1]

        self.assertIsInstance(done, AgentDone)
        self.assertEqual(done.turns, 2)
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(calls, [1])

    async def test_on_max_turns_error_becomes_config_error(self):
        tool = FakeTool("echo")
        provider = make_provider(tool_script([("call_0", "echo", {})]))

        async def on_max_turns(context):
            raise RuntimeError("limit policy failed")

        config = LoopConfig(
            system="",
            tools=[tool],
            max_turns=1,
            hooks=AgentHooks(on_max_turns=on_max_turns),
        )
        events = await collect(provider, [], config=config)
        error = events[-1]

        self.assertIsInstance(error, AgentError)
        self.assertEqual(error.kind, "config")
        self.assertIn("on_max_turns failed", error.message)


if __name__ == "__main__":
    unittest.main()
