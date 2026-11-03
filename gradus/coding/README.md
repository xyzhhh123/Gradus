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
- `coding` is the only layer that reads environment variables, holds API keys, and owns the connection pool lifecycle.
- `coding` never changes the `agent` / `ai` contracts. If a capability is missing, add an opening in the lower layer instead of bypassing it here.

## Responsibilities

It does:

- read config and API keys, build a `ModelSpec` and the provider;
- own the process-wide `httpx.AsyncClient` and the `Agent` lifecycle;
- implement concrete tools (files, search, shell) with `execution_mode` and timeouts;
- gate tool calls through `before_tool_call` and render the event stream for logs and the terminal;
- keep session history in memory and persist it later;
- context budget, compaction, and model routing (later);
- provide the CLI entry point.

It does not:

- own the loop, turns, tool scheduling, or event shapes;
- parse provider streams;
- retry network failures before the first byte (`ai` does that).

## Directory

```
gradus/coding/
├── README.md
├── README.zh.md
├── coding_plan.md      # milestones and deviations
├── config.py           # config and model selection: env -> ModelSpec
├── session.py          # session assembly: pool, provider, Agent lifecycle
├── hooks.py            # event subscription and hook wiring: display, logs
├── permissions.py      # tool permission policy
├── cli.py              # command-line entry point
├── tools/
│   ├── files.py        # read_file / write_file / list_dir
│   ├── search.py       # grep / glob
│   └── shell.py        # bash
├── docs/
│   ├── design.md       # design notes
│   └── problem.md      # archive of solved problems
└── tests/
```

No `__init__.py`; like `gradus/ai` and `gradus/agent` this is a namespace package.

## Status

Skeleton only: directories and responsibilities, no implementation. See [coding_plan.md](coding_plan.md).
