"""wire 层只做形状转换，测试不需要起服务，也不需要模型。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from gradus.agent.types import (
    AgentDone,
    AgentError,
    AgentStart,
    ToolExecutionEnd,
    ToolExecutionStart,
    ToolExecutionUpdate,
    ToolResult,
    TurnEnd,
    TurnStart,
)
from gradus.ai.types import (
    AssistantMessage,
    TextBlock,
    TextDelta,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from gradus.host.wire import encode_event, history_payload, snapshot_payload


def _call() -> ToolCallBlock:
    return ToolCallBlock(id="call-1", name="bash", arguments={"command": "ls"})


class WireTest(unittest.TestCase):
    def test_agent_start(self) -> None:
        assert encode_event(AgentStart()) == {"type": "agent/start"}

    def test_turn_start(self) -> None:
        assert encode_event(TurnStart(turn=2)) == {"type": "turn/start", "turn": 2}

    def test_text_delta(self) -> None:
        event = TextDelta(content_index=0, delta="你好", partial=AssistantMessage())
        assert encode_event(event) == {"type": "assistant/delta", "text": "你好"}

    def test_thinking_delta_is_not_forwarded(self) -> None:
        from gradus.ai.types import ThinkingDelta

        event = ThinkingDelta(content_index=0, delta="...", partial=AssistantMessage())
        assert encode_event(event) is None

    def test_tool_lifecycle(self) -> None:
        call = _call()

        assert encode_event(ToolExecutionStart(call=call)) == {
            "type": "tool/start",
            "call": {"id": "call-1", "name": "bash", "args": {"command": "ls"}},
        }

        assert encode_event(ToolExecutionUpdate(call=call, partial="REA")) == {
            "type": "tool/update",
            "id": "call-1",
            "partial": "REA",
        }

        end = ToolExecutionEnd(
            call=call,
            result=ToolResult(content="README.md"),
            duration_ms=12,
        )
        assert encode_event(end) == {
            "type": "tool/end",
            "id": "call-1",
            "result": {"content": "README.md", "isError": False},
            "durationMs": 12,
        }

    def test_usage_carried_by_turn_and_done(self) -> None:
        usage = Usage(input_tokens=10, output_tokens=4)

        assert encode_event(TurnEnd(assistant=AssistantMessage(), tool_results=[], usage=usage)) == {
            "type": "turn/end",
            "usage": {"inputTokens": 10, "outputTokens": 4},
        }

        done = AgentDone(new_messages=[], usage=usage, turns=1, stop_reason="stop")
        assert encode_event(done) == {
            "type": "agent/done",
            "usage": {"inputTokens": 10, "outputTokens": 4},
            "turns": 1,
        }

    def test_error_keeps_kind_and_message(self) -> None:
        error = AgentError(
            kind="aborted",
            message="已取消",
            partial=None,
            new_messages=[],
            usage=Usage(),
        )
        assert encode_event(error) == {
            "type": "agent/error",
            "kind": "aborted",
            "message": "已取消",
        }

    def test_history_only_keeps_text(self) -> None:
        payload = history_payload(
            (
                UserMessage("你好"),
                ToolResultMessage(tool_call_id="call-1", content="ok"),
                AssistantMessage(content=[ThinkingBlock(thinking="...")]),
                AssistantMessage(content=[TextBlock(text="回答"), ThinkingBlock(thinking="...")]),
            )
        )

        assert payload == [
            {"id": "m-0", "role": "user", "text": "你好"},
            {"id": "m-3", "role": "assistant", "text": "回答"},
        ]

    def test_snapshot_shape(self) -> None:
        spec = SimpleNamespace(provider="openai_compat", model="kimi")
        session = SimpleNamespace(
            messages=(UserMessage("hi"),),
            tools=(SimpleNamespace(spec=SimpleNamespace(name="bash")),),
            sandbox_backend="restricted",
            sandbox_warning="network NOT isolated",
            workspace="/tmp/demo",
            settings=SimpleNamespace(spec=spec, models=()),
        )

        payload = snapshot_payload(session)

        assert payload["protocolVersion"] == 1
        assert payload["messages"] == [{"id": "m-0", "role": "user", "text": "hi"}]
        assert payload["tools"] == ["bash"]
        assert payload["workspace"] == "/tmp/demo"
        assert payload["model"] == {
            "name": "kimi · openai_compat",
            "provider": "openai_compat",
            "model": "kimi",
        }
        assert payload["sandbox"] == {
            "backend": "restricted",
            "warning": "network NOT isolated",
        }
        assert "session/message" in payload["capabilities"]
