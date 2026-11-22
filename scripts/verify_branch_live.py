"""临时脚本：真机验证「切分支后缓存从分叉点重算」。

做法：跑两轮 → 记下第二轮的 usage → 切到第一条消息那条分支 → 再跑一轮。
预期：第三轮能成功，但 `cache_read` 明显小于上一轮的输入（前缀在分叉点之后变了）。
"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

from gradus.ai import envfile
from gradus.agent.types import AgentError, TurnEnd
from gradus.coding import config as cfg
from gradus.coding import session as sess


def filler(tag: str, size: int) -> str:
    line = "行：构建成功，用时 1.2 秒，没有新的告警。\n"
    head = f"[{tag}] 第 {tag} 批日志。\n"
    return head + line * max(1, (size - len(head)) // len(line))


async def turn(one, text):
    usage = None
    error = None
    async for event in one.prompt(text):
        if isinstance(event, TurnEnd):
            usage = event.usage
        elif isinstance(event, AgentError):
            error = event
    return usage, error


async def main() -> None:
    envfile.load_env_file(Path(".").resolve() / ".env")
    settings = cfg.load(
        overrides={
            "GRADUS_CONTEXT_WINDOW": "200000",
            "GRADUS_MAX_OUTPUT_TOKENS": "2048",
            "GRADUS_SANDBOX": "local",
        }
    )

    with tempfile.TemporaryDirectory(prefix="branch-live-") as tmp:
        one = await sess.Session.connect(
            settings, workspace=Path(tmp), session_dir=Path(tmp) / "sessions"
        )
        try:
            first, _ = await turn(one, "A 分支：只回一句「收到」。\n\n" + filler("A1", 2000))
            second, _ = await turn(one, "再回一句「收到」。\n\n" + filler("A2", 2000))
            print("第一轮:", getattr(first, "input_tokens", None), "cache_read", getattr(first, "cache_read_tokens", None))
            print("第二轮:", getattr(second, "input_tokens", None), "cache_read", getattr(second, "cache_read_tokens", None))
            print("消息数（切之前）:", len(one.messages))
            ids = one.message_ids
            print("条目 id:", ids[:4], "…")

            # 切到第一条消息所在的分支（相当于放弃后两轮）
            await one.switch_branch(ids[0])
            print("切分支后消息数:", len(one.messages))

            third, error = await turn(one, "B 分支：换一条路，只回一句「收到」。")
            print(
                "切换后一轮: in=", getattr(third, "input_tokens", None),
                "cache_read=", getattr(third, "cache_read_tokens", None),
                "err=", error,
            )
            print("消息数（切之后）:", len(one.messages))
            print("usage 合计:", one.usage_totals)

            before = getattr(second, "input_tokens", 0)
            after = getattr(third, "input_tokens", 0)
            hit = getattr(third, "cache_read_tokens", 0)
            print(
                "判定：切换后输入", after, "小于原第二轮", before,
                "→", after < before,
            )
            print("判定：缓存没有整段命中（cache_read < 上一轮输入）→", hit < before)
        finally:
            await one.aclose()

    shorter = after < before
    partial = hit < before
    print()
    print(f"[{'PASS' if shorter else 'FAIL'}] 切分支后历史变短（{after} < {before}）")
    print(f"[{'PASS' if partial else 'FAIL'}] 缓存只命中公共祖先（{hit} < {before}）")
    return 0 if (shorter and partial) else 1


sys.exit(asyncio.run(main()))
