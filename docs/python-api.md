# Python API

Gradus is importable as a library. There are two levels:

- `run_loop()` — one stateless run over a provider and a message list.
- `Agent` — the same loop with state kept across turns, plus a reusable HTTP client.

Both are async. Neither needs the CLI, the sandbox, or a config file.

## A complete run, offline

No network and no API key — the `fake` provider returns a scripted stream:

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

## A real model

`Agent` keeps history between calls and owns the connection pool:

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

## What comes out of the loop

Every provider reduces to the same 12 streaming events in `gradus/ai/types.py`, so code that consumes `run_loop()` does not care which vendor is behind it. `agent` forwards those events unchanged and adds run-level ones (`AgentDone`, `AgentError`, tool lifecycle, usage).

Tools implement the `AgentTool` protocol — `async def execute(call_id, arguments, on_update) -> ToolResult`. `agent` defines it; `gradus/coding/tools/` has the file, search, shell and codemode implementations.
