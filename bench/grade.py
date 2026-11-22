"""批量判定 + 实时进度：把官方 harness 的黑盒摊开。

    python bench/grade.py --run-id slice-01 --tasks-file bench/tasks.jsonl
    python bench/grade.py --predictions H:/bench/preds.jsonl -i id1 id2

做三件事：
1. 从 `bench/runs/<run-id>/patches/` 收集补丁（自动剔除测试文件，官方会自己打 test_patch）；
2. 在 Linux 容器里跑官方 harness（Windows 直接跑会写出 CRLF 的 eval.sh）；
3. **边跑边打日志**：构建镜像 / 跑测试 / 判定结果，逐行带实例前缀，不再干等。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.stdout.reconfigure(errors="replace")

LOGS_ROOT = Path(r"H:\bench\logs")
BASELINE_DIR = Path(r"H:\bench\baselines")
TEST_PATH = re.compile(r"(^|/)tests?/|(^|/)test_[^/]*\.py$")
INTERESTING = re.compile(
    r"Building|build image|Running|Grading|report:|resolved|FAILED|ERROR|successfully applied|Test Exit Code",
    re.IGNORECASE,
)


def collect_patches(run_dir: Path) -> list[dict]:
    """从一次运行里收集预测；测试文件剔除（官方 test_patch 会覆盖）。"""
    items = []
    for patch_path in sorted((run_dir / "patches").glob("*.diff")):
        instance = patch_path.stem
        raw = patch_path.read_text(encoding="utf-8").replace("\r\n", "\n")
        keep = []
        for part in raw.split("diff --git "):
            if not part.strip():
                continue
            path = part.split(" ", 1)[0]
            name = path[2:] if path.startswith("a/") else path
            if TEST_PATH.search(name):
                continue
            keep.append("diff --git " + part)
        payload = "".join(keep)
        if not payload.strip():
            print(f"  [跳过] {instance}: 补丁只改了测试文件")
            continue
        items.append(
            {
                "instance_id": instance,
                "model_name_or_path": "gradus+deepseek-v4.1-flash",
                "model_patch": payload,
            }
        )
    return items


def tail_logs(root: Path, stop: threading.Event, seen: dict[str, int]) -> None:
    """跟着 harness 的 run_instance.log 打增量。"""
    while not stop.is_set():
        for log in root.rglob("run_instance.log"):
            instance = log.parent.name
            try:
                lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            start = seen.get(str(log), 0)
            for line in lines[start:]:
                if INTERESTING.search(line):
                    print(f"  [{instance}] {line.strip()[:160]}", flush=True)
            seen[str(log)] = len(lines)
        stop.wait(3.0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="批量判定（带实时日志）")
    parser.add_argument("--run-id", help="bench/runs 下的一次运行")
    parser.add_argument("--predictions", help="已有的 predictions.jsonl")
    parser.add_argument("-i", "--instances", nargs="*", default=[], help="限定实例")
    parser.add_argument("--run-label", default="", help="harness 的 run id（默认自动）")
    parser.add_argument("--max-workers", type=int, default=2)
    parser.add_argument(
        "--with-gold",
        action="store_true",
        help="先跑 gold 基线：gold 也挂的测试记为环境问题，不算模型回归",
    )
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[1]
    if args.run_id:
        run_dir = root / "bench" / "runs" / args.run_id
        predictions = collect_patches(run_dir)
        label = args.run_label or f"grade-{args.run_id}"
    elif args.predictions:
        predictions = [
            json.loads(line)
            for line in Path(args.predictions).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        label = args.run_label or "grade-manual"
    else:
        print("需要 --run-id 或 --predictions", file=sys.stderr)
        return 2

    if args.instances:
        wanted = set(args.instances)
        predictions = [item for item in predictions if item["instance_id"] in wanted]
    if not predictions:
        print("没有可判定的补丁", file=sys.stderr)
        return 2

    pred_path = Path(r"H:\bench") / f"preds-{label}.jsonl"
    pred_path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in predictions) + "\n",
        encoding="utf-8",
    )
    ids = [item["instance_id"] for item in predictions]
    print(f"判定 {len(ids)} 题：{', '.join(ids)}", flush=True)
    print(f"预测文件：{pred_path}", flush=True)

    gold_failures: dict[str, set[str]] = {}
    if args.with_gold:
        gold_failures = ensure_baseline(ids, args.max_workers, args.timeout)
        for instance, failed in gold_failures.items():
            if failed:
                print(f"  [gold] {instance}: {len(failed)} 个测试在基准环境里本来就挂", flush=True)

    log_root = LOGS_ROOT / "run_evaluation" / label
    stop = threading.Event()
    seen: dict[str, int] = {}
    watcher = threading.Thread(target=tail_logs, args=(log_root, stop, seen), daemon=True)
    watcher.start()

    command = (
        "pip install -q swebench 2>&1 | tail -1 && "
        "python -m swebench.harness.run_evaluation "
        "-d SWE-bench/SWE-bench_Verified -s test "
        f"-p /bench/{pred_path.name} -id {label} "
        f"--max_workers {args.max_workers} --timeout {args.timeout}"
    )
    if args.instances:
        command += " -i " + " ".join(args.instances)
    else:
        command += " -i " + " ".join(ids)

    started = time.monotonic()
    process = subprocess.Popen(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            "H:\\bench:/bench",
            "-v",
            "/var/run/docker.sock:/var/run/docker.sock",
            "-w",
            "/bench",
            "python:3.11",
            "bash",
            "-lc",
            command,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert process.stdout is not None
    for line in process.stdout:
        line = line.rstrip()
        if line and not line.startswith("WARNING"):
            print(f"· {line[:200]}", flush=True)
    process.wait()
    stop.set()
    watcher.join(timeout=5)

    elapsed = time.monotonic() - started
    print()
    print(f"harness 退出码 {process.returncode}，用时 {elapsed / 60:.1f} 分钟")
    cleanup_containers()
    return report(log_root, ids, gold_failures)


def _read_reports(log_root: Path, ids: list[str]) -> dict[str, dict]:
    """按实例收集 report.json 的内容。"""
    found: dict[str, dict] = {}
    for instance in ids:
        paths = list(log_root.rglob(f"{instance}/report.json"))
        if not paths:
            continue
        try:
            payload = json.loads(paths[0].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        found[instance] = payload.get(instance) or next(iter(payload.values()))
    return found


def _failed_tests(data: dict) -> set[str]:
    tests = data.get("tests_status") or {}
    failed: set[str] = set()
    for bucket in ("PASS_TO_PASS", "FAIL_TO_PASS"):
        failed.update((tests.get(bucket) or {}).get("failure") or [])
    return failed


def ensure_baseline(ids: list[str], workers: int, timeout: int) -> dict[str, set[str]]:
    """每个实例跑一次 gold 补丁，把「基准环境里本来就挂」的测试记下来。

    这是判定可信度的关键一步：我们的容器网络/证书与官方环境不同，
    某些 PASS_TO_PASS 会无条件失败（实测 `CERTIFICATE_VERIFY_FAILED`）；
    若直接算成模型回归，会把环境问题误报成能力问题。
    """
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    missing = [
        instance
        for instance in ids
        if not (BASELINE_DIR / f"{instance}.json").exists()
    ]
    if missing:
        label = "gold-baseline"
        print(f"gold 基线：{len(missing)} 题需要先跑基准（缓存里没有）", flush=True)
        command = (
            "pip install -q swebench 2>&1 | tail -1 && "
            "python -m swebench.harness.run_evaluation "
            "-d SWE-bench/SWE-bench_Verified -s test "
            f"-p gold -id {label} --max_workers {workers} --timeout {timeout} "
            "-i " + " ".join(missing)
        )
        subprocess.run(
            [
                "docker", "run", "--rm",
                "-v", "H:\\bench:/bench",
                "-v", "/var/run/docker.sock:/var/run/docker.sock",
                "-w", "/bench",
                "python:3.11", "bash", "-lc", command,
            ],
            check=False,
        )
        for instance, data in _read_reports(LOGS_ROOT / "run_evaluation" / label, missing).items():
            (BASELINE_DIR / f"{instance}.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    baselines: dict[str, set[str]] = {}
    for instance in ids:
        path = BASELINE_DIR / f"{instance}.json"
        if not path.exists():
            baselines[instance] = set()
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            baselines[instance] = set()
            continue
        # 兼容两种写法：裸的 report 内容，或 {"<instance>": {...}} 包了一层
        if isinstance(data, dict) and "tests_status" not in data:
            data = data.get(instance) or next(iter(data.values()), {})
        baselines[instance] = _failed_tests(data)
    return baselines


def cleanup_containers() -> int:
    """清掉官方 harness 留下的 `sweb.eval.*` 容器。

    harness 自己的清理步骤要调 docker CLI，而它的运行镜像里没有——所以每次判定后
    都会留下几个 `Up` 的实例容器（它们只是 `tail -f /dev/null`，但占内存与磁盘）。
    """
    listed = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    names = [name for name in listed.stdout.split() if name.startswith("sweb.eval.")]
    for name in names:
        subprocess.run(
            ["docker", "rm", "-f", name],
            capture_output=True,
            text=True,
            errors="replace",
        )
    if names:
        print(f"清理 harness 残留容器 {len(names)} 个", flush=True)
    return len(names)


def report(
    log_root: Path, ids: list[str], gold_failures: dict[str, set[str]] | None = None
) -> int:
    """汇总每个实例的判定；gold 也挂的测试记为环境问题，不算模型回归。"""
    gold_failures = gold_failures or {}
    rows = []
    for instance in ids:
        found = list(log_root.rglob(f"{instance}/report.json"))
        if not found:
            rows.append((instance, "无报告", "", "", False))
            continue
        payload = json.loads(found[0].read_text(encoding="utf-8"))
        # report.json 的键是 instance_id
        data = payload.get(instance) or next(iter(payload.values()))
        tests = data.get("tests_status") or {}
        f2p = tests.get("FAIL_TO_PASS") or {}
        p2p = tests.get("PASS_TO_PASS") or {}
        failed = _failed_tests(data)
        env = gold_failures.get(instance, set())
        model_failures = failed - env
        f2p_ok = not (f2p.get("failure") or [])
        if data.get("resolved"):
            verdict = "PASS"
        elif not model_failures and f2p_ok:
            verdict = "ENV"
        else:
            verdict = "FAIL"
        detail = (
            f"f2p {len(f2p.get('success', []))}/{len(f2p.get('success', [])) + len(f2p.get('failure', []))}"
            f" p2p失败 {len(p2p.get('failure', []))}"
        )
        if env:
            detail += f" (环境 {len(env)})"
        if model_failures:
            detail += " 回归:" + ",".join(sorted(model_failures))[:60]
        rows.append(
            (
                instance,
                verdict,
                "已应用" if data.get("patch_successfully_applied") else "未应用",
                detail,
                bool(model_failures),
            )
        )

    width = max((len(row[0]) for row in rows), default=10)
    print(f"{'实例'.ljust(width)}  结果   补丁      细节")
    for instance, verdict, applied, detail, _ in rows:
        print(f"{instance.ljust(width)}  {verdict:<5} {applied:<8} {detail}")

    passed = sum(1 for row in rows if row[1] in {"PASS", "ENV"})
    raw = sum(1 for row in rows if row[1] == "PASS")
    env = sum(1 for row in rows if row[1] == "ENV")
    print()
    print(f"resolved {raw}/{len(rows)}（含环境折抵 {passed}/{len(rows)}，其中环境问题 {env} 题）")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
