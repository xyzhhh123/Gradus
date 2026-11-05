# agent 层设计

状态：`types.py`、`base.py`、`hooks.py`、`loop.py` 与测试均已完成。本文记录已经定下的形状与循环行为。

## 1. 定位

| 层 | 负责 | 不负责 |
| --- | --- | --- |
| `ai` | 协议面：把厂商格式统一成内部消息与 12 个流式事件 | 多轮循环、工具执行 |
| `agent` | 控制面：一次运行的轮次、工具批次、事件与终止 | 具体工具、会话、UI、配置 |
| `coding` | 产品面：工具实现、会话落盘、压缩、权限、界面 | 协议翻译、循环控制 |

`agent` 的输入是「已构造好的 Provider + 一份历史 + 一组工具实现」，输出是「一条事件流 + 新增消息」。

## 2. 依赖方向

```
coding/   产品层：具体工具、会话、UI
   |
agent/    循环、工具运行时、事件
   |
ai/       Provider、内部协议
```

规则：

- `agent` 只 import `gradus.ai.base`、`gradus.ai.types`，以及工厂 `gradus.ai.providers`（不含 `providers.*` 的具体适配器）。
- `agent` 不 import `gradus.coding`，也不 import `ai` 的 `transport` / `stream_assembler`。
- `agent` 允许 `httpx`：只有 `Agent.connect()` 用它建连接池，其余模块不碰网络。
- 接口由下层定义，实现由上层注入；`agent` 调用被传进来的对象，不 import 具体类。`create_provider` 是唯一例外：它是工厂，按 `ModelSpec.provider` 选实现，agent 不直接 import 任何厂商模块。
- `types.py` 只定义形状，不写逻辑；`base.py` 只定义协议与配置。

## 3. 事件模型

### 3.1 两层事件

- `ai` 的 12 个事件描述**一次模型请求**：`StreamStart` → 各内容块三阶段 → `StreamDone` / `StreamError`。
- `agent` 的 8 个事件描述**整次运行**：运行开始、轮次边界、工具执行、终止结果。
- `agent` 把 12 个事件原样透传，夹在自己的事件之间。

### 3.2 事件顺序

一次带工具的两轮运行：

```text
AgentStart
TurnStart(0)
  StreamStart
  TextStart / TextDelta / TextEnd
  ToolCallStart / ToolCallDelta / ToolCallEnd
  StreamDone(tool_use)
  ToolExecutionStart(read_file)
  ToolExecutionUpdate(read_file, "...")   # 可选，0..n 条
  ToolExecutionEnd(read_file)
TurnEnd(assistant_0, [tool_result_0], usage_0)
TurnStart(1)
  StreamStart
  TextStart / TextDelta / TextEnd
  StreamDone(stop)
TurnEnd(assistant_1, [], usage_1)
AgentDone(new_messages, total_usage, 2, "stop")
```

失败路径：

```text
AgentStart
TurnStart(0)
  StreamStart
  TextStart / TextDelta
  StreamError(error)
AgentError("provider", message, partial, new_messages, usage)
```

失败那一轮不发 `TurnEnd`；半成品放在 `AgentError.partial`，不写入 `new_messages`。

### 3.3 事件字段

| 事件 | 字段 | 何时发出 |
| --- | --- | --- |
| `AgentStart` | 无 | 运行开始 |
| `TurnStart` | `turn: int` | 每次模型请求前，从 0 递增 |
| `ToolExecutionStart` | `call: ToolCallBlock` | 工具执行前 |
| `ToolExecutionUpdate` | `call: ToolCallBlock`、`partial: str` | 工具执行过程中，0..n 次 |
| `ToolExecutionEnd` | `call: ToolCallBlock`、`result: ToolResult`、`duration_ms: int \| None` | 工具执行后 |
| `TurnEnd` | `assistant`、`tool_results`、`usage` | 一轮完成，工具结果已写回 |
| `AgentDone` | `new_messages`、`usage`、`turns`、`stop_reason` | 正常结束 |
| `AgentError` | `kind`、`message`、`partial`、`new_messages`、`usage` | 异常结束 |

`AgentError.kind`：

| 值 | 含义 |
| --- | --- |
| `provider` | 模型流失败，或流结束却没有 `StreamDone` |
| `aborted` | 模型流被中断，或运行被取消 |
| `max_turns` | 达到轮次上限 |
| `config` | 配置非法，例如工具重名 |

### 3.4 终止语义

- 一次运行**恰好一个**终止事件：`AgentDone` 或 `AgentError`。
- `StreamDone` 只表示单轮结束；一次运行可以有多个。
- `AgentDone.new_messages` 只包含本次运行新增的消息，不包含调用方传入的历史。
- `AgentDone.usage` 是各轮 `TurnEnd.usage` 的总和。
- `AgentDone.stop_reason` 是最后一条 assistant 消息的 `stop_reason`。
- 一个工具批次的每个结果都带 `terminate=True` 时，`TurnEnd` 后不再请求模型，直接 `AgentDone`；`stop_reason` 仍是最后一条 assistant 的 `tool_use`。

## 4. 工具执行契约

### 4.1 接口与注入

```python
class AgentTool(Protocol):
    spec: ToolSpec
    execution_mode: Literal["serial", "parallel"]

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: Callable[[str], None] | None = None,
    ) -> ToolResult: ...
```

- 工具实例由上层构造，通过 `LoopConfig.tools` 注入；`agent` 只按 `spec.name` 查找。
- `execution_mode` 由工具作者声明，`agent` 不分析参数、不做运行时推断；不提供时按 `serial` 处理。
- `on_update` 是执行期间的进度回调，调用后转成 `ToolExecutionUpdate`；`execute` 返回后的调用会被丢弃。
- `execute` 是异步的；同步工具由上层用 `asyncio.to_thread` 包装。
- 工具只拿 `call_id` 和已解析的 `arguments`，不接触 `Agent`、`Session` 或完整历史。
- 工具可以返回 `ToolResult(is_error=True)`，也可以抛异常；循环都会转成错误结果。

### 4.2 查找与校验

1. 用 `call.name` 在注册表里查找；找不到就生成 `is_error=True` 的结果，消息里说明未知工具。
2. 检查 `ToolSpec.parameters` 里的 `required` 字段是否齐全；缺失同样生成错误结果。这是快速失败路径。
3. 用 `validation.py` + `jsonschema`（Draft 2020-12）做完整校验；类型、枚举、格式等不合法都生成错误结果，不进入工具。
4. schema 本身非法时不阻塞运行，但会生成 `invalid parameters schema for <tool>` 错误结果。
5. 参数被截断时 `arguments` 可能是空 dict；只有 `stop_reason == "tool_use"` 才进入执行。`length` 不执行，但有工具调用时会为每个调用写错误结果并进入下一轮。

### 4.3 顺序、超时与完整性

- 同一批工具调用默认按 assistant 消息里的原始顺序**串行**执行。
- 只有批次内**每个**调用对应的工具都声明 `execution_mode="parallel"` 时才并发执行；只要有一个工具是 `serial`、未声明或查不到，整批退回串行（规则 A）。
- 并发时 `ToolExecutionStart` 按源顺序发出，`ToolExecutionEnd` 按完成顺序发出；`ToolResultMessage` 仍按源顺序写回。
- 每个被执行的 `ToolCallBlock` 都必须生成一条 `ToolResultMessage`，失败、未知工具、参数缺失、超时也不例外；否则下一轮请求可能被厂商拒绝。
- `LoopConfig.tool_timeout` 不为 `None` 时，用 `asyncio.timeout` 包住 `execute()`；超时生成 `is_error=True` 的结果，运行继续。
- `ToolExecutionEnd.duration_ms` 记录从 `ToolExecutionStart` 到结束的毫秒数，包含查找、校验、hook 与执行。
- `ToolResult` 到 `ToolResultMessage` 的映射：

```python
ToolResultMessage(
    tool_call_id=call.id,
    content=result.content,
    is_error=result.is_error,
)
```

#### 并行批次

- 并行是工具作者的声明，`agent` 不判断两个调用是否真的互不冲突；声明 `parallel` 表示作者保证可重入、无共享可变状态、不依赖同批其它调用。
- 并发任务用 `asyncio.wait` 收集；单个任务的异常、超时、未知工具、参数错误都只影响该调用的结果。
- 任一任务抛 `CancelledError` 或整次运行被取消时，其余任务被 `cancel()` 并等待清理，当前轮不提交，符合 AG-02 的原子性。
- 并发数暂不设上限：同一批调用通常很少，需要时再加 `max_parallel_tools`。

#### 工具更新事件

- 工具在执行期间调用 `on_update(partial)`，循环把它转成 `ToolExecutionUpdate(call, partial)` 即时发出。
- 每个调用的顺序固定为 `Start → Update* → End`：完成时先冲掉队列里剩余的更新，再发该调用的 `End`。
- 串行批次不交错；并行批次里不同调用的更新会交错，消费方按 `call.id` 归属。
- update 只用于 UI / 日志，不写入 `ToolResultMessage`、`new_messages` 或历史。
- 取消时未发出的更新直接丢弃，工具返回后的 `on_update` 调用也不会再生效。

#### 批次提前结束（terminate）

- `ToolResult.terminate`、`BeforeToolCallResult.terminate`、`AfterToolCallResult.terminate` 都可以给出提示；`None` 表示保留工具原值。
- 只有**整批每个结果**都为 `True` 时才生效；混合批次照常请求模型。
- 生效时只跳过“把工具结果交回模型”的那次自动请求：工具结果照常写入历史并发出事件，`finish_turn("end")` 仍然优先，steering 仍可强制继续，follow-up 仍会在停止边界执行。
- `terminate` 是运行时提示，不写入 `ToolResultMessage`，也不改变 `AgentDone.stop_reason`。

#### 超时的边界

`asyncio.timeout` 只让循环**不再等待**，不保证底层工作已经停止：

- 纯 async 工具：在 `await` 点收到 `CancelledError`，通常会被真正取消；
- 用 `asyncio.to_thread` 包装的同步工具：线程无法取消，会继续运行到函数结束，结果被丢弃；
- 工具自己启动的子进程 / shell：进程继续运行，除非工具在取消路径主动终止；
- 工具吞掉 `CancelledError`：即使 async 也可能继续；
- CPU 密集且不 `await` 的工具会阻塞事件循环，超时定时器无法触发。

由此产生的后果：副作用可能已经发生、模型重试可能重复执行、卡住的线程会占用线程池、连接或临时文件可能泄漏。

因此工具契约要求：

1. 不吞 `CancelledError`，让取消正常向上传播；
2. 在 `finally` 或取消路径清理连接、临时文件与子进程；
3. 需要“强杀”的工具应自己管理子进程生命周期；agent 不替工具杀线程，也无法强杀线程。

### 4.4 富工具结果

`ToolResult` 的字段按用途分开：

| 字段 | 用途 | 进入历史 | 计入 `AgentDone.usage` |
| --- | --- | --- | --- |
| `content` / `is_error` | 模型下一轮能看到的文本结果 | 是 | 否 |
| `details` | UI / 日志用的结构化附加信息 | 否 | 否 |
| `structured_content` | 机器可读的 JSON 结果 | 否 | 否 |
| `usage` | 工具自身消耗，例如子代理的 token | 否 | 否 |

- 只有 `content` / `is_error` 会写成 `ToolResultMessage`；模型看不到 `details`，因此不会占用上下文 token。
- `details` / `structured_content` / `usage` 只通过 `ToolExecutionEnd.result` 暴露给上层。
- `after_tool_call` 改写 `content` / `is_error` / `terminate` 时，其余字段原样保留。
- 多模态内容或让结构化结果进入后续模型请求，需要先扩展 `ai/types.py` 的 `ToolResultMessage`，不属于 agent 单独实现。

### 4.5 实现细节

- 工具重名在发请求前直接结束：`AgentError(kind="config")`，不会进入循环。
- 错误结果的消息格式是固定的，便于测试和上层展示：
  - 未知工具：`unknown tool: <name>`
  - 缺少必填参数：`missing required arguments for <name>: <a>, <b>`
  - 参数不符合 schema：`invalid arguments for <name>: <path>: <message>`
  - schema 非法：`invalid parameters schema for <name>: <message>`
  - 工具超时：`tool <name> timed out after <seconds>s`
  - `length` 截断：`tool <name> was not executed: response was truncated`
  - 工具抛异常：`<ExceptionType>: <message>`
- `StreamError(kind="error")` → `AgentError(kind="provider")`；`StreamError(kind="aborted")` → `AgentError(kind="aborted")`。
- provider 自身违约抛异常时，循环把它转成 `AgentError(kind="provider")`，消息为 `<ExceptionType>: <message>`；`asyncio.CancelledError` 不在此列。
- `asyncio.CancelledError` 先由 `run_loop` 转成 `AgentError(kind="aborted")` 发出，然后重新 `raise`，不在循环内部吞掉。

## 5. Hooks

hook 是控制点，不是事件监听器：事件只读，hook 可以拦截或改写。全部可选，未设置时行为与无 hook 版本一致。

顺序：

```text
before_run
-> transform_messages -> prepare_request -> provider.stream
-> before_tool_call -> execute -> after_tool_call
-> finish_turn -> TurnEnd -> prepare_next_turn
-> before_finish -> AgentDone

on_max_turns：轮次到达上限、尚未发 AgentError(max_turns) 时。
```

| Hook | 时机 | 返回 | 失败处理 |
| --- | --- | --- | --- |
| `before_run` | `AgentStart` 后、循环前 | 替换初始 `messages` / `config` | `AgentError(config)` |
| `transform_messages` | 每次请求前 | 新消息列表（只影响本次请求） | `AgentError(config)` |
| `prepare_request` | 请求组装后 | 新 `Request` 或 `None` | `AgentError(config)` |
| `before_tool_call` | `ToolExecutionStart` 后、必填与 schema 校验后 | `block` / `reason` | 错误工具结果 |
| `after_tool_call` | 执行后、`ToolExecutionEnd` 前 | 覆盖 `content` / `is_error` | 错误工具结果 |
| `finish_turn` | 工具结果写回后、`TurnEnd` 前 | `"end"` / `"continue"` / `None` | `AgentError(config)` |
| `prepare_next_turn` | `TurnEnd` 后、下一轮前 | 追加消息 / 新 `LoopConfig` | `AgentError(config)` |
| `before_finish` | 即将发 `AgentDone` 前 | 覆盖 `new_messages` / `usage` / `stop_reason` | `AgentError(config)` |
| `on_max_turns` | 轮次到达上限、尚未发 `AgentError(max_turns)` | `extend_by`（`> 0` 继续） | `AgentError(config)` |

- 工具 hook 抛异常不终止运行，模型会看到错误结果；其余 hook 抛异常终止运行。
- `asyncio.CancelledError` 一律向上传播。
- `before_run` 替换的 `messages` 只进入本次运行的 transcript，不改写调用方列表；替换的 `config` 会重新校验工具重名。
- `prepare_next_turn` 返回的 `messages` 会立即追加到 transcript（下一轮请求可见），但作为下一轮的一部分，随下一轮 `TurnEnd` 一起写入 `new_messages`；如果下一轮没有发生（取消或 `max_turns`），这些消息丢弃。返回的 `config` 会影响下一轮的 system / tools / 参数，并重建工具注册表与轮次上限。
- `transform_messages` / `prepare_request` 只影响当前请求，不改写调用方历史。
- `finish_turn` 在错误 / 取消路径不运行；这两条路径直接 `AgentError`。
- `before_finish` 只在正常 `AgentDone` 前运行，`AgentError` 与取消路径不运行。
- `on_max_turns` 每次到达上限都会调用；返回 `extend_by > 0` 时上限增加后继续，否则照常发 `AgentError(max_turns)`。是否无限续轮由上层决定。

## 6. `stop_reason` 决策表

| `stop_reason` | 动作 | 执行工具 | 终止事件 |
| --- | --- | --- | --- |
| `tool_use` | 执行本批工具，写回结果，进入下一轮 | 是 | 继续 |
| `stop` | 正常结束 | 否 | `AgentDone(stop_reason="stop")` |
| `length` 且无工具调用 | 正常结束（参数可能被截断） | 否 | `AgentDone(stop_reason="length")` |
| `length` 且有工具调用 | 不执行；为每个调用写错误结果，进入下一轮 | 否 | 继续 |
| `deferred` | 追加该消息后立即再请求，计入 `max_turns` | 否 | 继续 |
| `filtered` | 不重试，向用户说明 | 否 | `AgentDone(stop_reason="filtered")` |
| `error` / `aborted` | 只从 `StreamError` 来 | 否 | `AgentError` |
| `tool_use` 但没有调用 | 协议异常，结束 | 否 | `AgentError(kind="provider")` |
| `stop` 但带工具调用 | 结束，不执行 | 否 | `AgentDone(stop_reason="stop")` |

四条容易写错的规则：

- **只认 `tool_use`**。`stop` 即使带了工具调用也不执行，避免把协议异常当成正常请求。
- **`tool_use` 必须有调用**。没有调用是协议异常，发 `AgentError(kind="provider")`，不能当正常结束。
- **`length` 不执行，但要补错误结果**。输出被截断时工具参数可能只写了一半；有调用时为每个调用写 `is_error=True` 的结果并续跑，让模型重新发起。
- **`deferred` 不执行工具**。即使消息里带了工具调用，也先原样续跑，不消耗这一批调用。

`max_turns` 耗尽时：

- 先调用 `on_max_turns`；返回 `extend_by > 0` 时提高上限继续，不再发 `AgentError`；
- 未延长时，最后一轮如果是 `tool_use` 或 `deferred`，先把这一轮执行完并发出 `TurnEnd`，再发 `AgentError(kind="max_turns")`；
- `AgentError.new_messages` 包含已经完成的轮次，不包含“下一轮”；
- `AgentError.partial` 为 `None`，因为失败不是一个半成品 `AssistantMessage`。

## 7. 数据归属与用量

- `messages` 是只读输入；循环内部复制一份 transcript，调用方传入的列表不被修改。
- 新增的 assistant 消息与工具结果写入 `new_messages`，由终止事件交还上层。
- 一轮的消息先在本地组装，`TurnEnd` 前一次性写入 transcript 和 `new_messages`；取消时整轮丢弃，不会留下“有调用、没结果”的半轮。
- 历史、落盘、恢复、分支、压缩全部归 `coding`。
- `TurnEnd.usage` 是本轮模型请求的用量；`AgentDone.usage` 是整次运行的累计值。
- 失败时，`AgentError.usage` 也包含失败那一轮 `partial.usage`（如果厂商给了），避免漏掉已经产生的消耗。
- `Usage` 的语义由 `ai` 层统一：`input_tokens` 是总输入，缓存两项是它的子集。

### 7.1 Agent 门面

`Agent` 是 `run_loop` 之上的状态封装，持有：

- `provider` 与 `LoopConfig`；
- `messages`：跨运行的历史；
- `state`：`idle` / `running`；
- 当前运行的 task 与事件队列。

约定：

- `prompt(text)` 先追加 `UserMessage(text)`，再启动一次运行；`continue_()` 不追加消息。
- 调用即启动运行；返回的异步迭代器只负责消费事件，不消费也会继续执行并写入历史。
- 运行中再次 `prompt()` / `continue_()` 抛 `RuntimeError`；插话后续用 `steer()`。
- 正常结束时用终止事件的 `new_messages` 写回历史；取消时 `run_loop` 会发 `AgentError(kind="aborted")`，历史只保留已经发出 `TurnEnd` 的轮次，半轮与未使用的 `prepare_next_turn` 消息丢弃。
- `abort()` 取消内部 task；`wait_for_idle()` 等待但不取消；`reset()` 只允许在 idle 时清空历史。
- `steer(text)` / `follow_up(text)` 只在运行中可用，空闲时抛 `RuntimeError`。
- 每次运行新建 steering / follow-up 两个队列，并把它们的 `drain` 接到当次 `LoopConfig`；运行结束即丢弃队列，未消费的消息不会进入下一次运行。
- `state` 表示运行 task 是否结束，不表示事件队列是否消费完。
- 事件形状不变，`Agent` 只做状态与生命周期管理。

连接池的寿命：

- **创建**：`Agent.connect()` 返回前只建 `httpx.AsyncClient` 容器（连接池配置），不建 TCP。
- **首次连接**：第一次 `prompt()` 触发第一次模型请求时才建立 TCP/TLS；只 `connect()` 不运行不会连。
- **复用**：同 host 的后续请求从池里取空闲连接；两轮间隔超过 `keepalive_expiry`（当前 httpx 默认 5 秒）时连接已过期，下一次请求重建。
- **关闭**：`aclose()` 先 `abort()` 再关自建的 client，重复调用幂等；关闭后 `prompt()` / `continue_()` / `reset()` 抛 `RuntimeError`。代码不做超时关闭、引用计数或 GC 兜底，判断交给调用方：CLI 在进程退出时、服务端在会话销毁时（不是每次请求结束）、测试在用例结束时。
- 三种层级不要混：一次模型请求是一轮（`StreamDone` / `StreamError` 结束）；一次 `prompt()` 是一次运行（`AgentDone` / `AgentError` 结束）；Agent 生命周期从 `connect()` 到 `aclose()`。连接池跨多次运行存在，**不随 `AgentDone` 关闭**。
- `client=None` 时自建并拥有，`aclose()` 关闭；传入时只借用，由创建者关闭——同一个池子可以给多个 Agent / Provider 共用。
- 直接用 `Agent(provider, config)` 时不经过 `connect()`，池子走 ai 层默认行为：每次 `stream()` 自建并在结束时关闭，没有跨轮复用。

多用户与共享：

- **不要按用户分池**。连接与用户无关，`api_key` 在请求头里；推荐一个进程一个池，所有 Agent 共用。
- `Agent` 实例不共享：历史、状态、steering / follow-up 队列都按实例隔离；粒度由上层决定（coding 定为一个会话一个）；共享的只是 client，不是 Agent。
- 共享时所有 Agent 传同一个 client；只有 host、代理、TLS 或 `limits` 不同才需要另建池。一个 `AsyncClient` 本来就能管理多个 host 的连接。
- **当前没有自动全局池**：不传 client 时每个 Agent 自建一个。把池提到进程级的装配属于 coding 层（`coding_plan.md` 的 M0），agent 只提供注入口。

### 7.2 待注入消息队列

`queues.py` 的 `PendingMessageQueue` 只做暂存与排空：

- `push()` / `drain()` / `clear()` / `len()`；
- `mode="all"` 一次取走全部；`mode="one-at-a-time"` 一次只取最早一条；
- 队列本身不区分 steer / follow-up，两个实例由 `Agent` 在每次运行时新建并持有；
- 队列生命周期等于单次运行：运行结束即丢弃，没被 drain 的消息不会泄漏到下一次运行；
- 消息真正写入 transcript 的时机由 `run_loop` 的 drain 点决定。

`run_loop` 的 drain 点：

- **steering**：每次请求前、每轮结束后各取一次；取到的消息写入 transcript，并随下一轮提交进入 `new_messages`。
- **follow-up**：只有在本轮本来要结束（且 `finish_turn` 没有返回 `"end"`）时取一次；有消息就继续下一轮，没有就发 `AgentDone`。
- 两个 getter 抛异常时统一转成 `AgentError(kind="config")`。
- `max_turns` 仍按模型请求次数计，steering / follow-up 产生的请求也计入。

两种消息对应的用户操作不同：

- steering 对应“任务执行中插话”，例如让 agent 停手、换方案、补充约束；它是对当前任务的修正，必须在下一步就生效，因此可以阻止本轮结束。
- follow-up 对应“任务完成后再做下一件”，例如等 agent 说完成后才要跑测试；它不打断当前任务，只在本来要停止时被消费。

注入点选在请求前与轮次后，是因为流式请求和工具批次一旦开始就无法追加消息：请求前注入能进入下一次模型调用；轮次后注入决定 `should_continue`，否则模型已经给出停止原因，循环会直接 `AgentDone`，插话没有机会生效。


## 8. 异步与取消

- `Provider.stream()` 与 `run_loop()` 都是异步生成器；上层用 `async for` 消费。
- `AgentTool.execute` 是异步的；循环内不调用阻塞 I/O。
- 取消时 `run_loop` 先发 `AgentError(kind="aborted", partial=<最后一个快照>, new_messages=<已完成轮次>)`，再重新 `raise asyncio.CancelledError`；`aclose()` 触发的 `GeneratorExit` 不发事件。
- 工具批次执行到一半被取消时，当前轮不提交；上层不会看到不完整的工具结果。
- 测试使用 `unittest.IsolatedAsyncioTestCase`，与 `ai` 层一致，不引入 `pytest-asyncio`。

## 9. 不变量

1. 除 `asyncio.CancelledError` 外，`run_loop` 不抛异常；provider 违约异常、`StreamError`、工具异常都编码成事件。
2. 一次运行恰好一个终止事件：`AgentDone` 或 `AgentError`。
3. `ai` 的 12 个事件原样转发，不改形状、不改顺序。
4. 每个工具调用都有且只有一条对应的工具结果。
5. 只有 `stop_reason == "tool_use"` 才执行工具。
6. 调用方传入的 `messages` 不被修改。
7. `agent` 不 import `coding`，不 import `ai` 的具体适配器；网络只出现在 `Agent.connect()` 建的 client 里。
8. 每个工具调用的 `ToolExecutionUpdate` 只出现在它的 `ToolExecutionStart` 与 `ToolExecutionEnd` 之间，且不写入历史。

## 10. 实现状态与文件职责

| 文件 / 函数 | 职责 |
| --- | --- |
| `types.py` | 事件与 `ToolResult` 的形状 |
| `hooks.py` | 9 个控制钩子的 Context / Result / `AgentHooks` |
| `base.py` | `AgentTool` 协议（含 `execution_mode`）与 `LoopConfig`（含 `hooks`、`tool_timeout`、两个 getter） |
| `validation.py` | 基于 `jsonschema` 的完整参数校验与错误格式化 |
| `loop.py` / `run_loop` | 轮次、终止判断、事件转发、hook 调用 |
| `agent.py` / `Agent` | 有状态门面：历史、状态、`steer` / `follow_up`、`abort` / `wait_for_idle` / `reset`、`connect` / `aclose`（连接池寿命） |
| `queues.py` / `PendingMessageQueue` | steering / follow-up 的待注入消息队列 |
| `_build_registry` | 按名字建索引、检查重名 |
| `_can_run_parallel` | 规则 A：整批调用是否全部可并行 |
| `_run_tool` | 执行一次工具调用并计时，注入 `on_update` |
| `_drive_tool_tasks` | 等待工具完成，同时转发更新；完成时先冲队列再发 `ToolExecutionEnd` |
| `_execute_tool` | 查找、必填检查、schema 校验、before/after hook、超时、执行、异常归一 |
| `_finalize` | `AgentDone` 前调用 `before_finish` 并改写最终结果 |
| `_missing_required` | 只检查 JSON Schema 的 `required` |

已完成：

- `types.py`：`ToolResult`（含 `terminate` / `details` / `structured_content` / `usage`）+ 8 个运行级事件 + `AgentEvent`。
- `hooks.py`：`before_run`、`transform_messages`、`prepare_request`、`before_tool_call`、`after_tool_call`、`finish_turn`、`prepare_next_turn`、`before_finish`、`on_max_turns`。
- `base.py`：`AgentTool` 协议（含 `execution_mode`）+ `LoopConfig`（含 `hooks: AgentHooks`、`tool_timeout`）。
- `validation.py`：`jsonschema`（Draft 2020-12）参数校验，错误消息按字段路径排序。
- `loop.py`：单层异步循环，包含 `stop_reason` 分支、串行 / 并行工具批次、超时、usage 汇总、provider 异常边界、`tool_use` 零调用协议检查、整轮原子提交与 9 个 hook 接线。
- `agent.py`：`Agent` 门面，内部 task 驱动 `run_loop`，事件经队列转发；维护跨运行历史与 `idle` / `running` 状态；`steer` / `follow_up` 的队列按运行创建并在运行结束时丢弃；`connect()` 建 Provider 与连接池，`aclose()` 关闭自建 client（注入的不关）。
- `queues.py`：`PendingMessageQueue`，支持 `all` / `one-at-a-time`；已接入 `Agent`。
- `tests/test_loop.py`：23 个用例，覆盖文本、工具、顺序、错误归一、`length`（有无工具调用两条路径）、`stop`、`deferred`、`max_turns`（含 0）、`StreamError`、缺少 `StreamDone`、非 `ToolResult`、`partial.usage`、配置错误、provider 异常、取消与 usage。
- `tests/test_agent.py`：13 个用例，覆盖跨运行历史、`continue_`、`reset`、运行中拒绝新运行、abort 保留已完成轮次、abort 丢弃未使用的注入消息、`wait_for_idle`、工具批次后的 `steer`、停止前的 `follow_up`、空闲时拒绝插话、steering / follow-up 不跨运行泄漏、`one-at-a-time` 模式。
- `tests/test_client.py`：4 个用例，覆盖自建 client 被 `aclose()` 关闭、注入 client 跨两轮复用且不被 agent 关闭、关闭后拒绝新运行、构造失败不泄漏 client。
- `tests/test_parallel.py`：4 个用例，覆盖并发重叠、完成顺序与源顺序分离、一个串行工具导致整批串行、取消时丢弃整轮并清理并行任务。
- `tests/test_terminate.py`：6 个用例，覆盖整批终止、混合批次继续、`before_tool_call` / `after_tool_call` 终止、终止后的 follow-up 与 steering。
- `tests/test_updates.py`：4 个用例，覆盖更新位于 Start / End 之间、并行下按调用归属且不越序、更新不写入历史、无更新时不发事件。
- `tests/test_rich_results.py`：2 个用例，覆盖 `details` / `structured_content` / `usage` 只出现在事件中且不计入运行用量、`after_tool_call` 改写时保留富字段。
- `tests/test_steering.py`：7 个用例，覆盖请求前 / 轮次后的 steering、停止前的 follow-up、`finish_turn("end")` 跳过 follow-up 与 getter 异常。
- `tests/test_queues.py`：4 个用例，覆盖 `all` / `one-at-a-time`、`clear` / `len` 与非法模式。
- `tests/test_tools.py`：4 个用例，覆盖参数类型校验、超时、`duration_ms` 与超时下的取消传播。
- `tests/test_hooks.py`：16 个用例，覆盖 9 个 hook 的正常路径与失败路径。
- `tests/fakes.py`：共享假 provider、假工具、脚本构造器与 `RecordingProvider`。
- `tests/test_boundary.py`：2 个用例，检查 import 边界与 `types.py` 只依赖 `ai.types`。
- `pyproject.toml`：`testpaths` 已包含 `gradus/agent/tests`。
- 模块 import 通过，无诊断错误。

验证：

- `uv run pytest gradus/agent/tests -q` → 89 passed。
- `uv run pytest -q` → 215 passed（ai 126 + agent 89）。

后续：

- M7 的 agent 侧已全部完成；富结果进入历史、多模态等跨层项需要先做模型能力判断，暂不实现。
- 详见 [agent_plan.md](../agent_plan.md) 的「对齐 Pi 的改进计划」。

## 11. 未决问题

未决问题与分层归属见 [agent_plan.md](../agent_plan.md) 的「折中与分层归属」；[problem.md](problem.md) 只归档已解决问题。

| 问题 | 现状 | 备注 |
| --- | --- | --- |
| 上下文压缩 | 未做 | 归 coding，需要摘要模型调用与存储 |
| 子代理 / MCP | 未做 | 都是 coding 层实现的 `AgentTool` |
