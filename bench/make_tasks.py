"""从 SWE-bench Verified 生成题单（默认 5 题，跨仓库各取一题）。

跑在装了 `datasets` 的环境里（本项目是 `H:\\bench\\swenv`），产出：
- `bench/tasks.jsonl`：题目、仓库、base_commit、官方镜像名
- `H:\\bench\\dataset\\<id>.json`：原始元数据（判定时要用 test_patch / FAIL_TO_PASS）

刻意避开超大仓库（django 单题动辄十几分钟），优先小仓库各取一题。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from datasets import load_dataset

#: 小仓库优先；每个仓库只取一题，跨仓库才有区分度。
PREFERRED_REPOS = [
    "pallets/flask",
    "psf/requests",
    "mwaskom/seaborn",
    "pylint-dev/pylint",
    "pytest-dev/pytest",
    "pydata/xarray",
    "astropy/astropy",
    "sphinx-doc/sphinx",
]

#: 已经验证过的题不再进第一批（它只用于打通链路）。
SKIP = {"psf__requests-1142"}


def main(
    count: int = 5,
    repos: list[str] | None = None,
    exclude: set[str] | None = None,
    out: str = "bench/tasks.jsonl",
    per_repo: int = 1,
) -> int:
    dataset = load_dataset("SWE-bench/SWE-bench_Verified", split="test")
    rows = {row["instance_id"]: row for row in dataset}
    skip = set(SKIP) | (exclude or set())

    picked: list[dict] = []
    for repo in (repos or PREFERRED_REPOS):
        taken = 0
        for instance_id, row in rows.items():
            if row["repo"] != repo or instance_id in skip:
                continue
            if str(row.get("difficulty", "")).startswith(("1-4 hours", ">4 hours")):
                continue  # 先跑轻的
            picked.append(row)
            taken += 1
            if len(picked) >= count or taken >= per_repo:
                break
        if len(picked) >= count:
            break

    if len(picked) < count:
        # 兜底：按仓库多样性补齐
        seen = {row["repo"] for row in picked}
        for instance_id, row in rows.items():
            if instance_id in skip or row["repo"] in seen:
                continue
            picked.append(row)
            seen.add(row["repo"])
            if len(picked) >= count:
                break

    root = Path(__file__).resolve().parents[1]
    dataset_dir = Path(r"H:\bench\dataset")
    dataset_dir.mkdir(parents=True, exist_ok=True)

    lines = []
    for row in picked:
        instance_id = row["instance_id"]
        (dataset_dir / f"{instance_id}.json").write_text(
            json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        task = {
            "id": instance_id,
            "instruction": row["problem_statement"].strip()
            + "\n\n请在当前仓库里修复这个问题。不要提交 commit，工作区里的改动就是答案。",
            "workspace": f"H:/bench/work/{instance_id}",
            "repo": row["repo"],
            "base_commit": row["base_commit"],
            "image": row.get("image", ""),
            "test_cmd": "",
        }
        lines.append(json.dumps(task, ensure_ascii=False))

    target = root / out
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"写出 {len(lines)} 题 → {target}")
    for row in picked:
        print(f"  {row['instance_id']:32} {row['repo']:24} {row.get('difficulty', '?'):10} {row.get('image', '')}")
    return 0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="从 SWE-bench Verified 生成题单")
    parser.add_argument("count", nargs="?", type=int, default=5)
    parser.add_argument("--repos", default="", help="逗号分隔的仓库白名单")
    parser.add_argument("--exclude", default="", help="逗号分隔的实例 id（跳过）")
    parser.add_argument("--out", default="bench/tasks.jsonl")
    parser.add_argument("--per-repo", type=int, default=1)
    args = parser.parse_args()
    sys.exit(
        main(
            args.count,
            repos=[item for item in args.repos.split(",") if item] or None,
            exclude={item for item in args.exclude.split(",") if item},
            out=args.out,
            per_repo=args.per_repo,
        )
    )
