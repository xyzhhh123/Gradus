# Gradus `ai` 层

[English](README.md)

Gradus 的底层。它只做一件事：把各家厂商的线上格式统一成同一套内部协议，让这一层之上的
所有代码都看不到任何厂商字段名。

依赖方向（靠设计保证）：

```
providers/*  ->  base.py / stream_assembler.py  ->  types.py
     |
     +-------->  transport.py  ->  httpx
agent 层     ->  base.py / types.py
```

`types.py` 只允许 import 标准库；`transport.py` 是唯一允许碰网络和 httpx 的文件。

## 快速开始

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
    system="你是一个助手",
    messages=[UserMessage("看一下 a.py")],
    tools=[
        ToolSpec(
            name="read_file",
            description="读取文件内容",
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
            print("失败", event.kind, event.error_message)


asyncio.run(main())
```

`stream()` 是异步迭代器，必须 `async for`。除 `asyncio.CancelledError` 外不抛异常：
正常结束一定以 `StreamDone` 收尾，失败一定走 `StreamError`。
中间的 9 个事件是给 UI 与日志的，只要最终消息时可以只处理这两个。

跑测试：

```powershell
uv run python -m gradus.ai.tests.adapter
```

## 数据模型

一次请求产出一条消息，过程以事件流的形式交付：

```
Request ──> Provider.stream() ──> AssistantEvent × N ──> AssistantMessage
```

### 消息

| 类型 | 字段 | 说明 |
| --- | --- | --- |
| `UserMessage` | `content: str` | |
| `AssistantMessage` | `content: list[ContentBlock]`、`stop_reason: StopReason \| None`、`usage: Usage \| None` | 有序内容块，不是字符串加旁路字段。 |
| `ToolResultMessage` | `tool_call_id: str`、`content: str`、`is_error: bool` | 独立消息，不依附角色。 |

```python
Message = UserMessage | AssistantMessage | ToolResultMessage
```

### 内容块

| 类型 | 字段 | 说明 |
| --- | --- | --- |
| `TextBlock` | `text: str`、`refusal: bool = False` | `refusal` 为真表示这段是拒答内容，事件序列与普通文本相同。 |
| `ThinkingBlock` | `thinking: str`、`signature: str \| None` | `signature` 是厂商要求原样回传的不透明数据（Anthropic、Responses）。 |
| `ToolCallBlock` | `id: str`、`name: str`、`arguments: dict`、`raw: str` | `arguments` 永远是已解析的 dict；`raw` 是厂商原文，流式期间逐步变长，回传时以 `arguments` 为准。 |

```python
ContentBlock = TextBlock | ThinkingBlock | ToolCallBlock
```

### 辅助类型

| 类型 | 定义 |
| --- | --- |
| `Usage` | `input_tokens`、`output_tokens`、`cache_read_tokens`、`cache_write_tokens`；`input_tokens` 统一是**总输入**，缓存两项是它的子集；支持 `+`，用于用量跨事件给出的厂商。 |
| `StopReason` | `stop` / `length` / `tool_use` / `deferred` / `filtered` / `error` / `aborted` |

### 事件

共 12 个：`StreamStart` + 3 种块 × 3 阶段 + `StreamDone` / `StreamError`。
每个事件都带 `partial: AssistantMessage`，即到目前为止的完整消息快照。

| 事件 | 额外字段 |
| --- | --- |
| `StreamStart` | 无 |
| `TextStart` / `ThinkingStart` / `ToolCallStart` | `content_index`；工具调用另带 `id`、`name` |
| `TextDelta` / `ThinkingDelta` | `content_index`、`delta: str` |
| `ToolCallDelta` | `content_index`、`delta: str`（JSON 片段） |
| `TextEnd` | `content_index`、`text: str` |
| `ThinkingEnd` | `content_index`、`thinking: str`、`signature: str \| None` |
| `ToolCallEnd` | `content_index`、`arguments: dict` |
| `StreamDone` | `stop_reason`、`message: AssistantMessage`、`usage: Usage` |
| `StreamError` | `kind: "error" \| "aborted"`、`error_message: str` |

```python
AssistantEvent = StreamStart | TextStart | ... | StreamDone | StreamError
```

### 请求侧（`base.py`）

| 类型 | 字段 | 说明 |
| --- | --- | --- |
| `ToolSpec` | `name`、`description`、`parameters: dict` | JSON Schema。 |
| `ModelSpec` | `provider`、`model`、`base_url`、`context_window`、`max_output_tokens` | 靠这张表，一套 `openai_compat` 覆盖多个品牌。不含凭证：`api_key` 由上层读好，构造 Provider 时传入。 |
| `Request` | `system`、`messages`、`tools`、`max_output_tokens`、`temperature`、`reasoning` | `system` 是系统提示词的唯一入口，落到哪里由适配器决定（messages[0] / `system` / `instructions`）；模型名只从 `ModelSpec` 取。`reasoning` 只有上层显式指定档位时才非 `None`，不支持的厂商因此永远看不到这个字段。 |
| `Provider` | `spec`、`stream(request) -> AsyncIterator[AssistantEvent]` | 协议，没有实现。所有实现都以 `(spec, api_key, client=None)` 构造；`client` 传入长活的 `httpx.AsyncClient` 即可复用连接池，由调用方负责关闭，不传则每次自建并关闭。`Protocol` 表达不了构造函数，这条靠约定与测试保证。同一实例可并发调用：真实适配器的 `stream()` 不写实例状态。 |
| `ReasoningLevel` / `THINKING_BUDGETS` | 四档 -> 1024 / 2048 / 8192 / 16384 | 只有把强度表达成 token 预算的厂商查这张表，其余厂商直接发档位名。 |

### 不变量

1. `stream()` 除 `asyncio.CancelledError` 外不抛异常。网络错误、HTTP 4xx/5xx、解析失败都变成 `StreamError`；取消必须能穿透，调用方靠它打断一次阻塞读。
2. 成功时必须且只发一个 `StreamDone`，之后迭代结束。
3. 顺序：`StreamStart` -> 各块阶段 -> `StreamDone` / `StreamError`。
4. `content_index` 由本层从 0 分配，与厂商索引无关。
5. `length` 意味着参数可能被截断，这一批调用不得执行。

## 目录结构

```
gradus/ai/
├── types.py                    # 内部协议：消息、内容块、12 个流式事件
├── base.py                     # 对外接口：Request、ModelSpec、ToolSpec、Provider 协议
├── stream_assembler.py         # 共用装配器：块开关、partial 快照、content_index 分配
├── transport.py                # 传输层：POST、SSE 解析、重试、失败归一
├── ai_plan.md                  # 里程碑、进度检查、偏差记录
├── docs/
│   ├── design.md                 # 实现说明：逐文件的协议映射与取舍
│   └── problem.md                # 问题记录：现象 / 触发 / 原因 / 解决
├── tests/
│   ├── adapter.py                # 测试总入口：把各文件的用例汇总成一个 TestSuite
│   ├── fixtures/                 # 厂商原始 SSE 样本，回放测试的数据来源
│   ├── test_anthropic.py         # Messages 形状的请求体与 SSE 解析测试
│   ├── test_fake.py              # fake provider 的回归测试
│   ├── test_gemini.py            # parts 形状的请求体与 SSE 解析测试
│   ├── test_openai_compat.py     # Chat Completions 形状的请求与响应测试
│   ├── test_openai_responses.py  # Responses 形状的请求与响应测试
│   ├── test_replay.py            # 整段原始流的回放测试
│   └── test_transport.py         # SSE 解析与重试的离线测试
├── scripts/
│   ├── bodies/                   # 录制用的请求体：<service>.json 与 <service>-<场景>.json
│   ├── recorded/                 # 录制临时区，<协议>_<模型>_<时间戳>.sse
│   │   └── （空）
│   ├── record.py                 # 录一条真实流到 fixtures
│   └── replay.py                 # 回放 fixture 并打印内部事件
└── providers/
    ├── __init__.py             # 工厂：create_provider(spec, api_key, client=None)
    ├── openai_compat.py        # Chat Completions 形状（DeepSeek、Qwen、Kimi、GLM、Ollama）
    ├── anthropic.py            # Messages 形状（Claude）
    ├── gemini.py               # generateContent 形状（Gemini）
    ├── openai_responses.py     # Responses 形状（GPT）
    └── fake.py                 # 剧本驱动的假 provider，离线、结果确定
```

## 文件说明

| 层 | 负责 | 不得 |
| --- | --- | --- |
| `types.py` | 消息与事件的形状 | 引入任何依赖、写任何逻辑 |
| `base.py` | 请求侧类型与 `Provider` 契约 | 写契约的实现 |
| `stream_assembler.py` | 把内容增量变成事件 | 出现任何厂商字段名 |
| `transport.py` | 网络 I/O 与 SSE 切分 | 认识任何厂商字段、知道 12 个事件 |
| `providers/*` | 厂商线上格式 <-> 内部协议 | 自己累积消息状态、自造事件 |
| `tests/*` | 断言事件序列与终止路径 | 联网、为迁就实现而改断言 |
| `tests/adapter.py` | 把各文件的用例汇总成一个可执行入口 | 写断言、导入任何 `gradus` 模块 |

## 更细的内容

- [docs/design.md](docs/design.md)：逐文件的协议映射、事件分发、边界行为与取舍。
- [ai_plan.md](ai_plan.md)：里程碑、进度检查、偏差记录（D1–D27）。
- [docs/problem.md](docs/problem.md)：本轮修掉的问题与排查过程（P-01~P-11）。

