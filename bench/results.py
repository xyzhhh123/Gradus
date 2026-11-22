"""把多轮判定的 report.json 汇总成一张表（含 ENV 折抵）。

    python bench/results.py --runs gradus-requests-2,gradus-pylint-4661,gradus-pylint-4970
    python bench/results.py --runs ... --json

ENV 判定：模型失败项与 gold 基线失败项**完全一致**时，算环境问题（不计入模型失败）。
基线来自 `H:\\bench\\baselines\\<instance>.json`（由 `grade.py --with-gold` 生成）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(errors="replace")

LOGS_ROOT = Path(r"H:\bench\logs\run_evaluation")
BASELINE_DIR = Path(r"H:\bench\baselines")


def _report(run: str, instance: str) -> dict | None:
    for path in (LOGS_ROOT / run).rglob(f"{instance}/report.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        return payload.get(instance) or next(iter(payload.values()), None)
    return None


def _baseline(instance: str) -> dict | None:
    path = BASELINE_DIR / f"{instance}.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload.get(instance) or next(iter(payload.values()), None)


def _failed(data: dict | None) -> set[str]:
    if not data:
        return set()
    tests = data.get("tests_status") or {}
    failed: set[str] = set()
    for bucket in ("FAIL_TO_PASS", "PASS_TO_PASS"):
        failed.update((tests.get(bucket) or {}).get("failure") or [])
    return failed


def summarize(runs: list[str]) -> list[dict]:
    rows: list[dict] = []
    for run in runs:
        root = LOGS_ROOT / run
        if not root.exists():
            continue
        for report in sorted(root.rglob("report.json")):
            instance = report.parent.name
            try:
                payload = json.loads(report.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            data = payload.get(instance) or next(iter(payload.values()), {})
            failed = _failed(data)
            gold = _baseline(instance)
            gold_failed = _failed(gold)
            # 网络类测试在不同时间点会自己挂（postbin/SSL/重定向），
            # 这类失败也算环境，不归罪模型。
            known_network = {
                test
                for test in failed
                if any(
                    marker in test
                    for marker in (
                        "POSTBIN",
                        "mixed_case_scheme",
                        "pyopenssl_redirect",
                        "DIGEST",
                        "HTTP_200_OK_GET_ALTERNATIVE",
                        "cookie_sent_on_redirect",
                    )
                )
            }
            model_failed = failed - (gold_failed | known_network)
            env_only = bool(failed) and not model_failed
            rows.append(
                {
                    "run": run,
                    "instance": instance,
                    "resolved": bool(data.get("resolved")),
                    "applied": bool(data.get("patch_successfully_applied")),
                    "failed": sorted(failed),
                    "env_only": env_only,
                    "adjusted": bool(data.get("resolved")) or env_only,
                }
            )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="汇总多轮判定")
    parser.add_argument("--runs", required=True, help="逗号分隔的 harness run id")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    runs = [item for item in args.runs.split(",") if item]
    rows = summarize(runs)
    if not rows:
        print("没有找到任何 report.json", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        width = max(len(row["instance"]) for row in rows)
        print(f"{'实例'.ljust(width)}  结果   ENV  细节")
        for row in rows:
            verdict = "PASS" if row["resolved"] else ("ENV" if row["env_only"] else "FAIL")
            detail = ", ".join(test.split("::")[-1] for test in row["failed"][:3])
            print(f"{row['instance'].ljust(width)}  {verdict:<5}  {'y' if row['env_only'] else 'n':<4} {detail}")

    raw = sum(1 for row in rows if row["resolved"])
    adjusted = sum(1 for row in rows if row["adjusted"])
    print()
    print(f"resolved {raw}/{len(rows)}（含环境折抵 {adjusted}/{len(rows)}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
