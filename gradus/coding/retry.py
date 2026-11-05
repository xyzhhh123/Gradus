"""运行级重试：同模型重试，不自动换模型。

`ai` 的传输重试只覆盖「尚未吐出任何事件」的失败（408 / 409 / 425 / 429 / 5xx 与连接失败）。
一次运行仍可能因为中途断流、空流、厂商违约而终止：agent 会把它们统一编码成
`AgentError(kind="provider")`，并保证失败的半轮不写入历史。本模块只决定「要不要把整次
运行重跑一遍」，重跑本身由 `session.py` 用 `agent.continue_()` 完成。

不重试：`aborted` / `max_turns` / `config`，以及明确的不可重试 HTTP 状态（400 / 401 /
403 / 404 / 413 / 422 等）。换模型不在本模块：启动时由 `config` 选定，运行中换用
`Agent.connect(messages=...)` 重建。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..agent.types import AgentError

#: `attempts` 是额外重试次数，不是总次数。
DEFAULT_ATTEMPTS = 2

#: 首次退避秒数，之后翻倍。
DEFAULT_BACKOFF = 0.5

#: 单次退避上限，与 ai 层传输重试保持一致。
MAX_DELAY = 30.0

#: 与 `ai.transport.RETRYABLE_STATUS` 同口径；这里用于二次判断，避免重试鉴权失败。
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

_STATUS = re.compile(r"\bHTTP (\d{3})\b")


@dataclass(frozen=True)
class RetryPolicy:
    """重试次数与退避。"""

    attempts: int = DEFAULT_ATTEMPTS
    backoff: float = DEFAULT_BACKOFF

    def delay(self, attempt: int) -> float:
        """第 `attempt` 次重试（从 1 开始）前等待的秒数。"""
        return min(self.backoff * (2 ** (attempt - 1)), MAX_DELAY)


def is_retryable(error: AgentError) -> bool:
    """只有运行级 provider 失败才重试；能解析出 HTTP 状态时按可重试集合判断。"""
    if error.kind != "provider":
        return False

    match = _STATUS.search(error.message)
    if match is None:
        return True
    return int(match.group(1)) in RETRYABLE_STATUS
