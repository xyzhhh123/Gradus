# Gradus

A layered agent runtime. Model protocols, the control loop, and product assembly each live in their own layer, and dependencies only point downward:

```
gradus/coding   ->   gradus/agent   ->   gradus/ai
```

- **`ai`**: unifies Anthropic, OpenAI Chat, OpenAI Responses, and Gemini into one internal protocol — one message shape plus 12 streaming events.
- **`agent`**: turns a single completion into a tool-using task. One async loop, 9 control hooks, 8 run-level events, parallel tools, `steer` / `follow_up`, and connection-pool lifecycle.
- **`coding`**: the product layer — config, sessions, concrete tools, permissions, and the CLI. Currently a skeleton only.

## Status

| Layer | Contents | Tests |
| --- | --- | --- |
| `ai` | 4 vendor adapters + fake, fully async | 126 |
| `agent` | loop, hooks, parallel tools, message queues, `Agent` facade | 89 |
| `coding` | directories and responsibilities only | — |

215 tests passing. Python 3.13, runtime dependencies are `httpx` and `jsonschema`, managed with `uv`.

## Quick start

```bash
git clone <your-repo-url>
cd Gradus
uv sync
uv run pytest -q
```

Run a full loop offline (no network, no API key):

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

## Using a real model

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

agent = await Agent.connect(spec, LoopConfig(system="You are a helpful assistant"), api_key="sk-...")
try:
    async for event in agent.prompt("hi"):
        ...
finally:
    await agent.aclose()
```

`Agent.connect()` creates one `httpx.AsyncClient` and reuses it across runs; `aclose()` closes it. To share a pool between several agents, pass the same `client` to each `connect()` call.

## Layout

```
Gradus/
├── gradus/
│   ├── ai/        # providers, internal protocol, events, transport
│   ├── agent/     # loop, tool runtime, hooks, stateful Agent
│   └── coding/    # product assembly (skeleton)
├── pyproject.toml
└── uv.lock
```

## Documentation

- `ai`: [README](gradus/ai/README.md) · [design](gradus/ai/docs/design.md) · [plan](gradus/ai/ai_plan.md)
- `agent`: [README](gradus/agent/README.md) · [design](gradus/agent/docs/design.md) · [plan](gradus/agent/agent_plan.md)
- `coding`: [README](gradus/coding/README.md) · [design](gradus/coding/docs/design.md) · [plan](gradus/coding/coding_plan.md)

## Design principles

1. **Dependencies point down only.** Upper layers may call lower ones; lower layers never see upper ones. `ai` imports neither `agent` nor `coding`; `agent` never imports `coding`.
2. **Event-driven.** The 12 `ai` events are forwarded unchanged by `agent`, which only adds run-level events.
3. **Async all the way down.** `Provider.stream()`, `run_loop()`, and `AgentTool.execute()` are all async; the loop never blocks.
4. **Tools belong to the upper layer.** `agent` only defines the `AgentTool` protocol; file and shell operations live in `coding`.

## License

TBD.
