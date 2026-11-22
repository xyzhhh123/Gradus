"""一道题的执行器：在仓库副本里跑一轮 `gradus`，产出 patch 与各项指标。

只依赖 `coding` 层的公开接口（`Session.connect` 与事件流），不碰网络判定——
「这道题过没过」由 SWE-bench 官方 harness 判，我们只负责交出 patch。
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path

from gradus.agent.types import (
    AgentError,
    ToolExecutionEnd,
    ToolExecutionStart,
    TurnEnd,
    TurnStart,
)
from gradus.coding.config import Settings
from gradus.coding.session import Session, close_pool

from .common import Task, TaskResult


def _git_diff(workspace: Path) -> str:
    """取工作区里的改动（含新增文件），排除 `.gradus/`。

    `.gradus/` 放的是 agent 自己的任务图与会话数据——评测只看源码改动，
    不然它会把「agent 做了什么」混进提交给官方判定的 patch 里。
    """
    subprocess.run(
        ["git", "-C", str(workspace), "add", "-A", "-N"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    done = subprocess.run(
        [
            "git",
            "-C",
            str(workspace),
            "-c",
            "core.autocrlf=false",
            "diff",
            "--",
            ".",
            ":(exclude).gradus/**",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return to_lf(done.stdout) if done.returncode == 0 else ""


def to_lf(patch: str) -> str:
    """统一成 LF。

    Windows 上 `git diff` 的输出可能带 CRLF，Linux 容器里 `git apply` 会直接拒绝——
    这道转换是「本地跑 / 容器判」之间必须的桥。
    """
    return patch.replace("\r\n", "\n")


def _count_records(session_path: Path | None) -> tuple[int, int]:
    """数会话文件里的 compaction / trim 记录条数。"""
    if session_path is None or not Path(session_path).exists():
        return 0, 0
    compactions = trimmed = 0
    for line in Path(session_path).read_text(encoding="utf-8").splitlines():
        if '"type": "compaction"' in line:
            compactions += 1
        elif '"type": "trim"' in line:
            trimmed += 1
    return compactions, trimmed


def _run_tests(workspace: Path, test_cmd: str) -> bool | None:
    """跑题目自带的判定命令；没给命令就不判定。"""
    if not test_cmd:
        return None
    done = subprocess.run(
        test_cmd,
        shell=True,
        cwd=str(workspace),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return done.returncode == 0


async def _approve_all(question: str) -> bool:
    """评测用：工作区是一次性副本，所有需要确认的操作都放行。

    非交互会话如果不给 `confirm`，需要确认的工具会被**默认拒绝**——
    第一次跑真机题时就是这样：powershell / edit 全被挡，模型在原地打转 60 轮。
    """
    return True


def _failure_class(error: AgentError | None, result: TaskResult, patch: str) -> str:
    if result.status == "timeout":
        return "超时"
    if error is not None:
        return f"agent:{error.kind}"
    if not patch.strip():
        return "patch空"
    if result.resolved is False:
        return "测试未过"
    return ""


async def run_task(
    task: Task,
    settings: Settings,
    *,
    run_dir: Path,
    timeout_seconds: float = 900.0,
) -> TaskResult:
    """跑一题；异常与超时都收敛成 `TaskResult`，绝不把异常抛给调度器。"""
    workspace = Path(task.workspace).expanduser().resolve()
    started = time.monotonic()
    result = TaskResult(id=task.id, attempts=1)
    sessions = run_dir / "sessions" / task.id
    error: AgentError | None = None

    if not workspace.is_dir():
        # 题单里的路径没准备好：记一条失败，别让整片崩掉。
        result.status = "failed"
        result.failure_class = "workspace缺失"
        return result

    patch = ""
    try:
        one = await Session.connect(
            settings,
            workspace=workspace,
            session_dir=sessions,
            confirm=_approve_all,
            interactive=False,
        )
    except Exception as exc:  # 配置/环境问题：记失败，别让整片崩掉
        result.status = "failed"
        result.failure_class = f"connect:{type(exc).__name__}"
        return result

    try:

        async def consume() -> None:
            nonlocal error
            async for event in one.prompt(task.instruction):
                if isinstance(event, TurnStart):
                    result.turns += 1
                elif isinstance(event, ToolExecutionStart):
                    result.tool_calls += 1
                elif isinstance(event, ToolExecutionEnd):
                    if event.result.is_error:
                        result.tool_errors += 1
                elif isinstance(event, TurnEnd):
                    usage = event.usage
                    result.input += usage.input_tokens
                    result.output += usage.output_tokens
                    result.cache_read += usage.cache_read_tokens
                    result.cache_write += usage.cache_write_tokens
                elif isinstance(event, AgentError):
                    error = event

        try:
            await asyncio.wait_for(consume(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            result.status = "timeout"
            await one.abort()
    except Exception as exc:  # 执行器本身出错也要留下记录，而不是让整片崩掉
        result.status = "failed"
        result.failure_class = f"executor:{type(exc).__name__}"
    finally:
        result.wall_seconds = time.monotonic() - started
        try:
            patch = _git_diff(workspace)
        finally:
            session_path = one.session_path
            compactions, trimmed = _count_records(
                Path(session_path) if session_path else None
            )
            result.compactions = compactions
            result.trimmed = trimmed
            await one.aclose()

    patches = run_dir / "patches"
    patches.mkdir(parents=True, exist_ok=True)
    (patches / f"{task.id}.diff").write_text(patch, encoding="utf-8")

    if result.status == "done":
        result.resolved = _run_tests(workspace, task.test_cmd)
        result.failure_class = _failure_class(error, result, patch)
        if error is not None and result.status == "done":
            result.status = "failed"
    return result


async def close() -> None:
    await close_pool()


def dumps(result: TaskResult) -> str:
    return json.dumps(result.to_json(), ensure_ascii=False)
