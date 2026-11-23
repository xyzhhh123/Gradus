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
from pathlib import Path

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

GRADUS_REPO = "https://github.com/xyzhhh123/Gradus/archive/refs/heads/main.tar.gz"

#: Prebuilt Linux bundle (standalone CPython 3.13 + Gradus, no symlinks).
#: Built once locally; see the README section in bench/ for the Dockerfile.
BUNDLE = Path(os.environ.get("GRADUS_HARBOR_BUNDLE", r"H:\bench\gradus-bundle\gradus"))
BUNDLE_TARGET = "/opt/gradus"
#: The bundle ships its own standalone interpreter, so its lib dir must be on the
#: loader path before the venv python will start on a distro that lacks libpython.
BUNDLE_LD = f'export LD_LIBRARY_PATH=$(ls -d {BUNDLE_TARGET}/py/cpython-3.13*/lib 2>/dev/null | head -1)'

#: Where Terminal-Bench tasks put their repository.
WORKDIR = "/app"

#: `[done] 3 turns, in 11743 / out 340 tokens`
_DONE = re.compile(r"\[done\]\s*(\d+)\s*turns?,\s*in\s*(\d+)\s*/\s*out\s*(\d+)")
#: `[in 4,833 · out 154 · cache 3,456 (72%)]`
_USAGE = re.compile(r"\[in\s*([\d,]+)\s*·\s*out\s*([\d,]+)(?:\s*·\s*cache\s*([\d,]+))?")

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
        env.setdefault("GRADUS_MAX_TURNS", str(self._extra_env.get("max_turns", 100)))
        # The 8192 default truncates long tool calls (a single heredoc script can burn
        # the whole budget), and a truncated turn ends the loop without a deliverable.
        env.setdefault(
            "GRADUS_MAX_OUTPUT_TOKENS",
            str(self._extra_env.get("max_output_tokens", 32768)),
        )

        prompt = _flatten(instruction)
        # `--yes` is required: there is no one to answer the approval prompt in a
        # benchmark run, and stdin is closed the moment the instruction is consumed.
        command = (
            f"{BUNDLE_LD} && "
            f"printf '%s\\n' {shlex.quote(prompt)} | {BUNDLE_TARGET}/venv/bin/gradus --yes"
        )
        result = await environment.exec(
            command,
            cwd=WORKDIR,
            env=env,
            timeout_sec=int(self._extra_env.get("timeout_sec", 3600)),
        )
        output = (result.stdout or "") + (result.stderr or "")

        log_path = Path(self.logs_dir) / "gradus-agent.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(output, encoding="utf-8")

        self._record_usage(output, context)
        if result.return_code != 0:
            self.logger.warning("gradus exited with %s", result.return_code)

    # ---------------------------------------------------------------- helpers

    def _record_usage(self, output: str, context: AgentContext) -> None:
        """Pull token counts out of Gradus's summary lines."""
        tokens_in = tokens_out = cache = None

        if match := _USAGE.search(output):
            tokens_in = int(match.group(1).replace(",", ""))
            tokens_out = int(match.group(2).replace(",", ""))
            if match.group(3):
                cache = int(match.group(3).replace(",", ""))
        elif match := _DONE.search(output):
            tokens_in = int(match.group(2))
            tokens_out = int(match.group(3))

        if tokens_in is not None:
            context.n_input_tokens = tokens_in
        if tokens_out is not None:
            context.n_output_tokens = tokens_out
        if cache is not None:
            context.n_cache_tokens = cache
