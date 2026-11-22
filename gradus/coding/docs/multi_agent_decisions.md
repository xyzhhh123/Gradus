# 多 agent 协作：设计决策记录

> 这份文档只记录**决定**和**理由**，不重复实现细节。实现见 `multi_agent.md`。
> 目的：任何一条改动都能在这里找到"当初为什么这么定"，避免方案像凭空出现。

---

## D1. 派活归模型，不做运行时调度器

**备选**

| 路线 | 谁派活 | 主 agent 是否阻塞 |
| --- | --- | --- |
| A | 运行时调度器每 tick 扫图 | 否 |
| B（采用） | 模型调工具 | 是，在 `wait` 里等 |
| C | 空闲 agent 自取 | 否 |

**结论**：B。

**理由**

- A 需要"图 + 一次性 agent + 主 agent 不阻塞"三者同时成立，为此必须自造调度器，还要额外发明并发闸，偏离三家参考实现；
- C 要求 agent 长期常驻并由自己扫描任务板，等于换一套架构；
- B 与 Hermes（`delegate_task` 阻塞返回摘要）、Codex（`spawn_agent` + `wait`）一致，代码量最小。

**后果**

- 模型的职责是"建图 + 决定何时开始"；
- 图不会自己往前走，靠 `wait` 推进；
- 派活时机由模型掌握，两阶段建图期间"节点短暂无边"不再是问题，原先为它加的"只在主 agent 空闲时派活"这道闸可以删掉。

---

## D2. 子 agent 保留到显式 `close_agent`

**参考事实**

| | 跑完后 |
| --- | --- |
| Hermes | 销毁（从 `_active_subagents` 移除，会话 finalize） |
| Codex (采用) | 保留，`AgentStatus::Completed` 只是状态；只有 `close_agent` 才 `Shutdown` + `remove_thread` + 释放并发名额 + 移除 mailbox |
| s13 | 保留，进 IDLE |

**结论**：默认保留，跑完转 idle；`close_agent(name)` 才销毁。`spawn_agent(..., ephemeral=true)` 可按节点选择一次性。

**理由**

- 没有保留就没有收件箱：消息总线的全部意义是"接收者之后还会读"；
- 支持追问（同一个 agent 接着聊），不用为了多问一句重新 spawn；
- 成本可控：一个子 agent 常驻 = 一个 `Session`（客户端 + 消息链），数量由模型自己克制。

**后果**

- `AgentTeam` 必须在跑完后保留 `Session`，只在 close 时 `aclose()`；
- 需要 `close_agent` / `list_agents` 工具让模型能收尾，否则会话泄漏；
- 进程退出时必须全量关闭（`aclose()`）。

---

## D3. 不设硬性并发上限

**结论**：取消 `max_agents = 4`。

**理由**

- 派活归模型后，模型自己受回合长度与上下文约束，运行时不必再设常数闸；
- Codex 的 `agents.max_concurrent_threads_per_session` 默认也是**不设 = 不限**；Hermes 的 10 与 s13 的无上限同理，都是"要不要限制"的可选旋钮；
- 之前那个"最多 5 个 agent"是我凭空假设的，不能作为设计依据。

**后果**

- `AgentTeam` 不再有 `max_agents` 参数；
- 若将来需要保护，加成配置文件里的可选项，而不是结构常数。

---

## D4. 消息用总线（点对点），不用论坛式留言板

**备选**

| | Codex board | s13 bus（采用语义） |
| --- | --- | --- |
| 结构 | 频道 → 主题帖 → 回复 | 收件人 → 收件箱 |
| 历史 | 是资产，要搜索分页 | 是副作用，翻旧账无用 |
| 通知 | `post` 推给订阅者 | 投递即通知 |

**结论**：取"点对点投递"，不取频道/主题/订阅/分页。

**理由**

- 我们的 agent 数量由模型决定，通常个位数，"板块分区"没有意义；
- 真正需要的是"coder 告诉 reviewer 一件事"，这是寻址问题，不是归档问题。

**形态**

```python
Message(id, sender, recipient, text, created_at)

bus.send(sender, recipient, text)     # recipient 是 agent 名；不广播（见待定项）
bus.history(limit=100)                # 内存 ring buffer，不落盘
```

**投递复用已有通道**：接收者运行中 → 进它的 steering（下一轮请求前取用）；空闲 → 进它的 `_notices`，随后由 `_deliver` 叫醒一轮（`Session.continue_()`）把它读掉。这就是后台任务完成通知用的那条路，不再造第二套。

**后果**

- 不落盘：接收者销毁后消息无意义（`agents/*.jsonl` 里仍有它自己那侧的对话）；
- 需要 `send_message(to, text)` 工具；
- idle 的 agent 会被消息叫醒。s13 的 `wait_for_messages` 正是这个语义：不叫醒的话，"保留到 `close_agent`"就只是个占着会话的空壳——`spawn` 只认领 `pending` 节点，跑完的 agent 再也没有任何路径被 `prompt`，消息会永远躺在收件箱里；
- 不会变成 agent 互相闲聊烧 token：子 agent 没有消息工具，只能主 agent 单向下发。

---

## D5. 结果走 `wait` 返回值，消息走总线

**结论**：两条路径各有分工，不混用。

| 信息 | 载体 |
| --- | --- |
| 节点结果 | `wait` 的工具结果（同时写回 `Task.result`） |
| 协作中的发现/提醒 | `send_message` → 总线 → 接收者的通知通道 |

**理由**：否则同一条信息会以两种形式各出现一次，主 agent 的上下文被重复内容占据。

---

## D6. 删除运行时调度器及其配套

**结论**：删掉为 A 路线服务的代码。

| 删除项 | 原因 |
| --- | --- |
| `AgentTeam.tick` / `run` / `_schedulable` / `_reusable_agent` | 调度器本体 |
| `host/serve.py` 的 `_tick_team` | 派活时机改由模型掌握 |
| `_tick_summary` / `run_summary` | 不再自动唤醒主 agent，结果由 `wait` 带回 |
| `Session.prompt_harness` | 失去唯一调用方（`has_notices` 后来为叫醒重新加回） |
| `AgentTeam.on_done` 回调 | 同上；子 agent 完成不再单独通知主 agent |

`schedules.json` 的时钟不受影响（那是定时任务，不是派活）。

---

## 实现时的默认值（当时列为待定，现已落地）

| 问题 | 落地结果 |
| --- | --- |
| 允不允许广播 | 不允许。`send_message` 必须给 `to`，收件人不存在直接报错 |
| 子 agent 之间能否直连 | **不能**。比原计划更严：子会话根本没有消息工具，也没有任务图工具；跨节点信息要么走 `result`，要么经主 agent 转发 |
| `wait` 超时 | 默认 600 秒；超时返回"仍在运行"，不报错；`timeout_seconds=0` 表示只查状态 |
| 子 agent 的 `model` | 沿用 `Task.model`，命中 `config.json` 候选表则切换 |
| 并发上限 | 不设（D3） |
| 失败重试 | 不自动重试：放回 pending 并记 `last_error`，重不重派由模型决定 |
| 投给 idle agent | 叫醒它跑一轮（`Session.continue_()`），不追加用户消息 |

---

## 验收清单

1. 模型能 `create_task` → `update_task` → `spawn_agent` → `wait` 走完一条链；
2. 跑完的子 agent 仍在 `list_agents` 里（idle），`Task.owner` 保留；
3. `close_agent` 后它从列表消失，会话被关闭；
4. `send_message` 给运行中的 agent，它下一轮请求前能看到；
5. `send_message` 给 idle agent，消息进收件箱并把它叫醒跑一轮，这一轮能看到；
6. 图里没有硬性并发的常数闸；
7. 用户点停止能打断 `wait`，不会卡住主会话（依靠 `Agent.abort()` 取消运行任务；这一条需要手动验证）；
8. 进程退出时所有子会话被关闭（`stop_team` → `aclose`）。

---

## 变更清单（已完成）

| 文件 | 动作 |
| --- | --- |
| `coding/multiagent.py` | 重写：去掉调度，改成 `spawn` / `wait` / `close` / `interrupt` / `list` / `send` |
| `coding/bus.py` | 新增：消息记录、寻址校验、ring buffer |
| `coding/tools/multiagent.py` | 新增：六个工具 |
| `coding/session.py` | 删 `prompt_harness`；新增 `spawn` / `attach_team` / `team` / `continue_` / `has_notices`；子会话裁剪工具面 |
| `host/serve.py` | 删 `_tick_team` / `_tick_summary` / `run_summary` / `_on_agent_done` / `team_task` |
| `coding/docs/multi_agent.md` | 按最终形态重写 |
