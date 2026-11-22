"""评测的公共部分：任务、结果、记账、汇总。

设计约束（见 `bench/README.md`）：
- **按题独立**：一题一个工作区、一条结果记录；中断最多损失正在跑的那一题；
- **可续跑**：结果追加写进 `tasks.jsonl`，启动时跳过已完成的题；
- **参数冻结**：开跑时写 `plan.json`，切片期间不许改模型/配置/题单；
- **时间不是效果指标**：墙钟只用于排期与超时，报告里单列。
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

#: 一题的结果。字段与 `bench/README.md` 的「每题记什么」一一对应。
RESULT_FIELDS = (
    "id",
    "status",  # done / failed / timeout / skipped
    "resolved",  # True / False / None（还没判定）
    "input",
    "output",
    "cache_read",
    "cache_write",
    "turns",
    "tool_calls",
    "tool_errors",
    "compactions",
    "trimmed",
    "failure_class",
    "wall_seconds",
    "attempts",
    "checks",
    "self_check",
    "multiagent",
    "subagents",
    "sub_input",
    "sub_output",
)


@dataclass
class Task:
    """一道题。`workspace` 是仓库副本所在目录（由执行器准备）。"""

    id: str
    instruction: str
    workspace: str
    repo: str = ""
    base_commit: str = ""
    test_cmd: str = ""
    image: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskResult:
    id: str
    status: str = "done"
    resolved: bool | None = None
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    turns: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    compactions: int = 0
    trimmed: int = 0
    failure_class: str = ""
    wall_seconds: float = 0.0
    attempts: int = 1
    checks: int = 0
    self_check: str = ""
    multiagent: bool = False
    subagents: int = 0
    sub_input: int = 0
    sub_output: int = 0

    def to_json(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if key in RESULT_FIELDS}


def load_tasks(path: str | Path) -> list[Task]:
    """读题单：JSONL，一行一题。

    字段至少要有 `id` 与 `instruction`；`workspace` 缺省为空，由执行器填。
    """
    tasks: list[Task] = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{number} 不是 JSON: {exc}") from exc
        if not payload.get("id") or not payload.get("instruction"):
            raise ValueError(f"{path}:{number} 缺少 id 或 instruction")
        tasks.append(
            Task(
                id=str(payload["id"]),
                instruction=str(payload["instruction"]),
                workspace=str(payload.get("workspace") or ""),
                repo=str(payload.get("repo") or ""),
                base_commit=str(payload.get("base_commit") or ""),
                test_cmd=str(payload.get("test_cmd") or ""),
                image=str(payload.get("image") or ""),
                meta={
                    key: value
                    for key, value in payload.items()
                    if key
                    not in {"id", "instruction", "workspace", "repo", "base_commit", "test_cmd", "image"}
                },
            )
        )
    return tasks


def append_result(path: str | Path, result: TaskResult) -> None:
    """追加一条结果并立刻落盘：切片中断不丢已完成的题。"""
    line = json.dumps(result.to_json(), ensure_ascii=False)
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()


def read_results(path: str | Path) -> dict[str, TaskResult]:
    """读回结果；同一题出现多次时以最后一次为准（重跑覆盖）。"""
    results: dict[str, TaskResult] = {}
    target = Path(path)
    if not target.exists():
        return results
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or not payload.get("id"):
            continue
        known = {key: payload[key] for key in RESULT_FIELDS if key in payload}
        results[str(known["id"])] = TaskResult(**known)
    return results


def wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 置信区间；total 为 0 时返回 (0, 0)。"""
    if total <= 0:
        return (0.0, 0.0)
    phat = successes / total
    denominator = 1 + z * z / total
    centre = phat + z * z / (2 * total)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * total)) / total)
    return (max(0.0, (centre - margin) / denominator), min(1.0, (centre + margin) / denominator))


def summarize(results: Iterable[TaskResult], total_tasks: int) -> dict[str, Any]:
    """把逐题结果汇总成一份可报告的口径。"""
    items = list(results)
    done = [item for item in items if item.status == "done"]
    judged = [item for item in done if item.resolved is not None]
    solved = [item for item in judged if item.resolved]
    # 成本按「所有尝试过的题」算：超时/失败的请求也真的花了钱。
    tokens_in = sum(item.input for item in items)
    tokens_out = sum(item.output for item in items)
    cache_read = sum(item.cache_read for item in items)
    low, high = wilson(len(solved), len(judged))
    unjudged = sum(1 for item in done if item.resolved is None)
    failures: dict[str, int] = {}
    for item in items:
        if item.resolved is True:
            continue
        if item.status != "done":
            key = item.failure_class or item.status
        elif item.resolved is False:
            key = item.failure_class or "测试未过"
        else:
            key = item.failure_class or "未判定"
        failures[key] = failures.get(key, 0) + 1
    return {
        "total": total_tasks,
        "finished": len(items),
        "judged": len(judged),
        "solved": len(solved),
        "rate": (len(solved) / len(judged)) if judged else 0.0,
        "interval": (low, high),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cache_ratio": (cache_read / tokens_in) if tokens_in else 0.0,
        "paid_tokens": tokens_in - cache_read,
        "paid_per_solved": ((tokens_in - cache_read) / len(solved)) if solved else 0.0,
        "subagents": sum(r.subagents for r in results),
        "sub_tokens": sum(r.sub_input + r.sub_output for r in results),
        "multiagent_tasks": sum(1 for r in results if r.multiagent),
        "tokens_per_solved": (tokens_in / len(solved)) if solved else 0.0,
        "turns": sorted(item.turns for item in items),
        "tool_calls": sum(item.tool_calls for item in items),
        "tool_errors": sum(item.tool_errors for item in items),
        "compactions": sum(item.compactions for item in items),
        "trimmed": sum(item.trimmed for item in items),
        "unjudged": unjudged,
        "failures": failures,
        "wall_seconds": sum(item.wall_seconds for item in items),
    }


def format_report(summary: dict[str, Any], plan: dict[str, Any]) -> str:
    """固定成一段可粘贴的报告；缺项也不省略，写 0。"""
    total = summary["total"]
    finished = summary["finished"]
    rate = summary["rate"] * 100
    low, high = (value * 100 for value in summary["interval"])
    turns = summary["turns"] or [0]

    def compact(value: float) -> str:
        return f"{value / 1_000_000:.1f}M" if value >= 1_000_000 else f"{value / 1000:.1f}k"

    failures = " | ".join(f"{name} {count}" for name, count in sorted(summary["failures"].items()))
    lines = [
        f"Gradus {plan.get('commit', '?')} | {plan.get('model', '?')} | "
        f"{plan.get('dataset', '?')}({finished}/{total}) | trials={plan.get('trials', 1)}",
        f"resolved   {summary['solved']}/{summary['judged']}  "
        f"({rate:.0f}%, Wilson {low:.0f}–{high:.0f}%)",
        f"tokens     {compact(summary['tokens_in'])} in / {compact(summary['tokens_out'])} out "
        f"/ cache {summary['cache_ratio'] * 100:.0f}%",
        f"paid       {compact(summary['paid_tokens'])} 非缓存输入",
        f"（每通过一题 {compact(summary['paid_per_solved']) if summary['solved'] else '—'}）",
        f"per solved {compact(summary['tokens_per_solved']) if summary['solved'] else '—'} tokens（含缓存）",
        f"turns/task {turns[len(turns) // 2]} (median)   tools {summary['tool_calls']}   "
        f"tool-error {summary['tool_errors']}",
        (
            f"subagents  {summary['subagents']} 个 / 子 token {compact(summary['sub_tokens'])}"
            f"（{summary['multiagent_tasks']} 题启用多 agent）"
        ),
        f"compaction {summary['compactions']}   trimmed {summary['trimmed']}",
        f"unjudged   {summary['unjudged']}（跑完但没判定：本地模式没给测试命令时正常）",
        f"failures   {failures or '-'}",
        f"wall       {summary['wall_seconds'] / 60:.0f}m（仅排期参考）",
    ]
    return "\n".join(lines)


def write_text_lf(path: str | Path, text: str) -> None:
    """按 LF 写文本。

    Windows 上 `Path.write_text` 会把 `\n` 翻成 `\r\n`——patch 文件一旦带 CRLF，
    Linux 容器里的 `git apply` 就失败（实测官方 test_patch 被写成 CRLF，永远应用不上）。
    凡是给容器或 git 用的文本，一律走这里。
    """
    with Path(path).open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def write_plan(path: str | Path, plan: dict[str, Any]) -> None:
    write_text_lf(path, json.dumps(plan, ensure_ascii=False, indent=2))


def read_plan(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    if not target.exists():
        return {}
    return json.loads(target.read_text(encoding="utf-8"))
