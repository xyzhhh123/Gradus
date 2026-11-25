"""Harbor adapter for Gradus — runs Gradus as a Terminal-Bench agent.

Harbor drives it with::

    harbor run -e docker -d terminal-bench/terminal-bench@4.1.0 \
        -i <task> -k 1 -m <model> -a bench.harbor_agent:GradusAgent

What happens inside the task container:

1. ``setup()`` installs uv and then Gradus itself (via ``uv tool install``).
2. ``run()`` feeds the task instruction to the Gradus REPL on stdin and waits
   for the turn to finish.
3. Token usage is scraped back out of Gradus's own summary lines and written to
   the ``AgentContext`` so Harbor can report cost.

The Gradus REPL treats each stdin *line* as a separate prompt, so a multi-line
instruction is flattened onto one line before it is piped in.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from collections import Counter
from pathlib import Path

from harbor.agents.base import BaseAgent
from harbor.agents.capabilities import AgentCapabilities
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

GRADUS_REPO = "https://github.com/xyzhhh123/Gradus/archive/refs/heads/main.tar.gz"

#: Prebuilt Linux bundle (standalone CPython 3.13 + Gradus, no symlinks).
#: Built once by ``bench/build_bundle.sh``; see ``bench/README.md``.
#: Paths are relative to this file so a fresh clone only needs that one script run.
_CACHE = Path(__file__).resolve().parent / ".cache"
BUNDLE = Path(os.environ.get("GRADUS_HARBOR_BUNDLE", _CACHE / "gradus-bundle" / "gradus"))
BUNDLE_TARGET = "/opt/gradus"
#: The bundle ships its own standalone interpreter, so its lib dir must be on the
#: loader path before the venv python will start on a distro that lacks libpython.
BUNDLE_LD = f'export LD_LIBRARY_PATH=$(ls -d {BUNDLE_TARGET}/py/cpython-3.13*/lib 2>/dev/null | head -1)'

#: Where Terminal-Bench tasks put their repository.
WORKDIR = "/app"
AGENT_SESSIONS = "/app/.gradus/sessions"
AGENT_EVENTS = "/tmp/gradus-agent-events.jsonl"
REVIEW_PROMPT = (
    "Before finishing, derive a requirements coverage matrix from the original task. "
    "For each requirement, identify the evidence in the files or commands you used, "
    "run the most relevant available verification, and add at least one negative or "
    "adversarial variant that does not reuse assumptions from your implementation. "
    "Do not treat tests you wrote yourself as proof of correctness. Fix any bugs you "
    "find, and report the exact commands, results, and remaining unverified gaps."
)

#: `[done] 3 turns, in 11743 / out 340 tokens`
_DONE = re.compile(r"\[done\]\s*(\d+)\s*turns?,\s*in\s*(\d+)\s*/\s*out\s*(\d+)")
#: `[in 4,833 · out 154 · cache 3,456 (72%)]`
_USAGE = re.compile(r"\[in\s*([\d,]+)\s*·\s*out\s*([\d,]+)(?:\s*·\s*cache\s*([\d,]+))?")
#: `[done] 3 turns, ... (truncated: response hit the output-token limit)` — see render.py
_TRUNCATED = re.compile(r"\[done\][^\n]*truncated")
#: `[error] provider: timeout: ConnectTimeout`
_AGENT_ERROR = re.compile(r"(?m)^\[error\]\s+([a-z_]+):\s*(.*?)\s*$")

#: Host-side connection fallback, used when Harbor does not supply credentials.
_HOST_CONFIG = Path.home() / ".gradus" / "config.json"


def _flatten(instruction: str) -> str:
    """Collapse a multi-line instruction into one line for the line-oriented REPL."""
    return " ".join(instruction.split())


def _host_connection() -> dict[str, str]:
    """Read service/model/key from the host's Gradus config, if present."""
    try:
        data = json.loads(_HOST_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    env: dict[str, str] = {}
    for src, dst in (
        ("service", "GRADUS_SERVICE"),
        ("model", "GRADUS_MODEL"),
        ("api_key", "GRADUS_API_KEY"),
        ("base_url", "GRADUS_BASE_URL"),
    ):
        value = data.get(src)
        if isinstance(value, str) and value:
            env[dst] = value
    return env


class GradusAgent(BaseAgent):
    """Gradus as a Terminal-Bench agent."""

    capabilities = AgentCapabilities(skills=True)

    def __init__(self, *args, **kwargs):
        # Harbor passes --ak values as constructor kwargs, while _extra_env
        # contains actual environment variables. Preserve both channels.
        self._agent_options = dict(kwargs)
        super().__init__(*args, **kwargs)

    @staticmethod
    def name() -> str:
        return "gradus"

    def version(self) -> str | None:
        return "0.1.0"

    # ------------------------------------------------------------------ setup

    async def setup(self, environment: BaseEnvironment) -> None:
        """Push the prebuilt bundle into the container.

        Installing inside the task container does not work in general: Terminal-Bench
        images include bare ``ubuntu:24.04`` with no curl, git, or python, and apt is
        not reachable. Uploading a fixed bundle makes setup the same on every image.
        """
        if not BUNDLE.exists():
            raise RuntimeError(f"agent bundle missing at {BUNDLE}")

        await environment.upload_dir(BUNDLE, BUNDLE_TARGET)

        check = await environment.exec(
            f"test -x {BUNDLE_TARGET}/venv/bin/gradus && {BUNDLE_LD} && "
            f"{BUNDLE_TARGET}/venv/bin/gradus --help >/dev/null && echo BUNDLE_OK",
            timeout_sec=300,
        )
        out = (check.stdout or "") + (check.stderr or "")
        self.logger.info("bundle check (rc=%s): %s", check.return_code, out.strip()[-400:])
        if "BUNDLE_OK" not in out:
            raise RuntimeError(f"gradus bundle did not run in the task container: {out[-600:]}")

        if self.skills_dir:
            # Gradus discovers user skills under ~/.gradus/extensions/skills.
            # Harbor supplies the task's skills at an arbitrary absolute path.
            source = shlex.quote(self.skills_dir)
            install = await environment.exec(
                "mkdir -p ~/.gradus/extensions/skills && "
                f"cp -R {source}/. ~/.gradus/extensions/skills/",
                timeout_sec=120,
            )
            install_out = (install.stdout or "") + (install.stderr or "")
            if install.return_code != 0:
                raise RuntimeError(
                    "failed to install task skills for Gradus: "
                    f"{install_out.strip()[-600:]}"
                )
            self.logger.info("installed task skills from %s", self.skills_dir)

    # -------------------------------------------------------------------- run

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        env = dict(self._extra_env)
        if not env.get("GRADUS_API_KEY"):
            env.update(_host_connection())
        if self.model_name and not env.get("GRADUS_MODEL"):
            env["GRADUS_MODEL"] = self.model_name
        # Gradus stops after 20 turns by default. Benchmark tasks need far more: one
        # task can spend dozens of turns just exploring before it edits anything.
        # Both knobs also honour a host-side env var, so a run can be retuned without
        # rebuilding the container bundle.
        env.setdefault(
            "GRADUS_MAX_TURNS",
            os.environ.get("GRADUS_MAX_TURNS")
            or str(self._option("max_turns", 250)),
        )
        # The 8192 default truncates long tool calls (a single heredoc script can burn
        # the whole budget), and a truncated turn ends the loop without a deliverable.
        env.setdefault(
            "GRADUS_MAX_OUTPUT_TOKENS",
            os.environ.get("GRADUS_MAX_OUTPUT_TOKENS")
            or str(self._option("max_output_tokens", 32768)),
        )
        prompt = _flatten(instruction)
        review_raw = self._option("review_prompt", REVIEW_PROMPT)
        review = _flatten(str(review_raw)) if review_raw else ""
        # `--yes` is required: there is no one to answer the approval prompt in a
        # benchmark run, and stdin is closed the moment the instruction is consumed.
        if review:
            prompt_stream = (
                f"printf '%s\\n%s\\n' {shlex.quote(prompt)} {shlex.quote(review)}"
            )
        else:
            prompt_stream = f"printf '%s\\n' {shlex.quote(prompt)}"
        command = (
            f"{BUNDLE_LD} && {prompt_stream} | "
            f"{BUNDLE_TARGET}/venv/bin/gradus --yes --log {AGENT_EVENTS}"
        )
        try:
            result = await environment.exec(
                command,
                cwd=WORKDIR,
                env=env,
                timeout_sec=int(self._option("timeout_sec", 28800)),
            )
        finally:
            await self._archive_trajectory(environment)
        output = (result.stdout or "") + (result.stderr or "")

        log_path = Path(self.logs_dir) / "gradus-agent.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(output, encoding="utf-8")

        if not self._record_session_usage(context):
            self._record_usage(output, context)
        self._flag_truncation(output, context)
        self._record_agent_error(output, context)
        self._record_workflow_metadata(context)
        if result.return_code != 0:
            self.logger.warning("gradus exited with %s", result.return_code)

    # ---------------------------------------------------------------- helpers

    def _option(self, name: str, default):
        if name in self._agent_options:
            return self._agent_options[name]
        return self._extra_env.get(name, default)

    def _record_session_usage(self, context: AgentContext) -> bool:
        """Sum authoritative usage records from the archived session files."""
        sessions_dir = Path(self.logs_dir) / "sessions"
        paths = sorted(sessions_dir.glob("*.jsonl"))
        if not paths:
            return False

        input_tokens = output_tokens = cache_tokens = 0
        found = False
        for path in paths:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                self.logger.warning("failed to read Gradus session usage: %s", exc)
                continue
            for line in lines:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("type") != "usage":
                    continue
                found = True
                input_tokens += int(record.get("input") or 0)
                output_tokens += int(record.get("output") or 0)
                cache_tokens += int(record.get("cacheRead") or 0)
                cache_tokens += int(record.get("cacheWrite") or 0)

        if not found:
            return False
        context.n_input_tokens = input_tokens
        context.n_output_tokens = output_tokens
        context.n_cache_tokens = cache_tokens
        return True

    def _record_usage(self, output: str, context: AgentContext) -> None:
        """Pull token counts out of Gradus's summary lines."""
        usage = list(_USAGE.finditer(output))
        if usage:
            context.n_input_tokens = sum(
                int(match.group(1).replace(",", "")) for match in usage
            )
            context.n_output_tokens = sum(
                int(match.group(2).replace(",", "")) for match in usage
            )
            context.n_cache_tokens = sum(
                int(match.group(3).replace(",", ""))
                for match in usage
                if match.group(3)
            )
            return

        done = list(_DONE.finditer(output))
        if done:
            context.n_input_tokens = sum(
                int(match.group(2).replace(",", "")) for match in done
            )
            context.n_output_tokens = sum(
                int(match.group(3).replace(",", "")) for match in done
            )

    def _flag_truncation(self, output: str, context: AgentContext) -> None:
        """Make a truncated run loud.

        Gradus ends a run normally when the last turn hit the output-token ceiling,
        which looks exactly like a finished task from the outside. A benchmark score
        of 0 then reads as "the model gave up" when the run actually died mid-answer.
        """
        if _TRUNCATED.search(output):
            metadata = dict(context.metadata or {})
            metadata["gradus_truncated"] = True
            context.metadata = metadata
            self.logger.warning(
                "gradus run ended on a TRUNCATED response (output-token limit); "
                "the task did not finish and its score understates capability"
            )

    def _record_agent_error(self, output: str, context: AgentContext) -> None:
        """Expose a Gradus runtime error even when the CLI exits successfully."""
        errors = list(_AGENT_ERROR.finditer(output))
        if not errors:
            return

        all_errors = [
            {"kind": match.group(1), "message": match.group(2)}
            for match in errors
        ]
        metadata = dict(context.metadata or {})
        metadata["gradus_errors"] = all_errors
        last_done = max(
            (match.start() for match in _DONE.finditer(output)),
            default=-1,
        )
        if errors[-1].start() > last_done:
            metadata["gradus_error"] = all_errors[-1]
        context.metadata = metadata
        for error in all_errors:
            self.logger.warning(
                "gradus runtime error: %s: %s",
                error["kind"],
                error["message"],
            )

    def _record_workflow_metadata(self, context: AgentContext) -> None:
        """Record generic workflow signals without task-specific logic."""
        sessions_dir = Path(self.logs_dir) / "sessions"
        tool_counts: Counter[str] = Counter()
        tool_errors: Counter[str] = Counter()
        write_paths: Counter[str] = Counter()
        bash_write_patterns = 0
        compactions = trims = 0
        model_turns = 0
        max_input_tokens = 0
        review_seen = False
        review_tool_calls = 0

        for path in sorted(sessions_dir.glob("*.jsonl")):
            try:
                rows = [
                    json.loads(line)
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
            except (OSError, ValueError) as exc:
                self.logger.warning("failed to read workflow session: %s", exc)
                continue

            review_index: int | None = None
            for index, record in enumerate(rows):
                kind = record.get("type")
                if kind == "compaction":
                    compactions += 1
                elif kind == "trim":
                    trims += 1
                elif kind == "usage":
                    model_turns += 1
                    max_input_tokens = max(
                        max_input_tokens,
                        int(record.get("input") or 0),
                    )

                message = record.get("message") or {}
                role = message.get("role")
                content = message.get("content")
                if role == "user":
                    text = content if isinstance(content, str) else str(content)
                    if "Before finishing" in text and review_index is None:
                        review_index = index
                        review_seen = True

                if role != "assistant" or not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_call":
                        continue
                    tool_counts[str(block.get("name") or "unknown")] += 1
                    if review_index is not None:
                        review_tool_calls += 1
                    arguments = block.get("arguments") or {}
                    if not isinstance(arguments, dict):
                        continue
                    if block.get("name") == "write_file":
                        write_paths[str(arguments.get("path") or "")] += 1
                    if block.get("name") == "bash":
                        command = str(arguments.get("command") or "")
                        if re.search(
                            r"(?:sed\s+-i|python3?\s+-|cat\s*>|tee\s|\s>\s|>>)",
                            command,
                        ):
                            bash_write_patterns += 1

        events_path = Path(self.logs_dir) / "agent-events.jsonl"
        try:
            events = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            for event in events:
                if event.get("event") == "tool_end" and event.get("is_error"):
                    tool_errors[str(event.get("tool") or "unknown")] += 1
        except (OSError, ValueError) as exc:
            self.logger.warning("failed to read workflow events: %s", exc)

        metadata = dict(context.metadata or {})
        metadata["gradus_workflow"] = {
            "model_turns": model_turns,
            "max_input_tokens": max_input_tokens,
            "compactions": compactions,
            "trims": trims,
            "tool_calls": dict(sorted(tool_counts.items())),
            "tool_errors": dict(sorted(tool_errors.items())),
            "rewritten_paths": {
                path: count
                for path, count in sorted(write_paths.items())
                if path and count > 1
            },
            "bash_write_patterns": bash_write_patterns,
            "review_seen": review_seen,
            "review_tool_calls": review_tool_calls,
        }
        context.metadata = metadata

    async def _archive_trajectory(self, environment: BaseEnvironment) -> None:
        """Copy the full session and event logs before the container is deleted."""
        logs = Path(self.logs_dir)
        logs.mkdir(parents=True, exist_ok=True)

        try:
            await environment.download_dir(AGENT_SESSIONS, logs / "sessions")
        except Exception as exc:
            self.logger.warning("failed to archive Gradus sessions: %s", exc)

        try:
            await environment.download_file(
                AGENT_EVENTS,
                logs / "agent-events.jsonl",
            )
        except Exception as exc:
            self.logger.warning("failed to archive Gradus event log: %s", exc)
