# =============================================================================
# Google Gemini generateContent 格式
# 本文件上半部分记录协议格式，下半部分是实现。
# =============================================================================

# ---- 端点与鉴权 -------------------------------------------------------------
# POST {base_url}/v1beta/models/{model}:streamGenerateContent?alt=sse
# x-goog-api-key: <api_key>
# Content-Type: application/json

# ---- 请求体 ----------------------------------------------------------------
# {
#   "systemInstruction": {"parts": [{"text": "You are ..."}]},
#   "contents": [
#     {"role": "user", "parts": [{"text": "..."}]},
#     {"role": "model", "parts": [
#       {"text": "我先看下文件"},
#       {"functionCall": {"name": "read_file", "args": {"path": "a.py"}}}
#     ]},
#     {"role": "user", "parts": [
#       {"functionResponse": {"name": "read_file", "response": {"result": "print(1)"}}}
#     ]}
#   ],
#   "tools": [
#     {"functionDeclarations": [
#       {"name": "read_file", "description": "...",
#        "parameters": {"type": "object", "properties": {...}, "required": [...]}}
#     ]}
#   ],
#   "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1024}
# }
#
# 要点：
# - role 只有 "user" 和 "model"，没有 assistant，也没有独立的 tool 角色
# - 工具结果要以 functionResponse 的 part 形式放进 user 的 contents 里
# - functionResponse 靠 name 匹配调用，没有 call id；内部 id 需适配器自己合成
# - functionResponse.response 必须是对象，结果要包成 {"result": ...} 或 {"error": ...}
# - functionCall.args 是完整对象，不是 JSON 字符串

# ---- 流式响应 --------------------------------------------------------------
# SSE（alt=sse），每个 data 是一段完整 JSON，包含本轮的增量 candidates：
#
# data: {"candidates":[{"content":{"role":"model","parts":[{"text":"你"}]},"index":0}]}
# data: {"candidates":[{"content":{"role":"model","parts":[{"text":"好"}]},"index":0}]}
# data: {"candidates":[{"content":{"role":"model","parts":[
#         {"functionCall":{"name":"read_file","args":{"path":"a.py"}}}
#       ]},"index":0}]}
# data: {"candidates":[{"content":{"role":"model","parts":[...]},"finishReason":"STOP","index":0}],
#        "usageMetadata":{"promptTokenCount":100,"candidatesTokenCount":20,
#                         "totalTokenCount":120,"cachedContentTokenCount":64}}
#
# 要点：
# - 文本是增量拼接；同一条候选的 parts 会分散在多个 chunk 里
# - functionCall 一次性给出完整 args，没有参数分片事件
#   => 内部协议要求 ToolCallStart/ToolCallEnd，适配器必须自己合成这两个事件
# - 推理内容：part 形如 {"text": "...", "thought": true}，可能带 thoughtSignature
# - 安全拦截可能表现为 promptFeedback.blockReason（整条请求被拒），也可能是 finishReason
# - 空 parts 或缺失 role 的 chunk 需要容忍

# ---- 终止原因 → 内部 StopReason --------------------------------------------
# finishReason = "STOP"          -> "stop"
# finishReason = "MAX_TOKENS"    -> "length"
# finishReason = "SAFETY" / "RECITATION" / "PROHIBITED_CONTENT"
#                                / "BLOCKLIST" / "SPII"
#                                -> 现协议无对应值，待定（filtered）
# promptFeedback.blockReason 非空 -> 同上（filtered），且通常没有任何 candidates
# 带 functionCall 的响应没有专门的 finishReason，需按内容判定为 "tool_use"
# finishReason = "MALFORMED_FUNCTION_CALL" -> "error"
# 缺失 finishReason 且流已结束     -> "stop"

# ---- 用量 → 内部 Usage -----------------------------------------------------
# usageMetadata.promptTokenCount        -> input_tokens
# usageMetadata.candidatesTokenCount    -> output_tokens
# usageMetadata.cachedContentTokenCount -> cache_read_tokens
# usageMetadata.thoughtsTokenCount      -> 计入 output_tokens（不单列）
# 写入型缓存无对应字段

# ---- 内部形状 → 本厂商形状 的映射表 ------------------------------------------
# Request.system     -> 顶层 "systemInstruction"
# Request.reasoning  -> generationConfig.thinkingConfig {thinkingBudget, includeThoughts: true}
# UserMessage        -> {"role": "user", "parts": [{"text": ...}]}
# AssistantMessage   -> {"role": "model", "parts": [...]}
# TextBlock          -> {"text": ...}
# ThinkingBlock      -> {"text": ..., "thought": true}
# ToolCallBlock      -> {"functionCall": {"name": ..., "args": {...}}}
# ToolResultMessage  -> {"role": "user", "parts": [{"functionResponse": {...}}]}

# =============================================================================
# 实现
# =============================================================================

from __future__ import annotations

import json
from collections.abc import AsyncIterator
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

STOP_REASONS: dict[str, StopReason] = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "filtered",
    "RECITATION": "filtered",
    "PROHIBITED_CONTENT": "filtered",
    "BLOCKLIST": "filtered",
    "SPII": "filtered",
}


def build_payload(request: Request, spec: ModelSpec) -> dict[str, Any]:
    """把内部 Request 翻成 generateContent 请求体。纯函数。"""
    payload: dict[str, Any] = {"contents": build_contents(request)}

    if request.system:
        payload["systemInstruction"] = {"parts": [{"text": request.system}]}

    if request.tools:
        payload["tools"] = [
            {"functionDeclarations": [build_tool(tool) for tool in request.tools]}
        ]

    config: dict[str, Any] = {}
    if max_tokens := request.max_output_tokens or spec.max_output_tokens:
        config["maxOutputTokens"] = max_tokens
    if request.temperature is not None:
        config["temperature"] = request.temperature
    if request.reasoning:
        # 预算按 token 算；includeThoughts 打开才会返回 thought 标记的 part
        config["thinkingConfig"] = {
            "thinkingBudget": THINKING_BUDGETS[request.reasoning],
            "includeThoughts": True,
        }
    if config:
        payload["generationConfig"] = config

    return payload


def build_contents(request: Request) -> list[dict[str, Any]]:
    """拼 contents。

    本厂商只有 user / model 两种角色，且工具结果靠 name 匹配（没有 call id），
    所以要先从历史里建立 id -> name 的映射。相邻同角色合并，避免依赖厂商的宽容度。
    """
    contents: list[dict[str, Any]] = []
    names: dict[str, str] = {}

    def push(role: str, parts: list[dict[str, Any]]) -> None:
        if not parts:
            return
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": role, "parts": parts})

    for message in request.messages:
        if isinstance(message, UserMessage):
            push("user", [{"text": message.content}])
        elif isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolCallBlock):
                    names[block.id] = block.name
            push("model", build_model_parts(message))
        elif isinstance(message, ToolResultMessage):
            name = names.get(message.tool_call_id, message.tool_call_id)
            key = "error" if message.is_error else "result"
            push(
                "user",
                [{"functionResponse": {"name": name, "response": {key: message.content}}}],
            )

    return contents


def build_model_parts(message: AssistantMessage) -> list[dict[str, Any]]:
    """助手消息的块按顺序转成 parts。thinking 用 thought: true 标记。"""
    parts: list[dict[str, Any]] = []

    for block in message.content:
        if isinstance(block, TextBlock):
            parts.append({"text": block.text})
        elif isinstance(block, ThinkingBlock):
            item: dict[str, Any] = {"text": block.thinking, "thought": True}
            if block.signature:
                item["thoughtSignature"] = block.signature
            parts.append(item)
        elif isinstance(block, ToolCallBlock):
            parts.append({"functionCall": {"name": block.name, "args": block.arguments}})

    return parts


def build_tool(tool: ToolSpec) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.parameters,
    }


def parse_usage(usage: dict[str, Any]) -> Usage:
    # thoughtsTokenCount 计入 output，不单列
    return Usage(
        input_tokens=usage.get("promptTokenCount", 0),
        output_tokens=usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0),
        cache_read_tokens=usage.get("cachedContentTokenCount", 0),
    )


class GeminiProvider:
    """parts 形状适配器；functionCall 一次到齐，需自行合成三阶段事件。

    **测试中**：目前没有可用的 Gemini API key，尚未用真实流验证；请求与事件
    形状按官方格式实现，只有离线用例与录制格式覆盖。采用 Gemini 原生格式的
    厂商较少，暂不作为主要支持对象。
    """

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

        stop_reason: StopReason = "stop"
        signature = ""
        saw_tool = False
        tool_count = 0

        try:
            async for event in post_sse(
                self._url(),
                headers=self._headers(),
                payload=build_payload(request, self.spec),
                client=self._client,
            ):
                if isinstance(event, TransportFailure):
                    for _event in assembler.fail("error", event.message):
                        yield _event
                    return

                chunk = json.loads(event.data)

                if feedback := chunk.get("promptFeedback"):
                    if feedback.get("blockReason"):
                        # 整条请求被拒，没有任何候选；这不是失败，是"没给出答案"
                        for _event in assembler.done("filtered"):
                            yield _event
                        return

                if usage := chunk.get("usageMetadata"):
                    assembler.add_usage(parse_usage(usage))

                for candidate in chunk.get("candidates") or []:
                    # 只认第一条候选。我们不发 candidateCount，默认就是 1；
                    # 万一上游返回多条，混进同一条消息会串味。
                    if candidate.get("index", 0) != 0:
                        continue

                    content = candidate.get("content") or {}

                    for part in content.get("parts") or []:
                        if part.get("thought"):
                            if sig := part.get("thoughtSignature"):
                                signature = sig
                            if text := part.get("text"):
                                for _event in assembler.thinking(text):
                                    yield _event

                        elif "text" in part:
                            # 文本开始，思考块要先收尾（end_thinking 幂等）
                            for _event in assembler.end_thinking(signature or None):
                                yield _event
                            signature = ""
                            if part["text"]:
                                for _event in assembler.text(part["text"]):
                                    yield _event

                        elif "functionCall" in part:
                            for _event in assembler.end_thinking(signature or None):
                                yield _event
                            signature = ""
                            call = part["functionCall"]
                            # 本厂商没有 call id，自己合成一个
                            for _event in assembler.start_tool(f"call_{tool_count}", call["name"]):
                                yield _event
                            tool_count += 1
                            saw_tool = True
                            arguments = call.get("args") or {}
                            for _event in assembler.tool_arguments(
                                json.dumps(arguments, ensure_ascii=False)
                            ):
                                yield _event
                            for _event in assembler.end_tool():
                                yield _event

                    if reason := candidate.get("finishReason"):
                        if reason == "MALFORMED_FUNCTION_CALL":
                            for _event in assembler.fail(
                                "error", "gemini: MALFORMED_FUNCTION_CALL"
                            ):
                                yield _event
                            return
                        stop_reason = STOP_REASONS.get(reason, "stop")

            # 本厂商对纯工具调用不给专门的 finishReason，靠内容判定
            if saw_tool and stop_reason == "stop":
                stop_reason = "tool_use"

            for _event in assembler.done(stop_reason):
                yield _event

        # 厂商数据不可信：合法 JSON 也可能是数组或标量（`[]`、`"ok"`），
        # `chunk.get(...)` 会抛 AttributeError。契约要求除取消除外不外抛，所以这里
        # 兜底捕获 Exception；CancelledError / GeneratorExit 属于 BaseException，不会被吞。
        except Exception as exc:
            for _event in assembler.fail("error", f"parse failed: {type(exc).__name__}: {exc}"):
                yield _event

    def _url(self) -> str:
        model = self.spec.model
        base = self.spec.base_url.rstrip("/")
        return f"{base}/v1beta/models/{model}:streamGenerateContent?alt=sse"

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        if self.api_key:
            headers["x-goog-api-key"] = self.api_key
        return headers
