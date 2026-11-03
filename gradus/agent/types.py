"""Gradus agent 层内部协议：一次运行的事件与工具结果。

本模块只定义形状，不含逻辑。它回答的是「一次带工具的任务怎么进行」，
与 ai 层的「一次补全怎么流式返回」互补：

- ai 的 12 个事件描述单次模型请求，本层原样透传；
- 本层额外给出运行、轮次、工具执行的边界与终止结果。

一次运行的事件顺序：

    AgentStart
    (TurnStart -> ai 事件* -> [ToolExecutionStart / ToolExecutionEnd]* -> TurnEnd)*
    AgentDone | AgentError

`StreamDone` 是单轮结束，`AgentDone` 是整次运行结束；失败或取消走 `AgentError`。
`AgentError.partial` 是失败那一轮的半成品，不写入 `new_messages`，是否保留由上层决定。

依赖方向：本模块 -> gradus.ai.types -> 标准库。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Union

from ..ai.types import (
    AssistantEvent,
    AssistantMessage,
    Message,
    StopReason,
    ToolCallBlock,
    ToolResultMessage,
    Usage,
)

# =============================================================================
# 工具结果
# =============================================================================


@dataclass(frozen=True)
class ToolResult:
    """工具执行结果。

    `is_error` 为真时，循环会把它写成 `ToolResultMessage(is_error=True)`，
    模型能看出这次调用失败。工具也可以直接抛异常，循环同样会转成错误结果。

    `terminate=True` 是运行时提示：本批次所有结果都为 `True` 时，循环跳过
    “把结果交回模型”的自动请求。它不会写入 `ToolResultMessage`，
    也不改变 `AgentDone.stop_reason`。

    只有 `content` / `is_error` 会进入历史，模型下一轮只看到这两项。
    `details` / `structured_content` / `usage` 只通过 `ToolExecutionEnd` 暴露给
    UI、日志与统计；它们不进入历史，也不计入 `AgentDone.usage`。
    """

    content: str
    is_error: bool = False
    terminate: bool = False
    details: Any = None
    structured_content: Any = None
    usage: Usage | None = None


# =============================================================================
# 运行级事件（8 个）
# =============================================================================


@dataclass
class AgentStart:
    """一次运行开始。"""


@dataclass
class TurnStart:
    """一轮开始。

    一轮 = 一次模型请求 + 它要求的工具批次。`turn` 从 0 递增。
    """

    turn: int


@dataclass
class ToolExecutionStart:
    """开始执行一个工具调用。

    ai 层只负责产出 `ToolCallBlock`，真正执行发生在这里。
    """

    call: ToolCallBlock


@dataclass
class ToolExecutionUpdate:
    """工具执行过程中的增量进度。

    只用于 UI / 日志，不写入历史。同一个调用内按工具发出的顺序到达；
    并行批次里不同调用的更新会交错，消费方按 `call.id` 归属。
    """

    call: ToolCallBlock
    partial: str


@dataclass
class ToolExecutionEnd:
    """一个工具调用执行完毕。

    `duration_ms` 是从 `ToolExecutionStart` 到本次事件的毫秒数，
    包含查找、校验、hook 与执行本身。
    """

    call: ToolCallBlock
    result: ToolResult
    duration_ms: int | None = None


@dataclass
class TurnEnd:
    """一轮结束。

    `assistant` 是本轮最终消息；`tool_results` 是本轮执行产生的工具结果，
    没有执行工具时为空；`usage` 是本轮模型请求的用量。
    """

    assistant: AssistantMessage
    tool_results: list[ToolResultMessage]
    usage: Usage


@dataclass
class AgentDone:
    """整次运行正常结束。

    `new_messages` 只包含本次运行新增的消息，不包含调用方传入的历史；
    `usage` 是各轮用量的总和；`stop_reason` 是最后一条 assistant 消息的终止原因。
    """

    new_messages: list[Message]
    usage: Usage
    turns: int
    stop_reason: StopReason


@dataclass
class AgentError:
    """整次运行异常结束。

    `kind` 的含义：
    - provider  模型流失败，或流结束时缺少 StreamDone
    - aborted   模型流被中断，或运行被取消
    - max_turns 达到轮次上限
    - config    配置非法，例如工具重名

    `partial` 是失败那一轮的半成品，不写入 `new_messages`。
    """

    kind: Literal["provider", "aborted", "max_turns", "config"]
    message: str
    partial: AssistantMessage | None
    new_messages: list[Message]
    usage: Usage


AgentEvent = Union[
    AgentStart,
    TurnStart,
    AssistantEvent,
    ToolExecutionStart,
    ToolExecutionUpdate,
    ToolExecutionEnd,
    TurnEnd,
    AgentDone,
    AgentError,
]
