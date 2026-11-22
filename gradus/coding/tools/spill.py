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
from pathlib import Path

#: 指针里保留的预览长度（字符）。
PREVIEW_CHARS = 1_500

#: 落盘目录前缀；同一台机器上的多次运行共用，靠文件名区分。
PREFIX = "gradus-output"


def spill_dir() -> Path:
    """落盘目录；不存在时尝试建出来。"""
    directory = Path(tempfile.gettempdir()) / "gradus-outputs"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def spill(text: str, *, hint: str = "tool") -> Path:
    """把完整内容写进文件，返回路径。写不进去会抛 `OSError`。"""
    directory = spill_dir()
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in hint)[:40]
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        suffix=".txt",
        prefix=f"{PREFIX}-{safe}-",
        dir=directory,
        delete=False,
    )
    try:
        with handle:
            handle.write(text)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise
    return Path(handle.name)


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
    if len(text) <= limit:
        return text

    preview = text[:preview_chars]
    try:
        path = spill(text, hint=hint)
    except OSError:
        return f"{text[:limit]}\n… {len(text) - limit} chars truncated …"

    return (
        f"… 输出超过 {limit} 字符，完整内容已存到 {path}（读它用 read_file）…\n"
        f"… 预览（前 {preview_chars} 字符）：\n{preview}"
    )
