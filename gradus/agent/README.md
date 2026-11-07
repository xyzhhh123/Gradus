# Gradus `agent` layer

[中文](README.zh.md)

The control plane of Gradus: it turns individual `ai` completions into one task with tools.

## Dependency direction

```
coding/   product layer: concrete tools, sessions, UI
   |
agent/    loop, tool runtime, events
   |
ai/       providers, internal protocol
```

Dependencies only point down:

- `agent` imports `gradus.ai.base`, `gradus.ai.types`, the `gradus.ai.providers` factory, and third-party `jsonschema` / `httpx`.
- `agent` never imports `gradus.coding`, a concrete adapter (`providers.*`), or `transport`.
- Concrete tools live in the upper layer and are injected as `AgentTool` instances.

The interface belongs to the lower layer; the implementation belongs to the upper layer. `agent` calls an injected object, never an imported class. The one exception is the `create_provider` factory: it picks an implementation from `ModelSpec.provider`, so `agent` never imports a vendor module itself.

## Responsibilities

It does:

- build a `Request` and drive multiple model turns;
- decide whether to continue, finish, or stop from `stop_reason`;
- look up injected tools by name, check required arguments, validate against `jsonschema`, enforce `tool_timeout`, await them, and turn exceptions or timeouts into error results;
- run a tool batch in parallel only when every tool in it declares `execution_mode="parallel"`; otherwise the whole batch runs serially; results are still written back in source order;
- stop after a tool batch only when every result sets `terminate=True`; mixed batches continue normally;
- forward tool progress through `on_update` as `ToolExecutionUpdate`, strictly between `ToolExecutionStart` and `ToolExecutionEnd`;
- expose `details` / `structured_content` / `usage` only through `ToolExecutionEnd`; they never enter history or the run's token usage (**in testing**: rich results are deferred and the shape may change);
- run optional hooks before and after a run, before requests, around tools, at turn boundaries, and at the turn limit; no hooks means no behavior change;
- drain steering before requests and after turns, and follow-up before stopping;
- guarantee that every tool call gets a matching tool result;
- forward `ai` events and add run, turn, and tool-execution events;
- keep the per-run turn count and token usage;
- provide a stateful `Agent`: cross-run history, `steer` / `follow_up`, `abort`, `wait_for_idle`, `reset`;
- optionally own a connection pool: `connect()` builds the provider and an `httpx.AsyncClient` so connections are reused across runs, and `aclose()` closes the pool it created.

It does not:

- read API keys (the caller passes them in) or choose models (the caller decides `ModelSpec`);
- implement `read_file`, `bash`, `edit`, or MCP tools;
- persist, restore, or compact sessions;
- render UI, ask for permissions, or write logs;
- retry network requests (that is the `ai` layer's job).

## Contracts

- `ToolResult(content: str, is_error: bool = False, terminate: bool = False, details: Any = None, structured_content: Any = None, usage: Usage | None = None)`
- Eight run-level events: `AgentStart`, `TurnStart`, `ToolExecutionStart`, `ToolExecutionUpdate`, `ToolExecutionEnd`, `TurnEnd`, `AgentDone`, `AgentError`
- `AgentTool`: `spec: ToolSpec`, `execution_mode: "serial" | "parallel"`, `async def execute(call_id, arguments, on_update=None) -> ToolResult`
- `LoopConfig`: `system`, `tools`, `max_turns`, `max_output_tokens`, `temperature`, `reasoning`, `tool_timeout`, `hooks`, `get_steering_messages`, `get_follow_up_messages`
- `AgentHooks`: `before_run` / `transform_messages` / `prepare_request` / `before_tool_call` / `after_tool_call` / `finish_turn` / `prepare_next_turn` / `before_finish` / `on_max_turns`, all optional
- `AgentError.kind`: `provider` / `aborted` / `max_turns` / `config`
- `Agent.connect(spec, config, *, api_key="", client=None)` and `Agent.aclose()`: a pool the agent created is closed by `aclose()`; an injected pool is only borrowed.
- There are exactly two terminal events: `AgentDone` and `AgentError`; `max_turns` counts model requests.

See [docs/design.md](docs/design.md) for fields, ordering, and terminal semantics.

## Directory

```
gradus/agent/
├── types.py          # event shapes and ToolResult
├── base.py           # AgentTool protocol and LoopConfig
├── hooks.py          # contracts for the control hooks
├── validation.py     # JSON Schema validation for tool arguments
├── loop.py           # run_loop control flow
├── agent.py          # stateful Agent facade
├── queues.py         # steering / follow-up message queues
├── docs/
│   └── design.md     # events, tools, hooks, decision table
├── tests/
│   ├── fakes.py          # shared fake providers and tools
│   ├── test_loop.py      # offline loop tests
│   ├── test_parallel.py  # parallel tool batch tests
│   ├── test_terminate.py # batch early-termination tests
│   ├── test_updates.py   # tool progress update tests
│   ├── test_rich_results.py # rich tool result tests
│   ├── test_agent.py     # Agent history and state tests
│   ├── test_client.py    # connection pool lifecycle tests
│   ├── test_steering.py  # steering / follow-up injection tests
│   ├── test_queues.py    # message queue tests
│   ├── test_hooks.py     # hook behavior tests
│   ├── test_tools.py     # validation and timeout tests
│   └── test_boundary.py  # import boundary checks
├── README.md
└── README.zh.md
```

There is no `__init__.py`; the package relies on namespace packages, same as `gradus/ai`.

## Entry point

```python
async def run_loop(
    provider: Provider,
    messages: list[Message],
    config: LoopConfig,
) -> AsyncIterator[AgentEvent]: ...
```

Consume it from the upper layer:

```python
from gradus.agent.loop import run_loop
from gradus.agent.types import AgentDone, AgentError

async for event in run_loop(provider, messages, config):
    if isinstance(event, AgentDone):
        messages.extend(event.new_messages)
    elif isinstance(event, AgentError):
        print(event.kind, event.message)
```

`provider` is constructed by the upper layer and `messages` is never modified. The `prompt` argument is only appended to this run's transcript; the terminal event's `new_messages` starts with that prompt (an empty list when nothing was produced), so `AgentDone.new_messages` is exactly the history added by this run. The loop is an async generator, and persistence belongs to the upper layer.

### Stateful `Agent`

```python
from gradus.agent.agent import Agent

agent = Agent(
    provider,
    config,
    steering_mode="all",
    follow_up_mode="one-at-a-time",
)
async for event in agent.prompt("read a.py"):
    ...

agent.steer("also look at b.py")   # interject while running
agent.follow_up("summarize it")    # continue when it would stop
await agent.wait_for_idle()
agent.reset()
```

`Agent` owns the history and supports `prompt`, `continue_`, `steer`, `follow_up`, `abort`, `wait_for_idle`, and `reset`.

- `steer` / `follow_up` are only valid while running; idle calls raise `RuntimeError`;
- each run owns its message queues, so unconsumed messages are dropped with the run and never leak into the next one;
- `state` describes the run task, not whether the event stream has been fully consumed.

To reuse connections across runs, build the provider and client with `connect()`, then `aclose()` when done:

```python
agent = await Agent.connect(spec, config, api_key="sk-...")
try:
    async for event in agent.prompt("read a.py"):
        ...
finally:
    await agent.aclose()
```

With `client=None` the agent creates and owns the pool, and `aclose()` closes it. An injected client is only borrowed — the creator closes it — so one pool can serve several agents or providers. Constructing `Agent(provider, config)` directly skips this and opens a fresh connection per `stream()`.

## Concurrency

- `ai` is fully async; `run_loop` is async all the way down.
- `AgentTool.execute` is async. Synchronous tools are adapted by the upper layer with `asyncio.to_thread`; the loop itself never blocks.
- Parallelism is opt-in per tool via `execution_mode="parallel"`, declared by the tool author; the agent never infers it from arguments. One serial tool makes the whole batch serial.
- On cancellation, `run_loop` emits `AgentError(kind="aborted")` first, then re-raises `asyncio.CancelledError`; never swallow `CancelledError`, or the task will not really cancel.
- Tests use `unittest.IsolatedAsyncioTestCase`, same as the `ai` layer; no `pytest-asyncio`.

## Events

Agent-level events:

```text
AgentStart
TurnStart(index)
ToolExecutionStart(call)
ToolExecutionUpdate(call, partial)
ToolExecutionEnd(call, result, duration_ms)
TurnEnd(assistant, tool_results, usage)
AgentDone(new_messages, usage, turns, stop_reason)
AgentError(kind, message, partial, new_messages, usage)
```

The 12 events from `gradus.ai` are forwarded unchanged. `StreamDone` ends one model request; `AgentDone` ends the whole run.

## Tests

```powershell
uv run pytest gradus/agent/tests
```

Tests use the async `FakeProvider` and local async fake tools. No network. Test counts and coverage live in [agent_plan.md](agent_plan.md).

## More

- [agent_plan.md](agent_plan.md): milestones, progress check, deviation record.
- [docs/design.md](docs/design.md): event nesting, tool execution contract, `stop_reason` decision table, open questions.
- [docs/problem.md](docs/problem.md): archive of resolved issues only; open items live in agent_plan.md.
