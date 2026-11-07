# Gradus `ai` Layer

[中文文档](README.zh.md)

The bottom layer of Gradus. Its only job is to turn every provider's wire format into one
internal protocol, so that nothing above this layer ever sees a vendor-specific field name.

Dependency direction (enforced by design, not by tooling):

```
providers/*  ->  base.py / stream_assembler.py  ->  types.py
     |
     +-------->  transport.py  ->  httpx
agent layer  ->  base.py / types.py
```

`types.py` imports nothing but the standard library; `transport.py` is the only file allowed
to touch the network and httpx.

## Quick start

```python
import asyncio

from gradus.ai.base import ModelSpec, Request, ToolSpec
from gradus.ai.providers import create_provider
from gradus.ai.types import StreamDone, StreamError, UserMessage

spec = ModelSpec(
    provider="anthropic",
    model="claude-sonnet-4-5",
    base_url="https://api.anthropic.com",
    context_window=200_000,
    max_output_tokens=8192,
)
provider = create_provider(spec, api_key="sk-...")

request = Request(
    system="You are a helpful assistant",
    messages=[UserMessage("look at a.py")],
    tools=[
        ToolSpec(
            name="read_file",
            description="Read a file",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        )
    ],
    reasoning="medium",
)

async def main() -> None:
    async for event in provider.stream(request):
        if isinstance(event, StreamDone):
            print(event.stop_reason, event.message.usage)
        elif isinstance(event, StreamError):
            print("failed", event.kind, event.error_message)


asyncio.run(main())
```

`stream()` is an async iterator, so it must be consumed with `async for`. It never raises except
`asyncio.CancelledError`: a normal finish always ends with `StreamDone`, a failure always goes
through `StreamError`. Cancellation must propagate so a caller can interrupt a blocked read.
The other nine events feed UIs and logs; if you only need the final message, handle just these two.

Run the tests:

```powershell
uv run python -m gradus.ai.tests.adapter
```

## Data model

One request produces one message, delivered as a stream of events:

```
Request ──> Provider.stream() ──> AssistantEvent × N ──> AssistantMessage
```

### Messages

| Type | Fields | Note |
| --- | --- | --- |
| `UserMessage` | `content: str` | |
| `AssistantMessage` | `content: list[ContentBlock]`, `stop_reason: StopReason \| None`, `usage: Usage \| None` | Ordered blocks, not a string with side fields. |
| `ToolResultMessage` | `tool_call_id: str`, `content: str`, `is_error: bool` | A message in its own right, not attached to a role. |

```python
Message = UserMessage | AssistantMessage | ToolResultMessage
```

### Content blocks

| Type | Fields | Note |
| --- | --- | --- |
| `TextBlock` | `text: str`, `refusal: bool = False` | `refusal` marks a refusal; the event sequence is identical to plain text. |
| `ThinkingBlock` | `thinking: str`, `signature: str \| None` | `signature` is opaque data the vendor wants echoed back verbatim (Anthropic, Responses). |
| `ToolCallBlock` | `id: str`, `name: str`, `arguments: dict`, `raw: str` | `arguments` is always parsed; `raw` is the vendor's text, growing during the stream. Send `arguments` back to vendors. |

```python
ContentBlock = TextBlock | ThinkingBlock | ToolCallBlock
```

### Supporting types

| Type | Definition |
| --- | --- |
| `Usage` | `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`; `input_tokens` is always the **total** input, with the two cache fields as subsets; supports `+` for providers that split usage across events. |
| `StopReason` | `stop` / `length` / `tool_use` / `deferred` / `filtered` / `error` / `aborted` |

### Events

Twelve events: `StreamStart` + 3 block kinds × 3 phases + `StreamDone` / `StreamError`.
Every event carries `partial: AssistantMessage`, a full snapshot of the message so far.

| Event | Extra payload |
| --- | --- |
| `StreamStart` | — |
| `TextStart` / `ThinkingStart` / `ToolCallStart` | `content_index`; tool calls also carry `id`, `name` |
| `TextDelta` / `ThinkingDelta` | `content_index`, `delta: str` |
| `ToolCallDelta` | `content_index`, `delta: str` (JSON fragment) |
| `TextEnd` | `content_index`, `text: str` |
| `ThinkingEnd` | `content_index`, `thinking: str`, `signature: str \| None` |
| `ToolCallEnd` | `content_index`, `arguments: dict` |
| `StreamDone` | `stop_reason`, `message: AssistantMessage`, `usage: Usage` |
| `StreamError` | `kind: "error" \| "aborted"`, `error_message: str` |

```python
AssistantEvent = StreamStart | TextStart | ... | StreamDone | StreamError
```

### Request side (`base.py`)

| Type | Fields | Note |
| --- | --- | --- |
| `ToolSpec` | `name`, `description`, `parameters: dict` | JSON Schema. |
| `ModelSpec` | `provider`, `model`, `base_url`, `context_window`, `max_output_tokens` | One `openai_compat` adapter covers many brands through this table. Holds no credentials: `api_key` is read by the caller and passed when constructing a Provider. |
| `Request` | `system`, `messages`, `tools`, `max_output_tokens`, `temperature`, `reasoning` | `system` is the only entry point for the system prompt; the adapter decides where it lands (messages[0] / `system` / `instructions`). The model name comes from `ModelSpec` only. `reasoning` is `None` unless the caller asks for a level, so unsupported vendors never see the field. |
| `Provider` | `spec`, `stream(request) -> AsyncIterator[AssistantEvent]` | Protocol; no implementation. Every implementation is constructed as `(spec, api_key, client=None)`; pass a long-lived `httpx.AsyncClient` to reuse its connection pool — the caller owns and closes it, while `None` means each `stream()` creates and closes its own. `Protocol` cannot express a constructor, so this rests on convention and tests. One instance may be called concurrently: real adapters keep no per-stream state. |
| `ReasoningLevel` / `THINKING_BUDGETS` | four levels -> 1024 / 2048 / 8192 / 16384 | Only vendors that express strength as a token budget read the table; the others send the level name. |

### Invariants

1. `stream()` raises nothing except `asyncio.CancelledError`. Network errors, HTTP 4xx/5xx, and parse failures become `StreamError`; cancellation passes through so callers can interrupt a blocked read.
2. Success means exactly one `StreamDone`, then iteration ends.
3. Order: `StreamStart` -> block phases -> `StreamDone` / `StreamError`.
4. `content_index` is assigned by this layer from 0, independent of vendor indices.
5. `length` means arguments may be truncated, so the call batch must not be executed.

## Directory layout

```
gradus/ai/
├── types.py                    # internal protocol: messages, content blocks, 12 stream events
├── base.py                     # public interface: Request, ModelSpec, ToolSpec, Provider protocol
├── stream_assembler.py         # shared assembler: blocks, partial snapshots, content_index
├── transport.py                # transport: POST, SSE parsing, retries, failure mapping
├── envfile.py                  # `.env` parsing: file wins, blanks skipped; shared with the coding CLI
├── services.py                 # built-in services: name -> (protocol, base_url); shared with the coding CLI
├── ai_plan.md                  # milestones, progress checks, deviation log
├── docs/
│   ├── design.md                 # per-file notes (Chinese): vendor mappings and trade-offs
│   └── problem.md                # bug log: symptom / trigger / cause / fix
├── tests/
│   ├── adapter.py                # test entry point: every file's cases in one suite
│   ├── fixtures/                 # raw vendor SSE samples, data for the replay tests
│   ├── test_anthropic.py         # request-body and SSE parsing tests for the Messages shape
│   ├── test_fake.py              # regression tests for the fake provider
│   ├── test_gemini.py            # request-body and SSE parsing tests for the parts shape
│   ├── test_openai_compat.py     # request and response tests for Chat Completions
│   ├── test_openai_responses.py  # request and response tests for Responses
│   ├── test_replay.py            # replay tests over whole recorded streams
│   └── test_transport.py         # offline tests for SSE parsing and retries
├── scripts/
│   ├── bodies/                   # request bodies: <service>.json and <service>-<scenario>.json
│   ├── recorded/                 # recording scratch space, <provider>_<model>_<timestamp>.sse
│   │   └── (empty)
│   ├── record.py                 # record one real stream into fixtures
│   └── replay.py                 # replay a fixture and print internal events
└── providers/
    ├── __init__.py             # factory: create_provider(spec, api_key, client=None)
    ├── openai_compat.py        # Chat Completions shape (DeepSeek, Qwen, Kimi, GLM, Ollama)
    ├── anthropic.py            # Messages shape (Claude)
    ├── gemini.py               # generateContent shape (Gemini; in testing)
    ├── openai_responses.py     # Responses shape (GPT)
    └── fake.py                 # scripted provider, offline, deterministic
```

## Files

| Layer | Owns | Must not |
| --- | --- | --- |
| `types.py` | Message and event shapes | import anything or hold logic |
| `base.py` | Request-side types and the `Provider` contract | contain the implementation of that contract |
| `stream_assembler.py` | Turning content increments into events | mention any vendor field name |
| `transport.py` | Network I/O and SSE splitting | know any vendor field or the 12 events |
| `envfile.py` | Reading `.env` into `os.environ` (file wins, blanks skipped) | expand variables, touch the network or the protocol, import `gradus.coding` |
| `services.py` | Name -> `(protocol, base_url)` mapping for common services | hold request logic, credentials, or vendor field parsing |
| `providers/*` | Vendor wire format <-> internal protocol | accumulate message state or define new events |
| `tests/*` | Assert event sequences and termination paths | touch the network or bend assertions to fit the implementation |
| `tests/adapter.py` | Gather every file's cases into one runnable entry | hold assertions or import any `gradus` module |

## Going deeper

- [docs/design.md](docs/design.md) (Chinese): per-file vendor mappings, event dispatch, edge behavior.
- [ai_plan.md](ai_plan.md): milestones, progress checks, deviation log (D1–D27).
- [docs/problem.md](docs/problem.md): bugs found and fixed this round (P-01~P-11).

