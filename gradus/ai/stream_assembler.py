"""流式装配器：把内容增量装配成带 partial 快照的 12 个事件。

四个适配器都要处理同样的三件事：内容块何时开始、partial 快照怎么拼、结束事件带什么。
放在这里由四家共用，适配器只负责把厂商的 SSE 翻译成对 assembler 的调用。

不变量：
- content_index 由本类从 0 分配，与厂商索引无关
- partial 每次都是深拷贝快照，后续 delta 不会改写已发出的事件
- 同一时刻最多一个内容块是打开的；开新块会先关旧块
- done() / fail() 会先补上未关闭块的 End 事件
"""

from __future__ import annotations

import copy
import json
from typing import Any, Literal, cast

from .types import (
    AssistantEvent,
    AssistantMessage,
    ContentBlock,
    StopReason,
    StreamDone,
    StreamError,
    StreamStart,
    TextBlock,
    TextDelta,
    TextEnd,
    TextStart,
    ThinkingBlock,
    ThinkingDelta,
    ThinkingEnd,
    ThinkingStart,
    ToolCallBlock,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)

_BlockKind = Literal["text", "thinking", "tool"]


class StreamAssembler:
    """按块累积 AssistantMessage，并产出对应事件。"""

    def __init__(self) -> None:
        self._message = AssistantMessage()
        self._usage = Usage()
        self._open: _BlockKind | None = None
        self._index = -1
        self._tool_raw = ""

    # ---- 起始 ---------------------------------------------------------------

    def start(self) -> StreamStart:
        return StreamStart(partial=self._snapshot())

    # ---- 文本 ---------------------------------------------------------------

    def start_text(self, refusal: bool = False) -> tuple[AssistantEvent, ...]:
        """开一个文本块。refusal 只影响块上的标记，事件形状不变。"""
        if self._open == "text":
            return ()
        events = list(self._close_open())
        self._index = len(self._message.content)
        self._message.content.append(TextBlock(text="", refusal=refusal))
        self._open = "text"
        events.append(TextStart(content_index=self._index, partial=self._snapshot()))
        return tuple(events)

    def text(self, delta: str) -> tuple[AssistantEvent, ...]:
        events = list(self.start_text())
        if delta:
            self._block(TextBlock).text += delta
            events.append(
                TextDelta(content_index=self._index, delta=delta, partial=self._snapshot())
            )
        return tuple(events)

    def end_text(self) -> tuple[AssistantEvent, ...]:
        if self._open != "text":
            return ()
        block = self._block(TextBlock)
        self._open = None
        return (TextEnd(content_index=self._index, text=block.text, partial=self._snapshot()),)

    # ---- 思考 ---------------------------------------------------------------

    def start_thinking(self) -> tuple[AssistantEvent, ...]:
        if self._open == "thinking":
            return ()
        events = list(self._close_open())
        self._index = len(self._message.content)
        self._message.content.append(ThinkingBlock(thinking=""))
        self._open = "thinking"
        events.append(ThinkingStart(content_index=self._index, partial=self._snapshot()))
        return tuple(events)

    def thinking(self, delta: str) -> tuple[AssistantEvent, ...]:
        events = list(self.start_thinking())
        if delta:
            self._block(ThinkingBlock).thinking += delta
            events.append(
                ThinkingDelta(content_index=self._index, delta=delta, partial=self._snapshot())
            )
        return tuple(events)

    def end_thinking(self, signature: str | None = None) -> tuple[AssistantEvent, ...]:
        if self._open != "thinking":
            return ()
        block = self._block(ThinkingBlock)
        if signature is not None:
            block.signature = signature
        self._open = None
        return (
            ThinkingEnd(
                content_index=self._index,
                thinking=block.thinking,
                signature=block.signature,
                partial=self._snapshot(),
            ),
        )

    # ---- 工具调用 -----------------------------------------------------------

    def start_tool(self, call_id: str, name: str) -> tuple[AssistantEvent, ...]:
        events = list(self._close_open())
        self._index = len(self._message.content)
        self._message.content.append(ToolCallBlock(id=call_id, name=name))
        self._open = "tool"
        self._tool_raw = ""
        events.append(
            ToolCallStart(
                content_index=self._index,
                id=call_id,
                name=name,
                partial=self._snapshot(),
            )
        )
        return tuple(events)

    def tool_arguments(self, fragment: str) -> tuple[AssistantEvent, ...]:
        if self._open != "tool":
            return ()
        self._tool_raw += fragment
        # 同步到块上，让每个事件的 partial 都能看到参数进度
        self._block(ToolCallBlock).raw = self._tool_raw
        return (
            ToolCallDelta(content_index=self._index, delta=fragment, partial=self._snapshot()),
        )

    def end_tool(self, arguments: dict[str, Any] | None = None) -> tuple[AssistantEvent, ...]:
        """结束工具块。

        arguments 为 None 时解析累积的参数片段；解析失败（如 length 截断）留空 dict，
        该批调用是否执行由 StopReason 决定。Gemini 这类直接给完整对象的厂商传入 dict。
        """
        if self._open != "tool":
            return ()
        if arguments is None:
            try:
                arguments = json.loads(self._tool_raw) if self._tool_raw else {}
            except json.JSONDecodeError:
                arguments = {}
        block = self._block(ToolCallBlock)
        block.arguments = arguments
        # 没走过分片的厂商（Gemini）在这里补出 raw；走过分片的保留原文，截断时也能查到
        if not block.raw:
            block.raw = json.dumps(arguments, ensure_ascii=False)
        self._open = None
        self._tool_raw = ""
        return (
            ToolCallEnd(content_index=self._index, arguments=arguments, partial=self._snapshot()),
        )

    # ---- 用量与终止 ---------------------------------------------------------

    def add_usage(self, usage: Usage) -> None:
        """累加用量。用于各事件报的是"增量"或"各报一部分"的协议。"""
        self._usage = self._usage + usage

    def set_usage(self, usage: Usage) -> None:
        """整体替换用量。用于事件报的是"截至目前的累计值"而非增量的协议。

        Anthropic 的 message_delta 就属这一类：官方只重复 output_tokens，
        兼容端点会把完整 usage 再报一遍，累加会翻倍。
        """
        self._usage = usage

    def done(self, reason: StopReason) -> tuple[AssistantEvent, ...]:
        events = list(self._close_open())
        self._message.stop_reason = reason
        self._message.usage = self._usage
        events.append(
            StreamDone(
                stop_reason=reason,
                message=self._snapshot(),
                usage=self._usage,
            )
        )
        return tuple(events)

    def fail(
        self,
        kind: Literal["error", "aborted"],
        error_message: str,
    ) -> tuple[AssistantEvent, ...]:
        events = list(self._close_open())
        self._message.stop_reason = kind
        self._message.usage = self._usage
        events.append(
            StreamError(
                kind=kind,
                error_message=error_message,
                partial=self._snapshot(),
            )
        )
        return tuple(events)

    # ---- 内部 ---------------------------------------------------------------

    def _close_open(self) -> tuple[AssistantEvent, ...]:
        if self._open == "text":
            return self.end_text()
        if self._open == "thinking":
            return self.end_thinking()
        if self._open == "tool":
            return self.end_tool()
        return ()

    def _block(self, expected: type) -> Any:
        block: ContentBlock = self._message.content[self._index]
        if not isinstance(block, expected):
            raise TypeError(f"当前打开的块不是 {expected.__name__}")
        return cast(Any, block)

    def _snapshot(self) -> AssistantMessage:
        return copy.deepcopy(self._message)
