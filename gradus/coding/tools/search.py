"""搜索工具：`grep`（按内容）与 `glob`（按路径）。

对齐 Pi 的 `grep` / `find`：

- 相对路径基于 workspace 解析；
- 跳过 `.git`、`node_modules`、虚拟环境等目录，不额外读 `.gitignore`；
- `grep` 默认最多 100 条命中，单行截断到 300 字符；
- `glob` 默认最多 1000 个结果。

扫描走 `asyncio.to_thread`，避免阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ...agent.base import AgentTool, ToolUpdateCallback
from ...agent.types import ToolResult
from ...ai.base import ToolSpec

GREP_LIMIT = 100
GLOB_LIMIT = 1000
MAX_BYTES = 50 * 1024
MAX_LINE = 300

IGNORED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
    }
)

GREP_SPEC = ToolSpec(
    name="grep",
    description=(
        "Search file contents for a pattern. Returns matching lines as "
        "'path:line: text' with paths relative to the working directory. "
        "Respects common ignore directories (.git, node_modules, virtualenvs). "
        f"Output is capped at {GREP_LIMIT} matches or {MAX_BYTES // 1024}KB; "
        f"lines longer than {MAX_LINE} characters are truncated."
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Search pattern (regex, or literal string)",
            },
            "path": {
                "type": "string",
                "description": "File or directory to search (default: working directory)",
            },
            "glob": {
                "type": "string",
                "description": "Only search files matching this glob, e.g. '*.py'",
            },
            "ignoreCase": {
                "type": "boolean",
                "description": "Case-insensitive search (default: false)",
            },
            "literal": {
                "type": "boolean",
                "description": "Treat pattern as a literal string (default: false)",
            },
            "context": {
                "type": "integer",
                "minimum": 0,
                "description": "Lines to show before and after each match (default: 0)",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "description": f"Maximum matches to return (default: {GREP_LIMIT})",
            },
        },
        "required": ["pattern"],
    },
)

GLOB_SPEC = ToolSpec(
    name="glob",
    description=(
        "Find files by glob pattern, e.g. '*.py' or 'src/**/*.test.ts'. "
        "Returns paths relative to the searched directory, skipping common "
        f"ignore directories. Output is capped at {GLOB_LIMIT} paths."
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern, e.g. '**/*.json'",
            },
            "path": {
                "type": "string",
                "description": "Directory to search (default: working directory)",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "description": f"Maximum results (default: {GLOB_LIMIT})",
            },
        },
        "required": ["pattern"],
    },
)


class GrepTool:
    """按内容搜索；默认从 workspace 根开始。"""

    spec = GREP_SPEC
    execution_mode = "parallel"

    def __init__(self, root: Path) -> None:
        self._root = root

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        pattern = str(arguments.get("pattern", ""))
        if not pattern:
            return ToolResult("pattern is required", is_error=True)

        flags = re.IGNORECASE if arguments.get("ignoreCase") else 0
        source = re.escape(pattern) if arguments.get("literal") else pattern
        try:
            matcher = re.compile(source, flags)
        except re.error as exc:
            return ToolResult(f"Invalid regex: {exc}", is_error=True)

        target = _resolve(str(arguments.get("path") or ""), self._root)
        if not target.exists():
            return ToolResult(f"Path not found: {target}", is_error=True)

        glob_filter = arguments.get("glob") or None
        context = max(0, int(arguments.get("context") or 0))
        limit = max(1, int(arguments.get("limit") or GREP_LIMIT))

        return await asyncio.to_thread(
            _grep, target, self._root, matcher, glob_filter, context, limit
        )


class GlobTool:
    """按路径匹配；不读取文件内容。"""

    spec = GLOB_SPEC
    execution_mode = "parallel"

    def __init__(self, root: Path) -> None:
        self._root = root

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        pattern = str(arguments.get("pattern", ""))
        if not pattern:
            return ToolResult("pattern is required", is_error=True)

        base = _resolve(str(arguments.get("path") or ""), self._root)
        if not base.exists():
            return ToolResult(f"Path not found: {base}", is_error=True)
        if not base.is_dir():
            return ToolResult(f"Not a directory: {base}", is_error=True)

        limit = max(1, int(arguments.get("limit") or GLOB_LIMIT))
        return await asyncio.to_thread(_glob, base, pattern, limit)


def create_search_tools(root: str | Path) -> list[AgentTool]:
    base = Path(root).expanduser().resolve()
    return [GrepTool(base), GlobTool(base)]


def _grep(
    target: Path,
    root: Path,
    matcher: re.Pattern[str],
    glob_filter: str | None,
    context: int,
    limit: int,
) -> ToolResult:
    files = [target] if target.is_file() else list(_walk(target))
    output: list[str] = []
    bytes_used = 0
    hits = 0
    truncated = False

    for file in files:
        label = _label(file, root)
        if glob_filter and not Path(label).match(glob_filter):
            continue

        text = _read_text(file)
        if text is None:
            continue
        lines = text.splitlines()

        for index, line in enumerate(lines):
            if not matcher.search(line):
                continue
            if hits >= limit:
                truncated = True
                break

            start = max(0, index - context)
            end = min(len(lines), index + context + 1)
            for offset in range(start, end):
                marker = ":" if offset == index else "-"
                rendered = _clip_line(lines[offset])
                entry = f"{label}:{offset + 1}{marker} {rendered}"
                output.append(entry)
                bytes_used += len(entry.encode("utf-8")) + 1

            hits += 1
            if bytes_used >= MAX_BYTES:
                truncated = True
                break

        if truncated:
            break

    if not output:
        return ToolResult("(no matches)")

    body = "\n".join(output)
    if truncated:
        body += "\n… results truncated; narrow the pattern, add a glob, or raise limit …"
    return ToolResult(body)


def _glob(base: Path, pattern: str, limit: int) -> ToolResult:
    results = [
        path.relative_to(base).as_posix()
        for path in base.glob(pattern)
        if path.is_file() and not _ignored(path, base)
    ]

    if not results:
        return ToolResult("(no matches)")

    results.sort()
    truncated = len(results) > limit
    body = "\n".join(results[:limit])
    if truncated:
        body += "\n… results truncated; narrow the pattern or raise limit …"
    return ToolResult(body)


def _walk(root: Path) -> Iterator[Path]:
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(name for name in dirs if name not in IGNORED_DIRS)
        for name in sorted(files):
            yield Path(current) / name


def _ignored(path: Path, base: Path) -> bool:
    try:
        parts = path.relative_to(base).parts
    except ValueError:
        return False
    return any(part in IGNORED_DIRS for part in parts)


def _read_text(path: Path) -> str | None:
    """读文本文件；二进制或不可读时返回 `None`。"""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:1024]:
        return None
    return data.decode("utf-8", errors="replace")


def _clip_line(text: str) -> str:
    if len(text) <= MAX_LINE:
        return text
    return text[:MAX_LINE] + "…"


def _label(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _resolve(path: str, root: Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.resolve()
