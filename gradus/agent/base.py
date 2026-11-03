"""agent 层对外契约：工具执行与运行配置。

本模块只定义协议与配置，不含实现。具体工具由上层（coding）实现并注入，
agent 只按 `ToolSpec.name` 查找、检查参数、`await` 执行。

依赖方向：本模块 -> gradus.agent.types / gradus.ai.base。
不放：循环实现、具体工具、Provider 构造。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from ..ai.base import ReasoningLevel, ToolSpec
from ..ai.types import Message
from .hooks import AgentHooks
from .types import ToolResult

ExecutionMode = Literal["serial", "parallel"]
ToolUpdateCallback = Callable[[str], None]


class AgentTool(Protocol):
    """工具契约。

    实现放在上层（coding），agent 只认 `spec` 与 `execute`，不 import 任何具体工具。

    构造约定：工具实例由上层创建，通过 `LoopConfig.tools` 注入。需要环境、会话或
    权限时，在上层构造工具时自己持有，不要反过来访问 agent 的内部状态。

    `execute` 必须是异步的。同步工具由上层用 `asyncio.to_thread` 包一层，
    agent 不替工具决定用哪种执行方式。

    `execution_mode` 由工具作者声明，agent 不做运行时推断：`"parallel"` 表示同批次
    内的多个调用可以并发；不提供或为 `"serial"` 时按串行处理。只要批次里有一个
    串行工具，整批就串行执行。

    `on_update` 是循环提供的进度回调；工具可以在执行过程中调用它，把片段转成
    `ToolExecutionUpdate` 事件。只在执行期间有效，`execute` 返回后的调用会被丢弃。

    失败有两种表达方式：返回 `ToolResult(is_error=True)`，或直接抛异常。
    循环会把异常转成 `is_error` 的工具结果，不会中断整次运行。
    """

    spec: ToolSpec
    execution_mode: ExecutionMode

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        ...


@dataclass(frozen=True)
class LoopConfig:
    """一次运行的静态配置。

    模型名与 api_key 不在这里：Provider 由上层构造后传给 `run_loop`。

    `system` 是系统提示词的唯一入口。
    `tools` 是已构造好的工具实例，顺序决定发给模型的声明顺序。
    `max_turns` 按模型请求次数计，不按工具批次数计。
    `tool_timeout` 是单个工具执行的超时秒数；`None` 表示不设超时。
    `hooks` 是可选的控制钩子；不传时行为与无 hook 版本一致。
    `get_steering_messages` 在每次请求前、每轮结束后取运行中插话；
    `get_follow_up_messages` 只在本来要停止时取追加任务。
    两个 getter 都是同步的，返回空序列表示没有新消息。
    """

    system: str
    tools: Sequence[AgentTool] = ()
    max_turns: int = 20
    max_output_tokens: int | None = None
    temperature: float | None = None
    reasoning: ReasoningLevel | None = None
    tool_timeout: float | None = None
    hooks: AgentHooks = field(default_factory=AgentHooks)
    get_steering_messages: Callable[[], Sequence[Message]] | None = None
    get_follow_up_messages: Callable[[], Sequence[Message]] | None = None
