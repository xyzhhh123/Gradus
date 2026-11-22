"""清理评测残留：harness 容器与实例镜像。

官方 harness 自己的清理步骤要调 `docker` CLI，而它的运行镜像里没有 bin——
所以每次判定后都会留下 `sweb.eval.*` 容器（只是 `tail -f /dev/null`，但占内存）。
实例镜像每个 1–5 GB，跑完可以按需删掉，下次判定再重建。

    python bench/cleanup.py --containers            # 只清容器
    python bench/cleanup.py --tasks bench/tasks.jsonl --images   # 按题单删镜像
    python bench/cleanup.py --all-images            # 删所有评测相关镜像
    python bench/cleanup.py --dry-run               # 只打印不删
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def containers() -> list[str]:
    listed = _run("ps", "-a", "--format", "{{.Names}}")
    return [name for name in listed.stdout.split() if name.startswith("sweb.eval.")]


def images(prefixes: tuple[str, ...]) -> list[str]:
    listed = _run("images", "--format", "{{.Repository}}:{{.Tag}}")
    return [
        tag
        for tag in listed.stdout.split()
        if any(tag.startswith(prefix) for prefix in prefixes)
    ]


def instance_tags(tasks_file: Path) -> list[str]:
    """按题单收集实例镜像名。"""
    tags = []
    for line in tasks_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        task = json.loads(line)
        if task.get("image"):
            tags.append(task["image"] if ":" in task["image"] else task["image"] + ":latest")
    return tags


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清理评测残留")
    parser.add_argument("--containers", action="store_true", help="清 sweb.eval.* 容器")
    parser.add_argument("--tasks", help="题单 JSONL：删这些题的实例镜像")
    parser.add_argument("--all-images", action="store_true", help="删所有 swebench/sweb.eval 与 gradus-agent 镜像")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    removed = 0
    if args.containers or not (args.tasks or args.all_images):
        names = containers()
        for name in names:
            print(f"[容器] {name}")
            if not args.dry_run:
                _run("rm", "-f", name)
            removed += 1

    targets: list[str] = []
    if args.tasks:
        targets += instance_tags(Path(args.tasks))
    if args.all_images:
        targets += images(("swebench/sweb.eval", "gradus-agent:"))

    for tag in dict.fromkeys(targets):
        print(f"[镜像] {tag}")
        if not args.dry_run:
            _run("rmi", "-f", tag)
        removed += 1

    verb = "将清理" if args.dry_run else "已清理"
    print(f"{verb} {removed} 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
