"""主机侧的容器执行器：每题一个一次性容器，边界由容器提供。

流程：镜像（`bench/Dockerfile`）里预装 Gradus → 仓库 bind mount 到 `/workspace`
→ 容器内跑 `bench/container_task.py` → patch 与指标从 `/output` 取回。

不依赖 SWE-bench 的实例镜像：agent 只需要读改文件、跑 git/grep，不需要仓库的测试环境；
判定仍交给官方 harness（在它自己的实例镜像里打 patch 跑测试）。
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

from .common import Task, TaskResult, write_text_lf

IMAGE = "gradus-agent:latest"
CONTAINER_NAME = "gradus-eval"
STAGE = Path(r"H:\bench\build-context")

#: 评测时挡掉的「答案与依赖来源」：github / pypi / HF。
#: 不能直接 `--network none`——模型 API 本身也要走网络，全断会让 agent 连模型都调不通。
BLOCKED_HOSTS = (
    "github.com",
    "api.github.com",
    "codeload.github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
    "pypi.org",
    "files.pythonhosted.org",
    "huggingface.co",
)


def _docker(*args: str, timeout: float = 600.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def image_exists(name: str = IMAGE) -> bool:
    done = _docker("images", "-q", name)
    return bool(done.stdout.strip())


def stage_context(repo_root: Path, tag: str) -> Path:
    """把源码同步到**每个镜像独立的**构建目录。

    之前所有构建共用一个目录：两个 worker 同时 build 时，一个在 robocopy、
    另一个正被 docker 读取，两个 buildx 直接卡死（实测卡了十几分钟）。
    """
    stage = STAGE / tag.replace(":", "-").replace("/", "-")
    stage.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "robocopy",
            str(repo_root),
            str(stage),
            "/E",
            "/NFL",
            "/NDL",
            "/NJH",
            "/NJS",
            "/XD",
            ".git",
            ".venv",
            "node_modules",
            "runs",
            "__pycache__",
            ".pytest_cache",
            ".ruff_cache",
            "dist",
            "docs",
            "/XF",
            "*.pyc",
        ],
        capture_output=True,
        text=True,
        errors="replace",
    )
    wheels = Path(r"H:\bench\wheels")
    if wheels.is_dir():
        subprocess.run(
            ["robocopy", str(wheels), str(stage / "wheels"), "/E", "/NFL", "/NDL", "/NJH", "/NJS"],
            capture_output=True,
            text=True,
            errors="replace",
        )
    for name in ("Dockerfile", "Dockerfile.agent-instance"):
        source = repo_root / "bench" / name
        if source.exists():
            (stage / name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return stage


_BUILD_LOCK = threading.Lock()


def build_instance_image(task: Task, repo_root: Path) -> str:
    """用实例镜像当基底，造一个「装了 Gradus（py3.13）+ 仓库依赖」的镜像。"""
    tag = f"gradus-agent:{task.id.lower()}"
    if image_exists(tag):
        return tag
    if not task.image:
        return IMAGE
    # 构建串行化：docker build 会吃满 CPU/磁盘，同时两个还会互相踩缓存
    with _BUILD_LOCK:
        if image_exists(tag):
            return tag
        context = stage_context(repo_root, tag)
        done = _docker(
            "build",
            "-f",
            str(context / "Dockerfile.agent-instance"),
            "--build-arg",
            f"BASE={task.image}",
            "-t",
            tag,
            str(context),
            timeout=3600.0,
        )
    if done.returncode != 0:
        raise RuntimeError(f"构实例镜像失败 {task.id}:\n{done.stdout[-1500:]}\n{done.stderr[-1500:]}")
    return tag


def build_image(repo_root: Path) -> None:
    """构建 agent 盒子；已存在就跳过（`bench/Dockerfile` 变了才需要重建）。"""
    if image_exists():
        return
    done = _docker(
        "build",
        "-f",
        str(repo_root / "bench" / "Dockerfile"),
        "-t",
        IMAGE,
        str(repo_root),
        timeout=1800.0,
    )
    if done.returncode != 0:
        raise RuntimeError(f"构镜像失败:\n{done.stdout[-2000:]}\n{done.stderr[-2000:]}")


def _heartbeat(task_id: str, out_dir: Path, started: float) -> None:
    """从容器写回的会话文件里读进度：第几轮、累计输入、缓存命中。"""
    sessions = list((out_dir / "sessions").glob("*.jsonl"))
    if not sessions:
        return
    try:
        records = [
            json.loads(line)
            for line in sessions[0].read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip()
        ]
    except OSError:
        return
    usages = [record for record in records if record.get("type") == "usage"]
    if not usages:
        return
    total_in = sum(int(item.get("input") or 0) for item in usages)
    cached = sum(int(item.get("cacheRead") or 0) for item in usages)
    ratio = (cached / total_in * 100) if total_in else 0.0
    elapsed = time.monotonic() - started
    print(
        f"  [{task_id}] 第 {len(usages)} 轮 · 输入 {total_in:,} · 缓存 {ratio:.0f}% "
        f"· 已用 {elapsed / 60:.1f} 分钟",
        flush=True,
    )


TEST_HINT = (
    "\n\n环境说明（重要）：你的工作区就是 `/testbed`（仓库根目录）。"
    "仓库依赖已预装在 conda 环境 `testbed` 里，包是 editable 安装、指向 `/testbed`，"
    "所以改完直接 `conda run -n testbed python -m pytest <路径> -q` 就会测到你改的代码，"
    "**不需要把文件复制到别处**。"
    "容器**不能访问外网**（github / pypi / huggingface 都被挡了），不要 pip install，也不要查上游仓库。"
)


DATASET_DIR = Path(r"H:\bench\dataset")


def test_command(instance_id: str) -> tuple[Path, str] | None:
    """从 dataset 元数据里取出 (test_patch 路径, pytest 命令)；没有就返回 None。"""
    source = DATASET_DIR / f"{instance_id}.json"
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
    patch_path = Path(r"H:\bench") / f"{instance_id}.test_patch.diff"
    patch_path.write_text(patch, encoding="utf-8")
    return patch_path, "python -m pytest " + " ".join(files) + " -q"


def run_task_container(
    task: Task,
    *,
    repo_root: Path,
    run_dir: Path,
    timeout_seconds: float = 900.0,
    config_dir: Path | None = None,
    allow_network: bool = False,
    max_turns: int = 60,
) -> TaskResult:
    """在一次性容器里跑一题；异常收敛成 `TaskResult`。"""
    workspace = Path(task.workspace).expanduser().resolve()
    result = TaskResult(id=task.id, attempts=1)
    if not workspace.is_dir():
        result.status = "failed"
        result.failure_class = "workspace缺失"
        return result

    image = build_instance_image(task, repo_root)
    python = "/gradus-env/bin/python" if image != IMAGE else "python"

    out_dir = run_dir / "container" / task.id
    out_dir.mkdir(parents=True, exist_ok=True)
    patches = run_dir / "patches"
    patches.mkdir(parents=True, exist_ok=True)
    instruction = out_dir / "instruction.txt"
    body = task.instruction + (TEST_HINT if image != IMAGE else "")
    write_text_lf(instruction, body)

    check = test_command(task.id)
    check_args: list[str] = []
    if check is not None:
        patch_path, command = check
        write_text_lf(out_dir / "test_patch.diff", patch_path.read_text(encoding="utf-8"))
        check_args = [
            "--test-patch",
            "/output/test_patch.diff",
            "--test-command",
            command,
            "--max-checks",
            "2",
        ]

    config_dir = config_dir or Path.home() / ".gradus"
    name = f"{CONTAINER_NAME}-{task.id}"[:60].replace("__", "-")
    _docker("rm", "-f", name)

    started = time.monotonic()
    # 用 -d 起容器：只有这样才能边跑边看（日志与体积都在容器里），跑完再取出来删掉。
    network: list[str] = []
    if not allow_network:
        for host in BLOCKED_HOSTS:
            network += ["--add-host", f"{host}:0.0.0.0"]
    launch = _docker(
        "run",
        "-d",
        "--name",
        name,
        *network,
        "-w",
        "/testbed",
        "-v",
        f"{workspace}:/testbed",
        "-v",
        f"{out_dir}:/output",
        "-v",
        f"{config_dir}:/root/.gradus:ro",
        "-e",
        f"GRADUS_MAX_TURNS={max_turns}",
        image,
        python,
        "/gradus/bench/container_task.py",
        "--task-id",
        task.id,
        "--workspace",
        "/testbed",
        "--instruction-file",
        "/output/instruction.txt",
        "--timeout",
        str(timeout_seconds),
        *check_args,
        timeout=120.0,
    )
    if launch.returncode != 0:
        result.status = "failed"
        result.failure_class = "容器未启动"
        result.wall_seconds = time.monotonic() - started
        return result

    print(f"  [{task.id}] 容器 {name} 已启动", flush=True)
    deadline = started + timeout_seconds + 300.0
    while time.monotonic() < deadline:
        state = _docker("inspect", "-f", "{{.State.Running}}", name, timeout=30.0)
        if state.stdout.strip() != "true":
            break
        _heartbeat(task.id, out_dir, started)
        time.sleep(20.0)
    else:
        _docker("kill", name)
        result.status = "timeout"
        result.failure_class = "容器超时"

    result.wall_seconds = time.monotonic() - started
    logs = _docker("logs", name, timeout=120.0)
    done = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=logs.stdout, stderr=logs.stderr
    )
    _docker("rm", "-f", name, timeout=60.0)

    payload: dict[str, object] = {}
    for line in reversed(done.stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
                break
            except json.JSONDecodeError:
                continue

    if not payload:
        result.status = "failed"
        result.failure_class = "container失败"
        tail = (done.stderr or done.stdout).strip().splitlines()[-3:]
        if tail:
            write_text_lf(out_dir / "error.txt", "\n".join(tail))
        return result

    result.status = str(payload.get("status") or "done")
    result.turns = int(payload.get("turns") or 0)
    result.tool_calls = int(payload.get("tool_calls") or 0)
    result.tool_errors = int(payload.get("tool_errors") or 0)
    result.input = int(payload.get("input") or 0)
    result.output = int(payload.get("output") or 0)
    result.cache_read = int(payload.get("cache_read") or 0)
    result.cache_write = int(payload.get("cache_write") or 0)
    result.compactions = int(payload.get("compactions") or 0)
    result.trimmed = int(payload.get("trimmed") or 0)
    result.failure_class = str(payload.get("failure_class") or "")

    patch_path = out_dir / f"{task.id}.diff"
    if patch_path.exists():
        (patches / f"{task.id}.diff").write_text(
            patch_path.read_text(encoding="utf-8"), encoding="utf-8"
        )

    if result.status == "done" and task.test_cmd:
        # 容器里没有仓库依赖，跑不了测试；判定交给官方 harness。
        result.resolved = None
    return result
