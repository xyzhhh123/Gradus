"""真机验证：上下文管理在真实模型、真实文件、真实沙箱下的行为。

和 pytest 的分工：`test_context.py` / `test_session.py` 用假客户端固定住纯逻辑，
这个脚本才回答「在真机上会不会这样」——真实 token 用量与缓存命中、真实摘要质量、
真实会话文件、真实工具输出落盘。它会发几次真实请求，**花钱**，所以不进测试集。

用法：

    uv run python scripts/verify_context_live.py            # 全跑
    uv run python scripts/verify_context_live.py --no-live  # 只跑离线那几项

窗口被压到 30,000 是为了让压缩在两三轮内发生；真实模型的窗口要大得多，
但机制与小窗口完全一致。
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gradus.agent.types import AgentDone, AgentError, TurnEnd  # noqa: E402
from gradus.ai import envfile  # noqa: E402
from gradus.coding import config as cfg  # noqa: E402
from gradus.coding import context as ctx  # noqa: E402
from gradus.coding import session as sess  # noqa: E402

WINDOW = 40_000
MAX_OUTPUT = 4_096
FILLER_CHARS = 10_000
MAX_TURNS = 6

_failures: list[str] = []
_skipped: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        _failures.append(name)
    return ok


def skip(name: str, why: str) -> None:
    print(f"[SKIP] {name} — {why}")
    _skipped.append(name)


def filler(tag: str, size: int) -> str:
    """一段可压缩的长文本：留一个可检索的标记，其余是重复的行。"""
    head = f"[{tag}] 这是第 {tag} 批日志，用来把上下文撑过触发线。\n"
    line = "行：构建成功，用时 1.2 秒，没有新的告警。\n"
    body = line * max(1, (size - len(head)) // len(line))
    return head + body


async def run_turn(one: sess.Session, text: str) -> tuple[object | None, object | None]:
    """跑一轮，返回（最后的用量，错误事件）。"""
    usage = None
    error = None
    async for event in one.prompt(text):
        if isinstance(event, (TurnEnd, AgentDone)):
            usage = event.usage
        elif isinstance(event, AgentError):
            error = event
    return usage, error


def describe(usage: object | None) -> str:
    if usage is None:
        return "无用量"
    return (
        f"in={usage.input_tokens} out={usage.output_tokens} "
        f"cache_read={usage.cache_read_tokens} cache_write={usage.cache_write_tokens}"
    )


def texts(messages) -> list[str]:
    """把消息压成可比较的字符串，避免跨进程对象不等。"""
    return [str(getattr(message, "content", message))[:200] for message in messages]


async def phase_live(settings: cfg.Settings, workspace: Path) -> None:
    print("\n== 阶段一：压缩 / 缓存 / 检索 / 重启（真实模型）==")
    one = await sess.Session.connect(
        settings, workspace=workspace, session_dir=workspace / "sessions"
    )
    path: Path | None = None
    runtime_view: list[str] = []
    runtime_history = 0
    snapshot: dict[str, object] = {}
    try:
        budget = ctx.ContextBudget.for_model(
            settings.spec.context_window, settings.spec.max_output_tokens
        )
        print(
            f"预算：window={budget.window} reserve={budget.reserve} "
            f"threshold={budget.threshold} keep={budget.keep_recent}"
        )

        # 压缩只在「有可折的轮次」时发生：`select_cut` 不会切当前这一轮，所以第一轮
        # 结束时不动作，从第二轮起才有东西可折。轮数与每轮大小都留了余量，
        # 循环到看见压缩为止（上限 MAX_TURNS）。
        usages: list[object | None] = []
        first_error = None
        for index in range(1, MAX_TURNS + 1):
            usage, error = await run_turn(
                one,
                f"只看第 {index} 批日志，然后只回一句「收到」。\n\n"
                + filler(f"T{index}", FILLER_CHARS),
            )
            usages.append(usage)
            if error is not None:
                first_error = error
                break
            print(f"  第 {index} 轮：{describe(usage)}")
            if one.compaction is not None:
                break

        if not check(
            "真实请求成功",
            first_error is None and usages and usages[0] is not None,
            describe(usages[0] if usages else None),
        ):
            print(f"  错误：{first_error}")
            return

        if len(usages) > 1 and usages[1] is not None:
            second = usages[1]
            cached = second.cache_read_tokens
            if cached > 0:
                check(
                    "前缀缓存命中（追加历史不破坏前缀）",
                    True,
                    f"cache_read={cached}，"
                    f"未缓存输入={second.input_tokens - cached - second.cache_write_tokens}",
                )
            else:
                skip("前缀缓存命中", "厂商未上报缓存字段或本次未命中")
            check(
                "输入在增长（不是每轮重发小历史）",
                second.input_tokens > usages[0].input_tokens,
                f"{usages[0].input_tokens} → {second.input_tokens}",
            )

        state = one.compaction
        if not check(
            "越过触发线后发生压缩",
            state is not None,
            f"跑了 {len(usages)} 轮",
        ):
            return
        view, history = one.messages, one.history
        check(
            "视图比记录短（压缩只换投影，不删历史）",
            len(view) < len(history),
            f"view={len(view)} history={len(history)}",
        )
        check(
            "视图首条是摘要标记",
            bool(view) and str(getattr(view[0], "content", "")).startswith(ctx.SUMMARY_MARKER),
        )
        summary = state.layers[-1].text if state.layers else ""
        sections = ["目标", "已完成", "当前状态", "决策", "错误", "待办", "文件"]
        hits = [name for name in sections if name in summary]
        check("摘要是结构化交接（八栏里命中多栏）", len(hits) >= 4, f"命中 {hits}")
        check(
            "摘要层数在上限内",
            len(state.layers) <= ctx.MAX_SUMMARY_LAYERS,
            f"{len(state.layers)} 层",
        )
        print(f"  摘要长度：{ctx.estimate_text_tokens(summary)} token")

        # 摘要请求复用的是「上一次真实请求」的前缀，理应命中缓存。形态一致不够，
        # 要服务端真的认——所以看那次摘要调用自己的 usage。
        summary_usage = one.last_summary_usage
        if summary_usage is None:
            skip("摘要命中前缀缓存", "摘要调用没回传 usage")
        else:
            print(f"  摘要调用：{describe(summary_usage)}")
            check(
                "摘要命中前缀缓存（重放的是上一发请求的前缀）",
                summary_usage.cache_read_tokens > 0,
                f"输入 {summary_usage.input_tokens}，"
                f"cache_read={summary_usage.cache_read_tokens}",
            )

        ids = one.message_ids
        folded = ids[0]
        result = await one.recall([folded], "", 8)
        check(
            "recall 能取回已压缩区段的原文",
            not result.is_error and "T1" in result.content,
            f"#{folded} · {len(result.content)} 字符",
        )
        visible = next((entry for entry in reversed(ids) if entry), None)
        if visible is None:
            skip("recall 拒绝仍在上下文里的条目", "没有条目 id")
        else:
            refused = await one.recall([visible], "", 8)
            check("recall 拒绝仍在上下文里的条目", refused.is_error, str(refused.content)[:60])

        path = one.session_path
        check("会话文件已落盘", path is not None and Path(path).exists(), str(path))
        runtime_view = texts(one.messages)
        runtime_history = len(one.history)
        assert one.compaction is not None
        snapshot = dict(
            layers=[layer.text for layer in one.compaction.layers],
            first_kept=one.compaction.first_kept,
        )
    finally:
        await one.aclose()

    if path is None:
        return
    restored = await sess.Session.connect(settings, resume=path)
    try:
        check(
            "重启后视图与运行时逐条一致",
            texts(restored.messages) == runtime_view,
            f"{len(restored.messages)} 条",
        )
        check(
            "重启后完整历史长度不变",
            len(restored.history) == runtime_history,
            f"{runtime_history} 条",
        )
        check(
            "重启后压缩状态一致",
            dict(
                layers=[layer.text for layer in restored.compaction.layers],
                first_kept=restored.compaction.first_kept,
            )
            == snapshot,
        )
    finally:
        await restored.aclose()


async def phase_tools(settings: cfg.Settings, workspace: Path) -> None:
    print("\n== 阶段二：工具输出落盘与回读（真实沙箱）==")
    target = workspace / "big.txt"
    target.write_text("X" * 60_000, encoding="utf-8")

    one = await sess.Session.connect(settings, workspace=workspace)
    try:
        shell = next((tool for tool in one.tools if tool.spec.name in {"bash", "powershell"}), None)
        if shell is None:
            skip("命令输出落盘", f"当前沙箱（{one.sandbox_backend}）没有命令工具")
            return
        print(f"  沙箱：{one.sandbox_backend}，工具：{shell.spec.name}")
        command = (
            "python -c \"print(open('big.txt').read())\""
            if shell.spec.name == "bash"
            else "Get-Content -Raw big.txt"
        )
        result = await shell.execute("c1", {"command": command})
        check(
            "超大输出换成指针",
            "已存到" in result.content and "read_file" in result.content,
            f"{len(result.content)} 字符进上下文 · {result.content[:120]}",
        )
        check("完整输出没有进上下文", len(result.content) < 5_000, str(len(result.content)))

        reader = next((tool for tool in one.tools if tool.spec.name == "read_file"), None)
        if reader is None:
            skip("按指针回读", "没有 read_file 工具")
            return
        pointer_match = re.search(r"已存到\s*(.+?)\s*（", result.content)
        if pointer_match is None:
            skip("按指针回读", "指针格式里找不到路径")
            return
        pointer = pointer_match.group(1)
        back = await reader.execute("c2", {"path": pointer, "limit": 100})
        check(
            "按指针能读回原文",
            not back.is_error and "X" * 10 in back.content,
            f"{pointer} · {len(back.content)} 字符",
        )
    finally:
        await one.aclose()


async def phase_config(workspace: Path, sandbox: str) -> None:
    print("\n== 阶段三：四个新配置项真的生效（真实沙箱）==")
    settings = cfg.load(
        overrides={
            "GRADUS_CONTEXT_WINDOW": str(WINDOW),
            "GRADUS_MAX_OUTPUT_TOKENS": str(MAX_OUTPUT),
            "GRADUS_SANDBOX": sandbox,
            "GRADUS_OUTPUT_CHARS": "4000",
            "GRADUS_SUMMARY_LAYERS": "1",
            "GRADUS_SUMMARY_TOKENS": "800",
            "GRADUS_RECALL_TOKENS": "1500",
        }
    )
    check(
        "配置项进入 Settings",
        (settings.summary_layers, settings.summary_tokens, settings.recall_tokens,
         settings.tool_output_chars)
        == (1, 800, 1_500, 4_000),
        f"{(settings.summary_layers, settings.summary_tokens, settings.recall_tokens, settings.tool_output_chars)}",
    )

    one = await sess.Session.connect(settings, workspace=workspace)
    try:
        shell = next((tool for tool in one.tools if tool.spec.name in {"bash", "powershell"}), None)
        if shell is None:
            skip("落盘阈值可配", "没有命令工具")
            return
        command = (
            "python -c \"print('y' * 8000)\""
            if shell.spec.name == "bash"
            # PowerShell 里字符串不能乘，用 [string]::new 造 8,000 个 y。
            else "[string]::new([char]'y', 8000)"
        )
        result = await shell.execute("c1", {"command": command})
        check(
            "落盘阈值 4,000 生效（8,000 字符已换成指针）",
            "已存到" in result.content,
            f"{len(result.content)} 字符进上下文 · {result.content[:120]}",
        )
    finally:
        await one.aclose()


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-live", action="store_true", help="跳过真实模型请求")
    parser.add_argument(
        "--sandbox",
        default="auto",
        help="执行后端（auto / restricted / appcontainer / docker / local）",
    )
    args = parser.parse_args()

    envfile.load_env_file(ROOT / ".env")
    # 不套 `with_active`：它会把 spec 换成候选模型自己的那份，把下面的窗口覆盖掉。
    # CLI 走的也是 `load()`，这里跟 CLI 保持一致。
    settings = cfg.load(
        overrides={
            "GRADUS_CONTEXT_WINDOW": str(WINDOW),
            "GRADUS_MAX_OUTPUT_TOKENS": str(MAX_OUTPUT),
            # 召回一条 26k 字符的原文约 7.4k token，窗口 10% 的默认预算不够。
            "GRADUS_RECALL_TOKENS": "9000",
            "GRADUS_SANDBOX": args.sandbox,
        }
    )
    print(
        f"连接：{settings.spec.provider} / {settings.spec.model} / {settings.spec.base_url}\n"
        f"密钥：{'已配置' if settings.api_key else '缺失'}　"
        f"窗口：{settings.spec.context_window}　输出上限：{settings.spec.max_output_tokens}"
    )
    if not settings.api_key:
        print("没有密钥，只能跑离线部分", file=sys.stderr)

    with tempfile.TemporaryDirectory(prefix="gradus-verify-") as tmp:
        workspace = Path(tmp)
        if not args.no_live and settings.api_key:
            await phase_live(settings, workspace)
        await phase_tools(settings, workspace)
        await phase_config(workspace, args.sandbox)

    print("\n== 汇总 ==")
    print(f"失败 {len(_failures)} 项：{_failures}" if _failures else "全部通过")
    if _skipped:
        print(f"跳过 {len(_skipped)} 项：{_skipped}")
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
