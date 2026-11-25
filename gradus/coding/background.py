"""后台命令：启动、追踪、日志落盘。

shell 工具的 `run_in_background` 把命令交给本模块，立即拿到 task id 返回给模型；
命令在独立的 asyncio task 里跑，输出**边读边写**进
`<workspace>/.gradus/background/<task_id>.log`：头部直接落盘，超过上限后只在
内存里维护一个尾部缓冲，中间部分只记长度。进程内存不随输出增长。
完成时通过 `sink` 通知上层，由 `Session` 转成一条带标记的消息注入对话。

生命周期是会话，不进入对话历史：任务表与日志都不属于 `Message`。

三条约束：

- 同时在跑的任务数有上限，超了直接报错而不是排队——排队的任务对模型不可见，
  等于黑洞；
- 命令有最大存活时间，超时算完成并发通知，保证“开始过的任务一定回来”；
- 日志数量有上限，超出时删最旧的已完成任务，避免目录无限增长。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .sandbox import Environment

DEFAULT_MAX_RUNNING = 4
DEFAULT_MAX_KEPT = 50
DEFAULT_MAX_SECONDS = 1800.0

#: 日志上限：头部直接落盘，尾部留在内存，中间只记长度。文件最大约头 + 尾。
LOG_HEAD_CHARS = 64 * 1024
LOG_TAIL_CHARS = 256 * 1024

#: 通知里附带的输出尾部行数。
NOTICE_TAIL_LINES = 40

STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_KILLED = "killed"


class BackgroundLimit(RuntimeError):
    """并发后台任务已达上限。"""


@dataclass
class BackgroundTask:
    """一个后台任务的可观察状态。"""

    id: str
    tool: str
    command: str
    status: str
    log_path: Path
    started_at: float
    finished_at: float | None = None
    exit_code: int | None = None

    @property
    def running(self) -> bool:
        return self.status == STATUS_RUNNING


class BackgroundTasks:
    """一次会话内的后台任务表与执行器。"""

    def __init__(
        self,
        env: Environment,
        directory: str | Path,
        *,
        max_running: int = DEFAULT_MAX_RUNNING,
        max_kept: int = DEFAULT_MAX_KEPT,
        max_seconds: float = DEFAULT_MAX_SECONDS,
    ) -> None:
        self._env = env
        self._directory = Path(directory)
        self._max_running = max(1, max_running)
        self._max_kept = max(1, max_kept)
        self._max_seconds = max(1.0, max_seconds)
        self._tasks: dict[str, BackgroundTask] = {}
        self._order: deque[str] = deque()
        self._running: dict[str, asyncio.Task[None]] = {}
        self._processes: dict[str, Any] = {}
        self._counter = 0
        self._sink: Callable[[str], None] | None = None
        self._closed = False

    @property
    def directory(self) -> Path:
        return self._directory

    def bind(self, sink: Callable[[str], None]) -> None:
        """设置完成通知的接收方；由 `Session` 绑定到 Agent。"""
        self._sink = sink

    async def start(
        self,
        *,
        tool: str,
        command: str,
        argv: Sequence[str],
        cwd: Path,
    ) -> BackgroundTask:
        """启动一条命令，立即返回任务记录。"""
        if self._closed:
            raise BackgroundLimit("session is closed")
        if len(self._running) >= self._max_running:
            raise BackgroundLimit(
                f"too many background tasks ({len(self._running)} running); "
                "wait for one to finish or check it with check_background"
            )

        self._counter += 1
        task_id = f"bg_{self._counter:04d}"
        with suppress(OSError):
            self._directory.mkdir(parents=True, exist_ok=True)
        task = BackgroundTask(
            id=task_id,
            tool=tool,
            command=command,
            status=STATUS_RUNNING,
            log_path=self._directory / f"{task_id}.log",
            started_at=time.time(),
        )
        self._tasks[task_id] = task
        self._order.append(task_id)
        self._prune_old()
        self._running[task_id] = asyncio.create_task(self._run(task, argv, cwd))
        return task

    def snapshot(self, task_id: str | None = None, tail_lines: int = 60) -> str:
        """任务列表，或单个任务的状态与输出尾部。"""
        if task_id is not None:
            task = self._tasks.get(task_id)
            if task is None:
                return f"unknown background task: {task_id}"
            return self._describe(task, tail_lines)

        if not self._tasks:
            return "(no background tasks)"

        lines = [
            f"{self._tasks[key].id}: [{self._tasks[key].status}] "
            f"{_short(self._tasks[key].command)}"
            for key in self._order
            if key in self._tasks
        ]
        lines.append("Pass task_id for status and output tail.")
        return "\n".join(lines)

    async def aclose(self) -> None:
        """杀掉仍在跑的命令并停止追踪；幂等。"""
        if self._closed:
            return
        self._closed = True

        for process in list(self._processes.values()):
            with suppress(Exception):
                process.kill()

        pending = list(self._running.values())
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        self._running.clear()
        self._processes.clear()

        # 还没开始执行就被取消的任务不会走到 `_run` 的 finally，状态在这里补。
        for row in self._tasks.values():
            if row.running:
                row.status = STATUS_KILLED
                row.finished_at = time.time()

    # ---- 内部 ----

    async def _run(
        self,
        task: BackgroundTask,
        argv: Sequence[str],
        cwd: Path,
    ) -> None:
        status = STATUS_FAILED
        exit_code: int | None = None
        writer = _LogWriter(task.log_path)
        process: Any = None

        try:
            process = await self._env.spawn(argv, cwd=cwd, merge_stderr=True)
            self._processes[task.id] = process
            try:
                async with asyncio.timeout(self._max_seconds):
                    await _stream_output(process.stdout, writer)
                exit_code = process.returncode or 0
                status = STATUS_COMPLETED if exit_code == 0 else STATUS_FAILED
            except TimeoutError:
                with suppress(Exception):
                    process.kill()
                with suppress(Exception):
                    await process.wait()
                status = STATUS_KILLED
                writer.write(f"\n(killed after {self._max_seconds:g}s)\n")
        except asyncio.CancelledError:
            if process is not None:
                with suppress(Exception):
                    process.kill()
            status = STATUS_KILLED
            writer.write("\n(cancelled)\n")
            raise
        except Exception as exc:
            status = STATUS_FAILED
            writer.write(f"\n{type(exc).__name__}: {exc}\n")
        finally:
            self._running.pop(task.id, None)
            self._processes.pop(task.id, None)
            task.status = status
            task.exit_code = exit_code
            task.finished_at = time.time()
            tail = writer.close()
            if not self._closed and self._sink is not None:
                with suppress(Exception):
                    self._sink(self._notice(task, tail))

    def _notice(self, task: BackgroundTask, tail: str) -> str:
        exit_code = "-" if task.exit_code is None else str(task.exit_code)
        return (
            "<task_notification>\n"
            f"  <task_id>{task.id}</task_id>\n"
            f"  <status>{task.status}</status>\n"
            f"  <exit_code>{exit_code}</exit_code>\n"
            f"  <command>{_one_line(task.command)}</command>\n"
            f"  <inspect_with>check_background(task_id={task.id})</inspect_with>\n"
            f"  <tail>{_tail(tail, NOTICE_TAIL_LINES)}</tail>\n"
            "</task_notification>"
        )

    def _describe(self, task: BackgroundTask, tail_lines: int) -> str:
        exit_code = "-" if task.exit_code is None else str(task.exit_code)
        header = (
            f"task: {task.id}\n"
            f"status: {task.status}\n"
            f"exit_code: {exit_code}\n"
            f"command: {_one_line(task.command)}\n"
            f"task_id: {task.id}"
        )
        if task.running:
            return f"{header}\n(still running)"
        try:
            text = task.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"{header}\n(cannot read log: {exc})"
        return f"{header}\n\n{_tail(text, tail_lines)}"

    def _prune_old(self) -> None:
        while len(self._order) > self._max_kept:
            oldest = self._order[0]
            task = self._tasks.get(oldest)
            if task is not None and task.running:
                break
            self._order.popleft()
            self._tasks.pop(oldest, None)
            if task is not None:
                with suppress(OSError):
                    task.log_path.unlink(missing_ok=True)


async def _stream_output(reader: Any, writer: "_LogWriter") -> None:
    """边读边写：内存里永远只有当前这一块和尾部缓冲。"""
    while True:
        chunk = await _read_chunk(reader)
        if not chunk:
            return
        writer.write(chunk.decode("utf-8", errors="replace").replace("\r\n", "\n"))


async def _read_chunk(reader: Any) -> bytes:
    """读一块输出。优先按行，单行超限时退化成定长读。

    `asyncio.StreamReader` 有 `limit`（4MB），一行超过它 `readline()` 会抛
    `ValueError`；二进制输出正好没有换行。`win32.PipeReader.readline` 不会抛，
    但它的 `read()` 不接受长度参数，所以两条分支都要留。
    """
    try:
        return await reader.readline()
    except (ValueError, asyncio.LimitOverrunError):
        try:
            return await reader.read(65536)
        except TypeError:
            return await reader.read()


class _LogWriter:
    """把输出流式写进日志文件，内存只保留头尾。

    头部（`LOG_HEAD_CHARS`）直接落盘；之后的内容只计入省略长度，并维护一个
    有界的尾部缓冲，供通知与 `check_background` 使用。这样日志文件不会超过
    头 + 尾，进程内存也不会随输出线性增长。
    """

    def __init__(
        self,
        path: Path,
        *,
        head_chars: int = LOG_HEAD_CHARS,
        tail_chars: int = LOG_TAIL_CHARS,
    ) -> None:
        self._head_left = head_chars
        self._tail_chars = tail_chars
        self._tail = ""
        self._omitted = 0
        self._file = None
        self._closed = False
        with suppress(OSError):
            path.parent.mkdir(parents=True, exist_ok=True)
            self._file = path.open("w", encoding="utf-8", errors="replace")

    def write(self, text: str) -> None:
        if not text:
            return
        remainder = text
        if self._head_left > 0:
            piece = text[: self._head_left]
            self._head_left -= len(piece)
            self._emit(piece)
            remainder = text[len(piece) :]
        self._omitted += len(remainder)
        self._tail = (self._tail + text)[-self._tail_chars :]

    def close(self) -> str:
        """收尾并返回尾部文本；重复调用返回同一份。"""
        if self._closed:
            return self._tail
        self._closed = True
        if self._file is not None:
            with suppress(OSError):
                if self._omitted:
                    self._file.write(f"\n… {self._omitted} chars omitted …\n")
                    self._file.write(self._tail)
                self._file.close()
        return self._tail

    def _emit(self, text: str) -> None:
        if self._file is None:
            return
        with suppress(OSError):
            self._file.write(text)


def _tail(text: str, lines: int) -> str:
    if not text.strip():
        return "(no output)"
    parts = text.rstrip().splitlines()
    keep = max(1, lines)
    if len(parts) > keep:
        return "\n".join(parts[-keep:])
    return "\n".join(parts)


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _short(text: str, limit: int = 80) -> str:
    flat = _one_line(text)
    return flat if len(flat) <= limit else f"{flat[: limit - 1]}…"
