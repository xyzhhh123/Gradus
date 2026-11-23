"""切片运行器：一片跑一部分题，随时可停、可续。

    python bench/run.py --run-id swe-01 --tasks bench/tasks.jsonl --minutes 45

约定（见 README）：
- 开跑写 `plan.json`（模型、数据集、题单、trials），之后不改；
- 已完成的题跳过，结果追加进 `tasks.jsonl`；
- 到截止时间或题数上限就**等当前题跑完再退**，不半路杀。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench import agent, container, exec_mode  # noqa: E402
from bench.common import (  # noqa: E402
    Task,
    append_result,
    format_report,
    load_tasks,
    read_plan,
    read_results,
    summarize,
    write_plan,
)
from gradus.coding import config as cfg  # noqa: E402


def _git_commit() -> str:
    try:
        done = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parents[1]), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return done.stdout.strip() if done.returncode == 0 else "?"
    except OSError:
        return "?"


def pending(tasks: list[Task], done: dict) -> list[Task]:
    return [task for task in tasks if task.id not in done]


def expired(deadline: float, now: float | None = None) -> bool:
    return (now if now is not None else time.monotonic()) >= deadline


def plan_for(args: argparse.Namespace, tasks: list[Task]) -> dict:
    return {
        "dataset": args.dataset,
        "model": args.model or "from-config",
        "commit": _git_commit(),
        "trials": args.trials,
        "tasks": [task.id for task in tasks],
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "sandbox": args.sandbox,
        "note": "切片运行：model/题单/config 在开跑后不再改动",
    }


def _pid_alive(pid: int) -> bool:
    """判断进程是否还活着（Windows / POSIX 都走标准库）。"""
    import os

    try:
        if os.name == "nt":
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def acquire_lock(run_dir: Path) -> int | None:
    """同一 run-id 只允许一个进程；拿到返回 pid，拿不到返回 `None`。

    崩溃留下的旧锁（pid 已死）会被接管，不用手动删文件。
    """
    import os
    import time as _time

    lock = run_dir / ".lock"
    if lock.exists():
        try:
            holder = int(lock.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            holder = 0
        if holder and holder != os.getpid() and _pid_alive(holder):
            return None
        _time.sleep(0.2)
    lock.write_text(str(os.getpid()), encoding="utf-8")
    return os.getpid()


def release_lock(run_dir: Path) -> None:
    import os

    lock = run_dir / ".lock"
    try:
        if lock.exists() and lock.read_text(encoding="utf-8").strip() == str(os.getpid()):
            lock.unlink()
    except OSError:
        pass


async def run_many(
    tasks: list[Task],
    run_one: Any,
    *,
    workers: int,
    deadline: float,
    max_tasks: int,
) -> list[Any]:
    """并发跑一批题：最多 `workers` 个同时进行，结果按完成顺序返回。

    `run_one` 是 `async (task) -> TaskResult`；容器调用在线程里跑，不阻塞事件循环。
    """
    limit = max(1, workers)
    semaphore = asyncio.Semaphore(limit)

    async def one(task: Task) -> Any:
        async with semaphore:
            if expired(deadline):
                return None
            return await run_one(task)

    targets = tasks[:max_tasks] if max_tasks else tasks
    results = await asyncio.gather(*(one(task) for task in targets))
    return [result for result in results if result]


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="跑一片评测")
    parser.add_argument("--run-id", required=True, help="这一轮的目录名，如 swe-01")
    parser.add_argument("--tasks", required=True, help="题单 JSONL")
    parser.add_argument("--dataset", default="swe-bench-verified")
    parser.add_argument("--minutes", type=float, default=45.0, help="本片最长分钟数")
    parser.add_argument("--max-tasks", type=int, default=0, help="本片最多几题（0 表示不限）")
    parser.add_argument("--timeout", type=float, default=900.0, help="单题超时秒数")
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--model", default="", help="只用于写进 plan；实际取自配置")
    parser.add_argument("--sandbox", default="local", help="评测时用 local：容器本身就是边界")
    parser.add_argument("--mode", choices=("container", "exec", "host"), default="exec", help="container=每题一次性容器；host=宿主直跑（无边界，仅调试）")
    parser.add_argument("--max-turns", type=int, default=60, help="每题最多几次模型请求")
    parser.add_argument("--workers", type=int, default=1, help="同时跑几题（每题一个容器）")
    parser.add_argument("--self-checks", type=int, default=2, help="exec 模式：失败后再给几次修复机会")
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help="允许 agent 容器联网（默认断网：评测时联网等于可以去看上游答案）",
    )
    args = parser.parse_args(argv)

    run_dir = Path(__file__).resolve().parent / "runs" / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    if acquire_lock(run_dir) is None:
        print(
            f"run {args.run_id} 已有另一个进程在跑；换个 --run-id 或等它结束。",
            file=sys.stderr,
        )
        return 3
    results_path = run_dir / "tasks.jsonl"
    plan_path = run_dir / "plan.json"

    if not Path(args.tasks).exists():
        print(
            f"题单不存在：{args.tasks}\n"
            "格式见 bench/README.md；可以先复制 bench/tasks.example.jsonl 改一改。",
            file=sys.stderr,
        )
        return 2
    tasks = load_tasks(args.tasks)
    plan = read_plan(plan_path)
    if not plan:
        plan = plan_for(args, tasks)
        write_plan(plan_path, plan)
    done = read_results(results_path)
    todo = pending(tasks, done)
    print(f"run {args.run_id}: 共 {len(tasks)} 题，已完成 {len(done)}，本片从 {len(todo)} 题里取")
    if not todo:
        print("没有待跑的题")
        release_lock(run_dir)
        return 0

    settings = cfg.load(
        overrides={"GRADUS_SANDBOX": args.sandbox},
    )
    deadline = time.monotonic() + args.minutes * 60

    async def run_one(task: Task) -> Any:
        from bench.common import TaskResult

        try:
            return await _dispatch(task)
        except Exception as exc:  # 单题崩溃不该带走整片
            return TaskResult(
                id=task.id, status="failed", failure_class=f"executor:{type(exc).__name__}"
            )

    async def _dispatch(task: Task) -> Any:
        if args.mode == "exec":
            return await exec_mode.run_task_exec(
                task,
                repo_root=Path(__file__).resolve().parents[1],
                run_dir=run_dir,
                timeout_seconds=args.timeout,
                max_turns=args.max_turns,
                checks=args.self_checks,
            )
        if args.mode == "container":
            return await asyncio.to_thread(
                container.run_task_container,
                task,
                repo_root=Path(__file__).resolve().parents[1],
                run_dir=run_dir,
                timeout_seconds=args.timeout,
                allow_network=args.allow_network,
                max_turns=args.max_turns,
            )
        return await agent.run_task(
            task, settings, run_dir=run_dir, timeout_seconds=args.timeout
        )

    try:
        for result in await run_many(
            todo,
            run_one,
            workers=args.workers,
            deadline=deadline,
            max_tasks=args.max_tasks,
        ):
            append_result(results_path, result)
            mark = "PASS" if result.resolved else ("SKIP" if result.resolved is None else "FAIL")
            print(
                f"· {result.id} {mark} status={result.status} in={result.input} "
                f"out={result.output} cache={result.cache_read} turns={result.turns} "
                f"wall={result.wall_seconds:.0f}s {result.failure_class}"
            )
    finally:
        await agent.close()

    summary = summarize(read_results(results_path).values(), len(tasks))
    print()
    print(format_report(summary, plan))
    release_lock(run_dir)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
