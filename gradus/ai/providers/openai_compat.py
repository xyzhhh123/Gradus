# =============================================================================
# OpenAI Chat Completions 兼容格式
# 代表：DeepSeek；同类还有 Qwen / Kimi / GLM / 豆包 / SiliconFlow / Ollama / vLLM
# 上半部分记录协议格式，下半部分是实现。
# =============================================================================

# ---- 端点与鉴权 -------------------------------------------------------------
# POST {base_url}/chat/completions
# Authorization: Bearer <api_key>
# Content-Type: application/json

# ---- 请求体 ----------------------------------------------------------------
# {
#   "model": "deepseek-chat",
#   "messages": [
#     {"role": "system",  "content": "..."},
#     {"role": "user",    "content": "..."},
#     {"role": "assistant", "content": "我先看下文件", "tool_calls": [
#         {"id": "call_0", "type": "function",
#          "function": {"name": "read_file", "arguments": "{\"path\": \"a.py\"}"}}
#     ]},
#     {"role": "tool", "tool_call_id": "call_0", "content": "print(1)"}
#   ],
#   "tools": [
#     {"type": "function",
#      "function": {"name": "read_file", "description": "...",
#                   "parameters": {"type": "object", "properties": {...}, "required": [...]}}}
#   ],
#   "tool_choice": "auto",
#   "stream": true,
#   "stream_options": {"include_usage": true},
#   "max_tokens": 1024,
#   "temperature": 0.2
# }

# ---- 流式响应 --------------------------------------------------------------
# 纯 SSE，每行形如 "data: {json}"，以 "data: [DONE]" 结束。
#
# data: {"choices":[{"index":0,"delta":{"role":"assistant","content":"你"},"finish_reason":null}]}
# data: {"choices":[{"index":0,"delta":{"content":"好"},"finish_reason":null}]}
# data: {"choices":[{"index":0,"delta":{"tool_calls":[
#           {"index":0,"id":"call_0","type":"function","function":{"name":"read_file","arguments":"{\"pa"}}
#       ]},"finish_reason":null}]}
# data: {"choices":[{"index":0,"delta":{"tool_calls":[
#           {"index":0,"function":{"arguments":"th\": \"a.py\"}"}}
#       ]},"finish_reason":null}]}
# data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}
# data: {"choices":[],"usage":{"prompt_tokens":100,"completion_tokens":20,
#         "total_tokens":120,"prompt_tokens_details":{"cached_tokens":64}}}
# data: [DONE]
#
# 要点：
# - tool_calls 只在第一片带 id 和 name，后续片只带 arguments 的字符串片段
# - 多个工具调用用 tool_calls[].index 区分，同一 index 的 arguments 需要字符串拼接
# - finish_reason 出现时 delta 通常是空对象，此时不代表流结束（usage 可能还在后面）
# - 推理内容（DeepSeek）：delta.reasoning_content 是独立字段，不属于 content 数组
# - 拒答内容：delta.refusal 是独立字段（字符串），不在 content 里
# - content 在纯工具调用时可能为 null
# - 不带 index 的实现按 0 处理；arguments 为空字符串表示无参数
# - usage 一般挂在顶层，但 Kimi 把它放在 choices[0].usage 里（与 finish_reason 同一个
#   chunk），两处都要认，否则用量全是 0

# ---- 终止原因 → 内部 StopReason --------------------------------------------
# finish_reason = "stop"        -> "stop"
# finish_reason = "length"      -> "length"（参数可能被截断，调用方不得执行该批工具）
# finish_reason = "tool_calls"  -> "tool_use"
# finish_reason = "content_filter" -> "filtered"
# delta.refusal 非空           -> "filtered"（拒答通常配 stop，见 D18）

# ---- 用量 → 内部 Usage -----------------------------------------------------
# usage.prompt_tokens            -> input_tokens
# usage.completion_tokens        -> output_tokens
# usage.prompt_tokens_details.cached_tokens -> cache_read_tokens
# choices[].usage（Kimi 的位置） -> 同上，仅当顶层没有时采用，避免重复累加
# 写入型缓存无对应字段，cache_write_tokens 保持 0

# ---- 错误 ------------------------------------------------------------------
# HTTP 4xx/5xx，体为 {"error": {"message": "...", "type": "...", "code": "..."}}
# 429/5xx 可重试，需读取 Retry-After / retry-after-ms 响应头
# 个别兼容端点会在 200 的流里塞 {"error": {...}}，同样要转成 StreamError
# 这些错误必须被适配器转成 StreamError 事件，不允许抛异常

# ---- 内部形状 → 本厂商形状 的映射表 ------------------------------------------
# Request.system     -> messages[0] = {"role": "system", "content": ...}
# Request.reasoning  -> reasoning_effort（本协议没有统一字段，不支持的厂商会返回 400）
# UserMessage        -> {"role": "user", "content": ...}
# AssistantMessage   -> {"role": "assistant", "content": ..., "tool_calls": [...]}
# TextBlock          -> content 字符串
# ThinkingBlock      -> 独立字段 reasoning_content（无 signature 概念）
# ToolCallBlock      -> tool_calls[]，arguments 需 json.dumps 成字符串
# ToolResultMessage  -> {"role": "tool", "tool_call_id": ..., "content": ...}
#                       本协议没有 is_error 字段，错误结果加 "Error: " 前缀（见 D19）

# =============================================================================
# 实现
# =============================================================================

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable
from typing import Any

import httpx

from ..base import ModelSpec, Request, ToolSpec
from ..stream_assembler import StreamAssembler
from ..transport import TransportFailure, post_sse
from ..types import (
    AssistantEvent,
    AssistantMessage,
    StopReason,
    TextBlock,
    ToolCallBlock,
    ToolResultMessage,
    Usage,
    UserMessage,
)


def build_payload(request: Request, spec: ModelSpec) -> dict[str, Any]:
    """把内部 Request 翻成 Chat Completions 请求体。

    纯函数，不发请求。转换规则见本文件上半部分的映射表。
    """
    payload: dict[str, Any] = {
        "model": spec.model,
        "messages": build_messages(request),
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": request.max_output_tokens or spec.max_output_tokens,
    }

    if request.temperature is not None:
        payload["temperature"] = request.temperature

    if request.reasoning:
        # 部分兼容厂商认这个字段；不认的会返回 400，所以只有显式设置时才发
        payload["reasoning_effort"] = request.reasoning

    if request.tools:
        payload["tools"] = [build_tool(tool) for tool in request.tools]
        payload["tool_choice"] = "auto"

    return payload


def _tool_content(message: ToolResultMessage) -> str:
    """工具结果的 content。

    本协议没有 is_error 字段：错误结果包成 {"error": ...}，与成功结果的原文区分开，
    模型看到 JSON 对象就知道这次调用失败了。
    """
    if not message.is_error:
        return message.content
    return json.dumps({"error": message.content}, ensure_ascii=False)


def build_messages(request: Request) -> list[dict[str, Any]]:
    """system 落在 messages[0]，其余按消息类型逐个翻译。"""
    messages: list[dict[str, Any]] = []

    if request.system:
        messages.append({"role": "system", "content": request.system})

    for message in request.messages:
        if isinstance(message, UserMessage):
            messages.append({"role": "user", "content": message.content})
        elif isinstance(message, AssistantMessage):
            messages.append(build_assistant(message))
        elif isinstance(message, ToolResultMessage):
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": message.tool_call_id,
                    # 本协议没有 is_error 字段，错误结果包成 {"error": ...}，与成功结果区分开
                    "content": _tool_content(message),
                }
            )

    return messages


def build_assistant(message: AssistantMessage) -> dict[str, Any]:
    """助手消息：文本合成一个字符串，工具调用放 tool_calls。

    ThinkingBlock 不回传——Chat Completions 没有对应的输入字段，
    reasoning_content 只是响应侧的字段。
    """
    text = "".join(block.text for block in message.content if isinstance(block, TextBlock))
    calls = [
        {
            "id": block.id,
            "type": "function",
            "function": {
                "name": block.name,
                "arguments": json.dumps(block.arguments, ensure_ascii=False),
            },
        }
        for block in message.content
        if isinstance(block, ToolCallBlock)
    ]

    if calls:
        # 纯工具调用时 content 给 null，与本厂商的习惯一致
        return {"role": "assistant", "content": text or None, "tool_calls": calls}

    return {"role": "assistant", "content": text}


def build_tool(tool: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


STOP_REASONS: dict[str, StopReason] = {
    "stop": "stop",
    "length": "length",
    "tool_calls": "tool_use",
    "content_filter": "filtered",
}


def collect_tool_call(pending: dict[int, dict[str, str]], call: dict[str, Any]) -> None:
    """按 index 累积一次工具调用。

    厂商只在第一片给 id 和 name，后续片只有 arguments 的字符串碎片，
    所以这里按 index 建槽位、逐片追加。
    """
    index = call.get("index", 0)
    slot = pending.setdefault(index, {"id": "", "name": "", "args": ""})

    if call_id := call.get("id"):
        slot["id"] = call_id

    function = call.get("function") or {}
    if name := function.get("name"):
        slot["name"] = name
    if fragment := function.get("arguments"):
        slot["args"] += fragment


def replay_tool_calls(
    assembler: StreamAssembler, pending: dict[int, dict[str, str]]
) -> Iterable[AssistantEvent]:
    """按 index 顺序把攒下的工具调用发出去。

    正常结束时统一产出；中断时也用它，让 partial 里至少留下"调用了什么"的骨架。
    参数没拼完时 arguments 会是空 dict，是否执行由 StopReason 决定。
    """
    for index in sorted(pending):
        call = pending[index]
        if not call["name"]:
            continue
        for _event in assembler.start_tool(call["id"], call["name"]):
            yield _event
        if call["args"]:
            for _event in assembler.tool_arguments(call["args"]):
                yield _event
        for _event in assembler.end_tool():
            yield _event


def parse_usage(usage: dict[str, Any]) -> Usage:
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
    return Usage(
        input_tokens=usage.get("prompt_tokens", 0),
        output_tokens=usage.get("completion_tokens", 0),
        cache_read_tokens=cached,
    )


class OpenAIChatProvider:
    """Chat Completions 形状适配器，DeepSeek / Qwen / Kimi / GLM / Ollama 共用。"""

    def __init__(
        self,
        spec: ModelSpec,
        api_key: str = "",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.spec = spec
        self.api_key = api_key
        # 注入的 client 由调用方关闭；None 时 post_sse 每次自建并 aclose（D29）
        self._client = client

    async def stream(self, request: Request) -> AsyncIterator[AssistantEvent]:
        assembler = StreamAssembler()
        yield assembler.start()

        pending: dict[int, dict[str, str]] = {}
        stop_reason: StopReason = "stop"
        saw_refusal = False

        try:
            async for event in post_sse(
                self._url(),
                headers=self._headers(),
                payload=build_payload(request, self.spec),
                client=self._client,
            ):
                if isinstance(event, TransportFailure):
                    # 先把已攒下的调用补出来，partial 里至少能看到调用了什么
                    for _event in replay_tool_calls(assembler, pending):
                        yield _event
                    for _event in assembler.fail("error", event.message):
                        yield _event
                    return

                data = event.data.strip()
                if data == "[DONE]":
                    break

                chunk = json.loads(data)

                # 个别兼容端点会在 200 的流里发错误对象，不识别的话会静默变成空消息
                if error := chunk.get("error"):
                    detail = error.get("message", "stream error") if isinstance(error, dict) else str(error)
                    for _event in replay_tool_calls(assembler, pending):
                        yield _event
                    for _event in assembler.fail("error", detail):
                        yield _event
                    return

                if usage := chunk.get("usage"):
                    assembler.add_usage(parse_usage(usage))

                for choice in chunk.get("choices") or []:
                    # 只认第一条候选；混进同一条消息会串味
                    if choice.get("index", 0) != 0:
                        continue

                    # Kimi 把 usage 塞在 choice 里而不是顶层；顶层已有就不重复累加
                    if not chunk.get("usage") and (usage := choice.get("usage")):
                        assembler.add_usage(parse_usage(usage))

                    delta = choice.get("delta") or {}

                    if reasoning := delta.get("reasoning_content"):
                        for _event in assembler.thinking(reasoning):
                            yield _event

                    if content := delta.get("content"):
                        for _event in assembler.text(content):
                            yield _event

                    # 拒答是独立字段：先关掉正文块，再单独开一个带标记的文本块
                    if refusal := delta.get("refusal"):
                        saw_refusal = True
                        for _event in assembler.end_text():
                            yield _event
                        for _event in assembler.start_text(refusal=True):
                            yield _event
                        for _event in assembler.text(refusal):
                            yield _event

                    for call in delta.get("tool_calls") or []:
                        collect_tool_call(pending, call)

                    if reason := choice.get("finish_reason"):
                        stop_reason = STOP_REASONS.get(reason, "stop")

            # 工具调用攒到最后再发。assembler 同一时刻只能开一个块，而厂商会用
            # index 交错给出多个并行调用；参数本来也要拼完才能用，不损失流式价值。
            for _event in replay_tool_calls(assembler, pending):
                yield _event

            # 拒答也是一种"没给完整答案"，但 length / content_filter 更具体，不覆盖
            if saw_refusal and stop_reason == "stop":
                stop_reason = "filtered"

            for _event in assembler.done(stop_reason):
                yield _event

        # 厂商数据不可信：合法 JSON 也可能是数组或标量（`[]`、`"ok"`），
        # `chunk.get(...)` 会抛 AttributeError。契约要求除取消除外不外抛，所以这里
        # 兜底捕获 Exception；CancelledError / GeneratorExit 属于 BaseException，不会被吞。
        except Exception as exc:
            for _event in replay_tool_calls(assembler, pending):
                yield _event
            for _event in assembler.fail("error", f"parse failed: {type(exc).__name__}: {exc}"):
                yield _event

    def _url(self) -> str:
        return f"{self.spec.base_url.rstrip('/')}/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        # 空 key 表示端点不需要鉴权（如本地 Ollama）
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers
