# Gradus

[中文](README.zh.md)

**A layered coding-agent runtime.** Gradus turns model streams into tool-using sessions, and gives every session a sandboxed copy of the workspace that merges back when the session ends.

## Highlights

- **Four model protocols, one internal shape** — Anthropic Messages, OpenAI Chat Completions, OpenAI Responses, and Gemini all reduce to one message model plus 12 streaming events. Gemini is implemented against the documented format but is still **in testing** (no live endpoint verified yet).
- **A tool loop with real control** — one async loop, 9 hooks, 8 run-level events, parallel tool execution, `steer` / `follow_up`, cancellation, and retries.
- **Sandboxed execution** — `restricted` (Windows, no admin: read as usual, writes only inside the copy), `appcontainer` (deprecated), `docker`, or `local`; each session works on a private workspace copy, and added, edited, and deleted files merge back on close.
- **MCP and extensions** — project-scoped MCP servers with explicit trust and OAuth, plus Python tools, slash commands, and `SKILL.md` skills loaded from `.gradus/`.
- **Codemode** — the model writes Python that calls `tools.*`; only `text()` / `return` enters the context.
- **Sessions and context** — JSONL history, resume, token budgeting, and automatic compaction.

## Architecture

```
gradus/coding    product layer   config, sessions, tools, permissions, sandbox, CLI
      |
gradus/agent     control layer   loop, hooks, parallel tools, Agent facade
      |
gradus/ai        protocol layer  providers, streaming events, transport
```

Dependencies only point down. `ai` imports neither `agent` nor `coding`; `agent` never imports `coding`.

## Status

| Layer | What it contains | Tests |
| --- | --- | --- |
| `ai` | 4 vendor adapters + fake, fully async | 130 |
| `agent` | loop, hooks, parallel tools, message queues, `Agent` facade | 90 |
| `coding` | config, sessions, tools, permissions, MCP, extensions, codemode, sandbox, CLI | 380 |

600 tests collected: 589 pass offline; the 11 live sandbox contract checks skip unless `GRADUS_SANDBOX_LIVE=1`.

Python 3.13 · runtime dependencies: `httpx`, `httpx2`, `jsonschema`, `mcp` · managed with `uv`.

## Quick start

```bash
git clone https://github.com/xyzhhh123/Gradus.git
cd Gradus
uv sync
uv run pytest -q
```

### Run the CLI

Create `<workspace>/.env` with one connection block:

```ini
GRADUS_SERVICE=kimi-compat
GRADUS_MODEL=kimi-k2.7-code
GRADUS_API_KEY=sk-...
```

```bash
uv run gradus                      # workspace = current directory
uv run gradus --workspace ../app   # or point it somewhere else
uv run gradus --sandbox docker     # auto -> restricted -> appcontainer -> docker -> local
```

A key alone cannot identify a provider, so the service, model, and key are configured together. CLI flags override `.env`, which overrides `~/.gradus/config.json`.

| Flag | Purpose |
| --- | --- |
| `--workspace` | Root for tools and commands (default: current directory) |
| `--sandbox` | `auto` / `local` / `docker` / `appcontainer` / `restricted` |
| `--service` / `--provider` / `--model` / `--base-url` | Override the connection |
| `--resume` / `--session-dir` | Resume a JSONL session, or choose where new ones go |
| `--mcp` / `--config` | Explicit MCP and config file paths |
| `--untrust [path]` | Revoke trust for a project's MCP config, then exit |
| `--reset-sandbox` | Clear orphan copies, AppContainer grants, and the profile |
| `--log` | Append tool and error events to a JSONL file |

Inside the REPL: `/reload` rebuilds tools and extensions, `/command` and `/skill:name` expand saved prompts, and `exit` / `Ctrl-D` leaves.

### Switching models (web)

`~/.gradus/config.json` (or whatever `--config` points at) may carry a candidate list; the web model picker reads it directly:

```json
{
  "models": [
    { "name": "Kimi · Chat", "service": "kimi-compat", "model": "kimi-k2.7-code" },
    { "name": "Kimi · Messages", "service": "kimi-anthropic", "model": "kimi-k2.7-code" },
    { "name": "Kimi · Responses", "service": "kimi-responses", "model": "kimi-k2.7-code" }
  ]
}
```

Each entry accepts the same connection fields as the top level (`service` / `provider` / `model` / `base_url` / `api_key` / `context_window` / `max_output_tokens`); `name` is what the UI shows, and an omitted `api_key` reuses the current one. The list is read from the file only — environment variables still describe just the current model.

You do not have to hand-edit the JSON: the gear icon in the web header opens a settings panel that adds, edits, and removes candidate models for you (it never echoes keys back). The last model you pick is written to the top-level `"active"` field and restored on the next start. The panel's "Appearance" section also switches between light / dark / follow-system (kept in browser localStorage, not the config file).

Switching does not start a new session: history is the provider-neutral representation, translated per request by whichever provider is active, so one session can move between `openai_compat`, `anthropic`, and `openai_responses`. Vendor-private fields such as thinking-block `signature` are not converted and may be dropped or passed through unchanged.

## Sandbox

| Backend | When it is used | Boundary |
| --- | --- | --- |
| `restricted` | Windows; `auto` picks it first | Restricted token (synthetic SID + logon SID + `Everyone`): reads follow your account, writes only land in the copy and scratch. **No network isolation.** No admin, no extra install. |
| `appcontainer` | Windows; only when `restricted` is unavailable | AppContainer token, no network, Job Object kills child processes. **Deprecated**: kept working, no further work. |
| `docker` | Docker engine available | Container with `--network none`, memory / CPU / PID limits. **In testing**: kept as a cross-platform fallback, not yet verified against a live engine. |
| `local` | Fallback | None; commands run with your user privileges |

A sandbox session never edits your workspace directly. It gets a private copy — `git worktree` when the workspace is a git repository, a filtered tree copy otherwise — and the file tools and commands both operate on that copy. On close, added and modified files are written back, deletions are applied, and a failed merge keeps the copy and prints its path. Copies left behind by a killed process are removed on the next start.

Three boundaries worth stating plainly:

- **`restricted` does not block the network.** The write boundary is real; the network is not. Use `docker` when a command must be offline.
- **Command approval stays on.** The copy protects the host from accidental damage; it does not make arbitrary commands safe.
- **MCP server processes are not sandboxed.** They run on the host with your privileges. A stdio server's default `cwd` is pointed at the copy so its relative paths line up, but HTTP servers and absolute paths still reach the host.

## Python API

Run a complete loop offline — no network, no API key:

```python
import asyncio

from gradus.agent.base import LoopConfig
from gradus.agent.loop import run_loop
from gradus.agent.types import AgentDone
from gradus.ai.base import ModelSpec
from gradus.ai.providers.fake import FakeProvider
from gradus.ai.types import UserMessage

spec = ModelSpec(
    provider="fake",
    model="fake",
    base_url="",
    context_window=8192,
    max_output_tokens=1024,
)
provider = FakeProvider(
    spec,
    scripts=[{
        "name": "answer",
        "steps": [
            {"kind": "text", "chunks": ["Hello, ", "I am Gradus"]},
            {"kind": "finish", "reason": "stop"},
        ],
    }],
)

async def main() -> None:
    async for event in run_loop(provider, [UserMessage("hi")], LoopConfig(system="")):
        if isinstance(event, AgentDone):
            print(event.new_messages[-1].content[0].text)

asyncio.run(main())
```

Use a real model through the stateful `Agent` facade:

```python
from gradus.agent.agent import Agent
from gradus.agent.base import LoopConfig
from gradus.ai.base import ModelSpec

spec = ModelSpec(
    provider="anthropic",
    model="claude-sonnet-4-5",
    base_url="https://api.anthropic.com",
    context_window=200_000,
    max_output_tokens=8192,
)

agent = await Agent.connect(
    spec,
    LoopConfig(system="You are a helpful assistant"),
    api_key="sk-...",
)
try:
    async for event in agent.prompt("hi"):
        ...
finally:
    await agent.aclose()
```

`Agent.connect()` creates one `httpx.AsyncClient` and reuses it across runs; `aclose()` closes it. Pass the same `client` to several `connect()` calls to share a pool.

## Layout

```
Gradus/
├── gradus/
│   ├── ai/          # providers, internal protocol, streaming events, transport
│   ├── agent/       # loop, hooks, tool runtime, stateful Agent
│   └── coding/      # config, sessions, tools, permissions, sandbox, MCP, CLI
├── pyproject.toml
└── uv.lock
```

## Documentation

| Layer | README | Design | Plan |
| --- | --- | --- | --- |
| `ai` | [README](gradus/ai/README.md) | [design](gradus/ai/docs/design.md) | [plan](gradus/ai/ai_plan.md) |
| `agent` | [README](gradus/agent/README.md) | [design](gradus/agent/docs/design.md) | [plan](gradus/agent/agent_plan.md) |
| `coding` | [README](gradus/coding/README.md) | [design](gradus/coding/docs/design.md) | [plan](gradus/coding/coding_plan.md) |

## Design principles

1. **Dependencies point down only.** Upper layers may call lower ones; lower layers never see upper ones. `ai` imports neither `agent` nor `coding`; `agent` never imports `coding`.
2. **Events are the interface.** The 12 `ai` events are forwarded unchanged by `agent`, which only adds run-level events.
3. **Async all the way down.** `Provider.stream()`, `run_loop()`, and `AgentTool.execute()` are async; the loop never blocks on I/O.
4. **Tools belong to the upper layer.** `agent` defines the `AgentTool` protocol; file, search, and shell implementations live in `coding`.

## Acknowledgements

Gradus's layering, event model, and tool contract were informed by these open-source projects:

- **Pi** (`@earendil-works/pi-agent-core`): reference for the agent loop, hook system, steering / follow-up, and tool protocol.
- **OpenCode**: reference for API and tool design.

## License

[MIT](LICENSE)
