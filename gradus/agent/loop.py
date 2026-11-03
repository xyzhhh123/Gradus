"""agent 层控制流：一次带工具的运行。

本模块只做调度，不碰文件、网络、环境变量或具体工具。它依赖两件事：

- `ai` 层的 `Provider.stream()`：一次模型请求的 12 个事件；
- 上层注入的 `AgentTool`：按名字查找、`await` 执行。

一轮 = 一次模型请求 + 它要求的工具批次。steering 在请求前和轮次后注入；
follow-up 只在本来要停止时注入，有消息就在同一个循环里再跑一轮。

契约：
- 除 `asyncio.CancelledError` 外不抛异常；失败编码为 `AgentError`。
- 一次运行恰好一个终止事件：`AgentDone` 或 `AgentError`。
- ai 的事件原样转发；每个被执行的工具调用都有对应的工具结果。
- 只有 `stop_reason == "tool_use"` 才执行工具。
- 调用方传入的 `messages` 只读，新增消息由终止事件带回。

hook 与事件的分工：
- 事件是只读通知；
- hook 在固定时机运行，返回值可以拦截或改写；
- 全部 hook 都是可选的，未设置时行为与无 hook 版本一致。

依赖方向：本模块 -> gradus.agent.base / hooks / types + gradus.ai.types。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import suppress
from typing import Any

from ..ai.base import Provider, Request
from ..ai.types import (
    AssistantMessage,
    Message,
    StopReason,
    StreamDone,
    StreamError,
    ToolCallBlock,
    ToolResultMessage,
    Usage,
)
from .base import AgentTool, LoopConfig
from .hooks import (
    AfterToolCallContext,
    AgentHooks,
    BeforeFinishContext,
    BeforeRunContext,
    BeforeToolCallContext,
    FinishTurnContext,
    MaxTurnsContext,
    MaxTurnsDecision,
    PrepareNextTurnContext,
    PrepareRequestContext,
    TransformMessagesContext,
    TurnAction,
)
from .types import (
    AgentDone,
    AgentError,
    AgentEvent,
    AgentStart,
    ToolExecutionEnd,
    ToolExecutionStart,
    ToolExecutionUpdate,
    ToolResult,
    TurnEnd,
    TurnStart,
)
from .validation import validate_arguments


async def _run_loop_events(
    provider: Provider,
    messages: list[Message],
    config: LoopConfig,
) -> AsyncIterator[AgentEvent]:
    """驱动一次运行的核心循环，逐条产出 agent 事件。"""
    yield AgentStart()

    # before_run: 循环开始前替换初始消息或运行配置
    if config.hooks.before_run is not None:
        try:
            start_update = await config.hooks.before_run(
                BeforeRunContext(messages=tuple(messages), config=config)
            )
        except Exception as exc:
            yield _config_error("before_run", exc, [], Usage())
            return

        if start_update is not None:
            if start_update.config is not None:
                config = start_update.config
            if start_update.messages is not None:
                messages = list(start_update.messages)

    registry, config_error = _build_registry(config.tools)
    if config_error is not None:
        yield AgentError(
            kind="config",
            message=config_error,
            partial=None,
            new_messages=[],
            usage=Usage(),
        )
        return

    transcript = list(messages)
    new_messages: list[Message] = []
    pending_messages: list[Message] = []
    total_usage = Usage()

    turn = 0
    limit = config.max_turns

    while True:
        # on_max_turns: 到达上限时决定是否再给几轮
        if turn >= limit:
            decision: MaxTurnsDecision | None = None
            if config.hooks.on_max_turns is not None:
                try:
                    decision = await config.hooks.on_max_turns(
                        MaxTurnsContext(
                            new_messages=tuple(new_messages),
                            usage=total_usage,
                            turns=turn,
                            max_turns=limit,
                        )
                    )
                except Exception as exc:
                    yield _config_error(
                        "on_max_turns", exc, new_messages, total_usage
                    )
                    return

            if decision is not None and decision.extend_by > 0:
                limit += decision.extend_by
            else:
                yield AgentError(
                    kind="max_turns",
                    message=f"exceeded max_turns={limit}",
                    partial=None,
                    new_messages=new_messages,
                    usage=total_usage,
                )
                return

        yield TurnStart(turn)

        # steering：请求前把运行中插话并入 transcript，随本轮一起提交。
        try:
            steering = _take_messages(config.get_steering_messages)
        except Exception as exc:
            yield _config_error(
                "get_steering_messages", exc, new_messages, total_usage
            )
            return
        if steering:
            transcript.extend(steering)
            pending_messages.extend(steering)

        # transform_messages: 只影响本次请求，不写回 transcript
        request_messages = list(transcript)
        if config.hooks.transform_messages is not None:
            try:
                transformed = await config.hooks.transform_messages(
                    TransformMessagesContext(
                        messages=tuple(transcript),
                        turn=turn,
                    )
                )
                request_messages = list(transformed)
            except Exception as exc:
                yield _config_error(
                    "transform_messages", exc, new_messages, total_usage
                )
                return

        request = Request(
            system=config.system,
            messages=request_messages,
            tools=[tool.spec for tool in config.tools],
            max_output_tokens=config.max_output_tokens,
            temperature=config.temperature,
            reasoning=config.reasoning,
        )

        # prepare_request: 允许替换本次请求
        if config.hooks.prepare_request is not None:
            try:
                updated_request = await config.hooks.prepare_request(
                    PrepareRequestContext(request=request, turn=turn)
                )
                if updated_request is not None:
                    request = updated_request
            except Exception as exc:
                yield _config_error(
                    "prepare_request", exc, new_messages, total_usage
                )
                return

        assistant: AssistantMessage | None = None
        stop_reason: StopReason | None = None
        turn_usage = Usage()

        stream = provider.stream(request)
        while True:
            try:
                event = await anext(stream)
            except StopAsyncIteration:
                break
            except Exception as exc:
                # provider 违约抛异常：编码成 AgentError。
                # CancelledError 是 BaseException，不会走到这里。
                yield AgentError(
                    kind="provider",
                    message=f"{type(exc).__name__}: {exc}",
                    partial=None,
                    new_messages=new_messages,
                    usage=total_usage,
                )
                return

            yield event

            if isinstance(event, StreamDone):
                assistant = event.message
                stop_reason = event.stop_reason
                turn_usage = event.usage
                total_usage = total_usage + event.usage

            elif isinstance(event, StreamError):
                # 失败那一轮不计入 new_messages，半成品只放在 partial 里
                if event.partial.usage is not None:
                    total_usage = total_usage + event.partial.usage
                yield AgentError(
                    kind="aborted" if event.kind == "aborted" else "provider",
                    message=event.error_message,
                    partial=event.partial,
                    new_messages=new_messages,
                    usage=total_usage,
                )
                return

        if assistant is None:
            yield AgentError(
                kind="provider",
                message="stream ended without StreamDone",
                partial=None,
                new_messages=new_messages,
                usage=total_usage,
            )
            return

        calls = [
            block for block in assistant.content if isinstance(block, ToolCallBlock)
        ]

        if stop_reason == "tool_use" and not calls:
            yield AgentError(
                kind="provider",
                message="stop_reason=tool_use but no tool calls",
                partial=assistant,
                new_messages=new_messages,
                usage=total_usage,
            )
            return

        # 一整轮先在本地组装，成功后再一次性提交；取消时整轮丢弃。
        # pending_messages 是上一轮 prepare_next_turn 注入的消息，
        # 它们属于下一轮，随这一轮一起提交。
        turn_messages: list[Message] = [*pending_messages, assistant]
        pending_messages.clear()
        tool_results: list[ToolResultMessage] = []
        tool_terminated: list[bool] = []

        # length 截断时参数可能不完整：不执行工具，但每个调用都要有错误结果。
        truncated = stop_reason == "length"

        if truncated:
            for call in calls:
                yield ToolExecutionStart(call)
                result = ToolResult(
                    f"tool {call.name} was not executed: "
                    "response was truncated",
                    is_error=True,
                )
                yield ToolExecutionEnd(call, result, 0)
                _append_tool_result(
                    tool_results, turn_messages, tool_terminated, call, result
                )
        elif stop_reason == "tool_use":
            results: list[tuple[ToolResult, int] | None] = [None] * len(calls)
            updates: asyncio.Queue[tuple[ToolCallBlock, str]] = asyncio.Queue()

            def make_update(call: ToolCallBlock) -> Callable[[str], None]:
                def push(partial: str) -> None:
                    updates.put_nowait((call, partial))

                return push

            if _can_run_parallel(registry, calls):
                for call in calls:
                    yield ToolExecutionStart(call)

                tasks = {
                    asyncio.create_task(
                        _run_tool(
                            registry,
                            call,
                            assistant,
                            turn,
                            config.hooks,
                            config.tool_timeout,
                            make_update(call),
                        )
                    ): index
                    for index, call in enumerate(calls)
                }
                async for event in _drive_tool_tasks(
                    tasks, calls, updates, results
                ):
                    yield event

                # 结果按 assistant 里的源顺序写回，不按完成顺序。
                for index, call in enumerate(calls):
                    entry = results[index]
                    assert entry is not None
                    result, _ = entry
                    _append_tool_result(
                        tool_results, turn_messages, tool_terminated, call, result
                    )
            else:
                for call in calls:
                    yield ToolExecutionStart(call)
                    task = asyncio.create_task(
                        _run_tool(
                            registry,
                            call,
                            assistant,
                            turn,
                            config.hooks,
                            config.tool_timeout,
                            make_update(call),
                        )
                    )
                    async for event in _drive_tool_tasks(
                        {task: 0}, [call], updates, results
                    ):
                        yield event
                    entry = results[0]
                    assert entry is not None
                    result, _ = entry
                    _append_tool_result(
                        tool_results, turn_messages, tool_terminated, call, result
                    )

        # 只有整批结果都要求终止时才跳过下一次模型请求。
        batch_terminated = bool(tool_terminated) and all(tool_terminated)

        transcript.extend(turn_messages)
        new_messages.extend(turn_messages)

        # finish_turn: 在 TurnEnd 之前决定继续还是结束
        action: TurnAction | None = None
        if config.hooks.finish_turn is not None:
            try:
                action = await config.hooks.finish_turn(
                    FinishTurnContext(
                        assistant=assistant,
                        tool_results=tuple(tool_results),
                        new_messages=tuple(new_messages),
                        turn=turn,
                    )
                )
            except Exception as exc:
                yield _config_error(
                    "finish_turn", exc, new_messages, total_usage
                )
                return

        yield TurnEnd(assistant, tool_results, turn_usage)

        if action == "end":
            yield await _finalize(
                config.hooks,
                new_messages,
                total_usage,
                turn + 1,
                stop_reason or "stop",
                turn,
            )
            return

        # steering：本轮结束后再排空一次，新插话留给下一轮请求。
        try:
            steering = _take_messages(config.get_steering_messages)
        except Exception as exc:
            yield _config_error(
                "get_steering_messages", exc, new_messages, total_usage
            )
            return
        steering_added = bool(steering)
        if steering:
            transcript.extend(steering)
            pending_messages.extend(steering)

        should_continue = (
            (stop_reason == "tool_use" and not batch_terminated)
            or stop_reason == "deferred"
            or (truncated and bool(calls))
            or steering_added
            or action == "continue"
        )

        # follow-up：本来要停止时，再看有没有追加任务。
        if not should_continue:
            try:
                follow_up = _take_messages(config.get_follow_up_messages)
            except Exception as exc:
                yield _config_error(
                    "get_follow_up_messages", exc, new_messages, total_usage
                )
                return
            if follow_up:
                transcript.extend(follow_up)
                pending_messages.extend(follow_up)
                should_continue = True

        if not should_continue:
            yield await _finalize(
                config.hooks,
                new_messages,
                total_usage,
                turn + 1,
                stop_reason or "stop",
                turn,
            )
            return

        # prepare_next_turn: 在 TurnEnd 之后、下一轮之前
        if config.hooks.prepare_next_turn is not None:
            try:
                update = await config.hooks.prepare_next_turn(
                    PrepareNextTurnContext(
                        assistant=assistant,
                        tool_results=tuple(tool_results),
                        new_messages=tuple(new_messages),
                        turn=turn,
                        config=config,
                    )
                )
            except Exception as exc:
                yield _config_error(
                    "prepare_next_turn", exc, new_messages, total_usage
                )
                return

            if update is not None:
                if update.config is not None:
                    new_registry, update_error = _build_registry(
                        update.config.tools
                    )
                    if update_error is not None:
                        yield AgentError(
                            kind="config",
                            message=update_error,
                            partial=None,
                            new_messages=new_messages,
                            usage=total_usage,
                        )
                        return
                    config = update.config
                    limit = config.max_turns
                    registry = new_registry

                if update.messages:
                    transcript.extend(update.messages)
                    pending_messages.extend(update.messages)

        turn += 1


async def run_loop(
    provider: Provider,
    messages: list[Message],
    config: LoopConfig,
) -> AsyncIterator[AgentEvent]:
    """驱动一次运行，逐条产出 agent 事件。

    在核心循环外包一层，把取消编码成 `AgentError(kind="aborted")`：
    每次运行都有且只有一个终止事件；发完事件后继续抛 `CancelledError`。
    """
    committed: list[Message] = []
    usage = Usage()
    partial: AssistantMessage | None = None

    try:
        async for event in _run_loop_events(provider, messages, config):
            if isinstance(event, TurnStart):
                partial = None
            elif isinstance(event, TurnEnd):
                committed.append(event.assistant)
                committed.extend(event.tool_results)
            elif isinstance(event, StreamDone):
                usage = usage + event.usage
            elif isinstance(event, StreamError):
                if event.partial.usage is not None:
                    usage = usage + event.partial.usage

            candidate = getattr(event, "partial", None)
            if isinstance(candidate, AssistantMessage):
                partial = candidate

            yield event
    except asyncio.CancelledError:
        yield AgentError(
            kind="aborted",
            message="run was cancelled",
            partial=partial,
            new_messages=list(committed),
            usage=usage,
        )
        raise


async def _finalize(
    hooks: AgentHooks,
    new_messages: list[Message],
    usage: Usage,
    turns: int,
    stop_reason: StopReason,
    turn: int,
) -> AgentDone | AgentError:
    """发出 `AgentDone` 前，允许 `before_finish` 改写最终结果。"""
    final_messages = list(new_messages)
    final_usage = usage
    final_reason = stop_reason

    if hooks.before_finish is not None:
        try:
            update = await hooks.before_finish(
                BeforeFinishContext(
                    new_messages=tuple(new_messages),
                    usage=usage,
                    turns=turns,
                    stop_reason=stop_reason,
                    turn=turn,
                )
            )
        except Exception as exc:
            return _config_error("before_finish", exc, new_messages, usage)

        if update is not None:
            if update.new_messages is not None:
                final_messages = list(update.new_messages)
            if update.usage is not None:
                final_usage = update.usage
            if update.stop_reason is not None:
                final_reason = update.stop_reason

    return AgentDone(
        new_messages=final_messages,
        usage=final_usage,
        turns=turns,
        stop_reason=final_reason,
    )


def _config_error(
    hook_name: str,
    exc: Exception,
    new_messages: list[Message],
    usage: Usage,
) -> AgentError:
    """请求 / 轮次 hook 抛异常时，统一转成 config 错误。"""
    return AgentError(
        kind="config",
        message=f"{hook_name} failed: {type(exc).__name__}: {exc}",
        partial=None,
        new_messages=list(new_messages),
        usage=usage,
    )


def _take_messages(
    getter: Callable[[], Sequence[Message]] | None,
) -> list[Message]:
    """同步取出待注入消息；没有 getter 时返回空列表。"""
    if getter is None:
        return []
    return list(getter())


def _build_registry(
    tools: Sequence[AgentTool],
) -> tuple[dict[str, AgentTool], str | None]:
    """按 `spec.name` 建索引；重名视为配置错误，不进入循环。"""
    registry: dict[str, AgentTool] = {}

    for tool in tools:
        name = tool.spec.name
        if name in registry:
            return registry, f"duplicate tool name: {name}"
        registry[name] = tool

    return registry, None


def _can_run_parallel(
    registry: dict[str, AgentTool],
    calls: list[ToolCallBlock],
) -> bool:
    """规则 A：只要有一个工具不是 `parallel`，整批串行。

    未知工具按 serial 处理，避免对查不到实现的调用并发调度；
    单个调用没有并发收益，也直接走串行。
    """
    if len(calls) < 2:
        return False

    for call in calls:
        tool = registry.get(call.name)
        if tool is None:
            return False
        if getattr(tool, "execution_mode", "serial") != "parallel":
            return False

    return True


def _append_tool_result(
    tool_results: list[ToolResultMessage],
    turn_messages: list[Message],
    tool_terminated: list[bool],
    call: ToolCallBlock,
    result: ToolResult,
) -> None:
    """把一次工具执行结果写进本轮消息，并记录 `terminate` 提示。"""
    tool_result = ToolResultMessage(
        tool_call_id=call.id,
        content=result.content,
        is_error=result.is_error,
    )
    tool_results.append(tool_result)
    turn_messages.append(tool_result)
    tool_terminated.append(result.terminate)


async def _run_tool(
    registry: dict[str, AgentTool],
    call: ToolCallBlock,
    assistant: AssistantMessage,
    turn: int,
    hooks: AgentHooks,
    tool_timeout: float | None,
    on_update: Callable[[str], None],
) -> tuple[ToolResult, int]:
    """执行一次工具调用，返回结果与耗时。"""
    started = time.perf_counter()
    result = await _execute_tool(
        registry,
        call,
        assistant,
        turn,
        hooks,
        tool_timeout,
        on_update,
    )
    return result, int((time.perf_counter() - started) * 1000)


async def _drive_tool_tasks(
    tasks: dict[asyncio.Task[tuple[ToolResult, int]], int],
    calls: list[ToolCallBlock],
    updates: asyncio.Queue[tuple[ToolCallBlock, str]],
    results: list[tuple[ToolResult, int] | None],
) -> AsyncIterator[ToolExecutionEnd]:
    """等待工具完成，同时把执行中的更新按到达顺序转发。

    某个调用完成时，先冲掉队列里剩余的更新，再发它的 `ToolExecutionEnd`，
    保证 `End` 不会越过还没发出的 `Update`。取消时丢弃未发出的更新。
    """
    pending_tasks = set(tasks)
    getter = asyncio.create_task(updates.get())

    try:
        while pending_tasks:
            done, _ = await asyncio.wait(
                pending_tasks | {getter},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if getter in done:
                update_call, partial = getter.result()
                yield ToolExecutionUpdate(update_call, partial)
                getter = asyncio.create_task(updates.get())

            for task in done & pending_tasks:
                pending_tasks.discard(task)

                while not updates.empty():
                    update_call, partial = updates.get_nowait()
                    yield ToolExecutionUpdate(update_call, partial)

                index = tasks[task]
                result, duration_ms = task.result()
                results[index] = (result, duration_ms)
                yield ToolExecutionEnd(calls[index], result, duration_ms)
    finally:
        getter.cancel()
        with suppress(asyncio.CancelledError):
            await getter
        for task in pending_tasks:
            task.cancel()
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)


async def _execute_tool(
    registry: dict[str, AgentTool],
    call: ToolCallBlock,
    assistant: AssistantMessage,
    turn: int,
    hooks: AgentHooks,
    tool_timeout: float | None,
    on_update: Callable[[str], None],
) -> ToolResult:
    """执行一个工具调用，把一切失败转成 `is_error` 的结果。

    只捕获 `Exception`，因此 `asyncio.CancelledError` 会继续向上传播。
    """
    tool = registry.get(call.name)
    if tool is None:
        return ToolResult(f"unknown tool: {call.name}", is_error=True)

    missing = _missing_required(tool.spec.parameters, call.arguments)
    if missing:
        return ToolResult(
            f"missing required arguments for {call.name}: {', '.join(missing)}",
            is_error=True,
        )

    validation_error = validate_arguments(
        call.name,
        tool.spec.parameters,
        call.arguments,
    )
    if validation_error is not None:
        return ToolResult(validation_error, is_error=True)

    arguments = dict(call.arguments)

    if hooks.before_tool_call is not None:
        try:
            decision = await hooks.before_tool_call(
                BeforeToolCallContext(
                    call=call,
                    arguments=arguments,
                    assistant=assistant,
                    turn=turn,
                )
            )
        except Exception as exc:
            return ToolResult(
                f"before_tool_call failed: {type(exc).__name__}: {exc}",
                is_error=True,
            )

        if decision is not None and decision.block:
            return ToolResult(
                decision.reason or "tool execution was blocked",
                is_error=True,
                terminate=decision.terminate,
            )

    try:
        if tool_timeout is None:
            result = await tool.execute(
                call.id, arguments, on_update=on_update
            )
        else:
            async with asyncio.timeout(tool_timeout):
                result = await tool.execute(
                    call.id, arguments, on_update=on_update
                )
    except TimeoutError:
        result = ToolResult(
            f"tool {call.name} timed out after {tool_timeout}s",
            is_error=True,
        )
    except Exception as exc:
        result = ToolResult(f"{type(exc).__name__}: {exc}", is_error=True)

    if not isinstance(result, ToolResult):
        result = ToolResult(
            f"tool {call.name} returned invalid result",
            is_error=True,
        )

    if hooks.after_tool_call is not None:
        try:
            override = await hooks.after_tool_call(
                AfterToolCallContext(
                    call=call,
                    arguments=arguments,
                    assistant=assistant,
                    result=result,
                    turn=turn,
                )
            )
        except Exception as exc:
            return ToolResult(
                f"after_tool_call failed: {type(exc).__name__}: {exc}",
                is_error=True,
            )

        if override is not None:
            result = ToolResult(
                content=(
                    override.content
                    if override.content is not None
                    else result.content
                ),
                is_error=(
                    override.is_error
                    if override.is_error is not None
                    else result.is_error
                ),
                terminate=(
                    override.terminate
                    if override.terminate is not None
                    else result.terminate
                ),
                details=result.details,
                structured_content=result.structured_content,
                usage=result.usage,
            )

    return result


def _missing_required(
    parameters: dict[str, Any],
    arguments: dict[str, Any],
) -> list[str]:
    """第一版只检查 JSON Schema 的 `required`，不做完整校验。"""
    required = parameters.get("required")
    if not isinstance(required, list):
        return []
    return [name for name in required if isinstance(name, str) and name not in arguments]
