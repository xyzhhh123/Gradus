"""运行中待注入消息的队列。

`PendingMessageQueue` 只负责暂存与排空，不关心消息来自 steer 还是 follow-up；
具体注入时机由 `run_loop` 的 drain 点决定。
"""

from __future__ import annotations

from typing import Literal

from ..ai.types import Message

QueueMode = Literal["all", "one-at-a-time"]


class PendingMessageQueue:
    """steering / follow-up 共用的消息队列。

    `all`：一次 drain 取走全部；
    `one-at-a-time`：一次 drain 只取最早一条，其余留到下一个 drain 点。
    """

    def __init__(self, mode: QueueMode = "all") -> None:
        if mode not in ("all", "one-at-a-time"):
            raise ValueError(f"unknown queue mode: {mode}")
        self._mode = mode
        self._messages: list[Message] = []

    @property
    def mode(self) -> QueueMode:
        return self._mode

    def push(self, message: Message) -> None:
        self._messages.append(message)

    def drain(self) -> list[Message]:
        if self._mode == "one-at-a-time":
            if not self._messages:
                return []
            return [self._messages.pop(0)]

        messages = self._messages
        self._messages = []
        return messages

    def clear(self) -> None:
        self._messages.clear()

    def __len__(self) -> int:
        return len(self._messages)
