"""看一道题的轨迹：容器里到底发生了什么。

    python bench/inspect.py --run-id slice-01 --task pallets__flask-5014
    python bench/inspect.py --run-id slice-01 --task ... --full    # 打印全部工具输出

轨迹文件（`runs/<id>/container/<task>/<task>.trajectory.jsonl`）是 `container_task.py`
在容器内逐事件写的：turn_start / tool_call / tool_result / turn_end / error / end。
会话原文仍在 `runs/<id>/container/<task>/sessions/*.jsonl`。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(errors="replace")


def load(path: Path) -> list[dict]:
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="看一题的轨迹")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--full", action="store_true", help="打印完整工具输出（默认截断）")
    parser.add_argument("--tail", type=int, default=12, help="末尾打印多少个工具调用")
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[1] / "bench" / "runs" / args.run_id
    trace_path = root / "container" / args.task / f"{args.task}.trajectory.jsonl"
    if not trace_path.exists():
        print(f"没有轨迹文件：{trace_path}", file=sys.stderr)
        return 1

    events = load(trace_path)
    calls = [event for event in events if event["type"] == "tool_call"]
    results = [event for event in events if event["type"] == "tool_result"]
    turns = [event for event in events if event["type"] == "turn_start"]
    end = next((event for event in events if event["type"] == "end"), {})

    print(f"=== {args.task} ===")
    print(f"轮数 {len(turns)}  工具 {len(calls)}  错误 {sum(1 for r in results if r.get('is_error'))}")
    if end:
        print(
            f"状态 {end.get('status')}  输入 {end.get('input'):,}  "
            f"缓存 {end.get('cache_read'):,}  自检 {end.get('self_check') or '-'} "
            f"checks={end.get('checks')}"
        )
    print("工具分布:", dict(Counter(call.get("name") for call in calls)))

    errors = [result for result in results if result.get("is_error")]
    if errors:
        print("\n失败的调用（前 5 个）:")
        for result in errors[:5]:
            print(f"  - {result.get('name')}: {str(result.get('content'))[:160]}")

    print(f"\n最近 {args.tail} 次调用:")
    limit = len(calls) if args.full else args.tail
    for call in calls[-limit:]:
        args_text = str(call.get("args"))[:200].replace("\n", " ")
        print(f"  · {call.get('name')}: {args_text}")
    if not args.full:
        print(f"\n（要完整输出用 --full，或直接看 {trace_path.name} 与 sessions/）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
