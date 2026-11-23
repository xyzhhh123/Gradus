"""`--mode exec`：Gradus 跑在宿主，命令 `docker exec` 进实例容器。

与 `--mode container` 的区别：
- 不需要为每题构建 agent 镜像（没有 py3.13/uv/离线 wheel 那套）；
- 实例容器只提供 `/testbed` 与 conda 环境，容器由本模块启动与删除；
- 轨迹在宿主直接写（`<task>.trajectory.jsonl`），会话文件也直接落在 `run_dir`。

流程：起容器 → 九条预检 → 跑一轮 → 自检（可关）→ 取 patch → 删容器。
"""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
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
from gradus.coding import config as cfg
from gradus.coding.session import Session

from . import container as container_mod
from .common import Task, TaskResult, write_text_lf
from .trace_writer import TraceWriter

DATASET_DIR = Path(r"H:\bench\dataset")
CONTAINER_PREFIX = "gradus-exec"


def _docker(*args: str, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _exec(container: str, command: str, *, timeout: float = 900.0) -> subprocess.CompletedProcess[str]:
    return _docker("exec", container, "bash", "-lc", command, timeout=timeout)


def _host_git(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(workspace), "-c", "core.autocrlf=false", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def git_diff(workspace: Path) -> str:
    """宿主侧取 patch（排除 `.gradus/`，统一 LF）。"""
    _host_git(workspace, "add", "-A", "-N")
    done = _host_git(workspace, "diff", "--", ".", ":(exclude).gradus/**")
    return (done.stdout if done.returncode == 0 else "").replace("\r\n", "\n")


def start_container(task: Task, workspace: Path, *, network: bool = True) -> str:
    """按题单里的官方镜像起一个一次性容器，仓库 bind mount 到 /testbed。"""
    name = f"{CONTAINER_PREFIX}-{task.id.lower().replace('__', '-')}"[:60]
    _docker("rm", "-f", name)
    if not task.image:
        raise RuntimeError(f"{task.id}: 题单里没有 image，exec 模式需要官方实例镜像")
    args = [
        "run",
        "-d",
        "--name",
        name,
        "-w",
        "/testbed",
        "-v",
        f"{workspace}:/testbed",
    ]
    if not task.meta.get("allow_network"):
        for host in container_mod.BLOCKED_HOSTS:
            args += ["--add-host", f"{host}:0.0.0.0"]
    args += [task.image, "bash", "-lc", "sleep infinity"]
    done = _docker(*args)
    if done.returncode != 0:
        raise RuntimeError(f"起容器失败: {done.stderr.strip()[-400:]}")
    # 老仓库（pylint 等）的工作区行尾可能和索引不一致：脏的时候按索引还原一次。
    # 大仓库（django）在 NTFS 挂载上 checkout 很慢，所以先看状态，必要时才做，并给足超时。
    dirty = _exec(name, "git -C /testbed status --porcelain | head -1", timeout=600.0)
    if dirty.stdout.strip():
        _exec(name, "git -C /testbed checkout -- . >/dev/null 2>&1 || true", timeout=900.0)
    setup = str(task.meta.get("setup_cmd") or "").strip()
    if setup:
        ran = _exec(name, setup, timeout=900.0)
        if ran.returncode != 0:
            raise RuntimeError(f"setup_cmd 失败: {(ran.stdout + ran.stderr).strip()[-400:]}")
    return name


def preflight(task: Task, container: str, workspace: Path, *, trace: TraceWriter) -> list[str]:
    """九条预检；返回问题列表（空表示全过）。任何一条失败都不该启动 agent。"""
    problems: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        trace.record("preflight", name=name, ok=ok, detail=detail)
        if not ok:
            problems.append(f"{name}: {detail}" if detail else name)

    running = _docker("inspect", "-f", "{{.State.Running}}", container)
    check("container_running", running.stdout.strip() == "true", running.stderr.strip()[:120])

    has_testbed = _exec(container, "test -d /testbed && echo ok")
    check("testbed_exists", "ok" in has_testbed.stdout, has_testbed.stderr.strip()[:120])

    host_status = _host_git(workspace, "status", "--porcelain")
    check("host_repo_clean", not host_status.stdout.strip(), host_status.stdout.strip()[:120])

    container_status = _exec(container, "git -C /testbed status --porcelain")
    dirty = container_status.stdout.strip()
    if dirty:
        # 有些老仓库在镜像里只差行尾（宿主 NTFS 与容器 git 的 CRLF 判断不一致）；
        # 只要差异是 EOL-only，就不算脏——补丁在宿主侧取，不受影响。
        eol_only = _exec(container, "git -C /testbed diff --ignore-cr-at-eol --quiet")
        check(
            "container_repo_clean",
            eol_only.returncode == 0,
            ("仅行尾差异" if eol_only.returncode == 0 else dirty[:120]),
        )
    else:
        check("container_repo_clean", True)

    probe = workspace / ".gradus-probe"
    try:
        probe.write_text("probe", encoding="utf-8")
        seen = _exec(container, "test -f /testbed/.gradus-probe && echo ok")
        check("mount_shared", "ok" in seen.stdout)
    finally:
        probe.unlink(missing_ok=True)

    python = _exec(
        container,
        "/opt/miniconda3/envs/testbed/bin/python -c 'import sys; print(sys.version)' 2>/dev/null "
        "|| python3 -V",
    )
    output = (python.stdout + python.stderr).strip()
    check("testbed_python", "Python" in output or "." in output[:6], output[:120])

    if not task.meta.get("allow_network"):
        blocked = _exec(
            container,
            "curl -m 5 -sSI https://github.com >/dev/null && echo open || echo blocked",
        )
        check(
            "answer_sources_blocked",
            "blocked" in blocked.stdout,
            blocked.stdout.strip()[:120],
        )

    return problems


def _build_team(session: Any, trace: TraceWriter, result: TaskResult) -> Any:
    """按开关装配多 agent 团队，并把子 agent 的用量单独记账。

    子会话的 token 不进主会话的 `usage`，如果不在这里累计，报告里就会少算。
    """
    from gradus.coding.multiagent import AgentTeam

    def on_event(agent: Any, event: Any) -> None:
        if isinstance(event, TurnEnd):
            usage = event.usage
            result.sub_input += usage.input_tokens
            result.sub_output += usage.output_tokens
            trace.record(
                "subagent_turn",
                agent=agent.name,
                input=usage.input_tokens,
                output=usage.output_tokens,
                cache_read=usage.cache_read_tokens,
            )
        elif isinstance(event, AgentError):
            trace.record(
                "subagent_error", agent=agent.name, kind=event.kind, message=event.message
            )

    team = AgentTeam(
        session.tasks,
        session.spawn,
        lead_notify=session.notify,
        on_event=on_event,
        require_plan=True,
    )
    session.attach_team(team)
    result.multiagent = True
    trace.record("multiagent", enabled=True)
    return team


DELEGATION_HINT = """

## Subagents

This session has subagents enabled. Work split rules:

- **Plan first.** Call `plan_tasks` ONCE with every node (key, subject, detailed
  briefing, blocked_by). `spawn_agent` refuses to run until the graph is planned,
  and refuses nodes whose briefing is empty.
- **Your job is decompose + integrate.** Do not do a delegated task yourself.
- Delegate only work that is independent and big enough to be worth a whole separate
  conversation (several files, or a long investigation); keep trivial edits.
- When you delegate, the task description must carry your findings — files, commands,
  what you already ruled out. The subagent cannot see this conversation.
- After `spawn_agent`, that task belongs to the subagent: stop touching its files,
  call `wait`, then integrate the result.
- Upstream results are appended to the task automatically, so order tasks by dependency.
- Talk in types: `send_message(kind="question" | "result" | "status")`. You are notified
  automatically when a subagent finishes, so you do not have to poll.

The task and subagent tools may be deferred: run `tool_search` with a query such as
"task spawn_agent wait" to load them before first use."""


def _test_patch(task: Task) -> tuple[Path, str, list[str]] | None:
    """从 dataset 元数据取 (test_patch 路径, pytest 命令)。"""
    source = DATASET_DIR / f"{task.id}.json"
    if not source.exists():
        return None
    try:
        row = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    patch = row.get("test_patch") or ""
    targets = row.get("FAIL_TO_PASS") or []
    if isinstance(targets, str):
        try:
            targets = json.loads(targets)
        except json.JSONDecodeError:
            targets = []
    files: list[str] = []
    for target in targets:
        file = target.split("::", 1)[0]
        if file not in files:
            files.append(file)
    if not patch or not files:
        return None
    # 写在 dataset 旁边而不是写死 H:\bench：后者只在作者机器上存在，
    # 也让这个函数无法在不含 H: 盘的环境（CI、容器）里测试。
    path = DATASET_DIR / f"{task.id}.exec-test_patch.diff"
    write_text_lf(path, patch)
    # 只跑 FAIL_TO_PASS 的节点：整文件跑会把环境导致的无关失败也算进来
    quoted = " ".join(f"'{target}'" for target in targets)
    return path, f"-m pytest {quoted} -q", files


def detect_test_python(container: str) -> str:
    """优先用实例镜像里的 conda testbed，退回 python3（自定义镜像用）。"""
    # 直接用 testbed 环境的解释器，别经 `conda run`：
    # 老仓库的 requests 会通过 PYTHONPATH 盖住 conda 自己的依赖（实测 conda 直接崩）。
    probe = _exec(
        container,
        "test -x /opt/miniconda3/envs/testbed/bin/python && echo env-ok",
    )
    if "env-ok" in probe.stdout:
        return "/opt/miniconda3/envs/testbed/bin/python"
    return "python3"


def self_check(
    task: Task,
    container: str,
    test_patch: Path,
    command: str,
    test_files: list[str],
    tag: str,
    python_prefix: str = "conda run -n testbed",
) -> tuple[bool, str]:
    """在容器里的副本上打 test_patch 并跑目标测试。"""
    work = f"/tmp/check-{tag}"
    copy = _exec(container, f"rm -rf {work} && cp -a /testbed {work}", timeout=300.0)
    if copy.returncode != 0:
        return False, f"复制仓库失败: {copy.stderr.strip()[:300]}"
    # 清掉 intent-to-add 索引项；把模型可能改过的测试文件还原，避免和官方 test_patch 冲突
    _exec(container, f"cd {work} && git reset -q")
    if test_files:
        _exec(container, f"cd {work} && git checkout -- " + " ".join(test_files))
    _docker("cp", str(test_patch), f"{container}:{work}/.test_patch.diff")
    applied = _exec(
        container,
        f"cd {work} && git apply -3 .test_patch.diff || git apply --reject .test_patch.diff",
    )
    if applied.returncode != 0:
        return False, f"打 test_patch 失败: {applied.stderr.strip()[:300]}"
    run = _exec(
        container,
        f"cd {work} && {python_prefix} {command}",
        timeout=900.0,
    )
    output = (run.stdout + run.stderr).strip()
    return run.returncode == 0, output[-3000:]


async def _approve_all(question: str) -> bool:
    return True


async def run_task_exec(
    task: Task,
    *,
    repo_root: Path,
    run_dir: Path,
    timeout_seconds: float = 1200.0,
    max_turns: int = 100,
    checks: int = 2,
    keep_container: bool = False,
) -> TaskResult:
    """exec 模式跑一题：起容器 → 预检 → 跑 → 自检 → 取 patch → 删容器。"""
    workspace = Path(task.workspace).expanduser().resolve()
    result = TaskResult(id=task.id, attempts=1)
    if not workspace.is_dir():
        result.status = "failed"
        result.failure_class = "workspace缺失"
        return result

    out_dir = run_dir / "container" / task.id
    out_dir.mkdir(parents=True, exist_ok=True)
    trace_path = out_dir / f"{task.id}.trajectory.jsonl"
    started = time.monotonic()

    try:
        container = start_container(task, workspace)
    except Exception as exc:
        # 起容器过程中失败（例如 checkout 超时）：容器可能已经建出来了，必须收尾。
        name = f"{CONTAINER_PREFIX}-{task.id.lower().replace('__', '-')}"[:60]
        _docker("rm", "-f", name)
        result.status = "failed"
        result.failure_class = f"container失败:{type(exc).__name__}"
        result.wall_seconds = time.monotonic() - started
        return result

    print(f"  [{task.id}] 容器 {container} 已启动", flush=True)
    error: AgentError | None = None
    team = None
    try:
        with TraceWriter(trace_path) as trace:
            trace.record("start", task=task.id, workspace=str(workspace), mode="exec")
            python_prefix = detect_test_python(container)
            trace.record("test_python", prefix=python_prefix)
            problems = preflight(task, container, workspace, trace=trace)
            if problems:
                result.status = "failed"
                result.failure_class = "预检失败:" + ";".join(problems)[:120]
                trace.record("preflight_failed", problems=problems)
                return result

            settings = cfg.load(
                overrides={
                    "GRADUS_SANDBOX": "exec",
                    "GRADUS_SANDBOX_CONTAINER": container,
                    "GRADUS_SANDBOX_CONTAINER_WORKSPACE": "/testbed",
                    "GRADUS_MAX_TURNS": str(max_turns),
                }
            )
            one = await Session.connect(
                settings,
                workspace=workspace,
                session_dir=run_dir / "sessions" / task.id,
                confirm=_approve_all,
                interactive=False,
            )
            team = _build_team(one, trace, result)

            async def consume(prompt: str) -> None:
                nonlocal error
                async for event in one.prompt(prompt):
                    if isinstance(event, TurnStart):
                        result.turns += 1
                        trace.record("turn_start", turn=event.turn)
                    elif isinstance(event, ToolExecutionStart):
                        result.tool_calls += 1
                        if event.call.name == "spawn_agent":
                            result.subagents += 1
                        trace.record(
                            "tool_call",
                            name=event.call.name,
                            args=str(event.call.arguments)[:1500],
                        )
                    elif isinstance(event, ToolExecutionEnd):
                        if event.result.is_error:
                            result.tool_errors += 1
                        trace.record(
                            "tool_result",
                            name=event.call.name,
                            is_error=event.result.is_error,
                            duration_ms=event.duration_ms,
                            content=event.result.content[:1500],
                        )
                    elif isinstance(event, TurnEnd):
                        usage = event.usage
                        result.input += usage.input_tokens
                        result.output += usage.output_tokens
                        result.cache_read += usage.cache_read_tokens
                        result.cache_write += usage.cache_write_tokens
                        trace.record(
                            "turn_end",
                            usage={
                                "input": usage.input_tokens,
                                "output": usage.output_tokens,
                                "cache_read": usage.cache_read_tokens,
                            },
                        )
                    elif isinstance(event, AgentError):
                        error = event
                        trace.record("error", kind=event.kind, message=event.message)

            prompt = task.instruction + "\n\n" + container_mod.TEST_HINT
            prompt += DELEGATION_HINT
            if os.environ.get("GRADUS_MA_FORCE") == "1":
                prompt += (
                        "\n\nFor this run you MUST delegate: create a task with the task "
                        "tools, call spawn_agent on it, and wait for its result before you "
                        "finish your own answer."
                    )
            check_spec = _test_patch(task)
            try:
                for attempt in range(checks + 1):
                    try:
                        await asyncio.wait_for(consume(prompt), timeout=timeout_seconds)
                    except asyncio.TimeoutError:
                        result.status = "timeout"
                        result.failure_class = "超时"
                        await one.abort()
                        break
                    if error is not None or check_spec is None:
                        break
                    if not git_diff(workspace).strip():
                        break
                    result.checks = attempt + 1
                    passed, tail = self_check(
                        task, container, *check_spec, str(attempt), python_prefix
                    )
                    result.self_check = "pass" if passed else "fail"
                    trace.record(
                        "self_check", attempt=attempt + 1, passed=passed, tail=tail[-1200:]
                    )
                    if passed or attempt >= checks:
                        break
                    prompt = (
                        "目标测试没有通过，输出如下：\n\n"
                        f"{tail}\n\n请继续修复到测试通过；不要修改测试文件本身。"
                    )
            finally:
                if team is not None:
                    with suppress(Exception):
                        await team.aclose()
                session_path = one.session_path
                await one.aclose()
                from gradus.coding.session import close_pool

                await close_pool()
            if session_path is not None:
                text = Path(session_path).read_text(encoding="utf-8").splitlines()
                result.compactions = sum('"type": "compaction"' in line for line in text)
                result.trimmed = sum('"type": "trim"' in line for line in text)

            patch = git_diff(workspace)
            write_text_lf(out_dir / f"{task.id}.diff", patch)
            if not patch.strip():
                result.failure_class = result.failure_class or "patch空"
            trace.close(status=result.status, checks=result.checks, self_check=result.self_check)
    except Exception as exc:
        result.status = "failed"
        result.failure_class = f"executor:{type(exc).__name__}"
        write_text_lf(out_dir / "error.txt", f"{type(exc).__name__}: {exc}")
    finally:
        result.wall_seconds = time.monotonic() - started
        if error is not None and result.status == "done":
            result.status = "failed"
            result.failure_class = f"agent:{error.kind}"
        patches = run_dir / "patches"
        patches.mkdir(parents=True, exist_ok=True)
        source = out_dir / f"{task.id}.diff"
        if source.exists():
            write_text_lf(patches / f"{task.id}.diff", source.read_text(encoding="utf-8"))
        write_text_lf(out_dir / f"{task.id}.json", json.dumps(result.to_json(), ensure_ascii=False))
        if not keep_container:
            _docker("rm", "-f", container)
    return result
