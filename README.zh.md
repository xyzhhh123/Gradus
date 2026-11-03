# Gradus

[English](README.md)

分层的 Agent 运行时。模型协议、控制循环、产品装配各归一层，依赖只能向下：

```
gradus/coding   ->   gradus/agent   ->   gradus/ai
```

- **`ai`**：把 Anthropic、OpenAI Chat、OpenAI Responses、Gemini 统一成一套内部协议——1 种消息形状 + 12 个流式事件。
- **`agent`**：把一次补全变成一次带工具的任务。单层异步循环、9 个控制 hook、8 个运行级事件、并行工具、`steer` / `follow_up`、连接池生命周期。
- **`coding`**：产品装配层，读配置、建会话、实现具体工具、权限与 CLI。目前只有目录骨架。

## 当前状态

| 层 | 内容 | 测试 |
| --- | --- | --- |
| `ai` | 4 个厂商适配器 + fake，全异步 | 126 |
| `agent` | 循环、hook、并行工具、消息队列、`Agent` 门面 | 89 |
| `coding` | 目录与职责说明，尚未实现 | — |

合计 215 passed。Python 3.13，运行依赖 `httpx` 与 `jsonschema`，用 `uv` 管理环境。

## 快速开始

```bash
git clone <your-repo-url>
cd Gradus
uv sync
uv run pytest -q
```

离线跑通一次完整循环（不联网、不需要 key）：

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
            {"kind": "text", "chunks": ["你好，", "我是 Gradus"]},
            {"kind": "finish", "reason": "stop"},
        ],
    }],
)

async def main() -> None:
    async for event in run_loop(provider, [UserMessage("你好")], LoopConfig(system="")):
        if isinstance(event, AgentDone):
            print(event.new_messages[-1].content[0].text)

asyncio.run(main())
```

## 接入真实模型

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

agent = await Agent.connect(spec, LoopConfig(system="你是一个助手"), api_key="sk-...")
try:
    async for event in agent.prompt("你好"):
        ...
finally:
    await agent.aclose()
```

`Agent.connect()` 会建一个 `httpx.AsyncClient` 并在多次运行间复用它，`aclose()` 负责关闭。想多个 Agent 共用连接池，就把同一个 `client` 传给每个 `connect()`。

## 目录

```
Gradus/
├── gradus/
│   ├── ai/        # Provider、内部协议、事件、传输
│   ├── agent/     # 循环、工具运行时、hook、有状态 Agent
│   └── coding/    # 产品装配（骨架）
├── pyproject.toml
└── uv.lock
```

## 文档

- `ai`：[README](gradus/ai/README.zh.md) · [设计](gradus/ai/docs/design.md) · [计划](gradus/ai/ai_plan.md)
- `agent`：[README](gradus/agent/README.zh.md) · [设计](gradus/agent/docs/design.md) · [计划](gradus/agent/agent_plan.md)
- `coding`：[README](gradus/coding/README.zh.md) · [设计](gradus/coding/docs/design.md) · [计划](gradus/coding/coding_plan.md)

## 设计原则

1. **单向依赖**：上层可以调用下层，下层对上层完全不可见。`ai` 不 import `agent` / `coding`，`agent` 不 import `coding`。
2. **事件驱动**：`ai` 的 12 个事件被 `agent` 原样透传，`agent` 只补充运行级事件。
3. **全链路异步**：`Provider.stream()`、`run_loop()`、`AgentTool.execute()` 都是异步的，循环内不做阻塞 I/O。
4. **工具由上层实现**：`agent` 只定义 `AgentTool` 协议，读写文件、执行命令都在 `coding`。

## 许可

未定。
