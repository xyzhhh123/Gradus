# AGENTS.md

Project map for coding agents. Read this before exploring; it exists so you do not have to grep for it.

## Layout

```
gradus/host      web layer      serve.py (WebSocket entry), wire.py (event encoding), clock
gradus/coding    product layer  config, session, tools/, permissions, sandbox, mcp, cli
gradus/agent     control layer  loop, hooks, queues, Agent facade
gradus/ai        protocol layer providers/, base, types, transport, stream_assembler
bench/           SWE-bench harness (run.py, grade.py, exec_mode.py); outputs go to bench/runs/
web/             Vite + React browser bundle for the host
```

Dependencies point down only: `host -> coding -> agent -> ai`. `agent` never imports `coding`; `ai` imports neither. This is enforced by `gradus/agent/tests/test_boundary.py` (AST scan), so do not "just import" across layers.

## Conventions

- **Imports inside `gradus/` are relative** — `from ..ai.base import ModelSpec`, not `from gradus.ai.base import ...`. Grepping for `from gradus.` will find almost nothing; that is expected, not a missing feature.
- Tools implement the `AgentTool` protocol (`execute(call_id, arguments, on_update)`) and live in `gradus/coding/tools/`. `agent` defines the protocol; `coding` defines the implementations.
- Providers are built by `gradus/ai/providers/__init__.py::create_provider`. Four protocols: `anthropic`, `openai_compat`, `openai_responses`, `gemini`, plus `fake` for offline runs.
- Streaming: all four providers reduce to the same 12 events in `gradus/ai/types.py`; `agent` forwards them unchanged and only adds run-level events.

## Commands

```bash
uv run pytest -q                 # full suite, offline, ~80s
uv run pytest -q gradus/coding/tests/test_session.py   # one file
uv run gradus                     # CLI (needs a model configured)
uv run gradus-serve --port 8765 --token dev   # web host
uv run python scripts/check_readmes.py        # keep the four READMEs in sync
```

15 live checks are skipped by default: sandbox contracts need `GRADUS_SANDBOX_LIVE=1`, container tests need `GRADUS_DOCKER_LIVE=1`.

## Notes

- Seven READMEs must stay structurally identical: `README.md` (中文, the default GitHub shows), `.en.md`, `.ja.md`, `.ko.md`, `.es.md`, `.fr.md`, `.de.md`. After editing one, run `scripts/check_readmes.py` — CI fails otherwise. `README.md` is Chinese on purpose.
- Session history lives in `<workspace>/.gradus/sessions/` (gitignored).
- Docs per layer: `gradus/<layer>/README.md` and `gradus/<layer>/docs/design.md`.
