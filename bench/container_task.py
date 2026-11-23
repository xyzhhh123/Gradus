"""容器内跑一题：读指令、跑 Gradus、自检、把 patch 与用量写到 /output。

由 `bench/container.py` 在容器里调用；不联网判定，只负责产出：
- `/output/<id>.diff`：`git diff`（排除 `.gradus/`）
- `/output/<id>.json`：用量、轮次、自检结果

自检（P4）：如果有官方 `test_patch` 与目标测试命令，就在仓库副本里打上 test_patch
跑一次；失败就把输出喂回模型再修一轮（最多 `--max-checks` 次）。
这样模型不再"改完就走"，而是在收尾前真的知道自己有没有过。

容器本身就是边界，所以这里用 `sandbox: local`——本机的受限令牌在 Linux 上不存在。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from gradus.agent.types import (
    AgentError,
    ToolExecutionEnd,
    ToolExecutionStart,
    TurnEnd,
    TurnStart,
)
from gradus.coding import config as cfg
from gradus.coding.session import Session, close_pool


def git_diff(workspace: Path) -> str:
    """取改动（含新增文件），排除 `.gradus/`；统一 LF，跨平台可直接 git apply。"""
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
    return (done.stdout if done.returncode == 0 else "").replace("\r\n", "\n")


def count_records(session_path: Path | None) -> tuple[int, int]:
    if session_path is None or not Path(session_path).exists():
        return 0, 0
    compactions = trimmed = 0
    for line in Path(session_path).read_text(encoding="utf-8").splitlines():
        if '"type": "compaction"' in line:
            compactions += 1
        elif '"type": "trim"' in line:
            trimmed += 1
    return compactions, trimmed


async def _approve_all(question: str) -> bool:
    """容器是一次性的，所有确认都放行。"""
    return True


def run_tests(
    workspace: Path, test_patch: Path, command: str, tag: str
) -> tuple[bool, str]:
    """在仓库副本里打 test_patch 并跑目标测试；返回 (是否通过, 输出尾部)。"""
    copy = Path("/tmp") / f"check-{tag}"
    shutil.rmtree(copy, ignore_errors=True)
    shutil.copytree(workspace, copy, symlinks=True)

    applied = subprocess.run(
        ["git", "-C", str(copy), "apply", "-3", str(test_patch)],
        capture_output=True,
        text=True,
        errors="replace",
    )
    if applied.returncode != 0:
        subprocess.run(
            ["git", "-C", str(copy), "apply", "--reject", str(test_patch)],
            capture_output=True,
            text=True,
            errors="replace",
        )

    env = dict(os.environ)
    env["PYTHONPATH"] = f"{copy}:{copy / 'src'}"
    try:
        done = subprocess.run(
            ["conda", "run", "-n", "testbed", "bash", "-lc", command],
            cwd=str(copy),
            capture_output=True,
            text=True,
            errors="replace",
            env=env,
            timeout=900,
        )
    except subprocess.TimeoutExpired:
        return False, "测试超时（900s）"
    output = (done.stdout + done.stderr).strip()
    return done.returncode == 0, output[-3000:]


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instruction-file", required=True)
    parser.add_argument("--workspace", default="/workspace")
    parser.add_argument("--output-dir", default="/output")
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--test-patch", default="", help="官方 test_patch（自检用）")
    parser.add_argument("--test-command", default="", help="目标测试命令")
    parser.add_argument("--max-checks", type=int, default=0, help="失败后再给几次修复机会")
    args = parser.parse_args(argv)

    instruction = Path(args.instruction_file).read_text(encoding="utf-8").strip()
    workspace = Path(args.workspace).resolve()
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)

    result: dict[str, object] = {
        "id": args.task_id,
        "status": "done",
        "turns": 0,
        "tool_calls": 0,
        "tool_errors": 0,
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_write": 0,
        "failure_class": "",
        "checks": 0,
        "self_check": "",
    }
    error: AgentError | None = None

    settings = cfg.load(overrides={"GRADUS_SANDBOX": "local"})
    one = await Session.connect(
        settings,
        workspace=workspace,
        session_dir=output / "sessions",
        confirm=_approve_all,
        interactive=False,
    )

    trace = (output / f"{args.task_id}.trajectory.jsonl").open("a", encoding="utf-8")

    def record(kind: str, **fields: object) -> None:
        """逐事件落盘：即使中途超时被杀，已经发生的步骤也留得下来。"""
        payload = {"type": kind, **fields}
        trace.write(json.dumps(payload, ensure_ascii=False) + "\n")
        trace.flush()

    record("start", task=args.task_id, instruction=instruction[:400], workspace=str(workspace))

    async def consume(prompt: str) -> None:
        nonlocal error
        async for event in one.prompt(prompt):
            if isinstance(event, TurnStart):
                result["turns"] = int(result["turns"]) + 1
                record("turn_start", turn=event.turn)
            elif isinstance(event, ToolExecutionStart):
                result["tool_calls"] = int(result["tool_calls"]) + 1
                record(
                    "tool_call",
                    turn=result["turns"],
                    name=event.call.name,
                    args=str(event.call.arguments)[:1500],
                )
            elif isinstance(event, ToolExecutionEnd):
                if event.result.is_error:
                    result["tool_errors"] = int(result["tool_errors"]) + 1
                record(
                    "tool_result",
                    name=event.call.name,
                    is_error=event.result.is_error,
                    duration_ms=event.duration_ms,
                    content=event.result.content[:1500],
                )
            elif isinstance(event, TurnEnd):
                usage = event.usage
                result["input"] = int(result["input"]) + usage.input_tokens
                result["output"] = int(result["output"]) + usage.output_tokens
                result["cache_read"] = int(result["cache_read"]) + usage.cache_read_tokens
                result["cache_write"] = int(result["cache_write"]) + usage.cache_write_tokens
                record(
                    "turn_end",
                    usage={
                        "input": usage.input_tokens,
                        "output": usage.output_tokens,
                        "cache_read": usage.cache_read_tokens,
                    },
                )
            elif isinstance(event, AgentError):
                error = event
                record("error", kind=event.kind, message=event.message)

    try:
        prompt = instruction
        for attempt in range(args.max_checks + 1):
            try:
                await asyncio.wait_for(consume(prompt), timeout=args.timeout)
            except asyncio.TimeoutError:
                result["status"] = "timeout"
                await one.abort()
                break

            if error is not None or not args.test_patch or not args.test_command:
                break
            if not git_diff(workspace).strip():
                break

            result["checks"] = attempt + 1
            passed, tail = run_tests(
                workspace, Path(args.test_patch), args.test_command, str(attempt)
            )
            result["self_check"] = "pass" if passed else "fail"
            if passed or attempt >= args.max_checks:
                break
            prompt = (
                "目标测试没有通过，输出如下：\n\n"
                f"{tail}\n\n"
                "请继续修复到测试通过；不要修改测试文件本身。"
            )
    finally:
        session_path = one.session_path
        compactions, trimmed = count_records(
            Path(session_path) if session_path else None
        )
        result["compactions"] = compactions
        result["trimmed"] = trimmed
        await one.aclose()
        await close_pool()
        trace.write(json.dumps({"type": "end", **result}, ensure_ascii=False) + "\n")
        trace.close()

    if error is not None:
        result["status"] = "failed"
        result["failure_class"] = f"agent:{error.kind}"

    patch = git_diff(workspace)
    (output / f"{args.task_id}.diff").write_text(patch, encoding="utf-8")
    if not patch.strip():
        result["failure_class"] = result["failure_class"] or "patch空"
    (output / f"{args.task_id}.json").write_text(
        json.dumps(result, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
