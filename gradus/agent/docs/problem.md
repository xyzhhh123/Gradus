# agent 层问题记录

本文件只收录**已经解决**的问题。未解决、待定、延后以及归属其它层的事项在 `agent_plan.md` 的「折中与分层归属」中跟踪；解决后再归档到这里。

## 记录格式

### AG-xx 标题

- **状态**：已解决
- **现象**：
- **触发**：
- **原因**：
- **解决**：
- **验证**：关联测试或命令
- **关联**：`agent_plan.md` 的 AG-xx

记录时保留原编号，不删除历史；如果同一个问题后来复发，追加“复发”小节，而不是覆盖原记录。

## 已解决问题

### AG-01 `tool_use` 但零调用被当成正常结束

- **状态**：已解决（2026-11-02）
- **现象**：模型返回 `stop_reason="tool_use"`，但 `AssistantMessage` 里没有任何 `ToolCallBlock`，`run_loop` 发 `AgentDone(stop_reason="tool_use")`。
- **触发**：厂商或模型的协议异常。
- **原因**：循环只判断 `stop_reason`，没有校验调用列表是否为空。
- **解决**：在提交 assistant 之前检查 `calls`；为空时发 `AgentError(kind="provider", message="stop_reason=tool_use but no tool calls", partial=assistant)`，当前轮不写入 `new_messages`。
- **验证**：`test_tool_use_without_calls_becomes_agent_error`；`uv run pytest gradus/agent/tests -q` → 20 passed。
- **关联**：`agent_plan.md` 的 AG-01。

### AG-02 取消发生在工具批次中间时留下半轮

- **状态**：已解决（2026-11-02）
- **现象**：assistant 在工具批次之前就写入 `new_messages`；第二个工具抛 `CancelledError` 时，内存里留下“有工具调用、没有结果”的半轮。
- **触发**：工具抛 `asyncio.CancelledError`，或上层取消任务。
- **原因**：assistant 与工具结果分两次提交，提交不是原子的。
- **解决**：一轮先在本地 `turn_messages` 中组装，工具批次完成后在 `TurnEnd` 前一次性写入 `transcript` 和 `new_messages`；取消时整轮丢弃。取消向外传播前，`run_loop` 先发 `AgentError(kind="aborted")`，再重新 `raise CancelledError`。
- **验证**：`test_cancellation_mid_tool_batch_discards_turn`（断言没有 `TurnEnd`、`AgentDone`，只有一个 `AgentError(aborted)`）；`test_cancellation_propagates` 保留。
- **关联**：`agent_plan.md` 的 AG-02。

### AG-03 provider 违约抛异常穿透 `run_loop`

- **状态**：已解决（2026-11-02）
- **现象**：自定义 provider 不遵守“只 yield、不抛”的契约时，上层收到原始异常，而不是 `AgentError`。
- **触发**：provider 实现有 bug，或第三方适配器违约。
- **原因**：`run_loop` 没有包住 `provider.stream(request)`。
- **解决**：改成手动 `anext(stream)`，只把 `Exception` 转成 `AgentError(kind="provider", message=f"{type(exc).__name__}: {exc}")`；`asyncio.CancelledError` 是 `BaseException`，继续向上传播。
- **验证**：`test_provider_exception_becomes_agent_error`。
- **关联**：`agent_plan.md` 的 AG-03。

### AG-04 工具执行没有超时

- **状态**：已解决（2026-11-03，超时部分）
- **现象**：工具卡在 IO 上时，整次运行无法前进，也没有任何提示。
- **触发**：工具实现阻塞，或外部服务长时间不响应。
- **原因**：循环直接 `await tool.execute()`，没有时间上界。
- **解决**：`LoopConfig.tool_timeout` 不为 `None` 时，用 `asyncio.timeout` 包住执行；超时生成 `ToolResult(is_error=True)`，运行继续；`ToolExecutionEnd` 增加 `duration_ms` 记录耗时。
- **验证**：`test_tool_timeout_becomes_error_result`、`test_tool_execution_end_records_duration`、`test_cancellation_still_propagates_with_timeout`。
- **关联**：`agent_plan.md` 的 AG-04；运行级取消已由 M6 的 `Agent.abort()` 提供。

### AG-11 部分分支没有测试

- **状态**：已解决（2026-11-03）
- **现象**：`max_turns=0`、流结束但没有 `StreamDone`、工具返回非 `ToolResult`、`StreamError.partial.usage` 四条路径没有测试。
- **触发**：代码审查与 `agent_plan.md` 的 AG-11。
- **原因**：早期测试只覆盖主要路径，边界分支依赖人工检查。
- **解决**：新增 4 个用例，分别覆盖上述分支；`StreamError` 场景构造 `usage` 步骤，断言 `AgentError.usage` 计入 `partial.usage`。
- **验证**：`test_max_turns_zero_terminates_without_request`、`test_stream_without_done_becomes_provider_error`、`test_invalid_tool_result_becomes_error`、`test_stream_error_partial_usage_is_counted`。
- **关联**：`agent_plan.md` 的 AG-11。

### AG-13 工具参数只检查必填字段

- **状态**：已解决（2026-11-03）
- **现象**：模型给出类型错误的参数（例如 `text: 123`）时，仍然会进入工具。
- **触发**：模型生成不合规参数，或工具 schema 声明了类型约束。
- **原因**：`_execute_tool` 只检查 `required`，没有做完整 JSON Schema 校验。
- **解决**：新增 `validation.py`，用 `jsonschema`（Draft 2020-12）校验；失败生成 `invalid arguments for <tool>: <path>: <message>` 错误结果，不执行工具。
- **验证**：`test_invalid_argument_type_is_rejected`。
- **关联**：`agent_plan.md` 的 D4。

### AG-14 `length` 截断时工具调用被静默丢弃

- **状态**：已解决（2026-11-03）
- **现象**：`stop_reason="length"` 且消息里带 `ToolCallBlock` 时，循环直接 `AgentDone`；模型不知道这些调用没有执行，历史里也没有对应结果。
- **触发**：输出 token 达到上限，工具参数被截断。
- **原因**：`should_continue` 只认 `tool_use` / `deferred`，`length` 直接结束。
- **解决**：`length` 且存在工具调用时，不执行工具，但为每个调用发 `ToolExecutionStart/End`，写 `is_error=True` 的结果（`tool <name> was not executed: response was truncated`），然后进入下一轮；没有工具调用时仍正常结束。
- **验证**：`test_length_with_tool_calls_returns_errors_and_continues`、`test_length_without_tool_calls_finishes`。
- **关联**：`agent_plan.md` 的 M5 与 AG-14。

### AG-15 `prepare_next_turn` 注入的消息在取消时被写进历史

- **状态**：已解决（2026-11-03）
- **现象**：`prepare_next_turn` 返回的消息立即写入 `new_messages`；如果下一轮还没完成就被取消，这些消息已经进入历史，但模型从未使用过。
- **触发**：取消发生在注入消息之后、下一轮提交之前。
- **原因**：注入消息的提交与轮次提交不同步。
- **解决**：注入消息只立即写入 `transcript`（下一轮请求可见），`new_messages` 改为随下一轮 `TurnEnd` 一起提交；下一轮没有发生则丢弃。
- **验证**：`test_abort_drops_unused_prepare_next_turn_messages`、`test_prepare_next_turn_can_add_message_and_change_config`。
- **关联**：`agent_plan.md` 的 D12。

### AG-16 `vulture` 把 `RaisingProvider` 的 `yield` 误报为死代码

- **状态**：已解决（2026-11-03）
- **现象**：`vulture gradus/agent --min-confidence 80` 报 `fakes.py` 中 `raise` 之后的 `yield` 是不可达代码。
- **触发**：静态扫描测试替身。
- **原因**：`RaisingProvider.stream` 故意先 `raise`；为了让 `stream` 保持 async generator 而不是普通协程，函数体内必须有一个 `yield`，所以它不可达是结构性的。
- **解决**：保留 `yield`，并在代码中加注释说明用途；不删除。
- **验证**：`uvx ruff check gradus/agent --select F401,F811,F841` 全部通过；`uv run pytest -q` → 170 passed。
- **关联**：`agent_plan.md` 的 AG-16。

### AG-17 steering / follow-up 队列跨运行泄漏

- **状态**：已解决（2026-11-03）
- **现象**：上一次运行结束时队列里还有未被消费的 follow-up / steering 消息，下一次 `prompt()` 会把它当成新指令注入，模型收到与当前任务无关的内容。
- **触发**：运行提前结束（例如 `finish_turn` 返回 `"end"`、`abort()`、达到 `max_turns`），队列未被排空。
- **原因**：队列挂在 `Agent` 实例上，生命周期等于 Agent 而非单次运行。
- **解决**：`_start()` 每次运行新建 `PendingMessageQueue`，通过 `replace()` 接入 `LoopConfig` 的两个 getter；`_pump` 的 `finally` 与 `abort()` 都把队列引用置 `None`。队列只在运行中存在，空闲时 `steer` / `follow_up` 抛 `RuntimeError`。
- **验证**：`test_unconsumed_follow_up_does_not_leak_into_next_run`（断言下一次运行只发一次请求，且所有请求都不含残留消息）、`test_unconsumed_steering_does_not_leak_into_next_run`（在 `before_finish` 中插话，绕过当次 drain）、`test_steer_and_follow_up_require_running`、`test_follow_up_one_at_a_time`。
- **关联**：`agent_plan.md` 的 D14。

### AG-12 Provider 没有复用长活 client

- **状态**：已解决（2026-11-03）
- **现象**：每次 `stream()` 都新建并关闭 `httpx.AsyncClient`，多轮对话每轮重做 TLS 握手；`post_sse` 虽已支持注入 client，但四个适配器都不接受，上层没有注入口。
- **触发**：同一会话连续发起多次模型请求。
- **原因**：client 的寿命没有归属：ai 层不知道一次会话从哪开始、到哪结束，agent 层此前也不持有 Provider。
- **解决**：ai 层 `create_provider(spec, api_key, client=None)` 与四个适配器接受可选 client，注入时不关闭（ai 的 D29）；agent 层新增 `Agent.connect(spec, config, client=None)`，`client=None` 时自建并拥有，`aclose()` 关闭，注入时只借用，因此一个池子可以跨多个 Agent / Provider 共用。直接 `Agent(provider, config)` 仍走 ai 默认的每轮自建路径。
- **验证**：`test_owned_client_is_closed_by_aclose`、`test_injected_client_is_reused_across_runs_and_not_closed`、`test_aclose_is_idempotent_and_rejects_new_runs`、`test_unknown_provider_does_not_leak_owned_client`；`uv run pytest gradus/agent/tests -q` → 89 passed。
- **关联**：`agent_plan.md` 的 AG-12 与 D18。
