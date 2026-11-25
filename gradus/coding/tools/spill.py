"""工具输出的定形：超限就落盘换指针，落盘失败才退回截断。

为什么在「进入上下文时」做：事后再改写等于每轮都在破前缀缓存。所以这层只负责
把一次工具输出变成「预算内的文本」，之后不再动它。

指针里带什么：完整路径 + 一段预览。模型想看得细就去读文件——比直接截断多一条
活路，也比把几万行塞进上下文便宜。

已知偏差：落盘文件目前写在系统临时目录、没有引用计数清理（设计文档 §8 要求
按引用计数回收）。清理策略留到接上会话目录时再做。
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from ..sandbox import create_scratch

#: 指针里保留的预览长度（字符）。
PREVIEW_CHARS = 1_500

#: 落盘目录前缀；同一台机器上的多次运行共用，靠文件名区分。
PREFIX = "gradus-spill-"


class SpillStore:
    """Owner-scoped spill directory with explicit reference accounting."""

    def __init__(
        self,
        directory: Path | None = None,
        *,
        display_root: Path | None = None,
    ) -> None:
        self.directory = directory or create_scratch(PREFIX)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.display_root = (
            Path(display_root).expanduser().resolve()
            if display_root is not None
            else None
        )
        self._refs: dict[Path, int] = {}

    def spill(self, text: str, *, hint: str = "tool") -> Path:
        return self._write(text, mode="w", encoding="utf-8", hint=hint)

    def spill_bytes(self, data: bytes, *, hint: str = "tool") -> Path:
        return self._write(data, mode="wb", encoding=None, hint=hint)

    def _write(
        self,
        data: str | bytes,
        *,
        mode: str,
        encoding: str | None,
        hint: str,
    ) -> Path:
        safe = "".join(
            ch if ch.isalnum() or ch in "-_" else "_" for ch in hint
        )[:40]
        handle = tempfile.NamedTemporaryFile(
            mode,
            encoding=encoding,
            suffix=".txt",
            prefix=f"spill-{safe}-",
            dir=self.directory,
            delete=False,
        )
        path = Path(handle.name)
        try:
            with handle:
                handle.write(data)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        self._refs[path] = 1
        return path

    def retain(self, path: Path) -> int:
        resolved = Path(path)
        self._refs[resolved] = self._refs.get(resolved, 0) + 1
        return self._refs[resolved]

    def release(self, path: Path, *, delete: bool = True) -> int:
        resolved = Path(path)
        count = max(0, self._refs.get(resolved, 0) - 1)
        if count:
            self._refs[resolved] = count
        else:
            self._refs.pop(resolved, None)
            if delete:
                resolved.unlink(missing_ok=True)
        return count

    @property
    def references(self) -> dict[Path, int]:
        return dict(self._refs)

    def cleanup(self) -> None:
        for path in tuple(self._refs):
            path.unlink(missing_ok=True)
        self._refs.clear()
        marker = self.directory / ".owner"
        marker.unlink(missing_ok=True)
        try:
            self.directory.rmdir()
        except OSError:
            pass

    def cleanup_stale(self, max_age: float = 7 * 24 * 60 * 60) -> None:
        """Remove old spill files without touching the active owner marker."""
        cutoff = time.time() - max_age
        for path in self.directory.iterdir():
            if path.name == ".owner" or not path.is_file():
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                continue

    def with_pointer(
        self,
        text: str,
        limit: int,
        *,
        hint: str = "tool",
        preview_chars: int = PREVIEW_CHARS,
    ) -> str:
        if len(text) <= limit:
            return text

        preview = text[:preview_chars]
        try:
            path = self.spill(text, hint=hint)
        except OSError:
            return f"{text[:limit]}\n… {len(text) - limit} chars truncated …"

        display = self._display(path)
        return (
            f"… 输出超过 {limit} 字符，完整内容已存到 {display}"
            "（用 read_file 读取这个 workspace-relative 路径）…\n"
            f"… 预览（前 {preview_chars} 字符）：\n{preview}"
        )

    def _display(self, path: Path) -> str:
        if self.display_root is not None:
            try:
                return path.resolve().relative_to(self.display_root).as_posix()
            except ValueError:
                pass
        return str(path)

    def display_path(self, path: str | Path) -> str:
        return self._display(Path(path))


_DEFAULT_STORE: SpillStore | None = None


def spill_dir() -> Path:
    return _default_store().directory


def spill(text: str, *, hint: str = "tool") -> Path:
    """Compatibility wrapper using the process-owned spill store."""
    return _default_store().spill(text, hint=hint)


def with_pointer(
    text: str,
    limit: int,
    *,
    hint: str = "tool",
    preview_chars: int = PREVIEW_CHARS,
) -> str:
    """超限时换成「指针 + 预览」；落盘失败则退回硬截断。

    返回的文本始终不超过 `limit` 太多（多出来的只有指针那几十个字符）。
    """
    return _default_store().with_pointer(
        text,
        limit,
        hint=hint,
        preview_chars=preview_chars,
    )


def _default_store() -> SpillStore:
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        _DEFAULT_STORE = SpillStore()
    return _DEFAULT_STORE
