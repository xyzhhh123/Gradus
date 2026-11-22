"""汇总一轮评测的结果：通过率（Wilson 区间）、成本、失败分类。

    python bench/report.py --run-id swe-01
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.common import format_report, read_plan, read_results, summarize  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="汇总一轮评测")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--json", action="store_true", help="输出 JSON 而不是文本")
    args = parser.parse_args(argv)

    run_dir = Path(__file__).resolve().parent / "runs" / args.run_id
    if not run_dir.is_dir():
        print(f"没有这一轮：{run_dir}", file=sys.stderr)
        return 1

    plan = read_plan(run_dir / "plan.json")
    results = read_results(run_dir / "tasks.jsonl")
    total = len(plan.get("tasks") or []) or len(results)
    summary = summarize(results.values(), total)

    if args.json:
        payload = dict(summary)
        payload["plan"] = plan
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(format_report(summary, plan))
    return 0


if __name__ == "__main__":
    sys.exit(main())
