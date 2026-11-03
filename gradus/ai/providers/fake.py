# =============================================================================
# Fake Provider（不是真实厂商，没有外部协议）
# 作用：按预置剧本离线产出内部事件流，不联网、不花钱、结果确定。
# =============================================================================

# ---- 为什么需要它 -----------------------------------------------------------
# agent 层的循环、reducer、UI 都依赖事件流；如果只能靠真实 API 验证，
# 每次改动都要花钱、要等网络、还要看模型心情。
# fake 让「12 个事件」和「各种终止路径」变成可断言的固定剧本。

# ---- 剧本格式 ---------------------------------------------------------------
# 一个剧本对应一次 stream() 调用；多个剧本按顺序消费，用于多轮（先调工具、再回答）。
# 剧本用尽后再调用，返回一个空内容的 StreamDone("stop")。
#
# FakeProvider(spec, scripts=[
#     {"name": "tool_call", "steps": [
#         {"kind": "thinking", "chunks": ["需要读文件"]},
#         {"kind": "text",     "chunks": ["我先看下", "文件"]},
#         {"kind": "tool_call", "id": "call_0", "name": "read_file",
#          "arguments": {"path": "a.py"},
#          "arg_chunks": ["{\"path\"", ": \"a.py\"}"]},
#         {"kind": "usage",    "input": 100, "output": 20},
#         {"kind": "finish",   "reason": "tool_use"}
#     ]},
#     {"name": "answer", "steps": [
#         {"kind": "text",   "chunks": ["文件里是 print(1)"]},
#         {"kind": "finish", "reason": "stop"}
#     ]}
# ])
#
# 字段说明：
# - chunks：分片，用来模拟增量
# - signature：thinking 块的不透明回传数据，可省略
# - arg_chunks：模拟 OpenAI 那种参数字符串分片；省略则一次给出完整 json.dumps(arguments)
# - usage：可多次出现，累加进最终的 Usage
# - finish.reason：内部 StopReason，直接驱动调用方分支
# - error 步骤：{"kind": "error", "error_kind": "aborted", "message": "..."}
# - 步骤未知时产出 StreamError，不抛异常（与 Provider 契约一致）
# - 截断场景：给一个不完整的 arg_chunks，再 finish 成 "length"；本实现仍会补
#   ToolCallEnd，arguments 为空 dict，执不执行由 StopReason 决定
# - 事件本身的累积、快照、content_index 全部交给 StreamAssembler，本文件只管剧本

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from ..base import ModelSpec, Request
from ..stream_assembler import StreamAssembler
from ..types import AssistantEvent, Usage


class FakeProvider:
    """按剧本直接产出内部事件，不联网、结果确定。"""

    def __init__(
        self,
        spec: ModelSpec,
        scripts: list[dict[str, Any]],
        api_key: str = "",
    ) -> None:
        self.spec = spec
        self.api_key = api_key
        self._scripts = list(scripts)
        self._calls = 0

    async def stream(self, request: Request) -> AsyncIterator[AssistantEvent]:
        assembler = StreamAssembler()
        yield assembler.start()

        for step in self._take_steps():
            kind = step.get("kind")

            if kind == "text":
                for _event in assembler.start_text():
                    yield _event
                for chunk in step.get("chunks", []):
                    for _event in assembler.text(chunk):
                        yield _event
                for _event in assembler.end_text():
                    yield _event

            elif kind == "thinking":
                for _event in assembler.start_thinking():
                    yield _event
                for chunk in step.get("chunks", []):
                    for _event in assembler.thinking(chunk):
                        yield _event
                for _event in assembler.end_thinking(step.get("signature")):
                    yield _event

            elif kind == "tool_call":
                for _event in assembler.start_tool(step["id"], step["name"]):
                    yield _event
                fragments = step.get("arg_chunks")
                if fragments is None:
                    arguments = step.get("arguments", {})
                    fragments = [json.dumps(arguments, ensure_ascii=False)]
                for fragment in fragments:
                    for _event in assembler.tool_arguments(fragment):
                        yield _event
                for _event in assembler.end_tool():
                    yield _event

            elif kind == "usage":
                assembler.add_usage(
                    Usage(
                        input_tokens=step.get("input", 0),
                        output_tokens=step.get("output", 0),
                        cache_read_tokens=step.get("cache_read", 0),
                        cache_write_tokens=step.get("cache_write", 0),
                    )
                )

            elif kind == "error":
                for _event in assembler.fail(step.get("error_kind", "error"), step.get("message", "")):
                    yield _event
                return

            elif kind == "finish":
                for _event in assembler.done(step.get("reason", "stop")):
                    yield _event
                return

            else:
                for _event in assembler.fail("error", f"unknown script step: {kind!r}"):
                    yield _event
                return

        # 剧本没写终结步骤时，按正常结束处理
        for _event in assembler.done("stop"):
            yield _event

    def _take_steps(self) -> list[dict[str, Any]]:
        if self._calls >= len(self._scripts):
            self._calls += 1
            return []
        script = self._scripts[self._calls]
        self._calls += 1
        return list(script.get("steps", []))
