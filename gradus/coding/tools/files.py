"""文件工具：`read_file`、`list_dir`、`write_file`、`edit`、`delete_file`。

每个工具实现 `agent.base.AgentTool`：`spec`、`execution_mode`、`async execute()`。

对齐 Pi：
- 相对路径基于 workspace 解析，绝对路径与 `..` 允许；工具内不做沙箱，
  越界拦截由权限层（`permissions` / `before_tool_call`）负责；
- `read_file` 支持 `offset` / `limit`，输出不带行号，上限 2000 行或 50KB；
- `list_dir` 最多 500 项，目录名带 `/`，包含点文件；
- `write_file` 新建或覆盖，自动创建父目录；
- `edit` 用 `edits[]` 精确替换，每个 `old_text` 必须在原文中唯一且互不重叠；
- `delete_file` 删单个文件，不起进程——AppContainer 里 `Remove-Item` 被平台
  拒绝，模型需要一条不依赖 shell 的删除路径。

先读后写：`FileTracker` 记录已读文件，`edit` 未读过就拒绝。
读文件、列目录、写文件都走 `asyncio.to_thread`，避免阻塞事件循环。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ...agent.base import AgentTool, ToolUpdateCallback
from ...agent.types import ToolResult
from ...ai.base import ToolSpec

MAX_LINES = 2000
MAX_BYTES = 50 * 1024
MAX_ENTRIES = 500

READ_SPEC = ToolSpec(
    name="read_file",
    description=(
        "Read a text file. Relative paths resolve from the working directory. "
        f"Output is truncated to {MAX_LINES} lines or {MAX_BYTES // 1024}KB, "
        "whichever comes first. Use offset/limit for large files; to read the "
        "rest, continue with the offset given in the truncation notice."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File to read (relative or absolute)",
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

LS_SPEC = ToolSpec(
    name="list_dir",
    description=(
        "List directory contents. Relative paths resolve from the working directory. "
        f"Entries are sorted case-insensitively, directories end with '/', and output "
        f"is capped at {MAX_ENTRIES} entries or {MAX_BYTES // 1024}KB."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory to list (default: working directory)",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "description": f"Maximum number of entries (default: {MAX_ENTRIES})",
            },
        },
    },
)

WRITE_SPEC = ToolSpec(
    name="write_file",
    description=(
        "Create or overwrite a file. Parent directories are created automatically. "
        "Use this for new files or complete rewrites; use edit for small changes."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File to write (relative or absolute)",
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
        "Apply exact-text replacements to one file. Every edits[].old_text must "
        "appear exactly once in the original file, and replacements must not "
        "overlap. Read the file before editing."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File to edit (relative or absolute)",
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
        "Delete one file. Relative paths resolve from the working directory. "
        "Directories are rejected; remove files inside them one by one."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File to delete (relative or absolute)",
            },
        },
        "required": ["path"],
    },
)


class FileTracker:
    """记录本会话成功读过的文件，用于 `edit` 的先读后写检查。"""

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

    def __init__(self, root: Path, tracker: FileTracker | None = None) -> None:
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
        resolved = _resolve(path, self._root)

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


class ListDirTool:
    """列出目录项；路径相对 `root` 解析。"""

    spec = LS_SPEC
    execution_mode = "parallel"

    def __init__(self, root: Path, tracker: FileTracker | None = None) -> None:
        self._root = root

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        path = str(arguments.get("path", "."))
        limit = arguments.get("limit", MAX_ENTRIES)
        resolved = _resolve(path, self._root)

        if not resolved.exists():
            return ToolResult(f"Path not found: {path}", is_error=True)
        if not resolved.is_dir():
            return ToolResult(f"Not a directory: {path}", is_error=True)

        try:
            entries = await asyncio.to_thread(
                lambda: sorted(resolved.iterdir(), key=lambda p: p.name.lower())
            )
        except OSError as exc:
            return ToolResult(f"Cannot read directory {path}: {exc}", is_error=True)

        if not entries:
            return ToolResult("(empty directory)")

        shown: list[str] = []
        entry_limit_reached = False
        for entry in entries:
            if len(shown) >= limit:
                entry_limit_reached = True
                break
            try:
                is_dir = await asyncio.to_thread(entry.is_dir)
            except OSError:
                continue
            shown.append(entry.name + ("/" if is_dir else ""))

        content, truncated_by = _truncate("\n".join(shown), max_lines=len(shown))

        notices: list[str] = []
        if entry_limit_reached:
            notices.append(
                f"{limit} entries limit reached. Use limit={limit * 2} for more"
            )
        if truncated_by is not None:
            notices.append(f"{MAX_BYTES // 1024}KB limit reached")
        if notices:
            content += f"\n\n[{'. '.join(notices)}]"

        return ToolResult(content)


class WriteFileTool:
    """新建或覆盖文件；路径相对 `root` 解析。"""

    spec = WRITE_SPEC
    execution_mode = "serial"

    def __init__(self, root: Path, tracker: FileTracker | None = None) -> None:
        self._root = root
        self._tracker = tracker

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

        resolved = _resolve(path, self._root)
        if resolved.is_dir():
            return ToolResult(f"Not a file: {path}", is_error=True)

        try:
            await asyncio.to_thread(
                lambda: resolved.parent.mkdir(parents=True, exist_ok=True)
            )
            await asyncio.to_thread(resolved.write_text, content, encoding="utf-8")
        except OSError as exc:
            return ToolResult(f"Cannot write {path}: {exc}", is_error=True)

        if self._tracker is not None:
            self._tracker.mark_read(resolved)

        return ToolResult(f"Wrote {len(content.encode('utf-8'))} bytes to {path}")


class EditTool:
    """按 `edits[]` 做精确替换；要求先读过文件。"""

    spec = EDIT_SPEC
    execution_mode = "serial"

    def __init__(self, root: Path, tracker: FileTracker) -> None:
        self._root = root
        self._tracker = tracker

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        path = str(arguments.get("path", ""))
        edits = arguments.get("edits")
        resolved = _resolve(path, self._root)

        if not resolved.exists():
            return ToolResult(f"File not found: {path}", is_error=True)
        if resolved.is_dir():
            return ToolResult(f"Not a file: {path}", is_error=True)
        if not self._tracker.was_read(resolved):
            return ToolResult(
                f"Read {path} before editing it", is_error=True
            )
        if not isinstance(edits, list) or not edits:
            return ToolResult("edits must be a non-empty array", is_error=True)

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
                    f"edits[{index}].old_text not found in {path}", is_error=True
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
            await asyncio.to_thread(resolved.write_text, updated, encoding="utf-8")
        except OSError as exc:
            return ToolResult(f"Cannot write {path}: {exc}", is_error=True)

        return ToolResult(f"Edited {path}: {len(edits)} replacement(s)")


class DeleteFileTool:
    """删除单个文件；路径相对 `root` 解析。"""

    spec = DELETE_SPEC
    execution_mode = "serial"

    def __init__(self, root: Path) -> None:
        self._root = root

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        path = str(arguments.get("path", ""))
        if not path:
            return ToolResult("path must not be empty", is_error=True)

        resolved = _resolve(path, self._root)
        if resolved.is_dir():
            return ToolResult(f"Not a file: {path}", is_error=True)
        if not resolved.exists():
            return ToolResult(f"File not found: {path}", is_error=True)

        try:
            await asyncio.to_thread(resolved.unlink)
        except OSError as exc:
            return ToolResult(f"Cannot delete {path}: {exc}", is_error=True)

        return ToolResult(f"Deleted {path}")


def create_read_only_tools(
    root: str | Path,
    tracker: FileTracker | None = None,
) -> list[AgentTool]:
    """建一组只读工具，路径统一相对 `root` 解析。"""
    base = Path(root).expanduser().resolve()
    return [ReadFileTool(base, tracker), ListDirTool(base)]


def create_file_tools(
    root: str | Path,
    tracker: FileTracker | None = None,
) -> list[AgentTool]:
    """建完整的文件工具组，共享同一个 `FileTracker`。"""
    base = Path(root).expanduser().resolve()
    tracker = tracker or FileTracker()
    return [
        ReadFileTool(base, tracker),
        ListDirTool(base),
        WriteFileTool(base, tracker),
        EditTool(base, tracker),
        DeleteFileTool(base),
    ]


def _resolve(path: str, root: Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.resolve()


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
