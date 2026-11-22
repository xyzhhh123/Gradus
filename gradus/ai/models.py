"""模型能力：窗口与输出上限从哪来。

这里回答「这个模型最多能装多少」，**不是**「我们打算按多大做上下文管理」。
后者是预算口径（`config.context_window`），可以比能力小——压低是合理的成本控制；
但比能力大只会换来一次 400，所以 `config` 会把它收敛到能力以内。

来源三层，显式优先：

1. 配置里显式写的窗口 / 输出上限（用户的选择，只能往下压）；
2. 端点元数据（`GET {base}/models` 的 `limit.context` / `limit.output`），
   需要 `probe_limits` 打开；超时 2s、失败静默，拿不到就退回下一层；
3. 下面这张静态表，**只填实测过的**——填错比不填更糟：预算按错的数算，
   压缩可能在窗口外触发。

查不到就交给 `coding.config` 的默认值，行为与以前一致。本模块不依赖 `coding`。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import httpx

#: 模型 id -> (窗口, 输出上限)。只收录验证过的；新增前先跑一次端点确认。
VERIFIED_LIMITS: dict[str, tuple[int, int]] = {
    # OpenCode Go 的 /models（2026-11 实测：limit.context / limit.output）
    "deepseek-v4.1-flash": (1_000_000, 384_000),
}

#: 元数据里可能出现的字段名，按顺序取第一个能用的。
_CONTEXT_KEYS = ("context", "context_window", "context_length", "max_context_length")
_OUTPUT_KEYS = ("output", "max_output_tokens", "max_output_length")


@dataclass(frozen=True)
class Limits:
    """一个模型的能力上限；可能是部分信息（只报了一项）。"""

    window: int | None = None
    output: int | None = None


def normalize(model: str) -> str:
    """把 `deepseek/deepseek-v4.1-flash-1017` 这类带命名空间的 id 收成裸名。"""
    name = model.strip().lower()
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    return name


def limits_for(model: str) -> Limits | None:
    """按 model id 查静态表；查不到返回 `None`。"""
    name = normalize(model)
    if name in VERIFIED_LIMITS:
        window, output = VERIFIED_LIMITS[name]
        return Limits(window, output)
    # 允许 `deepseek-v4.1-flash-1017` 命中 `deepseek-v4.1-flash`
    for known, (window, output) in VERIFIED_LIMITS.items():
        if name.startswith(known):
            return Limits(window, output)
    return None


def _first_int(payload: Mapping[str, object], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value > 0:
            return value
    return None


def parse_limits(entry: Mapping[str, object]) -> Limits | None:
    """从一条 `/models` 记录里取上限；一项都没有时返回 `None`。"""
    limit = entry.get("limit")
    sources: list[Mapping[str, object]] = [entry]
    if isinstance(limit, Mapping):
        sources.insert(0, limit)

    window = output = None
    for source in sources:
        window = window if window is not None else _first_int(source, _CONTEXT_KEYS)
        output = output if output is not None else _first_int(source, _OUTPUT_KEYS)
    if window is None and output is None:
        return None
    return Limits(window, output)


async def fetch_limits(
    base_url: str,
    model: str,
    *,
    api_key: str = "",
    client: httpx.AsyncClient | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 2.0,
) -> Limits | None:
    """向 `{base_url}/models` 要这个模型的上限；任何一种失败都返回 `None`。

    刻意不抛异常、不重试：这是启动路径上的加固信息，拿不到就退回静态表与默认值。
    """
    url = f"{base_url.rstrip('/')}/models"
    request_headers = dict(headers or {})
    if api_key:
        request_headers.setdefault("Authorization", f"Bearer {api_key}")

    owned = client is None
    if client is None:
        client = httpx.AsyncClient()
    try:
        response = await client.get(
            url, headers=request_headers, timeout=timeout, follow_redirects=True
        )
        if response.status_code != 200:
            return None
        payload = response.json()
    except Exception:  # noqa: BLE001 - 探测失败不该影响启动
        return None
    finally:
        if owned:
            await client.aclose()

    items = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(items, list):
        return None

    wanted = normalize(model)
    for item in items:
        if not isinstance(item, Mapping):
            continue
        identifier = item.get("id")
        if not isinstance(identifier, str):
            continue
        candidate = normalize(identifier)
        if candidate != wanted and not candidate.startswith(wanted):
            continue
        return parse_limits(item)
    return None
