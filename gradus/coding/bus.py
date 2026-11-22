"""消息总线：点对点投递 + 内存历史。

不是队列，也不落盘：总线的价值在于"发给某个还活着的 agent"，接收者一旦关闭，
消息就没有意义了（它自己那条对话仍留在 `agents/*.jsonl` 里）。

投递复用接收者已有的通知通道（`Session.notify`），所以总线只负责两件事：
寻址（收件人存不存在）与留痕（最近的若干条，供排查）。
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

#: 主 agent 在总线上的地址；子 agent 用 `Subagent.name`（sub-1、sub-2…）。
LEAD = "lead"

HISTORY_LIMIT = 100

#: 消息类型：lead 收到时能区分的意图，而不是一堆无法分类的自由文本。
KINDS = ("text", "question", "result", "status")


class BusError(ValueError):
    """投递不合法，或收件人不存在。"""


@dataclass(frozen=True)
class Message:
    id: str
    sender: str
    recipient: str
    text: str
    created_at: float
    kind: str = "text"

    def render(self) -> str:
        """投递到接收者上下文里时的样子（与后台任务通知同一套标记风格）。"""
        attrs = f"from={self.sender}"
        if self.kind != "text":
            attrs += f" kind={self.kind}"
        return f"<message {attrs}>\n{self.text}\n</message>"


#: 把一条已渲染的消息交给收件人；返回 `False` 表示收件人不存在。
Deliver = Callable[[str, str], bool]


class Bus:
    def __init__(self, deliver: Deliver | None = None, *, limit: int = HISTORY_LIMIT) -> None:
        self._deliver = deliver
        self._history: deque[Message] = deque(maxlen=max(1, limit))

    def send(
        self, sender: str, recipient: str, text: str, *, kind: str = "text"
    ) -> Message:
        body = text.strip()
        who = recipient.strip()
        if kind not in KINDS:
            raise BusError(f"unknown kind: {kind} (expected one of {', '.join(KINDS)})")
        if not body:
            raise BusError("message cannot be empty")
        if not who:
            raise BusError("recipient is required")

        message = Message(
            id=f"msg_{uuid4().hex[:8]}",
            sender=sender,
            recipient=who,
            text=body,
            created_at=time.time(),
            kind=kind,
        )
        if self._deliver is None or not self._deliver(who, message.render()):
            raise BusError(f"unknown recipient: {who}")

        self._history.append(message)
        return message

    def history(self, limit: int = 20) -> tuple[Message, ...]:
        recent = list(self._history)
        return tuple(recent[-max(1, limit) :])

    def clear(self) -> None:
        self._history.clear()
