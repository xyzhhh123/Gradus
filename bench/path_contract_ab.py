"""Small A/B canary for Gradus's workspace-relative path contract.

The legacy variant is implemented with process-local monkeypatches and exists
only inside this evaluation process. Production behavior remains strict.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import random
import shutil
import statistics
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterator

from gradus.agent.types import AgentDone, AgentError, ToolExecutionEnd, TurnEnd
from gradus.coding import config as cfg
from gradus.coding import permissions, session
from gradus.coding.session import Session
from gradus.coding.tools import files, search


@dataclass(frozen=True)
class CanaryTask:
    name: str
    setup: Callable[[Path], None]
    prompt: Callable[[Path], str]
    verify: Callable[[Path], bool]


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _task_nested_edit() -> CanaryTask:
    def setup(root: Path) -> None:
        _write(root, "src/pkg/app.py", "VALUE = 1\n")

    return CanaryTask(
        "nested_edit",
        setup,
        lambda root: (
            "Work in the current workspace. Change VALUE from 1 to 2 in "
            "src/pkg/app.py. Do not create unrelated files."
        ),
        lambda root: (root / "src/pkg/app.py").read_text("utf-8") == "VALUE = 2\n",
    )


def _task_create_file() -> CanaryTask:
    return CanaryTask(
        "create_file",
        lambda root: None,
        lambda root: (
            "Create docs/notes.txt with exactly the line `hello path contract`. "
            "Do not create unrelated files."
        ),
        lambda root: (root / "docs/notes.txt").read_text("utf-8")
        == "hello path contract\n",
    )


def _task_patch_two_files() -> CanaryTask:
    def setup(root: Path) -> None:
        _write(root, "one.txt", "left\n")
        _write(root, "two.txt", "right\n")

    return CanaryTask(
        "patch_two_files",
        setup,
        lambda root: (
            "Update one.txt to contain `same` and two.txt to contain `same`. "
            "Use a coordinated edit and keep both files newline-terminated."
        ),
        lambda root: (root / "one.txt").read_text("utf-8") == "same\n"
        and (root / "two.txt").read_text("utf-8") == "same\n",
    )


def _task_search_edit() -> CanaryTask:
    def setup(root: Path) -> None:
        _write(root, "src/a.py", "STATUS = 'TARGET'\n")
        _write(root, "src/b.py", "STATUS = 'OTHER'\n")

    return CanaryTask(
        "search_edit",
        setup,
        lambda root: (
            "Find the file containing TARGET, then change TARGET to DONE in that "
            "file only. Leave files that do not contain TARGET unchanged."
        ),
        lambda root: (root / "src/a.py").read_text("utf-8")
        == "STATUS = 'DONE'\n"
        and (root / "src/b.py").read_text("utf-8") == "STATUS = 'OTHER'\n",
    )


def _task_absolute_user_path() -> CanaryTask:
    def setup(root: Path) -> None:
        _write(root, "src/app.py", "VALUE = 10\n")

    return CanaryTask(
        "absolute_user_path",
        setup,
        lambda root: (
            f"The workspace is {root}. Edit {root / 'src/app.py'} so VALUE is 11. "
            "Use the repository-relative path for tools."
        ),
        lambda root: (root / "src/app.py").read_text("utf-8") == "VALUE = 11\n",
    )


def _task_backslash_path() -> CanaryTask:
    def setup(root: Path) -> None:
        _write(root, "src/pkg/app.py", "NAME = 'old'\n")

    return CanaryTask(
        "backslash_path",
        setup,
        lambda root: (
            r"Edit src\pkg\app.py so NAME becomes 'new'. Use tool paths that "
            "resolve inside the workspace."
        ),
        lambda root: (root / "src/pkg/app.py").read_text("utf-8")
        == "NAME = 'new'\n",
    )


def _task_delete_file() -> CanaryTask:
    def setup(root: Path) -> None:
        _write(root, "obsolete.tmp", "remove me\n")
        _write(root, "keep.txt", "keep\n")

    return CanaryTask(
        "delete_file",
        setup,
        lambda root: (
            "Delete obsolete.tmp but keep keep.txt unchanged. Do not use a shell "
            "for the deletion if a file tool can do it."
        ),
        lambda root: not (root / "obsolete.tmp").exists()
        and (root / "keep.txt").read_text("utf-8") == "keep\n",
    )


def _task_multiline_edit() -> CanaryTask:
    def setup(root: Path) -> None:
        _write(
            root,
            "src/calc.py",
            "def add(a, b):\n    return a - b\n\n\ndef keep():\n    return 1\n",
        )

    return CanaryTask(
        "multiline_edit",
        setup,
        lambda root: (
            "Fix the bug in src/calc.py: add(a, b) must return a + b. Preserve "
            "the rest of the file."
        ),
        lambda root: "return a + b" in (root / "src/calc.py").read_text("utf-8"),
    )


def _task_traversal_refusal() -> CanaryTask:
    def setup(root: Path) -> None:
        outside = root.parent / "outside-secret.txt"
        outside.write_text("KEEP\n", encoding="utf-8")

    return CanaryTask(
        "traversal_refusal",
        setup,
        lambda root: (
            "Attempt to read ../outside-secret.txt with the file tool. If the path "
            "is blocked, write exactly `blocked` to boundary.txt. If the read "
            "succeeds, write exactly `read:<file contents>` to boundary.txt. Do not "
            "modify anything outside the workspace."
        ),
        lambda root: (root / "boundary.txt").read_text("utf-8").strip() == "blocked"
        and (root.parent / "outside-secret.txt").read_text("utf-8") == "KEEP\n",
    )


def _task_relative_artifact() -> CanaryTask:
    def setup(root: Path) -> None:
        _write(root, "large.txt", "x" * 40_000 + "NEEDLE\n")

    return CanaryTask(
        "relative_artifact",
        setup,
        lambda root: (
            "Use a shell command to print all of large.txt. If the output is "
            "stored as an artifact, read that artifact using the workspace-relative "
            "path shown by the tool, then write NEEDLE to result.txt."
        ),
        lambda root: (root / "result.txt").read_text("utf-8").strip() == "NEEDLE",
    )


def tasks() -> list[CanaryTask]:
    return [
        _task_nested_edit(),
        _task_create_file(),
        _task_patch_two_files(),
        _task_search_edit(),
        _task_absolute_user_path(),
        _task_backslash_path(),
        _task_delete_file(),
        _task_multiline_edit(),
        _task_traversal_refusal(),
        _task_relative_artifact(),
    ]


def _legacy_workspace_path(
    root: str | Path,
    value: str,
    *,
    allow_root: bool = False,
) -> Path:
    raw = value.strip()
    if not raw and allow_root:
        return Path(root).expanduser().resolve()
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = Path(root).expanduser() / candidate
    return candidate.resolve()


def _legacy_spec(spec: Any, description: str) -> Any:
    parameters = copy.deepcopy(spec.parameters)
    properties = parameters.get("properties")
    if isinstance(properties, dict) and "path" in properties:
        properties["path"]["description"] = "File path (relative or absolute)"
    return replace(spec, description=description, parameters=parameters)


@contextmanager
def _variant_mode(variant: str) -> Iterator[None]:
    if variant == "strict":
        yield
        return

    originals = {
        "files_path": files.workspace_path,
        "search_path": search.workspace_path,
        "permissions_path": permissions.workspace_path,
        "base_prompt": session.BASE_PROMPT,
        "read": files.ReadFileTool.spec,
        "write": files.WriteFileTool.spec,
        "edit": files.EditTool.spec,
        "delete": files.DeleteFileTool.spec,
        "patch": files.ApplyPatchTool.spec,
    }
    new_path_rules = (
        "- File, search and patch paths are workspace-relative POSIX paths. "
        "Absolute paths, drive letters, UNC paths and `..` are rejected; returned "
        "artifact paths are relative too.\n"
        "- The path rule constrains file tools, not arbitrary programs. Shell stays "
        "an execution boundary provided by the selected sandbox backend.\n"
    )
    old_rule = (
        "- grep/glob return paths relative to the working directory; paths outside "
        "it are refused.\n"
    )
    try:
        files.workspace_path = _legacy_workspace_path
        search.workspace_path = _legacy_workspace_path
        permissions.workspace_path = _legacy_workspace_path
        session.BASE_PROMPT = session.BASE_PROMPT.replace(new_path_rules, old_rule)
        files.ReadFileTool.spec = _legacy_spec(
            originals["read"],
            "Read a text file. Relative paths resolve from the working directory. "
            "Absolute paths are allowed.",
        )
        files.WriteFileTool.spec = _legacy_spec(
            originals["write"],
            "Create or replace a file. Relative paths resolve from the working "
            "directory. Absolute paths are allowed.",
        )
        files.EditTool.spec = _legacy_spec(
            originals["edit"],
            "Apply exact replacements to a file. Relative paths resolve from the "
            "working directory. Absolute paths are allowed.",
        )
        files.DeleteFileTool.spec = _legacy_spec(
            originals["delete"],
            "Delete one file. Relative paths resolve from the working directory. "
            "Absolute paths are allowed.",
        )
        files.ApplyPatchTool.spec = replace(
            originals["patch"],
            description=(
                "Apply a multi-file patch with Begin/Add/Update/Delete/End markers. "
                "File paths may be relative or absolute."
            ),
        )
        yield
    finally:
        files.workspace_path = originals["files_path"]
        search.workspace_path = originals["search_path"]
        permissions.workspace_path = originals["permissions_path"]
        session.BASE_PROMPT = originals["base_prompt"]
        files.ReadFileTool.spec = originals["read"]
        files.WriteFileTool.spec = originals["write"]
        files.EditTool.spec = originals["edit"]
        files.DeleteFileTool.spec = originals["delete"]
        files.ApplyPatchTool.spec = originals["patch"]


async def _run_one(
    settings: Any,
    root: Path,
    session_dir: Path,
    task: CanaryTask,
    variant: str,
    timeout: float,
    max_turns: int,
) -> dict[str, Any]:
    task.setup(root)
    no_mcp = root.parent / "mcp-empty.json"
    no_mcp.write_text('{"servers":{}}\n', encoding="utf-8")
    started = time.perf_counter()
    input_tokens = output_tokens = cache_read = cache_write = 0
    tool_calls = tool_errors = path_errors = 0
    turns = 0
    error: str | None = None
    stopped = "unknown"

    async def confirm(question: str) -> bool:
        return True

    one = await Session.connect(
        replace(settings, max_turns=max_turns),
        workspace=root,
        confirm=confirm,
        session_dir=session_dir,
        mcp_config=no_mcp,
        interactive=False,
    )
    try:
        async with asyncio.timeout(timeout):
            async for event in one.prompt(task.prompt(root)):
                if isinstance(event, ToolExecutionEnd):
                    tool_calls += 1
                    if event.result.is_error:
                        tool_errors += 1
                    content = event.result.content
                    if (
                        "must be relative to the workspace" in content
                        or "must not contain" in content
                        or "resolves outside the workspace" in content
                    ):
                        path_errors += 1
                elif isinstance(event, TurnEnd):
                    turns += 1
                    input_tokens += event.usage.input_tokens
                    output_tokens += event.usage.output_tokens
                    cache_read += event.usage.cache_read_tokens
                    cache_write += event.usage.cache_write_tokens
                elif isinstance(event, AgentDone):
                    turns = event.turns
                    stopped = "done"
                elif isinstance(event, AgentError):
                    stopped = event.kind
                    error = event.message
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        stopped = "exception"
    finally:
        await one.aclose()

    passed = False
    try:
        passed = bool(task.verify(root))
    except Exception as exc:
        error = f"verify: {type(exc).__name__}: {exc}"

    return {
        "variant": variant,
        "task": task.name,
        "passed": passed,
        "stopped": stopped,
        "error": error,
        "turns": turns,
        "tool_calls": tool_calls,
        "tool_errors": tool_errors,
        "path_errors": path_errors,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "wall_seconds": round(time.perf_counter() - started, 3),
    }


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for variant in ("legacy", "strict"):
        selected = [row for row in rows if row["variant"] == variant]
        if not selected:
            continue
        out[variant] = {
            "runs": len(selected),
            "passed": sum(bool(row["passed"]) for row in selected),
            "pass_rate": round(
                sum(bool(row["passed"]) for row in selected) / len(selected),
                4,
            ),
            "turns_median": statistics.median(row["turns"] for row in selected),
            "tool_errors": sum(row["tool_errors"] for row in selected),
            "path_errors": sum(row["path_errors"] for row in selected),
            "input_tokens": sum(row["input_tokens"] for row in selected),
            "output_tokens": sum(row["output_tokens"] for row in selected),
            "wall_seconds": round(sum(row["wall_seconds"] for row in selected), 2),
        }
    return out


def _fresh_dir(path: Path, run_root: Path) -> None:
    resolved = path.resolve()
    if not resolved.is_relative_to(run_root.resolve()):
        raise ValueError(f"refusing to clean outside run root: {resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True, exist_ok=True)


async def main_async(args: argparse.Namespace) -> int:
    settings = replace(cfg.load(), sandbox="local")
    run_root = Path(args.output).expanduser().resolve()
    run_root.mkdir(parents=True, exist_ok=True)
    results_path = run_root / "results.jsonl"
    results_path.write_text("", encoding="utf-8")
    selected = [task for task in tasks() if not args.tasks or task.name in args.tasks]
    rows: list[dict[str, Any]] = []
    rng = random.Random(args.seed)

    for task in selected:
        for repeat in range(args.repeats):
            variants = ["legacy", "strict"]
            if (repeat + int(task.name[0].encode()[0])) % 2:
                variants.reverse()
            rng.shuffle(variants)
            for variant in variants:
                root = run_root / "work" / variant / task.name / str(repeat)
                session_dir = run_root / "sessions" / variant / task.name / str(repeat)
                _fresh_dir(root, run_root)
                _fresh_dir(session_dir, run_root)
                with _variant_mode(variant):
                    row = await _run_one(
                        settings,
                        root,
                        session_dir,
                        task,
                        variant,
                        args.timeout,
                        args.max_turns,
                    )
                rows.append(row)
                with results_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                print(
                    f"{variant:6} {task.name:22} run={repeat + 1}/{args.repeats} "
                    f"passed={row['passed']} turns={row['turns']} "
                    f"path_errors={row['path_errors']}"
                )

    summary = _summary(rows)
    (run_root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    await session.close_pool()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="bench/runs/path-contract-canary")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--max-turns", type=int, default=16)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument(
        "--tasks",
        nargs="*",
        default=[],
        help="Optional task names. Empty means all canary tasks.",
    )
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
