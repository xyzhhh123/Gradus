# agent 层实施计划

## 目标与边界

**目标**：把 `ai` 层的一次补全，串成一次带工具的运行；对上暴露 `run_loop()` 与 `AgentEvent`，M6 再增加有状态 `Agent` 门面。

**边界**：只改 `gradus/agent/`。不碰 `ai/`、`coding/`。跨层结论（会话落盘、压缩、权限）只记录在本文档，不在本层实现。

**依赖方向**（不可逆）：

```
coding/*  ->  agent/*  ->  ai/*
```

`agent` 只 import `gradus.ai.base` / `gradus.ai.types`；不 import `gradus.coding`、`gradus.ai.providers`、`httpx`。

## 里程碑

| 编号 | 内容 | 状态 |
| --- | --- | --- |
| M0 | `types.py`：`ToolResult` + 运行级事件 + `AgentEvent` | 完成 |
| M1 | `base.py`：`AgentTool` 协议 + `LoopConfig` | 完成 |
| M2 | `loop.py`：单层异步循环、工具批次、`stop_reason` 分支 | 完成 |
| M3 | `tests/test_loop.py` + `test_boundary.py` | 完成 |
| M4 | `pyproject.toml` 的 `testpaths` 加上 `gradus/agent/tests` | 完成 |
| M5 | 工具安全与正确性：hooks、完整参数校验、超时 / 取消、`length` 语义 | 完成 |
| M6 | 有状态 `Agent` 与队列：`prompt` / `continue` / `abort`、steering / follow-up | 完成 |
| M7 | 并行与富结果：并行工具、富工具结果、`terminate`、工具更新事件 | 完成（agent 侧；富结果进入历史归 M8） |
| M8 | 跨层：`ai` 多模态 / 动态 key / session、`coding` 模型路由 / 压缩策略 | 归属其它层 |

顺序理由：先把数据形状定死（M0/M1），再写控制流（M2），最后用 `FakeProvider` 把行为固化成测试（M3）。反过来做的话，事件形状会在写循环时反复改，测试也跟着重写。M5 先做，因为它不改事件模型，只加工具执行前后的控制点；M6/M7 再动循环结构和工具数据契约。

## 完成定义

每个里程碑结束时必须同时满足：

1. `uv run python -c "import gradus.agent.<模块>"` 通过。
2. 事件顺序符合契约：`AgentStart` 打头，`TurnStart` / `TurnEnd` 成对，结尾恰好一个 `AgentDone` 或 `AgentError`。
3. 在 `tool_use` 或 `length` 的批次中，每个 `ToolCallBlock` 都有且只有一条对应的 `ToolResultMessage`；`stop` 带工具调用时不产生结果。
4. 只有 `stop_reason == "tool_use"` 才执行工具。
5. 调用方传入的 `messages` 不被修改。
6. 本文档的「进度检查」与「偏差记录」都已更新。

## 进度检查

### 累计进度（更新至 2026-11-03）

**已完成**

- `types.py`：`ToolResult`（含 `terminate` / `details` / `structured_content` / `usage`）、8 个运行级事件（含 `ToolExecutionUpdate`）、`AgentEvent` 联合；只依赖标准库与 `gradus.ai.types`。
- `base.py`：`AgentTool` 协议（`spec` + `execution_mode` + `async execute`）、`LoopConfig`（含 `hooks`、`tool_timeout`、`get_steering_messages`、`get_follow_up_messages`）；只依赖 `gradus.ai.base`、`gradus.ai.types`、`gradus.agent.hooks` 与 `gradus.agent.types`。
- `hooks.py`：9 个控制 hook 的 Context / Result / 回调类型与 `AgentHooks` 注册对象：`before_run`、`transform_messages`、`prepare_request`、`before_tool_call`、`after_tool_call`、`finish_turn`、`prepare_next_turn`、`before_finish`、`on_max_turns`；全部 async 且可选。
- `validation.py`：`jsonschema`（Draft 2020-12）完整参数校验与错误格式化。
- `loop.py`：单层异步循环，包含 `stop_reason` 分支、串行 / 并行工具批次、`asyncio.timeout` 超时、`length` 截断补错误结果、steering / follow-up drain 点、usage 汇总、provider 异常边界、`tool_use` 零调用协议检查、整轮原子提交，并接入 9 个 hook。
- `agent.py`：有状态 `Agent` 门面，内部 task 驱动 `run_loop`，事件经队列转发；支持 `prompt` / `continue_` / `abort` / `wait_for_idle` / `reset` / `steer` / `follow_up` 与跨运行历史；steering / follow-up 队列按单次运行创建，运行结束即丢弃。
- `queues.py`：`PendingMessageQueue`，支持 `all` / `one-at-a-time` 两种排空模式。
- `tests/fakes.py`：共享 `FakeProvider`、`FakeTool`、`RaisingProvider`、`NoDoneProvider`、`BadResultTool`、`CancelOnSecondTool`、`RecordingProvider`、`collect()`。
- `tests/test_loop.py`：23 个用例，覆盖文本、工具、顺序、错误归一、`length`（有无工具调用两条路径）、`stop`、`deferred`、`max_turns`（含 0）、`StreamError`、缺少 `StreamDone`、非 `ToolResult`、`partial.usage`、配置错误、provider 异常、取消与 usage。
- `tests/test_agent.py`：13 个用例，覆盖跨运行历史、`continue_`、`reset`、运行中拒绝新运行、abort 保留已完成轮次、abort 丢弃未使用的注入消息、`wait_for_idle`、工具批次后 `steer`、停止前 `follow_up`、空闲时拒绝插话、steering / follow-up 不跨运行泄漏与 `one-at-a-time` 模式。
- `tests/test_parallel.py`：4 个用例，覆盖并发重叠、完成顺序与源顺序分离、一个串行工具导致整批串行、取消时丢弃整轮并清理并行任务。
- `tests/test_terminate.py`：6 个用例，覆盖整批终止、混合批次继续、`before_tool_call` / `after_tool_call` 终止、终止后的 follow-up 与 steering。
- `tests/test_updates.py`：4 个用例，覆盖更新位于 Start / End 之间、并行下按调用归属且不越序、更新不写入历史、无更新时不发事件。
- `tests/test_rich_results.py`：2 个用例，覆盖富字段只出现在事件中且不计入运行用量、`after_tool_call` 改写时保留富字段。
- `tests/test_steering.py`：7 个用例，覆盖请求前 / 轮次后的 steering、停止前的 follow-up、`finish_turn("end")` 跳过 follow-up 与 getter 异常。
- `tests/test_queues.py`：4 个用例，覆盖 `all` / `one-at-a-time`、`clear` / `len` 与非法模式。
- `tests/test_hooks.py`：16 个用例，覆盖 9 个 hook 的正常路径与失败路径。
- `tests/test_tools.py`：4 个用例，覆盖参数类型校验、超时、`duration_ms` 与超时下的取消传播。
- `tests/test_boundary.py`：2 个用例，检查 import 边界、第三方白名单与 `types.py` 只依赖 `ai.types`。
- `tests/test_client.py`：4 个用例，覆盖自建 client 被 `aclose()` 关闭、注入 client 跨两轮复用且不被 agent 关闭、关闭后拒绝新运行、构造失败不泄漏 client。
- `pyproject.toml`：`testpaths` 已包含 `gradus/agent/tests`；新增 `jsonschema` 依赖。
- 所有模块 import 通过，无诊断错误。
- `AG-01`～`AG-03`、`AG-04`（超时部分）、`AG-11`、`AG-13`、`AG-14`、`AG-15` 与 `AG-16` 已修复，归档见 `docs/problem.md`。

**验证结果**

- `uv run python -c "import gradus.agent.types, gradus.agent.base, gradus.agent.hooks, gradus.agent.validation, gradus.agent.queues, gradus.agent.loop, gradus.agent.agent"` 通过。
- `uv run pytest gradus/agent/tests -q` → 89 passed。
- `uv run pytest -q` → 215 passed（ai 126 + agent 89）。

**未覆盖**

- M7 的 agent 侧已完成；富结果进入历史与多模态需要模型能力判断，属于 M8，当前不做。

## 偏差记录

| 编号 | 原规划 | 实际 | 处理 |
| --- | --- | --- | --- |
| D1 | `ai` 层是同步迭代器，agent 也按同步设计 | 用户把 `ai` 全链路改成异步 | 保留：`run_loop` 改为异步生成器，`AgentTool.execute` 为 async；同步工具由上层 `asyncio.to_thread` 包装 |
| D2 | 参考 Pi 做双层 loop（steering / follow-up） | 第一版只做单层 | 保留：M6 再加双层 loop 与 drain 点；`max_turns` 仍按模型请求次数计 |
| D3 | 工具执行可能同步也可能异步 | 契约定成 `async execute` | 保留：agent 内不出现阻塞调用；同步工具由 coding 层适配，agent 不替工具决定执行方式 |
| D4 | 完整校验工具参数 | 第一版只检查 `required` | 已落实：`jsonschema` + `validation.py`；必填检查保留为快速失败路径 |
| D5 | 像 Pi 一样引入 `AgentMessage` + `convertToLlm` | 直接复用 `ai.Message` | 保留：目前没有 UI-only 消息需求；需要时再加转换层，不提前造 |
| D6 | Provider 复用长活 `AsyncClient` | `ai` 层把 client 注入记录为未决项 | 保留：agent 不持有 client，也不 import `httpx`；等会话生命周期定下来再由上层注入 |
| D7 | 文档只有 README | 拆成 README + plan + design + problem | 保留：README 只做入口，plan 记进度与偏差，design 记稳定契约，problem 等真实踩坑再建 |
| D8 | `AgentError.usage` 只累计成功的轮次 | 失败轮次也把 `StreamError.partial.usage` 计入总用量 | 保留：partial 的 usage 是该请求已经产生的消耗，计入更接近真实成本；失败轮仍不写入 `new_messages` |
| D9 | 原计划只加 `before/after_tool_call`，并放在 `base.py` | 实现成 9 个 hook（6 轮次内 + 3 运行级），独立到 `hooks.py`，`LoopConfig.hooks` 统一注册 | 保留：hook 契约与配置分离，避免 `base.py` 膨胀；`transform_messages` / `prepare_request` 从 M7 提前到 M5 完成 |
| D10 | `Agent.continue()` | Python 关键字，改名为 `continue_()` | 保留：`prompt` / `continue_` 调用即启动内部 task，事件经队列转发，这样 `abort()` 才能真正取消运行 |
| D11 | 取消不发终止事件 | `run_loop` 外包一层，取消时先发 `AgentError(kind="aborted")` 再重新 `raise` | 保留：让“每次运行恰好一个终止事件”成立；直接消费 `run_loop` 的调用方也会收到该事件 |
| D12 | `prepare_next_turn` 的消息立即写入 `new_messages` | 改为随下一轮 `TurnEnd` 一起提交，未发生的下一轮则丢弃 | 保留：取消时不会留下“没用上却被写进历史”的注入消息 |
| D13 | 双层 loop（外层 follow-up、内层 steering） | 用同一个 turn 循环实现：steering 作为继续条件，follow-up 在停止前再开一轮 | 保留：行为等价，`max_turns` 仍全局计数；少一层嵌套，drain 点更集中 |
| D14 | steering / follow-up 队列挂在 `Agent` 上，跨运行复用 | 每次 `_start()` 新建两个队列，运行结束置 `None`；空闲时 `steer` / `follow_up` 抛 `RuntimeError` | 保留：上一轮未消费的消息不会被下一次运行误当成新指令；代价是空闲时不能预排队列 |
| D15 | M7 并行批次用“分段屏障”：连续的 parallel 调用成组，serial 调用作屏障 | 采用规则 A：只要批次里有一个工具不是 `parallel`，整批串行 | 保留：规则更容易预测，避免“部分并发、部分屏障”带来的取消与顺序推理成本；代价是一个串行工具会让整批失去并发收益 |
| D16 | `terminate` 允许任一工具结果提前结束 | 采用 Pi 的整批规则：只有批次内每个结果都 `terminate=True` 才跳过下一次请求 | 保留：混合批次里其它工具的结果仍需模型解读；steering 仍可强制继续，follow-up 仍在停止边界生效 |
| D17 | 富工具结果同时进入事件与历史 | 只扩展 `ToolResult` 并在 `ToolExecutionEnd` 暴露；`ToolResultMessage` 仍只写 `content` / `is_error` | 保留：避免为 `details` 等字段改 `ai/types.py`；模型看不到 UI 元数据，也不会计入上下文 token。进入历史与多模态需要模型能力判断，归 M8 |
| D18 | agent 只依赖 `ai.base` / `ai.types`，不碰 `httpx` 与 `providers` | `Agent.connect()` 要建连接池与 Provider，放开 `httpx` 与 `gradus.ai.providers` 工厂；仍禁止 `providers.*` 具体适配器与 `transport` | 保留：连接池的寿命只有会话层知道，ai 层不替它决定。所有权按「谁创建谁关闭」：自建的自关，注入的不关，因此一个池子可以跨多个 Agent / provider 共用 |

## 折中与分层归属

判断标准：属于“循环怎么跑、状态怎么保持一致”的在 agent 修；属于“用什么工具、输出什么内容、什么时候压缩/重试”的留给 coding；属于“消息/事件长什么样”的先改 ai。本节跟踪未解决项与归属；解决后按 `docs/problem.md` 的格式归档，编号沿用 AG-xx。

`AG-01`～`AG-03`、`AG-04` 的超时部分、`AG-11`～`AG-16` 已修复并归档到 `docs/problem.md`；下表保留其余未解决项与归属。

| 编号 | 事项 | 归属 | 处理 |
| --- | --- | --- | --- |
| AG-01 | `tool_use` 但零调用被当成正常结束 | agent | 已修复：`AgentError(provider, partial=assistant)`，测试 `test_tool_use_without_calls_becomes_agent_error` |
| AG-02 | 取消发生在工具批次中间时留下半轮 | agent | 已修复：整轮原子提交，取消丢弃整轮，测试 `test_cancellation_mid_tool_batch_discards_turn` |
| AG-03 | provider 违约抛异常穿透 `run_loop` | agent | 已修复：转 `AgentError(provider)`，测试 `test_provider_exception_becomes_agent_error` |
| AG-04 | 工具没有超时、没有取消信号 | agent 机制 + coding 配置 | 超时已实现：`LoopConfig.tool_timeout` + `asyncio.timeout`，超时转错误结果，`ToolExecutionEnd.duration_ms` 记录耗时；运行级取消由 M6 的 `Agent.abort()` 提供；具体超时值由 coding 定 |
| AG-05 | 每轮配置固定，没有请求钩子 | agent 机制 + coding 策略 | 已实现：轮次内 6 个 hook + 运行级 `before_run` / `before_finish` / `on_max_turns` 已接入 `loop.py`，测试见 `tests/test_hooks.py`；压缩、换模型仍由 coding 用这些 hook 实现 |
| AG-06 | 上下文窗口无人检查 | coding（依赖 AG-05） | 压缩、token 预算归 coding；agent 只提供钩子，不做 tokenizer |
| AG-07 | 工具结果不截断、只支持文本 | coding + ai | 截断/脱敏归 coding；M7 增加富 `ToolResult`；多模态/结构化结果先改 ai 的 `ToolResultMessage` |
| AG-08 | 工具异常无日志 / trace | coding | 订阅 `ToolExecutionEnd` 记录；agent 不写日志 |
| AG-09 | 重试 / 降级策略缺失 | coding | agent 只上报 `StreamError`；是否重跑由 coding 决定，ai 已重试首字节前失败 |
| AG-10 | 边界测试只查 import | agent 测试 | 加强 `test_boundary.py`，扫描 `open` / `subprocess` / `os.environ` 等调用 |
| AG-11 | 部分分支没有测试 | agent 测试 | 已修复：补 `max_turns=0`、缺少 `StreamDone`、非 `ToolResult`、`partial.usage`，见 `docs/problem.md` |
| AG-18 | 并行工具批次没有并发上限 | agent 机制 + coding 配置 | 未做：并行批次一次创建全部 task，模型给出大批次时可能打满连接、文件句柄或线程池。后续在 `LoopConfig` 加 `max_parallel_tools`，用 `asyncio.Semaphore` 限制同时执行数；不改变结果顺序与规则 A。当前批次通常很小，暂不处理 |
| AG-19 | 富工具结果无法进入历史（图片、结构化内容） | ai + coding | 未做：`ToolResultMessage.content` 仍是 `str`；要支持图片/块列表，需要先做模型能力判断（同一适配器下不同模型能力不同），再扩展 `ai/types.py` 与四个适配器。当前只把 `details` / `structured_content` / `usage` 暴露在 `ToolExecutionEnd` |

## 对齐 Pi 的改进计划

原则：不一次照搬 Pi。按“安全正确 → 可交互 → 上下文 / 并行 / 富结果 → 跨层”推进；每一阶段都保持 `run_loop` 的事件契约不倒退。

### M5 工具安全与正确性（完成）

目标：工具能拦、能停，参数错误不进入工具，`length` 截断有明确处理。

| 文件 | 操作 | 内容 | 状态 |
| --- | --- | --- | --- |
| `hooks.py` | 新增 | 9 个 hook：`before_run`、`TransformMessagesContext`、`PrepareRequestContext`、`BeforeToolCallContext/Result`、`AfterToolCallContext/Result`、`FinishTurnContext`、`PrepareNextTurnContext` / `NextTurnUpdate`、`BeforeFinishContext/Update`、`MaxTurnsContext/Decision`、`AgentHooks` | 完成 |
| `base.py` | 修改 | `LoopConfig` 增加 `hooks: AgentHooks` 与 `tool_timeout` | 完成 |
| `types.py` | 修改 | `ToolExecutionEnd` 增加 `duration_ms` | 完成 |
| `validation.py` | 新增 | 基于 `jsonschema`（Draft 2020-12）的完整参数校验和错误格式化 | 完成 |
| `loop.py` | 修改 | 9 个 hook 接线；`asyncio.timeout` 超时；`ToolExecutionEnd.duration_ms`；`length` 截断为每个调用写错误结果并续跑 | 完成 |
| `tests/fakes.py` | 新增 | 共享假 provider / 假工具 / `RecordingProvider` / `collect()` | 完成 |
| `tests/test_loop.py` | 修改 | `length` 用例拆成“有工具调用 → 错误结果 + 续跑”和“无工具调用 → 正常结束” | 完成 |
| `tests/test_hooks.py` | 新增 | 9 个 hook 的 16 个用例 | 完成 |
| `tests/test_tools.py` | 新增 | 参数校验、超时、`duration_ms`、超时下的取消传播，共 4 个用例 | 完成 |
| `tests/test_boundary.py` | 修改 | 第三方只允许 `jsonschema`，仍禁止 `gradus.coding` / `gradus.ai.providers` / `httpx` | 完成 |
| `pyproject.toml` | 修改 | 加 `jsonschema` 依赖 | 完成 |

M5 验收：

- `before_tool_call` 返回 `block` 时，仍发 `ToolExecutionStart/End`，但结果为错误，不调用工具；
- `after_tool_call` 能替换内容或 `is_error`；
- `transform_messages` 只影响本次请求；`prepare_request` 可替换请求；`finish_turn` 可提前结束或强制继续；`prepare_next_turn` 可追加消息、替换配置；
- `before_run` 可替换初始消息 / 配置；`before_finish` 可改写最终 `new_messages` / `usage` / `stop_reason`；`on_max_turns` 可延展轮次上限；
- 参数不符合 schema 时不执行工具，生成 `is_error` 结果；
- 超时产生 `is_error` 结果，运行继续；
- `length` 截断且有工具调用时，不执行工具，但每个调用都有错误结果，并进入下一轮；没有工具调用时仍正常结束；
- 现有 41 个测试全过。

说明：M5 引入 `jsonschema` 后，边界测试的允许依赖从“标准库 + ai”放宽为“标准库 + ai + jsonschema”；README 和 design 的依赖说明已同步更新。

### M6 有状态 Agent 与队列

目标：连续对话、中途插话、取消。

| 文件 | 操作 | 内容 | 状态 |
| --- | --- | --- | --- |
| `agent.py` | 新增 | `Agent` 类：`prompt` / `continue_` / `abort` / `wait_for_idle` / `reset` / `state` / `steer` / `follow_up`；队列按运行创建 | 完成 |
| `queues.py` | 新增 | `PendingMessageQueue`，支持 `all` / `one-at-a-time` | 完成 |
| `base.py` | 修改 | `LoopConfig` 增加 `get_steering_messages`、`get_follow_up_messages` | 完成 |
| `loop.py` | 修改 | 请求前、轮次后排空 steering；停止前排空 follow-up | 完成 |
| `types.py` | 修改 | 如需要 UI 事件，增加 `MessageStart` / `MessageUpdate` / `MessageEnd`；否则先不加 | 按需 |
| `tests/test_steering.py` | 新增 | steering / follow-up 注入时机与 getter 异常，共 7 个用例 | 完成 |
| `tests/test_queues.py` | 新增 | `all` / `one-at-a-time`、`clear` / `len` 与非法模式，共 4 个用例 | 完成 |
| `tests/test_agent.py` | 新增 | 历史、`continue_`、`reset`、`abort`、`wait_for_idle`、`steer` / `follow_up`、空闲拒绝、steering / follow-up 跨运行不泄漏与 `one-at-a-time`，共 13 个用例 | 完成 |

M6 验收：

- 已完成：连续两次 `prompt()` 保留历史；
- 已完成：steering 在请求前、轮次后注入；
- 已完成：follow-up 只在 agent 本来要停时注入；
- 已完成：`abort()` 能让当前运行退出，`wait_for_idle()` 能等待清理；
- 已完成：`Agent.steer()` / `follow_up()` 把队列接到 `LoopConfig` 的两个 getter；
- 已完成：`reset()` 清空历史；队列在每次运行结束时丢弃，未消费消息不进入下一运行。

### M7 并行与富结果

| 文件 | 操作 | 内容 |
| --- | --- | --- |
| `base.py` | 修改 | `AgentTool` 增加 `execution_mode`（完成）、`on_update`（完成）、`label` |
| `types.py` | 修改 | `ToolResult` 增加 `terminate`、`details`、`structured_content`、`usage`（完成）；新增 `ToolExecutionUpdate`（完成） |
| `hooks.py` | 修改 | `BeforeToolCallResult` / `AfterToolCallResult` 增加 `terminate`（完成） |
| `loop.py` | 修改 | 并行工具按完成顺序发事件、按源顺序写结果（完成）；整批 `terminate` 跳过下一次请求（完成）；更新事件先于 `End` 发出（完成）；`after_tool_call` 改写时保留富字段（完成） |
| `tests/test_parallel.py` | 新增 | 并行、整批串行，共 4 个用例（完成） |
| `tests/test_terminate.py` | 新增 | 整批终止、混合批次、hook 终止、follow-up / steering 交互，共 6 个用例（完成） |
| `tests/test_updates.py` | 新增 | 更新顺序、并行归属、不写入历史、无更新，共 4 个用例（完成） |
| `tests/test_rich_results.py` | 新增 | 富字段只走事件、不计入用量、hook 改写保留，共 2 个用例（完成） |

M7 验收：

- 已完成：只有批次内全部工具声明 `parallel` 时才并发；结果顺序与 assistant 源顺序一致；
- 已完成：取消并行批次时，其余任务被取消并清理，当前轮不提交；
- 已完成：整批结果 `terminate=True` 时，`TurnEnd` 后不再请求；混合批次照常继续；
- 已完成：`before_tool_call` / `after_tool_call` 可以设置或覆盖 `terminate`；
- 已完成：工具更新事件只在 `execute()` 未结束时转发，顺序为 `Start → Update* → End`；
- 已完成：`details` / `structured_content` / `usage` 通过 `ToolExecutionEnd` 暴露，不写入历史、不计入运行用量；
- 待办（M8）：富结果进入历史与多模态需要先做模型能力判断，当前不做。

### M8 跨层（不在 agent 单独实现）

| 层 | 内容 | 状态 |
| --- | --- | --- |
| `ai/base.py` + providers | Provider 可选注入长活 client（AG-12）；Anthropic 显式缓存断点；`prompt_cache_key` 透传 | A / B 已完成（ai 的 D29 / D30）；C 待稳定会话 id；agent 侧接入已完成（D18） |
| `ai/types.py` | 工具结果支持图片 / 结构化内容（AG-19） | 暂缓：需先做模型能力判断 |
| `coding` | 见 [coding_plan.md](../coding/coding_plan.md) 的「待办清单」 | 未开始 |

### M5–M7 不做的事

- 不把工具执行条件改成“内容里有工具调用就执行”，仍只认 `tool_use`；
- 不引入 `AgentMessage` / `convertToLlm`；
- 不把 system / tools 放进 transcript；
- 不把错误改成 assistant 消息，仍用 `AgentError`；
- 不移除 `max_turns`。

## 下一轮检查时要做的事

1. agent 层 M0–M7 已完成；下一步开 `coding` 层，条目见 `coding/coding_plan.md`。ai 层三项（client 注入、Anthropic 缓存断点、`prompt_cache_key`）走单独交接，不占 agent 的计划。
2. 确认 `agent` 没有 import `gradus.coding`、`gradus.ai.providers.*` 具体适配器与 `transport`；`httpx` 只出现在 `agent.py` 的 `connect()`。
3. 确认 `types.py` / `base.py` 没有引入运行逻辑。
4. 新增偏差写进上表；新增问题先加入本节的「折中与分层归属」，解决后归档到 `docs/problem.md`（附测试），不静默改掉。
5. 对照 `ai_plan.md` 的「agent 层」待办，避免重复实现。

## 相关文档

- [README.zh.md](README.zh.md) / [README.md](README.md)：入口与当前状态。
- [docs/design.md](docs/design.md)：事件模型、工具契约、`stop_reason` 决策表、不变量。
- [docs/problem.md](docs/problem.md)：只收录已解决的问题；未解决项见本节的「折中与分层归属」。
- [../ai/ai_plan.md](../ai/ai_plan.md)：`ai` 层里程碑，以及留给 agent 的待办（压缩、缓存统计、reasoning 兼容）。
