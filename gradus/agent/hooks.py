"""agent 层控制钩子。

hook 和事件是两种东西：

- 事件（`AgentEvent` / ai 事件）是“已经发生”的只读通知；
- hook 是“还没发生或正在发生”的控制点，返回值可以拦截或改写。

六个控制 hook 和三个运行级 hook 在生命周期中的位置：

    before_run
    -> transform_messages
    -> prepare_request
    -> provider.stream
    -> before_tool_call
    -> tool.execute
    -> after_tool_call
    -> finish_turn
    -> TurnEnd
    -> prepare_next_turn
    -> before_finish
    -> AgentDone

    on_max_turns：轮次到达上限、尚未以 AgentError(max_turns) 终止时调用。

依赖方向：本模块 -> gradus.agent.types / gradus.ai.base / gradus.ai.types。
不 import coding，也不持有具体工具。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from ..ai.base import Request
from ..ai.types import (
    AssistantMessage,
    Message,
    StopReason,
    ToolCallBlock,
    ToolResultMessage,
    Usage,
)
from .types import ToolResult

if TYPE_CHECKING:
    from .base import LoopConfig

TurnAction = Literal["continue", "end"]

# =============================================================================
# 运行侧
# =============================================================================


@dataclass(frozen=True)
class BeforeRunContext:
    """`AgentStart` 之后、循环开始之前。"""

    messages: tuple[Message, ...]
    config: LoopConfig


@dataclass(frozen=True)
class BeforeRunUpdate:
    """只覆盖显式给出的字段；为 `None` 的字段保持原值。"""

    messages: tuple[Message, ...] | None = None
    config: LoopConfig | None = None


@dataclass(frozen=True)
class BeforeFinishContext:
    """运行即将以 `AgentDone` 结束。"""

    new_messages: tuple[Message, ...]
    usage: Usage
    turns: int
    stop_reason: StopReason
    turn: int


@dataclass(frozen=True)
class BeforeFinishUpdate:
    """只覆盖显式给出的字段；为 `None` 的字段保持原值。"""

    new_messages: tuple[Message, ...] | None = None
    usage: Usage | None = None
    stop_reason: StopReason | None = None


@dataclass(frozen=True)
class MaxTurnsContext:
    """轮次到达上限、尚未发出 `AgentError(max_turns)`。"""

    new_messages: tuple[Message, ...]
    usage: Usage
    turns: int
    max_turns: int


@dataclass(frozen=True)
class MaxTurnsDecision:
    """`extend_by > 0` 时提高轮次上限并继续；否则终止。"""

    extend_by: int = 0


# =============================================================================
# 请求侧
# =============================================================================


@dataclass(frozen=True)
class TransformMessagesContext:
    """每次请求前，对本次请求看到的历史做裁剪或注入。

    `messages` 是 transcript 的快照；返回的新列表只影响本次请求，
    不写回调用方历史。
    """

    messages: tuple[Message, ...]
    turn: int


@dataclass(frozen=True)
class PrepareRequestContext:
    """请求已经组装好、尚未发给 provider。"""

    request: Request
    turn: int


# =============================================================================
# 工具侧
# =============================================================================


@dataclass(frozen=True)
class BeforeToolCallContext:
    """工具执行前、必填与 schema 校验之后。"""

    call: ToolCallBlock
    arguments: dict[str, Any]
    assistant: AssistantMessage
    turn: int


@dataclass(frozen=True)
class BeforeToolCallResult:
    """返回 `block=True` 可以阻止工具执行，`reason` 会变成错误结果。

    `terminate=True` 参与批次提前结束：本批次所有结果都为 `True` 时，
    循环跳过自动的下一次模型请求。
    """

    block: bool = False
    reason: str = ""
    terminate: bool = False


@dataclass(frozen=True)
class AfterToolCallContext:
    """工具执行后、`ToolExecutionEnd` 之前。"""

    call: ToolCallBlock
    arguments: dict[str, Any]
    assistant: AssistantMessage
    result: ToolResult
    turn: int


@dataclass(frozen=True)
class AfterToolCallResult:
    """只覆盖显式给出的字段；为 `None` 的字段保持原值。

    `terminate` 为 `None` 时保留工具返回的提示，否则用它替换；
    参与批次提前结束的规则与 `ToolResult.terminate` 相同。
    """

    content: str | None = None
    is_error: bool | None = None
    terminate: bool | None = None


# =============================================================================
# 轮次侧
# =============================================================================


@dataclass(frozen=True)
class FinishTurnContext:
    """一轮的工具结果已经写回，`TurnEnd` 尚未发出。"""

    assistant: AssistantMessage
    tool_results: tuple[ToolResultMessage, ...]
    new_messages: tuple[Message, ...]
    turn: int


@dataclass(frozen=True)
class PrepareNextTurnContext:
    """`TurnEnd` 之后、下一轮开始之前。"""

    assistant: AssistantMessage
    tool_results: tuple[ToolResultMessage, ...]
    new_messages: tuple[Message, ...]
    turn: int
    config: LoopConfig


@dataclass(frozen=True)
class NextTurnUpdate:
    """下一轮要追加的消息，或替换后的运行配置。"""

    messages: tuple[Message, ...] = ()
    config: LoopConfig | None = None


# =============================================================================
# 回调类型与注册对象
# =============================================================================

BeforeRun = Callable[[BeforeRunContext], Awaitable[BeforeRunUpdate | None]]
BeforeFinish = Callable[
    [BeforeFinishContext], Awaitable[BeforeFinishUpdate | None]
]
OnMaxTurns = Callable[[MaxTurnsContext], Awaitable[MaxTurnsDecision | None]]
TransformMessages = Callable[
    [TransformMessagesContext], Awaitable[Sequence[Message]]
]
PrepareRequest = Callable[[PrepareRequestContext], Awaitable[Request | None]]
BeforeToolCall = Callable[
    [BeforeToolCallContext], Awaitable[BeforeToolCallResult | None]
]
AfterToolCall = Callable[
    [AfterToolCallContext], Awaitable[AfterToolCallResult | None]
]
FinishTurn = Callable[[FinishTurnContext], Awaitable[TurnAction | None]]
PrepareNextTurn = Callable[
    [PrepareNextTurnContext], Awaitable[NextTurnUpdate | None]
]


@dataclass(frozen=True)
class AgentHooks:
    """一次运行可选的控制钩子。

    全部为 `None` 时，行为与没有 hook 的版本一致。
    """

    before_run: BeforeRun | None = None
    transform_messages: TransformMessages | None = None
    prepare_request: PrepareRequest | None = None
    before_tool_call: BeforeToolCall | None = None
    after_tool_call: AfterToolCall | None = None
    finish_turn: FinishTurn | None = None
    prepare_next_turn: PrepareNextTurn | None = None
    before_finish: BeforeFinish | None = None
    on_max_turns: OnMaxTurns | None = None
