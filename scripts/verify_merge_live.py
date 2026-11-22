"""临时脚本：真机验证「分层摘要的合并」（第 4 次压缩触发）。

窗口调小、每轮塞一点填充，让压缩频繁发生，直到出现 `merge` 记录。
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

from gradus.ai import envfile
from gradus.agent.types import AgentDone, AgentError, TurnEnd
from gradus.coding import config as cfg
from gradus.coding import context as ctx
from gradus.coding import session as sess

WINDOW = 24_000
MAX_OUTPUT = 2_048
FILLER_CHARS = 2_400
MAX_TURNS = 14


def filler(tag: str, size: int) -> str:
    head = f"[{tag}] 这是第 {tag} 批日志，用来把上下文推过触发线。\n"
    line = "行：构建成功，用时 1.2 秒，没有新的告警。\n"
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
            "GRADUS_CONTEXT_WINDOW": str(WINDOW),
            "GRADUS_MAX_OUTPUT_TOKENS": str(MAX_OUTPUT),
            "GRADUS_SANDBOX": "local",
        }
    )
    print(f"窗口 {WINDOW}，输出上限 {MAX_OUTPUT}，填充 {FILLER_CHARS} 字符/轮\n")

    # 判定要用的量先占位：中途抛异常时也要给出 FAIL，而不是 NameError。
    state = None
    total = 0
    merges: list[dict] = []

    with tempfile.TemporaryDirectory(prefix="merge-live-") as tmp:
        one = await sess.Session.connect(
            settings, workspace=Path(tmp), session_dir=Path(tmp) / "sessions"
        )
        try:
            for index in range(1, MAX_TURNS + 1):
                usage, error = await turn(
                    one, f"只回一句「收到」。\n\n{filler(f'T{index}', FILLER_CHARS)}"
                )
                state = one.compaction
                layers = len(state.layers) if state else 0
                print(
                    f"turn{index}: in={getattr(usage, 'input_tokens', None)} "
                    f"out={getattr(usage, 'output_tokens', None)} "
                    f"cache_read={getattr(usage, 'cache_read_tokens', None)} "
                    f"layers={layers} version={getattr(state, 'version', None)} err={error}"
                )
                if layers >= ctx.MAX_SUMMARY_LAYERS:
                    break

            state = one.compaction
            print(f"\n最终层数：{len(state.layers) if state else 0}")
            total = sum(ctx.estimate_text_tokens(layer.text) for layer in (state.layers if state else ()))
            print(f"摘要合计约 {total} token（上限 {ctx.MAX_SUMMARY_TOKENS}）")
            merges = []
            if one.session_path is not None:
                for line in Path(one.session_path).read_text("utf-8").splitlines():
                    if '"merge"' in line:
                        merges.append(json.loads(line))
                print(f"merge 记录：{len(merges)} 条")
                for item in merges:
                    print(
                        f"  {item['id']} supersedes={item.get('supersedes')} "
                        f"text={item['text'][:40]!r}"
                    )
            print("usage 合计：", one.usage_totals)
        finally:
            await one.aclose()

    # 成败判定：至少发生过一次合并，且层数没超上限、摘要总量有界。
    merged = bool(merges)
    bounded = len(state.layers) <= ctx.MAX_SUMMARY_LAYERS if state else False
    within = total <= ctx.MAX_SUMMARY_TOKENS
    print()
    print(f"[{'PASS' if merged else 'FAIL'}] 发生过合并（merge 记录 >= 1）")
    print(f"[{'PASS' if bounded else 'FAIL'}] 层数不超上限（{len(state.layers) if state else 0}/"
          f"{ctx.MAX_SUMMARY_LAYERS}）")
    print(f"[{'PASS' if within else 'FAIL'}] 摘要总量有界（{total}/{ctx.MAX_SUMMARY_TOKENS}）")
    return 0 if (merged and bounded and within) else 1


sys.exit(asyncio.run(main()))
