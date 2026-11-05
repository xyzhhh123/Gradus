"""沙箱自有 workspace：给沙箱后端一份项目副本，会话结束时合回宿主。

负责：

- 为宿主 workspace 准备副本：git 仓库优先 `git worktree`（快、省空间），
  其余整树复制；worktree 里再把宿主上未跟踪（含被 `.gitignore` 忽略）的
  文件带进来，否则 `.env`、本地脚本等对模型不可见；
- 结束时把副本里的改动合回宿主：worktree 模式用 `git apply` 打补丁，
  未跟踪文件直接复制，删除落到宿主；复制模式按文件比对后写回并删除；
- 清理副本；强杀留下的副本在下次启动时按 owner pid 清理。

不负责：进程与容器（`sandbox.py`）、工具绑定（`session.py`）、权限判断。

约束：

- 只给沙箱后端用；`local` 直接在宿主上操作，不复制；
- 副本内的写入不影响宿主，直到 `sync()` 被调用；
- 合并失败不抛异常：保留副本并在结果里给出原因与路径；
- 复制模式只删「建副本时就有、之后在副本里消失」的文件，会话期间宿主上
  新建的文件不会被误删；
- 副本放在 `%TEMP%\\gradus-ws-<pid>-<id>`（直接放在临时目录下，不嵌套子目录：
  AppContainer 进程在更深的目录里设不了工作目录），旁边一个 `.owner` 记录
  owner pid、用户名与宿主路径，用于强杀后的清理；清理只碰当前用户的副本。
"""

from __future__ import annotations

import asyncio
import filecmp
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .sandbox import _pid_alive, current_user

#: 复制模式跳过的目录：版本库元数据，或随时可重建的产物。
COPY_SKIP = frozenset(
    {
        ".git",
        ".gradus",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        "target",
    }
)

MARKER_SUFFIX = ".owner"

#: 副本目录前缀；清理时按它 + `.owner` 找残留。
DIR_PREFIX = "gradus-ws-"


@dataclass(frozen=True)
class WorkspaceSync:
    """一次合并的结果。"""

    applied: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    error: str | None = None
    kept: Path | None = None

    @property
    def changed(self) -> bool:
        return bool(self.applied or self.deleted)


class WorkspaceCopy:
    """沙箱私有的 workspace 副本。"""

    def __init__(
        self,
        host: Path,
        path: Path,
        *,
        worktree: bool,
        extra: tuple[str, ...] = (),
        baseline: tuple[str, ...] = (),
    ) -> None:
        self._host = host
        self._path = path
        self._worktree = worktree
        #: worktree 模式额外带进来的未跟踪文件（相对路径）。
        self._extra = frozenset(extra)
        #: 复制模式建副本时副本里的文件（相对路径），用来区分删除与宿主新建。
        self._baseline = frozenset(baseline)

    @property
    def path(self) -> Path:
        """宿主上能直接读写的副本路径（也是沙箱里看到的 workspace）。"""
        return self._path

    @property
    def worktree(self) -> bool:
        return self._worktree

    async def sync(self) -> WorkspaceSync:
        """把副本里的改动合回宿主；失败时保留副本。"""
        if self._worktree:
            return await self._sync_worktree()
        return await asyncio.to_thread(self._sync_files)

    async def discard(self) -> None:
        """删除副本；worktree 先解除注册再删目录。"""
        if self._worktree:
            await _git(
                ["-C", str(self._host), "worktree", "remove", "--force", str(self._path)]
            )
            await _git(["-C", str(self._host), "worktree", "prune"])
        await asyncio.to_thread(shutil.rmtree, self._path, True)
        _remove_marker(self._path)

    async def _sync_worktree(self) -> WorkspaceSync:
        staged = await _git(["-C", str(self._path), "add", "-A"])
        if staged[0] != 0:
            return WorkspaceSync(
                error=f"git add failed: {staged[2].strip()}",
                kept=self._path,
            )

        names = await _git(["-C", str(self._path), "diff", "--cached", "--name-status"])
        if names[0] != 0:
            return WorkspaceSync(
                error=f"git diff failed: {names[2].strip()}",
                kept=self._path,
            )

        patched: list[str] = []
        applied: list[str] = []
        copied: list[str] = []
        deleted: list[str] = []
        for line in names[1].decode("utf-8", errors="replace").splitlines():
            fields = line.split("\t")
            if len(fields) < 2:
                continue
            status, name = fields[0], fields[-1]
            if status.startswith("D"):
                # 已跟踪文件的删除：补丁里要做，结果里只算删除。
                patched.append(name)
                deleted.append(name)
            elif name in self._extra:
                # 带进来的未跟踪文件不在 HEAD 里，补丁会当成新建，只能直接复制。
                if not _same_file(self._path / name, self._host / name):
                    copied.append(name)
            else:
                patched.append(name)
                applied.append(name)

        removed = [name for name in self._extra if not (self._path / name).exists()]
        deleted.extend(removed)

        if patched:
            diff = await _git(
                [
                    "-C",
                    str(self._path),
                    "diff",
                    "--cached",
                    "--binary",
                    "--no-color",
                    "--",
                    *patched,
                ]
            )
            if diff[0] != 0:
                return WorkspaceSync(
                    error=f"git diff failed: {diff[2].strip()}",
                    kept=self._path,
                )
            applied_result = await _git(
                ["-C", str(self._host), "apply", "--binary", "--whitespace=nowarn"],
                stdin=diff[1],
            )
            if applied_result[0] != 0:
                return WorkspaceSync(
                    applied=tuple(applied + copied),
                    deleted=tuple(deleted),
                    error=f"git apply failed: {applied_result[2].strip()}",
                    kept=self._path,
                )

        for name in copied:
            target = self._host / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self._path / name, target)
        for name in removed:
            (self._host / name).unlink(missing_ok=True)

        return WorkspaceSync(applied=tuple(applied + copied), deleted=tuple(deleted))

    def _sync_files(self) -> WorkspaceSync:
        current = set(_walk_files(self._path))
        created: list[str] = []
        changed: list[str] = []

        for relative in sorted(current):
            source = self._path / relative
            target = self._host / relative
            if not target.exists():
                created.append(relative)
            elif not _same_file(source, target):
                changed.append(relative)

        deleted = sorted(self._baseline - current)

        for relative in (*created, *changed):
            target = self._host / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self._path / relative, target)
        for relative in deleted:
            (self._host / relative).unlink(missing_ok=True)

        return WorkspaceSync(applied=tuple(created + changed), deleted=tuple(deleted))


async def create(host: str | Path) -> WorkspaceCopy:
    """为 `host` 建一份副本：git 仓库根用 worktree，其余复制。"""
    root = Path(host).expanduser().resolve()
    path = _new_dir()

    try:
        if await _is_git_root(root) and await _add_worktree(root, path):
            extra = await _copy_untracked(root, path)
            _write_marker(path, root, worktree=True)
            return WorkspaceCopy(root, path, worktree=True, extra=extra)

        await asyncio.to_thread(_copy_tree, root, path)
        baseline = tuple(await asyncio.to_thread(_walk_files, path))
        _write_marker(path, root, worktree=False)
        return WorkspaceCopy(root, path, worktree=False, baseline=baseline)
    except BaseException:
        await asyncio.to_thread(shutil.rmtree, path, True)
        _remove_marker(path)
        raise


async def cleanup_orphans(keep: str | Path | None = None) -> tuple[str, ...]:
    """删掉 owner 进程已不存在的副本；`keep` 是当前会话要用的路径。"""
    root = _root_dir()
    if not root.is_dir():
        return ()

    keep_path = _key(keep) if keep is not None else ""
    removed: list[str] = []

    for marker in sorted(root.glob(f"{DIR_PREFIX}*{MARKER_SUFFIX}")):
        path = marker.with_name(marker.name[: -len(MARKER_SUFFIX)])
        if _key(path) == keep_path:
            continue

        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}

        pid = data.get("pid")
        user = data.get("user")
        # 临时目录被重定向到共享位置时，别去动别的用户的副本。
        if isinstance(user, str) and user and user != current_user():
            continue
        if isinstance(pid, int) and _pid_alive(pid):
            continue

        host = data.get("host")
        if data.get("worktree") and isinstance(host, str):
            await _git(["-C", host, "worktree", "remove", "--force", str(path)])
            await _git(["-C", host, "worktree", "prune"])

        await asyncio.to_thread(shutil.rmtree, path, True)
        marker.unlink(missing_ok=True)
        removed.append(str(path))

    return tuple(removed)


async def _is_git_root(path: Path) -> bool:
    """`path` 本身是 git 仓库根；子目录与没有 git 的情况都返回 False。"""
    try:
        code, stdout, _ = await _git(["-C", str(path), "rev-parse", "--show-toplevel"])
    except OSError:
        return False
    if code != 0:
        return False
    try:
        return Path(stdout.decode("utf-8", errors="replace").strip()).resolve() == path
    except OSError:
        return False


async def _add_worktree(host: Path, path: Path) -> bool:
    """在 `path` 上检出 HEAD；失败时让调用方回落复制。"""
    await _git(["-C", str(host), "worktree", "prune"])
    code, _, _ = await _git(
        ["-C", str(host), "worktree", "add", "--detach", str(path), "HEAD"]
    )
    return code == 0


async def _copy_untracked(host: Path, path: Path) -> tuple[str, ...]:
    """把宿主上未跟踪的文件带进 worktree。

    `git worktree` 只检出 HEAD；`.env`、本地脚本、被 `.gitignore` 忽略的
    文件都不在里面。不复制的话，模型在副本里根本看不到它们。
    """
    try:
        code, stdout, _ = await _git(["-C", str(host), "ls-files", "--others", "-z"])
    except OSError:
        return ()
    if code != 0:
        return ()

    relatives: list[str] = []
    for raw in stdout.split(b"\0"):
        if not raw:
            continue
        relative = raw.decode("utf-8", errors="surrogateescape")
        if any(part in COPY_SKIP for part in Path(relative).parts):
            continue
        relatives.append(relative)

    return await asyncio.to_thread(_copy_files, host, path, relatives)


def _copy_files(host: Path, path: Path, relatives: list[str]) -> tuple[str, ...]:
    copied: list[str] = []
    for relative in relatives:
        source = host / relative
        target = path / relative
        if target.exists() or not source.is_file():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(Path(relative).as_posix())
    return tuple(copied)


def _copy_tree(source: Path, target: Path) -> None:
    shutil.copytree(
        source,
        target,
        symlinks=True,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(*COPY_SKIP),
    )


def _walk_files(root: Path) -> list[str]:
    """相对路径列表；跳过 `COPY_SKIP` 里的目录。"""
    files: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in COPY_SKIP for part in relative.parts):
            continue
        files.append(relative.as_posix())
    return files


def _same_file(left: Path, right: Path) -> bool:
    """先看大小与 mtime，再按内容确认，避免把复制时间差当成改动。"""
    try:
        left_stat = left.stat()
        right_stat = right.stat()
    except OSError:
        return False
    if left_stat.st_size != right_stat.st_size:
        return False
    if left_stat.st_mtime_ns == right_stat.st_mtime_ns:
        return True
    return filecmp.cmp(left, right, shallow=False)


def _root_dir() -> Path:
    return Path(tempfile.gettempdir())


def _new_dir() -> Path:
    root = _root_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{DIR_PREFIX}{os.getpid()}-{uuid4().hex[:8]}"
    path.mkdir()
    return path


def _marker(path: Path) -> Path:
    return path.with_name(path.name + MARKER_SUFFIX)


def _write_marker(path: Path, host: Path, *, worktree: bool) -> None:
    payload = {
        "pid": os.getpid(),
        "user": current_user(),
        "host": str(host),
        "worktree": worktree,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        _marker(path).write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass


def _remove_marker(path: Path) -> None:
    try:
        _marker(path).unlink(missing_ok=True)
    except OSError:
        pass


def _key(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve())


async def _git(
    argv: list[str], *, stdin: bytes | None = None
) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        "git",
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate(stdin)
    return process.returncode or 0, stdout, stderr
