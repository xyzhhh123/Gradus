# =============================================================================
# Anthropic Messages API 格式（Claude）
# 本文件上半部分记录协议格式，下半部分是实现。
# =============================================================================

# ---- 端点与鉴权 -------------------------------------------------------------
# POST {base_url}/v1/messages
# x-api-key: <api_key>
# anthropic-version: 2023-06-01
# Content-Type: application/json

# ---- 请求体 ----------------------------------------------------------------
# {
#   "model": "claude-sonnet-4-5",
#   "max_tokens": 1024,                      # 必填，没有默认值
#   "system": "You are ...",                  # 顶层字段，也可传 [{"type":"text","text":"..."}]
#   "messages": [
#     {"role": "user", "content": "..."},
#     {"role": "assistant", "content": [
#       {"type": "text", "text": "我先看下文件"},
#       {"type": "tool_use", "id": "toolu_01", "name": "read_file",
#        "input": {"path": "a.py"}}
#     ]},
#     {"role": "user", "content": [
#       {"type": "tool_result", "tool_use_id": "toolu_01",
#        "content": "print(1)", "is_error": false}
#     ]}
#   ],
#   "tools": [
#     {"name": "read_file", "description": "...",
#      "input_schema": {"type": "object", "properties": {...}, "required": [...]}}
#   ]
# }
#
# 要点：
# - system 不在 messages 里，是顶层字段
# - messages 必须 user 开头，role 必须严格交替，相邻同角色需由适配器合并
# - assistant 的 content 是 block 数组，不是字符串
# - tool_result 必须放进紧跟 tool_use 的那条 user 消息
# - 工具参数 input 是对象，不是 JSON 字符串

# ---- 流式响应 --------------------------------------------------------------
# SSE，每行 "event: <类型>" + "data: {json}"
#
# event: message_start
# data: {"type":"message_start","message":{"id":"msg_01","role":"assistant","content":[],
#        "stop_reason":null,
#        "usage":{"input_tokens":25,"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}}
#
# event: content_block_start
# data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}
#
# event: content_block_delta
# data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"你"}}
#
# event: content_block_stop
# data: {"type":"content_block_stop","index":0}
#
# content_block 的三种类型及其增量：
# - text:     {"type":"text","text":"..."}           + text_delta
# - thinking: {"type":"thinking","thinking":"...","signature":"..."} + thinking_delta / signature_delta
# - tool_use: {"type":"tool_use","id":"...","name":"...","input":{}} + input_json_delta（partial_json 字符串分片）
#
# event: message_delta
# data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},
#        "usage":{"output_tokens":15}}
#
# event: message_stop
# data: {"type":"message_stop"}
#
# 要点：
# - 存在 ping 事件，需要忽略
# - 错误也是事件：{"type":"error","error":{"type":"overloaded_error","message":"..."}}
# - output_tokens 只在 message_delta 里出现，input 用量在 message_start 里，
#   所以 Usage 需要跨两个事件合并
# - tool_use 的 input 在 content_block_start 时是空对象 {}，真实参数靠 partial_json 拼接
# - thinking 的 signature 必须原样回传给下一轮请求，否则多轮工具调用会报错

# ---- 终止原因 → 内部 StopReason --------------------------------------------
# stop_reason = "end_turn"      -> "stop"
# stop_reason = "max_tokens"    -> "length"
# stop_reason = "tool_use"      -> "tool_use"
# stop_reason = "stop_sequence" -> "stop"
# stop_reason = "refusal"       -> 现协议无对应值，待定（filtered）
# stop_reason = "pause_turn"    -> "deferred"（服务端暂停，可续传）

# ---- 用量 → 内部 Usage -----------------------------------------------------
# usage.input_tokens                -> input_tokens（本厂商不含缓存，见下）
# usage.output_tokens               -> output_tokens（来自 message_delta）
# usage.cache_read_input_tokens     -> cache_read_tokens
# usage.cache_creation_input_tokens -> cache_write_tokens
# 本厂商的 input_tokens 只算没走缓存的那部分，缓存读写各自单列；OpenAI 系反过来，
# input 已含缓存、cached 只是子集。为了上层不用分协议，出口统一成"总输入"：
# input_tokens 报三者之和，缓存两项保留原值（D25）
# message_start 与 message_delta 都带上用量，后者是"累计最终值"而非增量：
# 官方只重复 output_tokens，Kimi 这类兼容端点会把完整 usage 再报一遍，
# 所以逐字段覆盖而不是累加（D26）

# ---- 内部形状 → 本厂商形状 的映射表 ------------------------------------------
# Request.system     -> 顶层 "system"（text 块数组，带 cache_control 断点）
# Request.reasoning  -> thinking {type: "enabled", budget_tokens}，并把 max_tokens 抬到预算之上
# UserMessage        -> {"role": "user", "content": [{"type":"text",...}]}
# AssistantMessage   -> {"role": "assistant", "content": [block...]}
# TextBlock          -> {"type": "text", "text": ...}
# ThinkingBlock      -> {"type": "thinking", "thinking": ..., "signature": ...}
# ToolCallBlock      -> {"type": "tool_use", "id": ..., "name": ..., "input": {...}}
# ToolResultMessage  -> {"role": "user", "content": [{"type":"tool_result", "tool_use_id": ...}]}

# =============================================================================
# 实现
# =============================================================================

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import httpx

from ..base import THINKING_BUDGETS, ModelSpec, Request, ToolSpec
from ..stream_assembler import StreamAssembler
from ..transport import TransportFailure, post_sse
from ..types import (
    AssistantEvent,
    AssistantMessage,
    StopReason,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultMessage,
    Usage,
    UserMessage,
)

ANTHROPIC_VERSION = "2023-06-01"

# 官方要求显式 cache_control 才写缓存：前缀按 tools -> system -> messages 累积，
# 断点放在每段末尾即可覆盖整段前缀。最多 4 个断点，本文件最多用 3 个（tools / system /
# 最后一条消息的最后一个可缓存块）。注意最小可缓存长度（Sonnet 4.5 为 1024 token，
# Sonnet 4.6 与 Haiku 为 2048），短会话即使打了点也不会命中。
CACHE_CONTROL = {"type": "ephemeral"}

# 可显式标记的内容块类型（官方文档：text / tool_use / tool_result）。
# thinking 块不能带 cache_control，遇到要往前找。
CACHEABLE_BLOCK_TYPES = frozenset({"text", "tool_use", "tool_result"})

STOP_REASONS: dict[str, StopReason] = {
    "end_turn": "stop",
    "max_tokens": "length",
    "tool_use": "tool_use",
    "stop_sequence": "stop",
    "refusal": "filtered",
    "pause_turn": "deferred",
}


def build_payload(request: Request, spec: ModelSpec) -> dict[str, Any]:
    """把内部 Request 翻成 Messages 请求体。

    纯函数。system 是顶层字段；max_tokens 必填；stream 恒开。

    三处缓存断点（官方要求显式声明才走缓存）：system 末尾、最后一个工具、
    最后一条消息的最后一个可缓存块，最多 3 个（上限是 4）。
    """
    payload: dict[str, Any] = {
        "model": spec.model,
        "max_tokens": request.max_output_tokens or spec.max_output_tokens,
        "messages": build_messages(request),
        "stream": True,
    }

    if request.system:
        # system 用块数组，断点打在末尾；字符串形式无法带 cache_control
        payload["system"] = [
            {"type": "text", "text": request.system, "cache_control": CACHE_CONTROL}
        ]

    if request.temperature is not None:
        payload["temperature"] = request.temperature

    if request.reasoning:
        # 本厂商用 token 预算表达强度，且预算必须小于 max_tokens，否则请求被拒
        budget = THINKING_BUDGETS[request.reasoning]
        payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
        payload["max_tokens"] = max(payload["max_tokens"], budget + 1024)

    if request.tools:
        tools = [build_tool(tool) for tool in request.tools]
        # 断点打在最后一个工具上，覆盖整段工具定义
        tools[-1]["cache_control"] = CACHE_CONTROL
        payload["tools"] = tools
        payload["tool_choice"] = {"type": "auto"}

    return payload


def build_messages(request: Request) -> list[dict[str, Any]]:
    """按本厂商的约束拼 messages。

    两条硬规则：
    - role 必须严格交替，所以相邻同角色要合并（连续的工具结果合成一条 user）
    - 内容一律是 block 数组，不是字符串
    """
    messages: list[dict[str, Any]] = []

    def push(role: str, blocks: list[dict[str, Any]]) -> None:
        if not blocks:
            return
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"].extend(blocks)
        else:
            messages.append({"role": role, "content": blocks})

    for message in request.messages:
        if isinstance(message, UserMessage):
            push("user", [{"type": "text", "text": message.content}])
        elif isinstance(message, AssistantMessage):
            push("assistant", build_assistant_blocks(message))
        elif isinstance(message, ToolResultMessage):
            push(
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": message.tool_call_id,
                        "content": message.content,
                        "is_error": message.is_error,
                    }
                ],
            )

    mark_cache_breakpoint(messages)
    return messages


def mark_cache_breakpoint(messages: list[dict[str, Any]]) -> None:
    """给最后一条消息的最后一个可缓存块打 `cache_control`（原地修改）。

    前缀按 tools -> system -> messages 累积，所以消息侧的断点放在末尾即可覆盖整段历史。
    `thinking` 块不能显式标记，遇到就往前找最近的 text / tool_use / tool_result；
    整段都没有可缓存块时不打点，宁可少一个断点也不要发出非法请求。
    """
    for message in reversed(messages):
        for block in reversed(message["content"]):
            if block.get("type") in CACHEABLE_BLOCK_TYPES:
                block["cache_control"] = CACHE_CONTROL
                return


def build_assistant_blocks(message: AssistantMessage) -> list[dict[str, Any]]:
    """助手消息的每个块都保留，ThinkingBlock 的 signature 必须回传。"""
    blocks: list[dict[str, Any]] = []

    for block in message.content:
        if isinstance(block, TextBlock):
            blocks.append({"type": "text", "text": block.text})
        elif isinstance(block, ThinkingBlock):
            item: dict[str, Any] = {"type": "thinking", "thinking": block.thinking}
            if block.signature:
                item["signature"] = block.signature
            blocks.append(item)
        elif isinstance(block, ToolCallBlock):
            blocks.append(
                {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.arguments,
                }
            )

    return blocks


def build_tool(tool: ToolSpec) -> dict[str, Any]:
    """本厂商用 input_schema，不是 parameters。"""
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.parameters,
    }


def merge_usage(base: Usage, raw: dict[str, Any]) -> Usage:
    """把 `raw` 里出现的用量字段覆盖到 `base` 上，没出现的保持原值。

    message_delta 的 usage 是"截至目前的累计值"而不是增量：官方只重复 output_tokens，
    Kimi 这类兼容端点会把完整 usage 再报一遍。累加会让 cache 翻倍、output 多算一次。
    """
    return Usage(
        input_tokens=raw.get("input_tokens", base.input_tokens),
        output_tokens=raw.get("output_tokens", base.output_tokens),
        cache_read_tokens=raw.get("cache_read_input_tokens", base.cache_read_tokens),
        cache_write_tokens=raw.get("cache_creation_input_tokens", base.cache_write_tokens),
    )


def with_total_input(usage: Usage) -> Usage:
    """把本厂商的"未缓存输入"补成"总输入"（D25）。

    本厂商的 input_tokens 不含缓存读写，OpenAI 系的 input 已经包含。上层若要知道
    这次请求一共吃了多少输入，两家算法不同；在出口统一成总输入，缓存两项原样保留，
    未缓存部分仍可用 `input - cache_read - cache_write` 还原。

    只能在交给 assembler 时调用：`merge_usage` 的 base 若已是总输入，再补就会重复。
    """
    return replace(
        usage,
        input_tokens=usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens,
    )


class AnthropicProvider:
    """Messages 形状适配器（Claude）。"""

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

        payload = build_payload(request, self.spec)

        # 本厂商要求 messages 以 user 开头。在发请求前拦下，给出明确原因，
        # 而不是等 API 返回一句笼统的 HTTP 400。
        first_role = payload["messages"][0]["role"] if payload["messages"] else "empty"
        if first_role != "user":
            for _event in assembler.fail(
                "error",
                f"anthropic requires messages to start with a user turn, got {first_role}",
            ):
                yield _event
            return

        stop_reason: StopReason = "stop"
        signature = ""
        usage = Usage()

        try:
            async for event in post_sse(
                self._url(),
                headers=self._headers(),
                payload=payload,
                client=self._client,
            ):
                if isinstance(event, TransportFailure):
                    for _event in assembler.fail("error", event.message):
                        yield _event
                    return

                kind = event.event
                if kind == "ping":
                    continue

                data = json.loads(event.data)

                if kind == "message_start":
                    # input 用量在这里，output 在 message_delta
                    usage = merge_usage(Usage(), data.get("message", {}).get("usage") or {})
                    assembler.set_usage(with_total_input(usage))

                elif kind == "content_block_start":
                    block = data["content_block"]
                    block_type = block.get("type")
                    if block_type == "text":
                        for _event in assembler.start_text():
                            yield _event
                    elif block_type == "thinking":
                        for _event in assembler.start_thinking():
                            yield _event
                    elif block_type == "tool_use":
                        for _event in assembler.start_tool(block["id"], block["name"]):
                            yield _event

                elif kind == "content_block_delta":
                    delta = data["delta"]
                    delta_type = delta.get("type")
                    if delta_type == "text_delta":
                        for _event in assembler.text(delta["text"]):
                            yield _event
                    elif delta_type == "thinking_delta":
                        for _event in assembler.thinking(delta["thinking"]):
                            yield _event
                    elif delta_type == "signature_delta":
                        signature += delta["signature"]
                    elif delta_type == "input_json_delta":
                        for _event in assembler.tool_arguments(delta["partial_json"]):
                            yield _event

                elif kind == "content_block_stop":
                    # 三个 end 都是幂等的，只有当前打开的那个会真正产出事件
                    for _event in assembler.end_text():
                        yield _event
                    for _event in assembler.end_thinking(signature or None):
                        yield _event
                    for _event in assembler.end_tool():
                        yield _event
                    signature = ""

                elif kind == "message_delta":
                    delta = data.get("delta") or {}
                    if reason := delta.get("stop_reason"):
                        stop_reason = STOP_REASONS.get(reason, "stop")
                    if raw_usage := data.get("usage"):
                        usage = merge_usage(usage, raw_usage)
                        assembler.set_usage(with_total_input(usage))

                elif kind == "error":
                    error = data.get("error") or {}
                    for _event in assembler.fail("error", error.get("message", "anthropic error")):
                        yield _event
                    return

            for _event in assembler.done(stop_reason):
                yield _event

        # 厂商数据不可信：合法 JSON 也可能是数组或标量（`[]`、`"ok"`），
        # `data.get(...)` 会抛 AttributeError。契约要求除取消除外不外抛，所以这里
        # 兜底捕获 Exception；CancelledError / GeneratorExit 属于 BaseException，不会被吞。
        except Exception as exc:
            for _event in assembler.fail("error", f"parse failed: {type(exc).__name__}: {exc}"):
                yield _event

    def _url(self) -> str:
        return f"{self.spec.base_url.rstrip('/')}/v1/messages"

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "anthropic-version": ANTHROPIC_VERSION,
        }
        # 本厂商用 x-api-key，不是 Authorization
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers
