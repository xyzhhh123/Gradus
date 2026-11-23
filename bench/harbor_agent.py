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
import re
import shlex
from pathlib import Path

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

GRADUS_REPO = "https://github.com/xyzhhh123/Gradus/archive/refs/heads/main.tar.gz"

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
        """Install uv, then Gradus, inside the task container."""
        await environment.exec("command -v curl >/dev/null || (apt-get update && apt-get install -y --no-install-recommends curl)",
                               timeout_sec=600)
        install = (
            "command -v uv >/dev/null || "
            "(curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1)"
        )
        await environment.exec(install, timeout_sec=600)

        result = await environment.exec(
            'export PATH="$HOME/.local/bin:$PATH" && '
            f"uv tool install --python 3.13 {GRADUS_REPO}",
            timeout_sec=1800,
        )
        out = (result.stdout or "") + (result.stderr or "")
        self.logger.info("gradus install (rc=%s): %s", result.return_code, out.strip()[-800:])
        if result.return_code != 0:
            raise RuntimeError(f"installing Gradus failed (rc={result.return_code}): {out[-800:]}")

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

        prompt = _flatten(instruction)
        # `--yes` is required: there is no one to answer the approval prompt in a
        # benchmark run, and stdin is closed the moment the instruction is consumed.
        command = (
            'export PATH="$HOME/.local/bin:$PATH" && '
            f"printf '%s\\n' {shlex.quote(prompt)} | gradus --yes"
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
