"""文件工具：`read_file`、`list_dir`、`write_file`、`edit`、`delete_file`。

每个工具实现 `agent.base.AgentTool`：`spec`、`execution_mode`、`async execute()`。

- 模型只传 workspace-relative POSIX 路径；绝对路径、盘符、UNC 与 `..` 由
  `coding.paths` 统一拒绝；
- 工具在边界把相对路径解析成副本内 canonical absolute path，并再次检查
  realpath 没有逃出 workspace；
- `read_file` 支持 `offset` / `limit`，输出不带行号，上限 2000 行或 50KB；
- `list_dir` 最多 500 项，目录名带 `/`，包含点文件；
- `write_file` 新建或覆盖，自动创建父目录；
- `edit` 用 `edits[]` 精确替换，每个 `old_text` 必须在原文中唯一且互不重叠；
- `delete_file` 删单个文件，不起进程——AppContainer 里 `Remove-Item` 被平台
  拒绝，模型需要一条不依赖 shell 的删除路径。

精确编辑：`FileTracker` 记录已读文件；`edit` 以 old_text 的唯一匹配为安全条件，
未读过时仍允许安全替换，找不到匹配时提示读取当前内容。
读文件、列目录、写文件都走 `asyncio.to_thread`，避免阻塞事件循环。
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...agent.base import AgentTool, ToolUpdateCallback
from ...agent.types import ToolCapability, ToolResult
from ...ai.base import ToolSpec
from ..paths import WorkspacePathError, workspace_path

MAX_LINES = 2000
MAX_BYTES = 50 * 1024
MAX_ENTRIES = 500

READ_CAPABILITY = ToolCapability(effects=("read",), result_kind="evidence")
WRITE_CAPABILITY = ToolCapability(
    effects=("write",),
    result_kind="output",
    mutates_workspace=True,
    idempotent=False,
)
DELETE_CAPABILITY = ToolCapability(
    effects=("write",),
    result_kind="output",
    mutates_workspace=True,
    idempotent=False,
)

READ_SPEC = ToolSpec(
    name="read_file",
    description=(
        "Read a text file. Path must be relative to the workspace root. "
        f"Output is truncated to {MAX_LINES} lines or {MAX_BYTES // 1024}KB, "
        "whichever comes first. Use offset/limit for large files."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative file path (POSIX separators)",
            },
            "offset": {
                "type": "integer",
                "minimum": 1,
                "description": "Line number to start from (1-indexed)",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "description": "Maximum number of lines to read",
            },
        },
        "required": ["path"],
    },
)

WRITE_SPEC = ToolSpec(
    name="write_file",
    description=(
        "Create a new file or intentionally replace a complete existing file. "
        "Path must be relative to the workspace root; parent directories are created "
        "automatically. For an existing file, "
        "prefer edit when only part of the file changes."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative file path (POSIX separators)",
            },
            "content": {
                "type": "string",
                "description": "Full content to write",
            },
        },
        "required": ["path", "content"],
    },
)

EDIT_SPEC = ToolSpec(
    name="edit",
    description=(
        "Apply exact-text replacements to one existing workspace-relative file. Read the file with "
        "read_file first; shell reads such as cat/sed do not satisfy this requirement. "
        "Every edits[].old_text must appear exactly once in the original file, and "
        "replacements must not overlap. Prefer this tool for scoped source edits."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative file path (POSIX separators)",
            },
            "edits": {
                "type": "array",
                "minItems": 1,
                "description": (
                    "Replacements matched against the original file, not "
                    "incrementally. Merge nearby changes into one edit."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "old_text": {
                            "type": "string",
                            "description": "Exact text to replace; must be unique",
                        },
                        "new_text": {
                            "type": "string",
                            "description": "Replacement text",
                        },
                    },
                    "required": ["old_text", "new_text"],
                },
            },
        },
        "required": ["path", "edits"],
    },
)


DELETE_SPEC = ToolSpec(
    name="delete_file",
    description=(
        "Delete one workspace-relative file. "
        "Directories are rejected; remove files inside them one by one."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative file path (POSIX separators)",
            },
        },
        "required": ["path"],
    },
)

PATCH_SPEC = ToolSpec(
    name="apply_patch",
    description=(
        "Apply a multi-file patch with Begin/Add/Update/Delete/End markers. All file "
        "paths must be workspace-relative. "
        "Update hunks use unified-diff prefixes: space keeps context, '-' removes "
        "a line, '+' adds a line. Every hunk must match exactly once. Use this for "
        "coordinated edits across one or more source files."
    ),
    parameters={
        "type": "object",
        "properties": {
            "patch": {
                "type": "string",
                "description": (
                    "Patch text. Example:\n"
                    "*** Begin Patch\n"
                    "*** Update File: src/a.py\n"
                    "@@\n"
                    " old\n"
                    "-before\n"
                    "+after\n"
                    "*** End Patch"
                ),
            }
        },
        "required": ["patch"],
        "additionalProperties": False,
    },
)


class FileTracker:
    """记录本会话成功读过的文件，供工具结果和诊断使用。"""

    def __init__(self) -> None:
        self._read: set[Path] = set()

    def mark_read(self, path: Path) -> None:
        self._read.add(path)

    def was_read(self, path: Path) -> bool:
        return path in self._read


class ReadFileTool:
    """读取文本文件；路径相对 `root` 解析。"""

    spec = READ_SPEC
    execution_mode = "parallel"
    capability = READ_CAPABILITY

    def __init__(
        self,
        root: Path,
        tracker: FileTracker | None = None,
    ) -> None:
        self._root = root
        self._tracker = tracker

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        path = str(arguments.get("path", ""))
        offset = arguments.get("offset")
        limit = arguments.get("limit")
        try:
            resolved = _resolve(path, self._root)
        except WorkspacePathError as exc:
            return ToolResult(str(exc), is_error=True)

        if not resolved.exists():
            return ToolResult(f"File not found: {path}", is_error=True)
        if resolved.is_dir():
            return ToolResult(f"Not a file: {path}", is_error=True)

        try:
            text = await asyncio.to_thread(
                resolved.read_text, encoding="utf-8", errors="replace"
            )
        except OSError as exc:
            return ToolResult(f"Cannot read {path}: {exc}", is_error=True)

        if self._tracker is not None:
            self._tracker.mark_read(resolved)

        if text == "":
            return ToolResult("(empty file)")

        lines = text.split("\n")
        total = len(lines)
        start = 0 if offset is None else offset - 1
        if start >= total:
            return ToolResult(
                f"Offset {offset} is beyond end of file ({total} lines total)",
                is_error=True,
            )

        end = total if limit is None else min(start + limit, total)
        selected = lines[start:end]

        if len(selected[0].encode("utf-8")) > MAX_BYTES:
            head = selected[0].encode("utf-8")[:MAX_BYTES].decode("utf-8", "ignore")
            return ToolResult(
                f"{head}\n\n[Line {start + 1} truncated to {MAX_BYTES // 1024}KB]"
            )

        content, truncated_by = _truncate("\n".join(selected))
        shown = len(content.split("\n")) if content else 0
        shown_end = start + shown

        if truncated_by is not None:
            content += (
                f"\n\n[Showing lines {start + 1}-{shown_end} of {total}. "
                f"Use offset={shown_end + 1} to continue.]"
            )
        elif end < total:
            content += (
                f"\n\n[{total - end} more lines in file. "
                f"Use offset={end + 1} to continue.]"
            )

        return ToolResult(content)


class WriteFileTool:
    """新建或覆盖文件；路径相对 `root` 解析。"""

    spec = WRITE_SPEC
    execution_mode = "serial"
    capability = WRITE_CAPABILITY

    def __init__(
        self,
        root: Path,
        tracker: FileTracker | None = None,
        locks: PathLocks | None = None,
        guard: "Guard | None" = None,
    ) -> None:
        self._root = root
        self._tracker = tracker
        self._locks = locks or _DEFAULT_LOCKS
        self._guard = guard

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        path = str(arguments.get("path", ""))
        content = arguments.get("content")

        if not path:
            return ToolResult("path must not be empty", is_error=True)
        if not isinstance(content, str):
            return ToolResult("content must be a string", is_error=True)

        try:
            resolved = _resolve(path, self._root)
        except WorkspacePathError as exc:
            return ToolResult(str(exc), is_error=True)
        owner = self._guard(resolved) if self._guard is not None else None
        if owner:
            return ToolResult(_owned_message(path, owner), is_error=True)
        if resolved.is_dir():
            return ToolResult(f"Not a file: {path}", is_error=True)

        try:
            async with self._locks.lock_for(resolved):
                await asyncio.to_thread(
                    lambda: resolved.parent.mkdir(parents=True, exist_ok=True)
                )
                await asyncio.to_thread(_write_lf, resolved, content)
        except OSError as exc:
            return ToolResult(f"Cannot write {path}: {exc}", is_error=True)

        if self._tracker is not None:
            self._tracker.mark_read(resolved)

        return ToolResult(f"Wrote {len(content.encode('utf-8'))} bytes to {path}")


class EditTool:
    """按 `edits[]` 做精确替换；唯一匹配优先，不要求单次调用前读取。"""

    spec = EDIT_SPEC
    execution_mode = "serial"
    capability = WRITE_CAPABILITY

    def __init__(
        self,
        root: Path,
        tracker: FileTracker,
        locks: PathLocks | None = None,
        guard: "Guard | None" = None,
    ) -> None:
        self._root = root
        self._tracker = tracker
        self._locks = locks or _DEFAULT_LOCKS
        self._guard = guard

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        path = str(arguments.get("path", ""))
        edits = arguments.get("edits")
        try:
            resolved = _resolve(path, self._root)
        except WorkspacePathError as exc:
            return ToolResult(str(exc), is_error=True)
        owner = self._guard(resolved) if self._guard is not None else None
        if owner:
            return ToolResult(_owned_message(path, owner), is_error=True)

        if not resolved.exists():
            return ToolResult(f"File not found: {path}", is_error=True)
        if resolved.is_dir():
            return ToolResult(f"Not a file: {path}", is_error=True)
        if not isinstance(edits, list) or not edits:
            return ToolResult("edits must be a non-empty array", is_error=True)

        # 读-改-写整段进锁：中间有 `to_thread` 的让出点，并行批次改同一个文件时
        # 不加锁会互相覆盖。
        async with self._locks.lock_for(resolved):
            try:
                text = await asyncio.to_thread(
                    resolved.read_text, encoding="utf-8"
                )
            except (OSError, UnicodeDecodeError) as exc:
                return ToolResult(f"Cannot read {path}: {exc}", is_error=True)

            spans: list[tuple[int, int, str]] = []
            for index, edit in enumerate(edits, start=1):
                if not isinstance(edit, dict):
                    return ToolResult(f"edits[{index}] must be an object", is_error=True)
                old = edit.get("old_text")
                new = edit.get("new_text")
                if not isinstance(old, str) or old == "":
                    return ToolResult(
                        f"edits[{index}].old_text must be a non-empty string",
                        is_error=True,
                    )
                if not isinstance(new, str):
                    return ToolResult(
                        f"edits[{index}].new_text must be a string", is_error=True
                    )

                count = text.count(old)
                if count == 0:
                    return ToolResult(
                        f"edits[{index}].old_text not found in {path}; "
                        "use read_file to inspect the current text",
                        is_error=True,
                    )
                if count > 1:
                    return ToolResult(
                        f"edits[{index}].old_text appears {count} times; "
                        "it must be unique",
                        is_error=True,
                    )

                start = text.index(old)
                spans.append((start, start + len(old), new))

            spans.sort(key=lambda span: span[0])
            for previous, current in zip(spans, spans[1:]):
                if current[0] < previous[1]:
                    return ToolResult(
                        "edits overlap; merge them into one replacement", is_error=True
                    )

            updated = text
            for start, end, replacement in reversed(spans):
                updated = updated[:start] + replacement + updated[end:]

            try:
                await asyncio.to_thread(_write_lf, resolved, updated)
            except OSError as exc:
                return ToolResult(f"Cannot write {path}: {exc}", is_error=True)

            self._tracker.mark_read(resolved)
            return ToolResult(f"Edited {path}: {len(edits)} replacement(s)")


class DeleteFileTool:
    """删除单个文件；路径相对 `root` 解析。"""

    spec = DELETE_SPEC
    execution_mode = "serial"
    capability = DELETE_CAPABILITY

    def __init__(
        self,
        root: Path,
        locks: PathLocks | None = None,
        guard: "Guard | None" = None,
    ) -> None:
        self._root = root
        self._locks = locks or _DEFAULT_LOCKS
        self._guard = guard

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        path = str(arguments.get("path", ""))
        if not path:
            return ToolResult("path must not be empty", is_error=True)

        try:
            resolved = _resolve(path, self._root)
        except WorkspacePathError as exc:
            return ToolResult(str(exc), is_error=True)
        owner = self._guard(resolved) if self._guard is not None else None
        if owner:
            return ToolResult(_owned_message(path, owner), is_error=True)
        if resolved.is_dir():
            return ToolResult(f"Not a file: {path}", is_error=True)
        if not resolved.exists():
            return ToolResult(f"File not found: {path}", is_error=True)

        try:
            async with self._locks.lock_for(resolved):
                await asyncio.to_thread(resolved.unlink)
        except OSError as exc:
            return ToolResult(f"Cannot delete {path}: {exc}", is_error=True)

        return ToolResult(f"Deleted {path}")


class PatchError(ValueError):
    """The supplied patch is malformed or does not match the workspace."""


@dataclass(frozen=True)
class _Hunk:
    old: tuple[str, ...]
    new: tuple[str, ...]
    additions: int
    deletions: int


@dataclass(frozen=True)
class _FilePatch:
    action: str
    path: str
    content: str = ""
    hunks: tuple[_Hunk, ...] = ()


class ApplyPatchTool:
    """Apply a Codex-style multi-file patch atomically enough for agent edits."""

    spec = PATCH_SPEC
    execution_mode = "serial"
    capability = WRITE_CAPABILITY

    def __init__(
        self,
        root: Path,
        tracker: FileTracker | None = None,
        locks: PathLocks | None = None,
        guard: "Guard | None" = None,
    ) -> None:
        self._root = root
        self._tracker = tracker
        self._locks = locks or _DEFAULT_LOCKS
        self._guard = guard

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        raw = arguments.get("patch")
        if not isinstance(raw, str) or not raw.strip():
            return ToolResult("patch must be a non-empty string", is_error=True)
        try:
            patches = _parse_patch(raw)
            prepared = await self._prepare(patches)
            changes = await self._commit(prepared)
        except (PatchError, WorkspacePathError, OSError, UnicodeDecodeError) as exc:
            return ToolResult(f"patch failed: {exc}", is_error=True)

        summary = ", ".join(
            f"{change['action']} {change['path']}" for change in changes
        )
        return ToolResult(
            f"Applied patch: {summary}",
            structured_content={"workspace_changes": changes},
            details={"workspace_changes": changes},
        )

    async def _prepare(self, patches: list[_FilePatch]) -> dict[str, Any]:
        prepared: dict[str, Any] = {}
        for patch in patches:
            resolved = _resolve(patch.path, self._root)
            relative = _relative(resolved, self._root)
            if relative in prepared:
                raise PatchError(f"file appears more than once: {relative}")
            owner = self._guard(resolved) if self._guard is not None else None
            if owner:
                raise PatchError(_owned_message(relative, owner))

            if patch.action == "add":
                if resolved.exists():
                    raise PatchError(f"cannot add existing file: {relative}")
                prepared[relative] = {
                    "path": resolved,
                    "action": "add",
                    "before": None,
                    "after": patch.content,
                    "additions": len(patch.content.splitlines()),
                    "deletions": 0,
                }
                continue

            if not resolved.exists():
                raise PatchError(f"file not found: {relative}")
            if resolved.is_dir():
                raise PatchError(f"not a file: {relative}")
            before = await asyncio.to_thread(resolved.read_text, encoding="utf-8")

            if patch.action == "delete":
                prepared[relative] = {
                    "path": resolved,
                    "action": "delete",
                    "before": before,
                    "after": None,
                    "additions": 0,
                    "deletions": len(before.splitlines()),
                }
                continue

            after = before
            additions = deletions = 0
            for number, hunk in enumerate(patch.hunks, start=1):
                old = "\n".join(hunk.old)
                if not old:
                    raise PatchError(
                        f"{relative}: hunk {number} has no context or removed lines"
                    )
                count = after.count(old)
                if count == 0:
                    raise PatchError(
                        f"{relative}: hunk {number} did not match; re-read the file"
                    )
                if count > 1:
                    raise PatchError(
                        f"{relative}: hunk {number} matched {count} times; add context"
                    )
                after = after.replace(old, "\n".join(hunk.new), 1)
                deletions += hunk.deletions
                additions += hunk.additions

            if after == before:
                raise PatchError(f"{relative}: patch made no changes")
            prepared[relative] = {
                "path": resolved,
                "action": "update",
                "before": before,
                "after": after,
                "additions": additions,
                "deletions": deletions,
            }
        return prepared

    async def _commit(self, prepared: dict[str, Any]) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = []
        applied: list[dict[str, Any]] = []
        async with AsyncExitStack() as stack:
            for item in sorted(
                prepared.values(),
                key=lambda value: str(value["path"]),
            ):
                await stack.enter_async_context(
                    self._locks.lock_for(item["path"])
                )
            try:
                for relative, item in prepared.items():
                    path: Path = item["path"]
                    if item["action"] == "delete":
                        await asyncio.to_thread(path.unlink)
                    else:
                        await asyncio.to_thread(
                            path.parent.mkdir, parents=True, exist_ok=True
                        )
                        await asyncio.to_thread(_write_lf, path, item["after"])
                        if self._tracker is not None:
                            self._tracker.mark_read(path)
                    applied.append(item)
                    changes.append(
                        {
                            "path": relative,
                            "action": item["action"],
                            "additions": item["additions"],
                            "deletions": item["deletions"],
                        }
                    )
            except OSError:
                for item in reversed(applied):
                    path: Path = item["path"]
                    if item["before"] is None:
                        path.unlink(missing_ok=True)
                    else:
                        await asyncio.to_thread(_write_lf, path, item["before"])
                raise
        return changes


class PathLocks:
    """同一路径上的写操作排队。

    读写都走 `asyncio.to_thread`，所以「读-改-写」之间存在真实的让出点：并行批次里
    两个调用改同一个文件时，后写的会覆盖前一个的结果。按路径排队把这段窗口消掉——
    这也是 Pi 的 `withFileMutationQueue` 在做的事。

    键是解析后的绝对路径；表的大小被「一次会话里碰过的不同路径数」限住。
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def lock_for(self, path: Path) -> asyncio.Lock:
        return self._locks.setdefault(str(path), asyncio.Lock())


_DEFAULT_LOCKS = PathLocks()


def create_read_only_tools(
    root: str | Path,
    tracker: FileTracker | None = None,
) -> list[AgentTool]:
    """建一组只读工具，路径统一相对 `root` 解析。"""
    base = Path(root).expanduser().resolve()
    return [ReadFileTool(base, tracker)]


def create_file_tools(
    root: str | Path,
    tracker: FileTracker | None = None,
    guard: "Guard | None" = None,
) -> list[AgentTool]:
    """建完整的文件工具组，共享同一个 `FileTracker`。

    `guard(resolved_path)` 返回占用者名字时，写/改/删会被拒绝：用来实现「派出去的
    任务归子 agent 所有，父 agent 别再动同一批文件」。
    """
    base = Path(root).expanduser().resolve()
    tracker = tracker or FileTracker()
    # 同一组工具共享一张路径锁表：并行批次里改同一个文件时按路径排队。
    locks = PathLocks()
    return [
        ReadFileTool(base, tracker),
        WriteFileTool(base, tracker, locks, guard),
        EditTool(base, tracker, locks, guard),
        DeleteFileTool(base, locks, guard),
        ApplyPatchTool(base, tracker, locks, guard),
    ]


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _parse_patch(text: str) -> list[_FilePatch]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "*** Begin Patch":
        raise PatchError("patch must start with *** Begin Patch")
    if lines[-1].strip() != "*** End Patch":
        raise PatchError("patch must end with *** End Patch")

    patches: list[_FilePatch] = []
    index = 1
    while index < len(lines) - 1:
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        header = _file_header(line)
        if header is None:
            raise PatchError(f"expected file header, got: {line}")
        action, path = header
        index += 1

        if action == "add":
            body: list[str] = []
            while (
                index < len(lines) - 1
                and lines[index].strip() != "*** End Patch"
                and _file_header(lines[index]) is None
            ):
                current = lines[index]
                if not current.startswith("+"):
                    raise PatchError(
                        f"add file {path}: every content line must start with '+'"
                    )
                body.append(current[1:])
                index += 1
            content = "\n".join(body)
            if body:
                content += "\n"
            patches.append(_FilePatch("add", path, content=content))
            continue

        if action == "delete":
            while (
                index < len(lines) - 1
                and lines[index].strip() != "*** End Patch"
                and _file_header(lines[index]) is None
            ):
                if lines[index].strip():
                    raise PatchError(f"delete file {path}: unexpected content")
                index += 1
            patches.append(_FilePatch("delete", path))
            continue

        hunks: list[_Hunk] = []
        old: list[str] | None = None
        new: list[str] = []
        additions = deletions = 0
        while (
            index < len(lines) - 1
            and lines[index].strip() != "*** End Patch"
            and _file_header(lines[index]) is None
        ):
            current = lines[index]
            if current.startswith("@@"):
                if old is not None:
                    hunks.append(
                        _Hunk(tuple(old), tuple(new), additions, deletions)
                    )
                old = []
                new = []
                additions = deletions = 0
                index += 1
                continue
            if old is None:
                raise PatchError(f"update file {path}: hunk must start with @@")
            prefix = current[:1]
            if prefix == " ":
                old.append(current[1:])
                new.append(current[1:])
            elif prefix == "-":
                old.append(current[1:])
                deletions += 1
            elif prefix == "+":
                new.append(current[1:])
                additions += 1
            else:
                raise PatchError(
                    f"update file {path}: hunk lines need ' ', '-', or '+'"
                )
            index += 1
        if old is not None:
            hunks.append(_Hunk(tuple(old), tuple(new), additions, deletions))
        if not hunks:
            raise PatchError(f"update file {path}: no hunks")
        patches.append(_FilePatch("update", path, hunks=tuple(hunks)))

    if not patches:
        raise PatchError("patch contains no file operations")
    return patches


def _file_header(line: str) -> tuple[str, str] | None:
    for label, action in (
        ("*** Add File: ", "add"),
        ("*** Update File: ", "update"),
        ("*** Delete File: ", "delete"),
    ):
        if line.startswith(label):
            path = line[len(label) :].strip()
            if not path:
                raise PatchError(f"{label.strip()} requires a path")
            return action, path
    return None


def _resolve(path: str, root: Path) -> Path:
    return workspace_path(root, path)


def _truncate(
    text: str,
    *,
    max_lines: int = MAX_LINES,
    max_bytes: int = MAX_BYTES,
) -> tuple[str, str | None]:
    """按行数与字节数截断，返回内容和触发的上限（`None` 表示未截断）。"""
    if text == "":
        return "", None

    lines = text.split("\n")
    if len(lines) <= max_lines and len(text.encode("utf-8")) <= max_bytes:
        return text, None

    kept: list[str] = []
    used = 0
    for line in lines[:max_lines]:
        size = len(line.encode("utf-8")) + (1 if kept else 0)
        if used + size > max_bytes:
            break
        kept.append(line)
        used += size

    content = "\n".join(kept)
    truncated_by = "lines" if len(kept) >= max_lines else "bytes"
    return content, truncated_by

def _write_lf(path: Path, content: str) -> None:
    """按字节写 LF。

    Windows 上 `Path.write_text` 默认会把 `\n` 翻成 `\r\n`——文件被改成 CRLF 后
    `git diff` 会把整棵树算成改动（exec 模式实测整文件 1465 行全变）。写文件必须
    保持行尾原样，跨平台才能和仓库索引对得上。
    """
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


def _owned_message(path: str, owner: str) -> str:
    return (
        f"{path} is owned by {owner} right now: do not edit it. "
        f"Call wait to collect that subagent's result, then integrate it."
    )
