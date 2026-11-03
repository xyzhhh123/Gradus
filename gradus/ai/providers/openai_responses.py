# =============================================================================
# OpenAI Responses API 格式（GPT）
# 注意：这与 Chat Completions 是两套不同协议，改 base_url 切不过去。
# 本文件上半部分记录协议格式，下半部分是实现。
# =============================================================================

# ---- 端点与鉴权 -------------------------------------------------------------
# POST {base_url}/responses
# Authorization: Bearer <api_key>
# Content-Type: application/json

# ---- 请求体 ----------------------------------------------------------------
# {
#   "model": "gpt-5",
#   "instructions": "You are ...",              # 等价于 system prompt
#   "input": [
#     {"role": "user", "content": [{"type": "input_text", "text": "..."}]},
#     {"role": "assistant", "content": [{"type": "output_text", "text": "我先看下文件"}]},
#     {"type": "function_call", "call_id": "call_0", "name": "read_file",
#      "arguments": "{\"path\": \"a.py\"}"},
#     {"type": "function_call_output", "call_id": "call_0", "output": "print(1)"}
#   ],
#   "tools": [
#     {"type": "function", "name": "read_file", "description": "...",
#      "parameters": {"type": "object", "properties": {...}, "required": [...]}}
#   ],
#   "stream": true,
#   "store": false
# }
#
# 要点：
# - 工具声明是扁平结构，不再有 function 包一层
# - input 是 item 数组：消息 item 与工具调用 item 平级
# - function_call 与 function_call_output 通过 call_id 配对，不靠 role
# - arguments 仍是 JSON 字符串（和 Chat Completions 一样）
# - 若 store=true，可用 previous_response_id 让服务端保存上下文；
#   Gradus 统一走客户端全量历史，因此这里固定 store=false

# ---- 流式响应 --------------------------------------------------------------
# SSE，每行 "event: <类型>" + "data: {json}"，事件粒度比内部协议更细：
#
# event: response.created
# data: {"type":"response.created","response":{"id":"resp_1","status":"in_progress"}}
#
# event: response.output_item.added
# data: {"type":"response.output_item.added","output_index":0,
#        "item":{"type":"message","id":"msg_1","role":"assistant","content":[]}}
#
# event: response.content_part.added
# data: {"type":"response.content_part.added","item_id":"msg_1","output_index":0,
#        "content_index":0,"part":{"type":"output_text","text":""}}
#
# event: response.output_text.delta
# data: {"type":"response.output_text.delta","item_id":"msg_1","output_index":0,
#        "content_index":0,"delta":"你"}
#
# event: response.output_text.done
# event: response.content_part.done
# event: response.output_item.done
#
# # 工具调用是独立 item：
# event: response.output_item.added
# data: {"output_index":1,"item":{"type":"function_call","id":"fc_1","call_id":"call_0",
#        "name":"read_file","arguments":""}}
# event: response.function_call_arguments.delta
# data: {"item_id":"fc_1","output_index":1,"delta":"{\"pa"}
# event: response.function_call_arguments.done
# data: {"item_id":"fc_1","output_index":1,"arguments":"{\"path\": \"a.py\"}"}
#
# event: response.completed
# data: {"type":"response.completed","response":{"status":"completed",
#        "usage":{"input_tokens":100,"output_tokens":20,
#                 "input_tokens_details":{"cached_tokens":64},
#                 "output_tokens_details":{"reasoning_tokens":12}}}}
#
# 要点：
# - 事件有三层：item 级（output_item）> part 级（content_part）> delta 级
#   内部协议只到 part 级，多余的层级要在适配器里折叠
# - 索引有两套：output_index（item 序号）与 content_index（part 序号），不能混用
# - item.id 用于事件路由，call_id 用于回填 function_call_output，两者不同
# - 并行工具调用是多个平级 item，参数事件会按 item_id 交替出现，
#   不能假设"先 added 的 item 一定先收完参数"
# - 推理内容是独立的 reasoning item（可带 encrypted_content），不是文本块；
#   增量事件为 response.reasoning_summary_text.delta 与 response.reasoning_text.delta
# - 拒答内容是独立的 refusal part：content_part 的 type 是 "refusal"，
#   后续增量走 response.refusal.delta，不混在 output_text 里
# - 没有 message_delta；终止信息在 response.completed / response.incomplete 里
# - 失败事件：response.failed、error；被截断时是 response.incomplete

# ---- 终止原因 → 内部 StopReason --------------------------------------------
# status = "completed"                              -> "stop"（或含 function_call 时 "tool_use"）
# status = "incomplete", reason = "max_output_tokens" -> "length"
# status = "incomplete", reason = "content_filter"    -> "filtered"
# 出现过 refusal part                                -> "filtered"（见 D18）
# status = "failed"                                   -> "error"
# 客户端主动断开                                       -> "aborted"

# ---- 用量 → 内部 Usage -----------------------------------------------------
# usage.input_tokens                            -> input_tokens
# usage.output_tokens                           -> output_tokens
# usage.input_tokens_details.cached_tokens      -> cache_read_tokens
# usage.output_tokens_details.reasoning_tokens  -> 计入 output_tokens（不单列）
# 写入型缓存无对应字段

# ---- 内部形状 → 本厂商形状 的映射表 ------------------------------------------
# Request.system     -> 顶层 "instructions"
# Request.reasoning  -> reasoning {effort, summary: "auto"}
# UserMessage        -> {"role": "user", "content": [{"type": "input_text", ...}]}
# AssistantMessage   -> {"role": "assistant", "content": [{"type": "output_text", ...}]}
# TextBlock          -> {"type": "output_text", "text": ...}
# ThinkingBlock      -> {"type": "reasoning", ...}（含 encrypted_content）
# ToolCallBlock      -> {"type": "function_call", "call_id": ..., "name": ..., "arguments": "..."}
# ToolResultMessage  -> {"type": "function_call_output", "call_id": ..., "output": ...}
#                       本协议同样没有 is_error 字段，错误结果包成 {"error": ...}（见 D19）

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
    """把内部 Request 翻成 Responses 请求体。纯函数。"""
    payload: dict[str, Any] = {
        "model": spec.model,
        "input": build_input(request),
        "stream": True,
        # Gradus 统一走客户端全量历史，不用服务端会话
        "store": False,
    }

    if request.system:
        payload["instructions"] = request.system

    if request.tools:
        payload["tools"] = [build_tool(tool) for tool in request.tools]

    if request.temperature is not None:
        payload["temperature"] = request.temperature

    if request.reasoning:
        # summary=auto 让上游顺带给出可展示的思考摘要
        payload["reasoning"] = {"effort": request.reasoning, "summary": "auto"}

    if max_tokens := request.max_output_tokens or spec.max_output_tokens:
        payload["max_output_tokens"] = max_tokens

    return payload


def _tool_output(message: ToolResultMessage) -> str:
    """工具结果的 output。

    本协议没有 is_error 字段：错误结果包成 {"error": ...}，与成功结果的原文区分开。
    """
    if not message.is_error:
        return message.content
    return json.dumps({"error": message.content}, ensure_ascii=False)


def build_input(request: Request) -> list[dict[str, Any]]:
    """拼 input。

    与 Chat Completions 最大的不同：工具调用不是消息里的字段，而是和消息**平级**的
    item。所以一条 AssistantMessage 可能被拆成「一个 message item + N 个 function_call」。
    """
    items: list[dict[str, Any]] = []

    for message in request.messages:
        if isinstance(message, UserMessage):
            items.append(
                {"role": "user", "content": [{"type": "input_text", "text": message.content}]}
            )
        elif isinstance(message, AssistantMessage):
            text = "".join(b.text for b in message.content if isinstance(b, TextBlock))
            if text:
                items.append(
                    {"role": "assistant", "content": [{"type": "output_text", "text": text}]}
                )
            for block in message.content:
                if isinstance(block, ToolCallBlock):
                    items.append(
                        {
                            "type": "function_call",
                            "call_id": block.id,
                            "name": block.name,
                            "arguments": json.dumps(block.arguments, ensure_ascii=False),
                        }
                    )
        elif isinstance(message, ToolResultMessage):
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id,
                    "output": _tool_output(message),
                }
            )

    return items


def build_tool(tool: ToolSpec) -> dict[str, Any]:
    """本厂商的工具声明是扁平结构，没有 function 包一层。"""
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.parameters,
    }


def parse_usage(usage: dict[str, Any]) -> Usage:
    # reasoning_tokens 已含在 output_tokens 里，不单列
    return Usage(
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        cache_read_tokens=(usage.get("input_tokens_details") or {}).get("cached_tokens", 0),
    )


def extract_error(data: dict[str, Any]) -> str:
    if isinstance(data.get("error"), dict):
        return data["error"].get("message", "responses error")
    response = data.get("response") or {}
    error = response.get("error") or {}
    return error.get("message") or data.get("message") or "responses failed"


def slot_for(pending: dict[str, dict[str, str]], key: str) -> dict[str, str]:
    """取（或新建）一次工具调用的槽位。只写字典，不碰 assembler。"""
    if key not in pending:
        pending[key] = {"call_id": "", "name": "", "args": ""}
    return pending[key]


def collect_item(pending: dict[str, dict[str, str]], item: dict[str, Any]) -> None:
    """收敛一次工具调用的身份信息与完整参数。

    并行调用时多个 function_call item 交替出现，只有 item.id 能确定归属；
    done 阶段的 item 带完整 arguments，直接覆盖逐片拼出来的结果。
    """
    slot = slot_for(pending, item.get("id") or item.get("call_id") or "")
    if call_id := item.get("call_id"):
        slot["call_id"] = call_id
    if name := item.get("name"):
        slot["name"] = name
    if arguments := item.get("arguments"):
        slot["args"] = arguments


def replay_function_calls(
    assembler: StreamAssembler, pending: dict[str, dict[str, str]]
) -> Iterable[AssistantEvent]:
    """按 item 出现顺序把攒下的调用发出去。

    正常结束时统一产出；中断时也用它，让 partial 里至少留下"调用了什么"的骨架。
    参数没拼完时 arguments 会是空 dict，是否执行由 StopReason 决定。
    """
    for slot in pending.values():
        if not slot["name"]:
            continue
        for _event in assembler.start_tool(slot["call_id"], slot["name"]):
            yield _event
        if slot["args"]:
            for _event in assembler.tool_arguments(slot["args"]):
                yield _event
        for _event in assembler.end_tool():
            yield _event


class OpenAIResponsesProvider:
    """Responses 形状适配器（GPT）；三层事件需折叠为两层。"""

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

        # 并行工具调用会按 item_id 交错给出参数，先攒在字典里，流结束再重放
        pending: dict[str, dict[str, str]] = {}
        stop_reason: StopReason = "stop"
        saw_refusal = False
        refusal_open = False

        try:
            async for event in post_sse(
                self._url(),
                headers=self._headers(),
                payload=build_payload(request, self.spec),
                client=self._client,
            ):
                if isinstance(event, TransportFailure):
                    # 先把已攒下的调用补出来，partial 里至少能看到调用了什么
                    for _event in replay_function_calls(assembler, pending):
                        yield _event
                    for _event in assembler.fail("error", event.message):
                        yield _event
                    return

                kind = event.event
                if kind is None:
                    continue

                data = json.loads(event.data)

                if kind == "response.output_item.added":
                    item = data.get("item") or {}
                    if item.get("type") == "function_call":
                        collect_item(pending, item)

                elif kind == "response.content_part.added":
                    part = data.get("part") or {}
                    if part.get("type") == "output_text":
                        for _event in assembler.start_text():
                            yield _event
                    elif part.get("type") == "refusal":
                        saw_refusal = True

                elif kind == "response.output_text.delta":
                    if delta := data.get("delta"):
                        for _event in assembler.text(delta):
                            yield _event

                elif kind in (
                    "response.reasoning_summary_text.delta",
                    "response.reasoning_text.delta",
                ):
                    if delta := data.get("delta"):
                        for _event in assembler.thinking(delta):
                            yield _event

                elif kind == "response.refusal.delta":
                    if delta := data.get("delta"):
                        saw_refusal = True
                        # 拒答要单独成块：先关掉可能还在长的正文块
                        if not refusal_open:
                            for _event in assembler.end_text():
                                yield _event
                            for _event in assembler.start_text(refusal=True):
                                yield _event
                            refusal_open = True
                        for _event in assembler.text(delta):
                            yield _event

                elif kind == "response.content_part.done":
                    for _event in assembler.end_text():
                        yield _event
                    refusal_open = False

                elif kind == "response.function_call_arguments.delta":
                    if delta := data.get("delta"):
                        slot_for(pending, data.get("item_id", ""))["args"] += delta

                elif kind == "response.function_call_arguments.done":
                    # 上游在这一步给出完整且已校验的 JSON，比逐片拼接可靠
                    if arguments := data.get("arguments"):
                        slot_for(pending, data.get("item_id", ""))["args"] = arguments

                elif kind == "response.output_item.done":
                    item = data.get("item") or {}
                    if item.get("type") == "function_call":
                        collect_item(pending, item)
                    elif item.get("type") == "reasoning":
                        # encrypted_content 是回传给厂商续推理用的，这里先存进 signature
                        for _event in assembler.end_thinking(item.get("encrypted_content")):
                            yield _event

                elif kind in ("response.completed", "response.incomplete"):
                    response = data.get("response") or {}
                    if usage := response.get("usage"):
                        assembler.add_usage(parse_usage(usage))
                    if response.get("status") == "incomplete":
                        reason = (response.get("incomplete_details") or {}).get("reason")
                        stop_reason = "length" if reason == "max_output_tokens" else "filtered"

                elif kind in ("response.failed", "error"):
                    for _event in replay_function_calls(assembler, pending):
                        yield _event
                    for _event in assembler.fail("error", extract_error(data)):
                        yield _event
                    return

            # 工具调用攒到最后再发：assembler 同一时刻只能开一个块，而并行调用
            # 的参数事件会交错；参数本来也要拼完才能用，提前发 start 没有价值。
            for _event in replay_function_calls(assembler, pending):
                yield _event

            # 拒答也是一种"没给完整答案"，但 length / filtered 更具体，不覆盖
            if saw_refusal and stop_reason == "stop":
                stop_reason = "filtered"
            # 本厂商的 completed 不带 tool_use，靠有没有 function_call item 判定
            elif pending and stop_reason == "stop":
                stop_reason = "tool_use"

            for _event in assembler.done(stop_reason):
                yield _event

        # 厂商数据不可信：合法 JSON 也可能是数组或标量（`[]`、`"ok"`），
        # `data.get(...)` 会抛 AttributeError。契约要求除取消除外不外抛，所以这里
        # 兜底捕获 Exception；CancelledError / GeneratorExit 属于 BaseException，不会被吞。
        except Exception as exc:
            for _event in replay_function_calls(assembler, pending):
                yield _event
            for _event in assembler.fail("error", f"parse failed: {type(exc).__name__}: {exc}"):
                yield _event

    def _url(self) -> str:
        return f"{self.spec.base_url.rstrip('/')}/responses"

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers
