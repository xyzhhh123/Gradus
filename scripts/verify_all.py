"""真机验证入口：一条命令跑完三个脚本，汇总成败。

三个脚本各自独立、也能单跑；这里只按顺序执行并汇总：

    python scripts/verify_all.py                # 全部跑
    python scripts/verify_all.py --only branch  # 只跑一个
    python scripts/verify_all.py --list         # 只列名字

每个脚本自己打印 `[PASS]/[FAIL]` 行，并用退出码表示成败（0 通过 / 1 失败）。
需要网络与真实模型：会真的发请求、消耗 token。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

# 控制台可能是 GBK：子进程强制 UTF-8 输出，本进程打印时把不可编码的字符替换掉——
# 否则一条乱码字符就能把整个汇总脚本打挂。
sys.stdout.reconfigure(errors="replace")

SCRIPTS = {
    "context": "verify_context_live.py",
    "merge": "verify_merge_live.py",
    "branch": "verify_branch_live.py",
}


def run(name: str, path: Path) -> tuple[int, float, list[str]]:
    """跑一个脚本，返回 (退出码, 用时秒, 结果行)。"""
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    elapsed = time.monotonic() - started

    lines = [line for line in result.stdout.splitlines() if line.startswith("[PASS]") or line.startswith("[FAIL]")]
    if result.returncode != 0 and not any(line.startswith("[FAIL]") for line in lines):
        tail = (result.stderr or result.stdout).strip().splitlines()[-3:]
        lines.extend(f"[FAIL] 进程退出码 {result.returncode}: {line}" for line in tail)
    return result.returncode, elapsed, lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="跑 Gradus 的真机验证脚本")
    parser.add_argument(
        "--only",
        choices=sorted(SCRIPTS),
        help="只跑其中一个",
    )
    parser.add_argument("--list", action="store_true", help="只列出脚本名")
    args = parser.parse_args(argv)

    if args.list:
        for name, filename in SCRIPTS.items():
            print(f"{name}: scripts/{filename}")
        return 0

    here = Path(__file__).resolve().parent
    wanted = [args.only] if args.only else list(SCRIPTS)
    failures: list[str] = []

    for name in wanted:
        path = here / SCRIPTS[name]
        print(f"=== {name} ({SCRIPTS[name]}) ===", flush=True)
        code, elapsed, lines = run(name, path)
        for line in lines:
            print("  " + line)
        print(f"  → 退出码 {code}，用时 {elapsed:.1f}s", flush=True)
        if code != 0:
            failures.append(name)

    print()
    if failures:
        print(f"结果：{len(failures)}/{len(wanted)} 失败 → {failures}")
        return 1
    print(f"结果：{len(wanted)}/{len(wanted)} 通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
