"""传输层：HTTP POST、SSE 解析、重试与失败归一。

本模块不认识任何厂商字段，也不知道 12 个事件的存在。它只做两件事：
- 把字节流切成 `SseEvent`（event 名 + data 文本）
- 把一切失败变成 `TransportFailure`，而不是抛异常

谁把 `SseEvent` 翻译成内部事件、谁把 `TransportFailure` 翻译成 `StreamError`，
都是适配器的事。这样拆开的好处：SSE 解析是纯函数，可以拿录制的文本离线断言。

全链路是异步的：`post_sse` 是异步生成器，等待网络时不占用线程，调用方可以同时
照看多个会话。唯一允许穿透的异常是 `asyncio.CancelledError`——取消必须能打断
阻塞读，这个不能吞。

依赖方向：providers/* -> transport.py -> httpx
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterable, AsyncIterator, Iterable, Iterator
from dataclasses import dataclass
from typing import Any

import httpx

__all__ = [
    "SseEvent",
    "TransportFailure",
    "parse_sse",
    "post_sse",
]


@dataclass(frozen=True)
class SseEvent:
    """一条 SSE 记录。

    event 是 `event:` 字段，缺省为 None；data 已按规范把多行 `data:` 用 "\\n" 拼好。
    `[DONE]` 之类的哨兵值原样保留，由适配器判断。
    """

    event: str | None
    data: str


@dataclass(frozen=True)
class TransportFailure:
    """传输层失败。适配器要把它翻译成 `StreamError("error", ...)`。

    只有失败这一种：调用方主动中断由取消任务（`CancelledError`）表达，不经过这里。
    """

    message: str
    status_code: int | None = None


# 408 请求超时 / 409 冲突 / 425 过早 / 429 限流 / 5xx 服务端故障
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)

#: 多久没解析出事件就认定这一流停滞了。
#:
#: `DEFAULT_TIMEOUT` 的 `read` 只量 socket 读取，而 SSE 的保活注释行（`: ping`）
#: 每次都会把它重置——上游只要不停发心跳，停滞的补全就永远等不到超时。
#: 这里量的是「解析出的事件」，心跳骗不过它。
DEFAULT_STALL_TIMEOUT = 120.0
ENV_STALL_TIMEOUT = "GRADUS_STALL_TIMEOUT"


def _stall_timeout_from_env() -> float:
    """读 `GRADUS_STALL_TIMEOUT`；非法值退回默认。"""
    raw = os.environ.get(ENV_STALL_TIMEOUT)
    if not raw:
        return DEFAULT_STALL_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_STALL_TIMEOUT
    return value if value > 0 else DEFAULT_STALL_TIMEOUT


class _Stall(Exception):
    """`stall_timeout` 秒内没有解析出任何事件。"""


async def _guard_stall(
    source: AsyncIterator[SseEvent],
    stall_timeout: float,
) -> AsyncIterator[SseEvent]:
    """逐条转发事件，两次事件间隔超过 `stall_timeout` 就中止这一流。

    超时会取消正在等待的读取，把连接一起断掉，而不是把它留在后台挂着。
    """
    iterator = source.__aiter__()
    while True:
        try:
            event = await asyncio.wait_for(anext(iterator), timeout=stall_timeout)
        except StopAsyncIteration:
            return
        except (TimeoutError, asyncio.TimeoutError):
            raise _Stall(f"no event for {stall_timeout:.0f}s") from None
        yield event


def _user_agent() -> str:
    """客户端标识。有些网关明确要求「自己的名字」而不是通用 SDK 的 UA。"""
    try:
        from importlib.metadata import PackageNotFoundError, version

        return f"gradus/{version('gradus')}"
    except (PackageNotFoundError, ImportError):  # 源码树里直接跑，没有安装元数据
        return "gradus"


#: 自建连接池时统一带上的请求头。注入 client 时由注入方决定。
DEFAULT_HEADERS = {"User-Agent": _user_agent()}

# 单次失败响应体最多带多少字符进错误消息，避免把整页 HTML 塞进事件
_MAX_ERROR_BODY = 300


class _SseParser:
    """逐行喂入的 SSE 状态机。

    同步与异步两条读取路径共用同一份解析逻辑：一边是 `Iterable[str]`，
    一边是 `AsyncIterable[str]`，区别只在怎么拿行。
    """

    __slots__ = ("_event", "_data")

    def __init__(self) -> None:
        self._event: str | None = None
        self._data: list[str] = []

    def feed(self, raw: str) -> SseEvent | None:
        """喂一行；遇到空行时返回刚刚结束的记录（没有 data 则返回 None）。"""
        line = raw.rstrip("\r\n")

        if not line:
            event = self._emit()
            self._event, self._data = None, []
            return event

        if line.startswith(":"):
            return None

        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]

        if field == "event":
            self._event = value
        elif field == "data":
            self._data.append(value)
        return None

    def close(self) -> SseEvent | None:
        """流结束时交付残留记录（末尾没有空行的实现靠这一条）。"""
        return self._emit()

    def _emit(self) -> SseEvent | None:
        if not self._data:
            return None
        return SseEvent(event=self._event, data="\n".join(self._data))


def parse_sse(lines: Iterable[str]) -> Iterator[SseEvent]:
    """把 SSE 文本行解析成事件。

    按规范：空行表示一条记录结束；`:` 开头是注释；`data:` 多行用 "\\n" 拼接；
    `id:` / `retry:` 与未知字段忽略。没有 data 的记录不产出事件。

    入参的行可以带或不带行尾符。末尾没有空行时，残留记录照样交付。
    """
    parser = _SseParser()
    for raw in lines:
        if event := parser.feed(raw):
            yield event
    if event := parser.close():
        yield event


async def _parse_sse_async(lines: AsyncIterable[str]) -> AsyncIterator[SseEvent]:
    """`parse_sse` 的异步版：同一份状态机，只是逐行 await 取。"""
    parser = _SseParser()
    iterator = lines.__aiter__()
    try:
        async for raw in iterator:
            if event := parser.feed(raw):
                yield event
    finally:
        # `aiter_lines()` 也是异步生成器：不关它，httpx 的响应体不会被释放。
        close = getattr(iterator, "aclose", None)
        if close is not None:
            await close()
    if event := parser.close():
        yield event


async def post_sse(
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    client: httpx.AsyncClient | None = None,
    timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    max_retries: int = 2,
    backoff: float = 0.5,
    stall_timeout: float | None = None,
) -> AsyncIterator[SseEvent | TransportFailure]:
    """发一次流式 POST，逐条吐出 SSE 事件。

    契约：
    - 除 `asyncio.CancelledError` 外不抛异常。连不上、超时、4xx/5xx、流中断都在
      迭代器里变成 `TransportFailure`。
    - 只在「还没吐出任何事件」时重试。已经交付过内容再重试会造成重复。
    - 失败只出现在末尾，且之后迭代立刻结束。

    client 可注入，测试用 `httpx.MockTransport` 就能离线跑。注入的 client 由调用方
    负责关闭；本函数只在自建时关闭。
    """
    owned = client is None
    if client is None:
        client = httpx.AsyncClient(headers=DEFAULT_HEADERS)

    events = _post_with_retry(
        client,
        url,
        headers=headers,
        payload=payload,
        timeout=timeout,
        max_retries=max_retries,
        backoff=backoff,
        stall_timeout=_stall_timeout_from_env() if stall_timeout is None else stall_timeout,
    )
    try:
        async for item in events:
            yield item
    finally:
        # 调用方可能只取到 `[DONE]` 就结束迭代；不显式关掉它，httpx 的流会一直挂着。
        await events.aclose()
        if owned:
            await client.aclose()


async def _post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: httpx.Timeout,
    max_retries: int,
    backoff: float,
    stall_timeout: float,
) -> AsyncIterator[SseEvent | TransportFailure]:
    attempt = 0

    while True:
        delay: float | None = None

        try:
            async with client.stream(
                "POST", url, headers=headers, json=payload, timeout=timeout
            ) as response:
                if response.status_code >= 400:
                    message = f"HTTP {response.status_code}: {_brief(await response.aread())}"

                    if response.status_code in RETRYABLE_STATUS and attempt < max_retries:
                        delay = _retry_delay(response, attempt, backoff)
                    else:
                        yield TransportFailure(message, response.status_code)
                        return
                else:
                    parsed = _parse_sse_async(response.aiter_lines())
                    delivered = 0
                    try:
                        async for item in _guard_stall(parsed, stall_timeout):
                            delivered += 1
                            yield item
                    except _Stall as exc:
                        if delivered == 0 and attempt < max_retries:
                            # 什么都没交付，重发是安全的
                            delay = backoff * (2**attempt)
                        else:
                            yield TransportFailure(f"stream stalled: {exc}", response.status_code)
                            return
                    except httpx.HTTPError as exc:
                        # 已经交付过内容，不重试，避免重复
                        yield TransportFailure(
                            f"stream interrupted: {type(exc).__name__}: {exc}",
                            response.status_code,
                        )
                    else:
                        return
                    finally:
                        # 上游可能只取到 `[DONE]` 就结束；不关它，httpx 的响应体会一直挂着。
                        await parsed.aclose()

        except httpx.TimeoutException as exc:
            if attempt >= max_retries:
                yield TransportFailure(f"timeout: {type(exc).__name__}: {exc}")
                return
            delay = backoff * (2**attempt)

        except httpx.HTTPError as exc:
            if attempt >= max_retries:
                yield TransportFailure(f"connection failed: {type(exc).__name__}: {exc}")
                return
            delay = backoff * (2**attempt)

        attempt += 1
        if delay:
            await asyncio.sleep(delay)


def _retry_delay(response: httpx.Response, attempt: int, backoff: float) -> float:
    """优先听服务端的 Retry-After，否则指数退避。上限 30 秒。"""
    header = response.headers.get("retry-after", "").strip()
    if header.isdigit():
        return min(float(header), 30.0)
    return min(backoff * (2**attempt), 30.0)


def _brief(body: bytes) -> str:
    text = body.decode("utf-8", "replace").strip()
    if len(text) > _MAX_ERROR_BODY:
        text = text[:_MAX_ERROR_BODY] + "..."
    return text or "<empty body>"
