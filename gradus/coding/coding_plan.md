# coding 层实施计划

## 目标与边界

**目标**：把 `ai` 的一次补全和 `agent` 的循环装成一个能直接跑的产品：读配置、建会话、调工具、受权限约束、能落盘、能压缩上下文。

**边界**：只改 `gradus/coding/`。`agent` / `ai` 的协议不改；缺能力时先在下层加口子，并在本文件记录。

**依赖方向**（不可逆）：

```
coding  ->  agent  ->  ai
```

`coding` 是唯一允许读环境变量、持有凭证、管理连接池生命周期的层。

## 里程碑

| 编号 | 内容 | 对应待办 | 状态 |
| --- | --- | --- | --- |
| M0 | 配置与会话：`config.py` + `session.py`，进程级连接池 + 每用户一个 `Agent` | CO-01、CO-12 | 未开始 |
| M1 | 只读工具：`read_file`、`list_dir` | CO-04 | 未开始 |
| M2 | CLI 闭环：`prompt` -> 模型 -> 工具 -> 回答 | CO-04 | 未开始 |
| M3 | 权限与观测：`before_tool_call` 拦截、事件渲染与日志 | CO-06、CO-08 | 未开始 |
| M4 | 写工具与结果约束：`write_file`、结果截断与脱敏 | CO-04、CO-05 | 未开始 |
| M5 | 会话落盘与恢复 | CO-01 | 未开始 |
| M6 | 上下文预算与压缩 | CO-03 | 未开始 |
| M7 | 模型路由与重试降级 | CO-02、CO-07 | 未开始 |
| M8 | 工具注册：内置工具之外的注册表（MCP / 自定义），统一转成 `AgentTool` | CO-13 | 未开始 |

暂缓（依赖下层先改）：

| 事项 | 依赖 |
| --- | --- |
| 缓存命中率展示 | 无，可随时接 CO-09 |
| 并发上限配置 | agent 加 `max_parallel_tools`（AG-18） |
| 多模态工具结果进历史 | ai 支持块列表与模型能力判断（AG-19） |

顺序理由：先有会话装配和只读工具，才能在真实模型上验证；CLI 闭环之后再补权限与落盘，避免一开始就把交互形态定死。

## 完成定义

每个里程碑结束时必须同时满足：

1. `uv run python -c "import gradus.coding.<模块>"` 通过；
2. 新增能力有离线测试，不依赖真实 API key；
3. `uv run pytest -q` 全绿，且 `agent` / `ai` 的用例数不变；
4. 本文档的「进度检查」与「偏差记录」已更新。

## 进度检查

**已完成**

- 目录骨架、README、设计文档、本计划。

**验证结果**

骨架阶段没有实现，暂不记录测试数量。

## 偏差记录

| 编号 | 原规划 | 实际 | 处理 |
| --- | --- | --- | --- |
| 暂无 | | | |

## 待办清单

原表在 `agent_plan.md` 的 M8 小节，现已整体迁入本文件；编号保持不变，便于跨层引用。

依赖方向不变：`coding -> agent -> ai`。下列条目只使用 agent / ai 已暴露的接口；需要下层先改的已写在「依赖」列。

| 编号 | 待办 | 内容 | 依赖 |
| --- | --- | --- | --- |
| CO-01 | 会话装配 | 选 `ModelSpec`、创建 provider、管理跨轮 client 生命周期；对外只暴露一个 `prompt` 入口 | 已就绪：`Agent.connect()`（agent 的 D18） |
| CO-02 | 模型路由 | 按任务 / 成本切换 provider 与模型，用 `prepare_request` 换 `Request` | CO-01、agent 的 AG-05 |
| CO-03 | 上下文预算与压缩 | 用 `transform_messages` 在请求前裁剪；超阈值时摘要旧消息、保留近期原文 | CO-01 + tokenizer |
| CO-04 | 具体工具集 | 第一版：读文件、写文件、列目录、搜索、执行命令；每个工具声明 `execution_mode` 与自身超时值 | 无 |
| CO-05 | 工具结果截断与脱敏 | 在工具内部或 `after_tool_call` 限制长度、去掉密钥与多余绝对路径 | CO-04、agent 的 AG-07 |
| CO-06 | 观测与日志 | 订阅 `ToolExecutionEnd` / `AgentError` 记录耗时、错误与 trace；agent 不写日志 | CO-04、agent 的 AG-08 |
| CO-07 | 重试与降级 | 收到 `AgentError(provider)` 后决定重跑、换模型或终止 | CO-02、agent 的 AG-09 |
| CO-08 | 权限策略 | `before_tool_call` 拦截危险工具，按白名单 / 确认模式决定放行 | CO-04 |
| CO-09 | 缓存命中展示 | 消费 `Usage.cache_read_tokens`，在日志或界面给出命中率 | CO-01 |
| CO-10 | 并发上限配置 | agent 加 `max_parallel_tools` 后由 coding 给具体值 | agent 的 AG-18，暂不阻塞 |
| CO-11 | 多模态结果进入历史 | ai 支持块列表并能判断模型能力后，把图片等结果写进 `ToolResultMessage` | agent 的 AG-19，暂缓 |
| CO-12 | 连接池共享装配 | 进程级建一个 `httpx.AsyncClient`，所有用户 / Agent 通过 `connect(client=pool)` 共用；按 host / 代理 / TLS / `limits` 分组，不按用户分组；进程退出时统一 `aclose()`。`Agent` 仍每用户一个，历史不共享 | 已就绪：`Agent.connect(client=)`（agent 的 D18） |
| CO-13 | 工具注册接口 | 内置工具之外提供注册表，挂载 MCP / 自定义工具；统一转成 `AgentTool`，按名字校验重名。对齐 Pi 的 `extensions.tools` 机制 | CO-04 |
