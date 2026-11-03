"""把 fixture 回放成内部事件，用来人工核对解析结果。

用法（在 Gradus 目录下）：
    uv run python -m gradus.ai.scripts.replay <provider> <fixture>
    uv run python -m gradus.ai.scripts.replay anthropic gradus/ai/tests/fixtures/anthropic_text_tool.sse

做的事：读 fixture → `parse_sse()` 切分 → 顶掉适配器的 `post_sse` → 打印 12 种事件。
不写断言；断言在 `tests/test_replay.py`。怀疑某家解析有问题时，先跑这个看事件对不对。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
from pathlib import Path
from typing import Any

from gradus.ai.base import ModelSpec, Request
from gradus.ai.transport import parse_sse
from gradus.ai.types import (
    AssistantMessage,
    StreamDone,
    StreamError,
    TextDelta,
    TextEnd,
    ThinkingDelta,
    ThinkingEnd,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    UserMessage,
)

PROVIDERS = {
    "openai_compat": ("gradus.ai.providers.openai_compat", "OpenAIChatProvider"),
    "anthropic": ("gradus.ai.providers.anthropic", "AnthropicProvider"),
    "gemini": ("gradus.ai.providers.gemini", "GeminiProvider"),
    "openai_responses": ("gradus.ai.providers.openai_responses", "OpenAIResponsesProvider"),
}


def make_spec(provider: str) -> ModelSpec:
    # 回放不联网，这些值只用来构造 Provider，不参与解析
    return ModelSpec(
        provider=provider,
        model="replay",
        base_url="http://replay.invalid",
        context_window=1024,
        max_output_tokens=1024,
    )


def describe(event: Any) -> str:
    name = type(event).__name__
    bits: list[str] = []

    index = getattr(event, "content_index", None)
    if index is not None:
        bits.append(f"idx={index}")

    if isinstance(event, (TextDelta, ThinkingDelta)):
        bits.append(f"+{len(event.delta)} {event.delta!r}")
    elif isinstance(event, TextEnd):
        bits.append(f"len={len(event.text)}")
    elif isinstance(event, ThinkingEnd):
        bits.append(f"len={len(event.thinking)} sig={event.signature!r}")
    elif isinstance(event, ToolCallStart):
        bits.append(f"id={event.id} name={event.name}")
    elif isinstance(event, ToolCallDelta):
        bits.append(f"+{len(event.delta)} {event.delta!r}")
    elif isinstance(event, ToolCallEnd):
        args = json.dumps(event.arguments, ensure_ascii=False)
        bits.append(f"args={args}")
    elif isinstance(event, StreamDone):
        usage = event.usage
        bits.append(
            f"stop={event.stop_reason} in={usage.input_tokens} out={usage.output_tokens} "
            f"cache_read={usage.cache_read_tokens} cache_write={usage.cache_write_tokens}"
        )
    elif isinstance(event, StreamError):
        bits.append(f"kind={event.kind} {event.error_message}")

    return f"{name:<14}  " + "  ".join(bits)


def describe_message(message: AssistantMessage) -> str:
    parts: list[str] = []
    for block in message.content:
        kind = type(block).__name__
        if kind == "TextBlock":
            tag = "refusal" if block.refusal else "text"
            parts.append(f"{tag}({len(block.text)})")
        elif kind == "ThinkingBlock":
            parts.append(f"thinking({len(block.thinking)})")
        else:
            args = json.dumps(block.arguments, ensure_ascii=False)
            parts.append(f"tool:{block.name}{args}")
    return " ".join(parts) if parts else "(empty)"


def main() -> int:
    parser = argparse.ArgumentParser(description="回放 fixture 并打印内部事件")
    parser.add_argument("provider", choices=sorted(PROVIDERS))
    parser.add_argument("fixture", help="fixture 文件路径")
    args = parser.parse_args()

    fixture = Path(args.fixture)
    if not fixture.exists():
        print(f"fixture 不存在：{fixture}")
        return 1

    module_path, class_name = PROVIDERS[args.provider]
    module = importlib.import_module(module_path)
    provider = getattr(module, class_name)(make_spec(args.provider))

    lines = fixture.read_text(encoding="utf-8").splitlines(keepends=True)
    events = list(parse_sse(lines))

    async def fake_post_sse(*args: Any, **kwargs: Any):
        # 顶掉网络：适配器 await 的每一条记录都来自 fixture
        for event in events:
            yield event

    async def collect() -> list[Any]:
        request = Request(system="", messages=[UserMessage("replay")])
        return [event async for event in provider.stream(request)]

    module.post_sse = fake_post_sse  # 其余链路照旧
    result = asyncio.run(collect())

    for event in result:
        print(describe(event))

    last = result[-1]
    if isinstance(last, StreamError):
        print(f"\nblocks: {describe_message(last.partial)}")
    else:
        print(f"\nblocks: {describe_message(last.message)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
