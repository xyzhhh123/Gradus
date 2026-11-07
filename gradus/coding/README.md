# Gradus `coding` layer

[中文](README.zh.md)

The product layer of Gradus: it assembles the `agent` loop into an assistant you can actually use.

## Dependency direction

```
coding/   product layer: config, session, tools, permissions, entry point
   |
agent/    loop, tool runtime, events
   |
ai/       providers, internal protocol
```

Rules:

- `coding` may import `gradus.agent.*`, `gradus.ai.*`, and third-party packages.
- Among library code, `coding` is the only layer that reads environment variables, holds API keys, and owns the connection pool lifecycle; the dev script `ai/scripts/record.py` is the exception (it reads `.env` for its recording key, parsed in `ai/envfile.py`).
- `coding` never changes the `agent` / `ai` contracts. If a capability is missing, add an opening in the lower layer instead of bypassing it here.

## Responsibilities

It does:

- read config and API keys, build a `ModelSpec` and the provider;
- own the process-wide `httpx.AsyncClient` and the `Agent` lifecycle;
- implement concrete tools (files, search, shell, MCP resources, code-mode scripts) with `execution_mode` and timeouts;
- gate tool calls through `before_tool_call` and render the event stream for logs and the terminal;
- keep session history in memory and persist it;
- context budget, compaction, retry, and model routing;
- connect MCP servers listed in `.gradus/mcp.json` (the project config is confirmed once, then remembered) and expose their tools per server/`toolExposure` policy — `deferred` by default, loaded through `tool_search`; HTTP servers with `oauth` sign in through the browser and keep tokens in `~/.gradus/mcp-auth.json`;
- load extensions from `<workspace>/.gradus/extensions` (the directory needs trust) — Python tools, Markdown slash commands, and `SKILL.md` skills; `/reload` re-reads them without restarting;
- read `AGENTS.override.md` / `AGENTS.md` / `CLAUDE.md` from the workspace and its ancestors, root-first, into the system prompt (no trust needed);
- run commands and `codemode` through a replaceable execution environment: `restricted` (Windows, no admin: reads follow your account, writes only inside the copy; **no network isolation**), `appcontainer` (Windows, zero install, deprecated), `local` (host), or `docker` (long-lived container, no network by default, memory / CPU / PID limits, no host environment variables; **in testing**, kept as a cross-platform fallback); when the selected backend is unavailable it does not fall back to `local` — command tools are disabled with a warning;
- provide the CLI entry point.

It does not:

- own the loop, turns, tool scheduling, or event shapes;
- parse provider streams;
- retry network failures before the first byte (`ai` does that).

## Configuration

Precedence: CLI flags (`--service` / `--provider` / `--model` / `--base-url`) > environment variables > `~/.gradus/config.json`. A key missing from one source falls back to the next.

`~/.gradus/config.json` (override with `--config`):

```json
{
  "service": "deepseek",
  "model": "deepseek-chat",
  "api_key": "${DEEPSEEK_API_KEY}",
  "context_window": 200000,
  "max_output_tokens": 8192
}
```

`service` is a built-in provider shortcut (`deepseek` / `kimi-compat` / `qwen` / `anthropic` / `openai` and more; the table lives in `gradus/ai/services.py`): it fills in `provider` and `base_url`. An explicit `provider` / `base_url` still wins over the table, but a `provider` that disagrees with `service`'s protocol is an error.

`${NAME}` is replaced with an environment variable. Unknown fields, malformed JSON and unset variables are errors; a missing default file simply means "no file".

The file may also carry a `models` array for the web model picker — each entry takes the same connection fields as the top level plus an optional display `name`. A top-level `active` holds the name last picked in the UI and is applied on the next start; when that name is gone, the top-level model is used instead. Environment variables still describe only the current model.

The CLI also reads `<workspace>/.env` on startup. Values in the file win, overriding same-named environment variables; blank values count as unset. Parsing lives in `gradus/ai/envfile.py` and is shared with `ai/scripts/record.py`. The file is organised as one connection per block — `GRADUS_SERVICE` (which implies protocol and `base_url`), `GRADUS_MODEL` and `GRADUS_API_KEY` together, plus `GRADUS_BASE_URL` for custom endpoints. A key alone does not identify the provider, so the endpoint and the key must sit in the same block; switching providers means replacing the whole block.

Once a project is in the MCP trust record, `gradus --untrust [path]` removes it (the current workspace when the path is omitted) and exits.

Sandbox settings: `sandbox` (`auto` / `local` / `docker` / `appcontainer` / `restricted`, default `auto`), `sandbox_image` (default `python:3.13-slim`), `sandbox_network` (default `false`, i.e. `--network none`). `auto` picks the first available backend in the order `restricted` -> `appcontainer` -> `docker` -> `local`; pass `--sandbox docker` / `--sandbox restricted` to force one. When Docker is unavailable the command tools are disabled and a `sandbox warning:` is printed. Sandboxed sessions work on their own copy of the workspace (a `git worktree` plus untracked files when the workspace is a repository root, a full copy otherwise); file tools and commands both read and write the copy, which is merged back on exit — additions, edits and deletions alike. A failed merge keeps the copy and prints its path, and copies left behind by a killed process are cleaned on the next start, so command and write approvals stay on. Containers carry a `gradus.owner=<pid>` label, so leftovers from a force-killed process are removed on the next docker-backed start. `restricted` is the Windows target backend: a synthetic SID plus `CreateRestrictedToken` (`WRITE_RESTRICTED`) and `CreateProcessAsUserW`; reads follow your account, writes only land in the copy and scratch, and **the network is not isolated**. `appcontainer` is kept as a deprecated zero-install fallback: AppContainer isolation, no network, `powershell` only (no Git Bash).

## Directory

```
gradus/coding/
├── README.md
├── README.zh.md
├── coding_plan.md      # milestones, progress, open items
├── config.py           # config and model selection: env -> ModelSpec
├── session.py          # session assembly and JSONL history
├── context.py          # token budget, cut point, summarization
├── retry.py            # run-level retry policy (same model)
├── registry.py         # tool registry and function-tool adapter
├── mcp.py              # MCP servers -> AgentTool
├── render.py           # event rendering: display, logs
├── permissions.py      # tool permission policy
├── sandbox.py          # execution environment: protocol, local, docker
├── restricted.py       # Windows restricted-token backend (target)
├── win32.py            # shared Win32 primitives: pipes, job object, ACL
├── appcontainer.py     # legacy AppContainer backend (deprecated)
├── workspace.py        # sandbox-owned workspace copy and merge back
├── cli.py              # command-line entry point
├── tools/
│   ├── files.py        # read_file / write_file / edit / list_dir
│   ├── search.py       # grep / glob
│   └── shell.py        # bash / powershell
├── docs/
│   ├── design.md       # design notes
│   └── problem.md      # archive of solved problems
└── tests/
```

No `__init__.py`; like `gradus/ai` and `gradus/agent` this is a namespace package.

## Status

See [coding_plan.md](coding_plan.md) for current status and open items.
