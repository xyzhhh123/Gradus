# coding 层设计

## 1. 定位

`coding` 是 Gradus 的产品层。它不定义循环与协议，只决定「用什么模型、拿什么工具、在什么权限下、把结果给谁看」。

## 2. 依赖方向

```
coding  ->  agent  ->  ai
```

- `coding` 可以使用下层的全部公开接口，包括 `Agent.connect` / `Agent.aclose`、`AgentHooks`、`AgentTool`、`create_provider`。
- `coding` 不修改下层协议。需要新的 hook 点或事件字段时，先改 `agent`，再回来用。
- `coding` 独占：环境变量、API key、连接池生命周期、文件系统与子进程访问。

## 3. 装配关系

一次会话的对象图（骨架，尚未实现）：

```text
config.load()            -> ModelSpec
session.Session
  ├── httpx.AsyncClient          # 进程级，所有用户共用
  ├── create_provider(spec, key, client=pool)
  └── Agent.connect(..., client=pool)   # 每用户一个，历史隔离
cli
  └── agent.prompt() -> AsyncIterator[AgentEvent] -> hooks.render()
```

连接池的创建、首次连接与关闭时机见 `agent/docs/design.md` §7.1；`coding` 只负责在进程退出或会话销毁时调用 `aclose()`。

## 4. 模块职责

| 文件 | 职责 |
| --- | --- |
| `config.py` | 从环境变量 / 配置文件读 key、`base_url`、模型名，产出 `ModelSpec`；不做网络 |
| `session.py` | 建池、建 Provider、建 `Agent`；暴露 `prompt` / `abort` / `aclose` |
| `hooks.py` | 把 `AgentEvent` 渲染到终端或日志；把权限策略接进 `before_tool_call` |
| `permissions.py` | 判断一次工具调用是放行、拒绝还是需要确认 |
| `tools/files.py` | `read_file` / `write_file` / `list_dir` |
| `tools/search.py` | `grep` / `glob` |
| `tools/shell.py` | `bash` |
| `cli.py` | 参数解析、交互循环、把用户输入接到 `session` |

## 5. 工具契约

沿用 `agent.base.AgentTool`：

- `spec: ToolSpec`：名字、描述、JSON Schema；
- `execution_mode`：只读工具可声明 `parallel`，写操作与命令保持 `serial`；
- `async execute(call_id, arguments, on_update=None) -> ToolResult`；
- 长输出通过 `on_update` 上报进度；`details` 等富字段只给 UI，不进入模型上下文。

第一版只做 `read_file` 与 `list_dir`，等 CLI 闭环跑通再加写操作与命令。

内置工具与注册工具走同一个契约：MCP 与自定义工具在 M8 通过 `registry.py` 适配成 `AgentTool` 后挂进来，重名直接报错，不覆盖内置工具。

## 6. 权限

`before_tool_call` 返回 `BeforeToolCallResult(block=True, reason=...)` 时，agent 不会执行工具，但会生成一条 `is_error` 结果交给模型。

策略待定：

- 默认全放行，还是危险工具默认拒绝；
- 需要确认时如何把「等待用户确认」这一个异步过程接进同步 hook；
- 白名单按工具名还是按路径 / 命令前缀。

## 7. 会话与历史

第一版历史只放内存，进程退出即丢。落盘的候选格式是 JSONL：一行一个 `Message`，从 `AgentDone.new_messages` / `TurnEnd` 追加。

恢复时必须保证 `ToolCallBlock` 与 `ToolResultMessage` 成对，否则模型侧会拒绝。压缩后如何保持配对也在这条约束下处理。

## 8. 观测

`coding` 订阅 `AgentEvent`，不改事件：

- `ToolExecutionStart` / `End`：展示工具名、耗时、首行结果；
- `ToolExecutionUpdate`：实时输出；
- `AgentError`：打印 `kind` 与消息；
- `StreamDone.usage`：累计 token 与缓存命中。

## 9. 待定

| 问题 | 现状 | 备注 |
| --- | --- | --- |
| 交互形态 | 按 CLI 起步 | 服务端形态替换 `cli.py`，其余模块不变 |
| 历史落盘格式 | 未定 | 候选 JSONL，需保证工具调用与结果成对 |
| 权限确认 | 未定 | 同步 hook 里等待用户输入需要额外机制 |
| tokenizer | 未定 | 压缩与预算需要按模型选 |
| 多 provider | 未定 | 路由需要换 Provider，`Request` 不含模型名 |
