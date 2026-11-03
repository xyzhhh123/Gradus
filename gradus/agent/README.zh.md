# Gradus `agent` 层

[English](README.md)

Gradus 的控制面：把 `ai` 层的一次补全，串成一次带工具的任务。

## 依赖方向

```
coding/   产品层：具体工具、会话、UI
   |
agent/    循环、工具运行时、事件
   |
ai/       Provider、内部协议
```

依赖只能向下：

- `agent` 只 import `gradus.ai.base`、`gradus.ai.types`、工厂 `gradus.ai.providers`，以及第三方 `jsonschema`、`httpx`。
- `agent` 不 import `gradus.coding`，也不 import `ai` 的具体适配器（`providers.*`）与 `transport`。
- 具体工具由上层实现，以 `AgentTool` 实例注入。

接口由下层定义，实现由上层提供。`agent` 调用的是被传进来的对象，不是 import 进来的类；唯一的例外是 `create_provider` 工厂——它按 `ModelSpec.provider` 选实现，agent 不直接 import 任何厂商模块。

## 职责

负责：

- 组装 `Request`，驱动多轮模型请求；
- 根据 `stop_reason` 决定继续、结束或终止；
- 按名字调用注入的工具，检查必填参数、用 `jsonschema` 校验参数、按 `tool_timeout` 限时，`await` 执行结果，把异常与超时转成错误结果；
- 只有批次内每个工具都声明 `execution_mode="parallel"` 时才并发执行，否则整批串行；结果仍按源顺序写回；
- 只有整批工具结果都 `terminate=True` 时才跳过下一次模型请求；混合批次照常继续；
- 工具的 `on_update` 进度会转成 `ToolExecutionUpdate`，只出现在 `ToolExecutionStart` 与 `ToolExecutionEnd` 之间；
- `details` / `structured_content` / `usage` 只通过 `ToolExecutionEnd` 暴露，不进入历史，也不计入运行用量；
- 在运行前后、请求前、工具前后、轮次收尾与上限判断调用可选 hooks；不传时行为不变；
- 在请求前、轮次后排空 steering，在停止前排空 follow-up；
- 保证每个工具调用都有对应的工具结果；
- 透传 `ai` 事件，并补充运行、轮次、工具执行事件；
- 统计本次运行的轮次与 token 用量；
- 提供有状态 `Agent`：跨运行历史、`steer` / `follow_up`、`abort`、`wait_for_idle`、`reset`；
- 可选地持有连接池：`connect()` 建 Provider 与 `httpx.AsyncClient`，让连接跨轮复用，`aclose()` 关闭自建的池。

不负责：

- 读 API key（由调用方传入）、选择模型（`ModelSpec` 由调用方决定）；
- 实现 `read_file`、`bash`、`edit`、MCP 等具体工具；
- 会话落盘、恢复、压缩；
- 渲染 UI、权限弹窗、写日志；
- 网络重试（由 `ai` 层负责）。

## 对外契约

- `ToolResult(content: str, is_error: bool = False, terminate: bool = False, details: Any = None, structured_content: Any = None, usage: Usage | None = None)`
- 8 个运行级事件：`AgentStart`、`TurnStart`、`ToolExecutionStart`、`ToolExecutionUpdate`、`ToolExecutionEnd`、`TurnEnd`、`AgentDone`、`AgentError`
- `AgentTool`：`spec: ToolSpec`，`execution_mode: "serial" | "parallel"`，`async def execute(call_id, arguments, on_update=None) -> ToolResult`
- `LoopConfig`：`system`、`tools`、`max_turns`、`max_output_tokens`、`temperature`、`reasoning`、`tool_timeout`、`hooks`、`get_steering_messages`、`get_follow_up_messages`
- `AgentHooks`：`before_run` / `transform_messages` / `prepare_request` / `before_tool_call` / `after_tool_call` / `finish_turn` / `prepare_next_turn` / `before_finish` / `on_max_turns`，全部可选
- `AgentError.kind`：`provider` / `aborted` / `max_turns` / `config`
- `Agent.connect(spec, config, *, api_key="", client=None)` 与 `Agent.aclose()`：自建的连接池由 `aclose()` 关闭，注入的只借用不关闭。
- 终止事件只有 `AgentDone` 和 `AgentError` 两个；`max_turns` 按模型请求次数计。

字段、事件顺序与终止语义见 [docs/design.md](docs/design.md)。

## 目录

```
gradus/agent/
├── types.py          # 事件形状与 ToolResult
├── base.py           # AgentTool 协议与 LoopConfig
├── hooks.py          # 控制 hook 的契约
├── validation.py     # 工具参数的 JSON Schema 校验
├── loop.py           # run_loop 控制流
├── agent.py          # 有状态 Agent 门面
├── queues.py         # steering / follow-up 消息队列
├── docs/
│   └── design.md     # 设计说明：事件、工具、hook、决策表
├── tests/
│   ├── fakes.py          # 共享的假 provider / 假工具
│   ├── test_loop.py      # 循环的离线测试
│   ├── test_parallel.py  # 并行工具批次测试
│   ├── test_terminate.py # 批次提前结束测试
│   ├── test_updates.py   # 工具进度更新测试
│   ├── test_rich_results.py # 富工具结果测试
│   ├── test_agent.py     # Agent 历史与状态测试
│   ├── test_client.py    # 连接池生命周期测试
│   ├── test_steering.py  # steering / follow-up 注入测试
│   ├── test_queues.py    # 消息队列测试
│   ├── test_hooks.py     # hook 行为测试
│   ├── test_tools.py     # 参数校验与超时测试
│   └── test_boundary.py  # import 边界检查
├── README.md
└── README.zh.md
```

没有 `__init__.py`，和 `gradus/ai` 一样使用 namespace package。

## 入口

```python
async def run_loop(
    provider: Provider,
    messages: list[Message],
    config: LoopConfig,
) -> AsyncIterator[AgentEvent]: ...
```

上层消费：

```python
from gradus.agent.loop import run_loop
from gradus.agent.types import AgentDone, AgentError

async for event in run_loop(provider, messages, config):
    if isinstance(event, AgentDone):
        messages.extend(event.new_messages)
    elif isinstance(event, AgentError):
        print(event.kind, event.message)
```

`provider` 由上层构造；`messages` 不会被修改，新增消息从 `AgentDone.new_messages` 取。循环是异步生成器，持久化归上层。

### 有状态 `Agent`

```python
from gradus.agent.agent import Agent

agent = Agent(
    provider,
    config,
    steering_mode="all",
    follow_up_mode="one-at-a-time",
)
async for event in agent.prompt("读一下 a.py"):
    ...

agent.steer("顺便看看 b.py")      # 运行中插话
agent.follow_up("再总结一下")     # 本来要停时继续
await agent.wait_for_idle()
agent.reset()
```

`Agent` 自己保存历史，支持 `prompt`、`continue_`、`steer`、`follow_up`、`abort`、`wait_for_idle`、`reset`。

- `steer` / `follow_up` 只在运行中可用，空闲时抛 `RuntimeError`；
- 每条消息的队列只属于当前这次运行，没被消费就随运行结束丢弃，不会进入下一次运行；
- `state` 只表示运行 task 的状态，不表示事件是否已经消费完。

想让连接池跨轮复用，用 `connect()` 建 Provider 与 client，结束时 `aclose()`：

```python
agent = await Agent.connect(spec, config, api_key="sk-...")
try:
    async for event in agent.prompt("读一下 a.py"):
        ...
finally:
    await agent.aclose()
```

`client=None` 时 agent 自建并拥有，`aclose()` 关闭；传入 client 时只借用，由创建者关闭——同一个池子可以给多个 Agent / Provider 共用。直接用 `Agent(provider, config)` 则不经过这一步，每次 `stream()` 自建连接。

## 并发与取消

- `ai` 层已经全异步，`run_loop` 也一路异步。
- `AgentTool.execute` 是异步的。同步工具由上层用 `asyncio.to_thread` 包一层，循环本身不阻塞。
- 并行由工具作者通过 `execution_mode="parallel"` 声明，agent 不从参数推断；只要批次里有一个串行工具，整批就串行。
- 取消时 `run_loop` 先发 `AgentError(kind="aborted")`，再重新 `raise asyncio.CancelledError`；不要吞掉 `CancelledError`，否则任务不会真正取消。
- 测试使用 `unittest.IsolatedAsyncioTestCase`，与 `ai` 层一致，不引入 `pytest-asyncio`。

## 事件

agent 自己的事件：

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

`gradus.ai` 的 12 个事件原样透传。`StreamDone` 表示单轮结束，`AgentDone` 表示整次运行结束。

## 测试

```powershell
uv run pytest gradus/agent/tests
```

使用异步的 `FakeProvider` 和本地异步假工具，不联网。测试数量与覆盖范围见 [agent_plan.md](agent_plan.md)。

## 更细的内容

- [agent_plan.md](agent_plan.md)：里程碑、进度检查、偏差记录。
- [docs/design.md](docs/design.md)：事件嵌套、工具执行契约、`stop_reason` 决策表、未决问题。
- [docs/problem.md](docs/problem.md)：只归档已解决的问题；未解决项见 agent_plan.md。
