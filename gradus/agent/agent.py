"""有状态 Agent：在 `run_loop` 之上保存历史与运行状态。

M6 先做最小门面：

- `prompt()` 追加一条用户消息并启动一次运行；
- `continue_()` 不追加消息，直接在现有历史上再跑一次；
- `abort()` 取消当前运行，已完成的轮次保留在历史里；
- `wait_for_idle()` 等待当前运行结束；
- `reset()` 清空历史；
- `aclose()` 取消运行并关闭自建的连接池。

`connect()` 是另一个构造入口：自己建 Provider 与 `httpx.AsyncClient`，让连接池跨轮复用，
寿命与本实例一致。

事件仍然来自 `run_loop`，本模块不改事件形状。运行放在内部 task 里，
事件经队列转发，因此 `abort()` 可以真正取消模型流或工具执行。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import replace
from typing import Any, Literal

import httpx

from ..ai.base import ModelSpec, Provider
from ..ai.providers import create_provider
from ..ai.types import Message, UserMessage
from .base import LoopConfig
from .loop import run_loop
from .queues import PendingMessageQueue, QueueMode
from .types import AgentDone, AgentError, AgentEvent, TurnEnd

AgentState = Literal["idle", "running"]

_END = object()


class Agent:
    """保存历史的单次运行门面。

    `prompt()` / `continue_()` 会立即启动运行，并返回事件迭代器；
    即使调用方不消费事件，运行也会继续，完成后写入历史。
    运行中再次调用 `prompt()` / `continue_()` 会抛 `RuntimeError`。
    """

    def __init__(
        self,
        provider: Provider,
        config: LoopConfig,
        messages: list[Message] | None = None,
        *,
        steering_mode: QueueMode = "all",
        follow_up_mode: QueueMode = "all",
    ) -> None:
        self._provider = provider
        self._config = config
        self._messages: list[Message] = list(messages or [])
        self._state: AgentState = "idle"
        self._task: asyncio.Task[None] | None = None
        self._events: asyncio.Queue[Any] | None = None
        self._ended = False
        self._steering_mode = steering_mode
        self._follow_up_mode = follow_up_mode
        self._steering: PendingMessageQueue | None = None
        self._follow_up: PendingMessageQueue | None = None
        self._client: httpx.AsyncClient | None = None
        self._owns_client = False
        self._closed = False

    @classmethod
    async def connect(
        cls,
        spec: ModelSpec,
        config: LoopConfig,
        *,
        api_key: str = "",
        client: httpx.AsyncClient | None = None,
        messages: list[Message] | None = None,
        steering_mode: QueueMode = "all",
        follow_up_mode: QueueMode = "all",
    ) -> Agent:
        """建 Provider，并把连接池的寿命绑到本实例。

        `client=None` 时自建一个并拥有它，`aclose()` 负责关闭；传入 `client` 时只借用，
        `aclose()` 不关闭——同一个池子可以给多个 Agent / Provider 共用，由创建者统一关。
        传 `None` 且不调 `aclose()` 会泄漏，这条与 ai 层 `create_provider` 的约定一致。
        """
        owned = client is None
        if client is None:
            client = httpx.AsyncClient()

        try:
            provider = create_provider(spec, api_key, client=client)
        except BaseException:
            if owned:
                await client.aclose()
            raise

        agent = cls(
            provider,
            config,
            messages,
            steering_mode=steering_mode,
            follow_up_mode=follow_up_mode,
        )
        agent._client = client
        agent._owns_client = owned
        return agent

    @property
    def state(self) -> AgentState:
        return self._state

    @property
    def messages(self) -> tuple[Message, ...]:
        """当前历史的只读快照。"""
        return tuple(self._messages)

    def prompt(self, text: str) -> AsyncIterator[AgentEvent]:
        """追加一条用户消息并启动一次运行。"""
        self._ensure_idle()
        self._messages.append(UserMessage(text))
        return self._start()

    def continue_(self) -> AsyncIterator[AgentEvent]:
        """不追加用户消息，直接在现有历史上再跑一次。"""
        self._ensure_idle()
        return self._start()

    def steer(self, text: str) -> None:
        """运行中插话；空闲时调用会抛 `RuntimeError`。"""
        self._push(self._steering, text, "steer")

    def follow_up(self, text: str) -> None:
        """追加一个“做完当前任务后”的任务；空闲时抛 `RuntimeError`。"""
        self._push(self._follow_up, text, "follow_up")

    def _push(
        self,
        queue: PendingMessageQueue | None,
        text: str,
        name: str,
    ) -> None:
        if self._state != "running" or queue is None:
            raise RuntimeError(f"cannot {name}: agent is not running")
        queue.push(UserMessage(text))

    async def abort(self) -> None:
        """取消当前运行；已完成的轮次保留在历史里。"""
        task = self._task
        if task is None:
            return

        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await self._finish_if_needed()

    async def aclose(self) -> None:
        """取消当前运行，关闭自己创建的连接池；外部注入的 client 不关。

        幂等。关闭后 `prompt()` / `continue_()` / `reset()` 抛 `RuntimeError`。
        """
        if self._closed:
            return

        await self.abort()
        self._closed = True

        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
            self._owns_client = False

    async def wait_for_idle(self) -> None:
        """等待当前运行结束，不取消它。"""
        task = self._task
        if task is None:
            return

        with suppress(asyncio.CancelledError):
            await task
        await self._finish_if_needed()

    def reset(self) -> None:
        """清空历史；运行中不允许重置。"""
        self._ensure_idle()
        self._messages.clear()

    def _ensure_idle(self) -> None:
        if self._closed:
            raise RuntimeError("agent is closed")
        if self._state != "idle":
            raise RuntimeError("agent is already running")

    def _start(self) -> AsyncIterator[AgentEvent]:
        self._state = "running"
        self._ended = False
        queue: asyncio.Queue[Any] = asyncio.Queue()
        self._events = queue
        self._steering = PendingMessageQueue(self._steering_mode)
        self._follow_up = PendingMessageQueue(self._follow_up_mode)

        config = replace(
            self._config,
            get_steering_messages=self._steering.drain,
            get_follow_up_messages=self._follow_up.drain,
        )
        self._task = asyncio.create_task(self._pump(queue, config))
        return self._consume(queue, self._task)

    async def _pump(
        self, queue: asyncio.Queue[Any], config: LoopConfig
    ) -> None:
        """驱动 `run_loop`，转发事件，并把最终消息写入历史。"""

        turn_messages: list[Message] = []
        try:
            async for event in run_loop(
                self._provider,
                list(self._messages),
                config,
            ):
                await queue.put(event)

                if isinstance(event, TurnEnd):
                    turn_messages.append(event.assistant)
                    turn_messages.extend(event.tool_results)
                elif isinstance(event, (AgentDone, AgentError)):
                    self._messages.extend(event.new_messages)
                    turn_messages.clear()
        except asyncio.CancelledError:
            # 取消时保留已经完成的轮次，丢弃当前半轮。
            self._messages.extend(turn_messages)
            raise
        finally:
            self._ended = True
            self._state = "idle"
            self._task = None
            self._steering = None
            self._follow_up = None
            await queue.put(_END)

    async def _consume(
        self, queue: asyncio.Queue[Any], task: asyncio.Task[None]
    ) -> AsyncIterator[AgentEvent]:
        try:
            while True:
                item = await queue.get()
                if item is _END:
                    break
                yield item
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await self._finish_if_needed(queue)

    async def _finish_if_needed(
        self, queue: asyncio.Queue[Any] | None = None
    ) -> None:
        """任务在启动前就被取消时，`_pump` 的 `finally` 不会执行。

        `queue` 是这一次运行的队列。调用方中断消费后（例如重试前直接丢弃
        旧迭代器），旧迭代器可能在新一次运行开始之后才被回收；此时只能
        收尾它自己那一次运行，否则会把新一轮置回 idle 并塞进结束标记。
        """
        if queue is None:
            queue = self._events
        if queue is not self._events or self._ended:
            return

        self._ended = True
        self._state = "idle"
        self._task = None
        self._steering = None
        self._follow_up = None
        if self._events is not None:
            await self._events.put(_END)
