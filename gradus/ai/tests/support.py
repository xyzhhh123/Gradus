"""测试共用的小工具：边收边断言的事件流、伪造网络层。

`Provider.stream()` 与 `post_sse()` 都是异步生成器，用例本身也是异步的
（`IsolatedAsyncioTestCase`）。`StreamTestCase.consume()` 逐条消费，每收到一个事件
就当场断言，不把整条流先收成列表——事件的数量与先后顺序因此也在断言范围内。

`fake_sse()` 只负责顶掉网络：把事先拼好的记录列表包成异步迭代器。
"""

from __future__ import annotations

import unittest
from collections.abc import AsyncIterator, Iterable, Sequence
from typing import Any


class StreamTestCase(unittest.IsolatedAsyncioTestCase):
    """消费事件流的基类：边收边断言。"""

    async def consume(
        self, stream: AsyncIterator[Any], expected: Sequence[str] | None = None
    ) -> Any:
        """逐条消费 `stream`，返回最后一个事件。

        给出 `expected` 时，每收到一个事件就比对类型名；循环结束后核对总数，
        少一个、多一个都会失败。
        """
        index = 0
        last: Any = None

        async for event in stream:
            if expected is not None:
                self.assertLess(
                    index, len(expected), f"多出第 {index} 个事件：{type(event).__name__}"
                )
                self.assertEqual(type(event).__name__, expected[index], f"第 {index} 个事件")
            index += 1
            last = event

        if expected is not None:
            self.assertEqual(index, len(expected), "事件数量少于预期")
        return last


async def fake_sse(events: Iterable[Any]) -> AsyncIterator[Any]:
    """把现成的记录列表包成异步迭代器，用来顶替 `post_sse`。"""
    for event in events:
        yield event
