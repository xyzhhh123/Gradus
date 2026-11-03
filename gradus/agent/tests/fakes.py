"""agent 测试共用的假 provider、假工具与脚本构造器。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from gradus.agent.base import AgentTool, LoopConfig
from gradus.agent.loop import run_loop
from gradus.agent.types import ToolResult
from gradus.ai.base import ModelSpec, Provider, Request, ToolSpec
from gradus.ai.providers.fake import FakeProvider
from gradus.ai.types import AssistantEvent, Usage, UserMessage


def make_spec() -> ModelSpec:
    return ModelSpec(
        provider="fake",
        model="fake-1",
        base_url="http://localhost",
        context_window=8000,
        max_output_tokens=1024,
    )


def make_provider(*scripts: dict[str, Any]) -> FakeProvider:
    return FakeProvider(make_spec(), list(scripts))


def text_script(
    text: str = "ok",
    *,
    reason: str = "stop",
    usage: tuple[int, int] | None = None,
) -> dict[str, Any]:
    steps: list[dict[str, Any]] = [{"kind": "text", "chunks": [text]}]
    if usage is not None:
        steps.append({"kind": "usage", "input": usage[0], "output": usage[1]})
    steps.append({"kind": "finish", "reason": reason})
    return {"name": "text", "steps": steps}


def tool_script(
    calls: list[tuple[str, str, dict[str, Any]]],
    *,
    reason: str = "tool_use",
    usage: tuple[int, int] | None = None,
) -> dict[str, Any]:
    steps: list[dict[str, Any]] = [
        {
            "kind": "tool_call",
            "id": call_id,
            "name": name,
            "arguments": arguments,
        }
        for call_id, name, arguments in calls
    ]
    if usage is not None:
        steps.append({"kind": "usage", "input": usage[0], "output": usage[1]})
    steps.append({"kind": "finish", "reason": reason})
    return {"name": "tools", "steps": steps}


class FakeTool:
    """最小的 AgentTool 实现，记录调用参数并返回固定结果。"""

    def __init__(
        self,
        name: str = "echo",
        *,
        required: tuple[str, ...] = (),
        result: str | None = None,
        error: BaseException | None = None,
        delay: float = 0.0,
        execution_mode: str = "serial",
        terminate: bool = False,
        updates: tuple[str, ...] = (),
        details: Any = None,
        structured_content: Any = None,
        usage: Usage | None = None,
    ) -> None:
        properties = {key: {"type": "string"} for key in required}
        parameters: dict[str, Any] = {
            "type": "object",
            "properties": properties,
        }
        if required:
            parameters["required"] = list(required)

        self.spec = ToolSpec(name=name, description="", parameters=parameters)
        self.execution_mode = execution_mode
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._result = result
        self._error = error
        self._delay = delay
        self._terminate = terminate
        self._updates = updates
        self._details = details
        self._structured_content = structured_content
        self._usage = usage

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: Any = None,
    ) -> ToolResult:
        self.calls.append((call_id, arguments))
        for chunk in self._updates:
            if on_update is not None:
                on_update(chunk)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        if self._result is not None:
            return ToolResult(
                self._result,
                terminate=self._terminate,
                details=self._details,
                structured_content=self._structured_content,
                usage=self._usage,
            )
        return ToolResult(
            str(arguments.get("text", "ok")),
            terminate=self._terminate,
            details=self._details,
            structured_content=self._structured_content,
            usage=self._usage,
        )


class RaisingProvider:
    """违反 Provider 契约、直接抛异常的测试替身。"""

    spec = make_spec()

    async def stream(self, request: Any):
        # 必须保留 yield：它让 stream 成为 async generator，而不是普通协程。
        # 因为前面直接 raise，这一行不可达是故意的。
        raise RuntimeError("boom")
        yield  # pragma: no cover


class _EmptyStream:
    def __aiter__(self) -> "_EmptyStream":
        return self

    async def __anext__(self) -> AssistantEvent:
        raise StopAsyncIteration


class NoDoneProvider:
    """不发 StreamDone、迭代直接结束的测试替身。"""

    spec = make_spec()

    def stream(self, request: Request) -> AsyncIterator[AssistantEvent]:
        return _EmptyStream()


class BadResultTool:
    """返回非 ToolResult 的测试替身。"""

    execution_mode = "serial"

    spec = ToolSpec(
        name="bad",
        description="",
        parameters={"type": "object", "properties": {}},
    )

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: Any = None,
    ) -> ToolResult:
        return "not a tool result"  # type: ignore[return-value]


class CancelOnSecondTool:
    """第一次调用成功，第二次抛 CancelledError。"""

    execution_mode = "serial"

    spec = ToolSpec(
        name="cancel",
        description="",
        parameters={"type": "object", "properties": {}},
    )

    def __init__(self) -> None:
        self.calls = 0

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: Any = None,
    ) -> ToolResult:
        self.calls += 1
        if self.calls == 2:
            raise asyncio.CancelledError()
        return ToolResult("first")


class RecordingProvider:
    """包住 FakeProvider，记录每次请求，用于断言 hook 改写了什么。"""

    def __init__(self, inner: Provider) -> None:
        self.spec = inner.spec
        self.inner = inner
        self.requests: list[Request] = []

    async def stream(self, request: Request) -> AsyncIterator[AssistantEvent]:
        self.requests.append(request)
        async for event in self.inner.stream(request):
            yield event


async def collect(
    provider: Provider,
    tools: list[AgentTool],
    *,
    max_turns: int = 20,
    messages: list[UserMessage] | None = None,
    config: LoopConfig | None = None,
) -> list[Any]:
    if config is None:
        config = LoopConfig(system="", tools=tools, max_turns=max_turns)
    target = messages if messages is not None else [UserMessage("hi")]
    return [event async for event in run_loop(provider, target, config)]


def names_of(events: list[Any]) -> list[str]:
    return [type(event).__name__ for event in events]
