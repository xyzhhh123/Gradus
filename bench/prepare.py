"""准备题目工作区：按 base_commit 拉一份**LF 行尾**的仓库副本。

为什么要显式 `core.autocrlf=false`：Windows 上默认的 autocrlf=true 会把工作区转成
CRLF，而 Linux 容器里的 git 认为 CRLF 就是改动——`git status`/`git diff` 一片红，
模型会被自己「没改过的文件全变了」搞乱（实测 60 轮 1M token 还没收尾）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def prepare(instance_id: str, repo: str, base_commit: str, root: Path) -> Path:
    """克隆并 checkout；已存在就重置到 base_commit。"""
    target = root / instance_id
    if target.exists():
        _git("fetch", "--quiet", "origin", cwd=target)
        # 老仓库可能是在 autocrlf=true 时克隆的：工作区 CRLF、索引 LF，
        # Linux 容器里会看到一堆「假改动」。先把仓库级配置钉死，再从索引重写工作区。
        _git("config", "core.autocrlf", "false", cwd=target)
        _git("config", "core.eol", "lf", cwd=target)
        _git("checkout", "--quiet", "-f", base_commit, cwd=target)
        _git("reset", "--quiet", "--hard", base_commit, cwd=target)
        _git("checkout", "--quiet", "--", ".", cwd=target)
        _git("clean", "--quiet", "-fdx", cwd=target)
        return target

    done = _git(
        "-c",
        "core.autocrlf=false",
        "clone",
        "--quiet",
        f"https://github.com/{repo}",
        str(target),
    )
    if done.returncode != 0:
        raise RuntimeError(f"clone 失败: {done.stderr[-500:]}")
    # 让这份仓库永久保持 LF：容器里的 git 才不会把整棵树看成改动
    _git("config", "core.autocrlf", "false", cwd=target)
    _git("config", "core.eol", "lf", cwd=target)
    done = _git("checkout", "--quiet", "-f", base_commit, cwd=target)
    if done.returncode != 0:
        raise RuntimeError(f"checkout {base_commit} 失败: {done.stderr[-500:]}")
    # clone 的初始 checkout 可能已经用全局 autocrlf 把文件写成 CRLF；
    # 设好仓库级配置后再按索引重写工作区，否则容器里会看到几十个「假改动」。
    _git("reset", "--quiet", "--hard", base_commit, cwd=target)
    return target


def normalize_lf(root: Path) -> int:
    """把工作区里的 CRLF 改回 LF（只处理文本文件）。

    为什么要这么暴力：某些老仓库是在 autocrlf=true 时克隆的，工作区 CRLF、索引 LF；
    即使仓库配置改成 false，`git status` 也可能因为缓存/属性判断而不报错，但 Linux 容器里
    会看到整棵树都是改动。逐文件把 CRLF 归一，索引与工作区就真正一致了。
    """
    changed = 0
    for path in root.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\r\n" not in data:
            continue
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            continue  # 二进制文件不动
        path.write_bytes(data.replace(b"\r\n", b"\n"))
        changed += 1
    return changed


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="准备题目工作区（LF 行尾）")
    parser.add_argument("--instance", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--root", default=r"H:\bench\work")
    args = parser.parse_args(argv)

    target = prepare(args.instance, args.repo, args.commit, Path(args.root))
    print(f"{target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
