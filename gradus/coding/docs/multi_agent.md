# Multi-Agent — 模型派活，宿主不调度

> *"图决定做什么和先后，模型决定什么时候开始，子 agent 决定怎么做。"*
>
> **Harness 层**：任务图 + 子 agent 注册表 + 点对点消息。

---

## 问题

单个 agent 做不完"一批彼此有依赖的事"：

- 上下文会被整件事的中间过程撑爆；
- 有些步骤本来可以并行（改 A 文件、补 B 测试），但一个 `messages` 链只能串行；
- 长任务跨会话无法恢复，重开就要从零讲一遍。

需要三样：**依赖关系**、**独立执行单元**、**有人负责在正确的时机开始**。

---

## 解决方案

```
主 agent（Lead）
  create_task ───────────────────────────────→ 节点
  update_task ───────────────────────────────→ 边
  spawn_agent(task) ─────────────┐
                                 ↓
                          子 agent sub-1（独立会话）
                                 │ 跑完 → graph.complete → 解锁下游
  wait([...])  ←──── 结果 ───────┘
  send_message(to, text) ──→ bus ──→ 接收者的通知通道
  close_agent(sub-1) ──────→ 释放会话
```

| 层 | 文件 | 负责 | 不负责 |
| --- | --- | --- | --- |
| 任务图 | `coding/tasks.py` | 节点、依赖、认领、解锁、落盘 | 不碰 asyncio，不认识 agent |
| 团队 | `coding/multiagent.py` | 启停子 agent、等待、写回结果 | 不扫描图、不做调度 |
| 消息 | `coding/bus.py` | 点对点投递与留痕 | 不落盘，不排队 |
| 工具 | `coding/tools/tasks.py`、`tools/multiagent.py` | 把上面两者暴露给模型 | 从不隐式启动任何东西 |
| 会话工厂 | `Session.spawn` | 给子 agent 建独立会话 | 不决定何时派活 |

**没有调度器**。这是与"运行时每 tick 扫图派活"方案的根本区别：图不会自己往前走，模型 `wait` 一次，图才前进一步。

---

## 工作原理

### Task：节点只有九个字段

```python
@dataclass
class Task:
    id: str                       # task_xxxxxxxx
    subject: str                  # 一行目标
    description: str = ""         # 执行者需要的细节（它看不到主对话）
    status: str = "pending"       # pending | in_progress | completed
    owner: str | None = None      # 谁的，例如 sub-1
    blocked_by: tuple[str, ...] = ()   # 唯一的边
    role: str | None = None       # coder / reviewer 之类的提示
    model: str | None = None      # 候选表里的模型名或 id
    result: str | None = None     # 完成时记下的结论
```

状态机两个动作：`claim`（pending → in_progress）、`complete`（→ completed）。`release` 把进行中的节点放回 pending（中止、执行者失败时用），`remove` 删节点并摘掉别处的悬空引用。

### 两阶段建图

```python
schema    = create_task("setup schema")           # ① 先建全部节点，拿到 id
endpoints = create_task("create endpoints")
update_task(endpoints.id, add_blocked_by=[schema.id])   # ② 再连边
```

模型在一条回复里并行发出的工具调用，**参数在任何结果产生之前就已确定**，它没法引用另一个调用刚生成的 id。任何沿用"调用 + 结果"协议的实现都躲不开这个限制。

### 依赖判定只有一句

```python
def ready(self):
    return tuple(
        task for task in self._tasks.values()
        if task.status == "pending" and task.owner is None and not self.blockers(task.id)
    )
```

- 入度为 1 是普通依赖；入度大于 1 就是天然的 join，不需要额外语法；
- 出度大于 1 时，完成一个节点会一次解锁多个下游；
- **依赖指向不存在的节点也算阻塞**：悬空引用不该被当成满足。

`complete()` 返回**刚刚**被解锁的下游（改状态前后各算一次 `ready()` 取差集），模型不用自己重算全图。

建边校验四件事：目标任务存在、依赖存在、目标仍 `pending` 且无人认领、不成环（沿 `blocked_by` 反向 DFS，含自依赖）。

### 派活：`spawn_agent(task_id)`

```python
task = graph.get(task_id)
name = f"sub-{counter + 1}"
graph.claim(task.id, name)          # ① 原子认领：前面没有 await
session = await connect(name, task) # ② 建子会话
agent.handle = create_task(_work(agent, task))   # ③ 点火，不等
return agent                        # 返回时它已经在跑
```

顺序是"先认领、再建会话、最后建协程"：

- 认领在前，认领失败（被阻塞 / 已被占）就不会留下空的会话文件；
- 建会话失败会把节点 `release` 回去，避免任务卡在 in_progress；
- 协程用 `create_task` 起，所以 `spawn_agent` 本身立即返回，模型可以连着派几个。

### 等待：`wait(agents, timeout)`

```python
pending = [a.handle for a in agents if not a.handle.done()]
await asyncio.wait(pending, timeout=timeout)
```

- 超时**不是错误**：返回时这些 agent 可能还在 `running`，模型自己决定再等还是先做别的；
- `timeout_seconds=0` 等价于"只查状态"，不阻塞；
- 用户点停止会取消这一轮，`asyncio.wait` 随之被打断——这是它必须用 `asyncio.wait` 而不是裸 `await` 的原因（后者同样可取消，但 `wait` 允许等一批）。

### 生命周期：跑完不销毁

| | 默认 | `ephemeral=true` |
| --- | --- | --- |
| 跑完 | 转 idle，**保留会话** | 关会话、退出注册表 |
| 之后 | `send_message` 能投进去并叫醒它跑一轮，`wait` 能再查 | 只能重新 spawn |
| 谁这么做 | Codex（`close_agent` 才真正关闭） | Hermes（一次委派一次生死） |

保留是消息总线存在的前提：接收者转眼就没了，投递就没有意义。`close_agent` 才是终点——取消在跑的协程、把节点放回 pending、`aclose()` 子会话、从注册表移除。

### 结果与消息分两条路

| 信息 | 载体 | 进入上下文的方式 |
| --- | --- | --- |
| 节点结果 | `wait` 的返回值（同时写回 `Task.result`） | 工具结果 |
| 协作中的提醒 | `send_message(to, text)` | 接收者的 `Session.notify` |

`notify` 就是后台任务完成通知用的那条通道，返回「是否只是暂存」：

```
运行中 → 推进 steering，下一轮请求前取用，返回 False
空闲时 → 存进收件箱，返回 True
```

`True` 表示没人会读它，所以 `_deliver` 紧接着叫醒这个 agent：`Session.continue_()` 起一轮（不追加用户消息），`_flush_notices` 在开头把收件箱补成插话。没有这一步，消息会一直躺在收件箱里——`spawn` 只认领 `pending` 节点，跑完的 agent 再也不会被 `prompt`。

叫醒有两条收尾补漏：`_work` / `_resume` 结束时若 `session.has_notices` 仍为真，再起一轮。覆盖「消息恰好落在收尾瞬间」与「已经排了一轮但还没开跑」两种情形，且不会多开空轮。

于是总线上不需要第二套投递机制。总线只做两件事：**寻址**（收件人存不存在）与**留痕**（最近 100 条在内存里，供排查）。不落盘：收件人一旦关闭，消息就没有意义。

### 子 agent 的工具面是裁剪过的

`Session.spawn` 建子会话时**不传任务图、不传团队**，所以子 agent：

- 没有 `create_task` / `claim_task`——它不能改图；
- 没有 `spawn_agent`——它不能无限派生下一层；
- 仍然有文件、搜索、命令、后台任务——它得干活。

它拿到的上下文只有两样：节点描述，以及已完成前驱的 `result` 摘要（每个截断到 2000 字符）。

### 子会话与主会话的差别

| | `connect`（主） | `spawn`（子） |
| --- | --- | --- |
| 沙箱副本 | 可能新建 | **不新建**，共用主 agent 的 `_tool_root` |
| 执行环境 | 自己拥有 | 共用，`owns_env=False`，关闭时不回收 |
| MCP | 连接 | 不连 |
| 项目规则文件 | 读 | 不读 |
| 会话文件 | `<session_dir>/*.jsonl` | `<session_dir>/agents/<name>.jsonl`（不进会话列表） |
| 后台任务表 | 自己的 | 自己的，日志按 agent 名分目录（任务号会重号） |
| 任务图 / 团队 | 有 | 都没有 |

`Task.model` 命中 `config.json` 的候选表时用 `with_choice` 换模型，否则跟随主 agent。

---

## 参考实现对照

| | s10 task system | s13 agent teams | Hermes `delegate_task` | Codex MAv2 | 本章 |
| --- | --- | --- | --- | --- | --- |
| 依赖图 | ✅ | ✅ 复用 s10 | ❌ 树 | ❌ 黑板 | ✅ |
| 谁派活 | 单 agent 自己 | agent 自取 | Lead 一次性传入 | Lead 用 `spawn_agent` | **Lead 用 `spawn_agent`** |
| 主 agent 是否阻塞 | — | 否 | 是（等摘要） | 是（`wait`） | 是（`wait`） |
| 子 agent 跑完 | — | 保留（idle） | 销毁 | 保留到 `close_agent` | 保留到 `close_agent` |
| 通信 | 无 | 文件收件箱，任意两端 | 父→子 steer | 收件箱 + 频道黑板 | 点对点总线，主↔子 |
| 并发上限 | 无 | 无 | 默认 10 | 可选，默认不限 | **不设** |
| 节点级模型 | ❌ | ❌ | ❌ | ✅ | ✅ |

抄了什么、没抄什么：

- **抄 s10**：图结构、两阶段建图、claim/complete 语义；
- **抄 s13**：认领必须原子；
- **抄 Hermes**：默认给子 agent 一个聚焦提示词（节点 + 上游结论），只回结果；
- **抄 Codex**：派活是模型的动作、跑完保留到显式关闭、节点级 role/model；
- **没抄**：频道/订阅式留言板（个位数 agent，要的是寻址不是归档）、九个生命周期工具（`send_input` 之类等有需求再说）、运行时调度器。

---

## 已知边界

- **图不会自动推进**：模型若派活后既不等也不在下一轮检查，节点会停在 in_progress（`list_agents` / `list_tasks` 能看到）；这是"派活归模型"的直接代价；
- **不能广播**：`send_message` 必须给收件人；子 agent 之间不能直连（它们没有消息工具），所有跨节点信息要么走 `result`，要么经过主 agent；
- **共享工作区**：两个真并行的节点写同一文件会互相覆盖，图表达不了资源冲突；
- **失败不自动重试**：失败会把节点放回 pending 并记 `last_error`，重不重派由模型决定；
- **前端只看到主 agent**：子 agent 的对话在 `agents/*.jsonl` 里，界面上没有独立视图。

---

## 文件与测试

| 文件 | 内容 |
| --- | --- |
| `coding/tasks.py` | `Task`、`TaskGraph`（增删、连边、环检测、认领、完成解锁、放回、原子读写、加载时释放中断节点） |
| `coding/multiagent.py` | `Subagent`、`AgentTeam`（spawn / wait / close / interrupt / aclose / send）、`_task_prompt` |
| `coding/bus.py` | `Message`、`Bus`（寻址校验 + ring buffer） |
| `coding/tools/tasks.py` | 任务图工具（`plan_tasks` + create/update/list/get/claim/complete） |
| `coding/tools/multiagent.py` | `spawn_agent`（含 `files` 所有权）/ `wait` / `list_agents` / `send_message`（含 `kind`）/ `close_agent` / `interrupt_agent` |
| `coding/session.py` | `spawn`（子会话）、`attach_team` / `team`（间接引用）、`_settings_for` |
| `host/serve.py` | 创建团队并回填引用；`stop_team` 全量关闭 |

测试：`coding/tests/test_tasks.py`（图与工具）、`coding/tests/test_multiagent.py`（派活、等待、生命周期、总线、工具）、`host/tests/test_clock.py`（时钟与团队收尾）、`coding/tests/test_session.py`（会话文件命名与模型选择）。

## 2026-11-22：先规划、再分工、可互通（本轮补的约束）

上面描述的是**能力**；这一节记录为了让这套能力「按正确顺序使用」加的三条约束，以及 bench 实测。

### 1. 先规划后执行：`plan_tasks` + `require_plan`

以前模型可以建一个空节点就 `spawn_agent`——图在，但规划没有发生。现在：

- `coding/tools/tasks.py` 新增 `plan_tasks(nodes)`：一次提交整张 DAG
  （`key / subject / description / blocked_by`），服务端**先全量校验**（key 唯一、
  描述非空、依赖只指向本次 key、环检测），通过后建节点、连边，并置 `TaskGraph.planned`；
- `AgentTeam(require_plan=True)` 下，`spawn_agent` 拒绝两种情况：
  1. 图还没规划 → `no plan yet: call plan_tasks ...`；
  2. 节点 `description` 为空 → 要求先写 briefing（执行者看不到主对话）。

宿主/bench 打开 `require_plan` 后，"任务规划好才执行"从提示词变成**前置条件**。

### 2. 分工的所有权：`spawn_agent(files=[...])` + 写保护

父子共享工作区，之前没有机制阻止父 agent 继续改同一批文件——实测会把同一件事做两遍。
现在：

- `spawn_agent` 接受 `files`（相对工作区的文件/目录），记到 `Subagent.scope`；
- `AgentTeam.write_blocked(path)`：子 agent **运行期间**，其 scope 内的路径归它独占；
- `create_file_tools(..., guard=...)` 的 `write_file` / `edit` / `delete_file` 命中时直接失败：
  `pkg/a.py is owned by sub-1 right now: do not edit it. Call wait ... then integrate it.`；
- 子 agent 结束/关闭后自动解除；`spawn_agent` 返回值同时写明 handoff。

### 3. 通信：带类型 + 完成通知

- `Message.kind`：`text | question | result | status`，渲染成 `<message from=sub-1 kind=question>`；
  `send_message` 新增 `kind` 参数，非法值报错；
- 子 agent 完成/失败时**主动通知 lead**（`AgentTeam._notify_lead`）：
  `[subagent sub-1] task X (subject) completed\nresult: ...`，失败带 `error:`；
  以前只有 `wait` 能看到结果，现在边跑边能收到。

### 4. bench 接入与实测

- `bench/run.py --multiagent`；`exec_mode` 建 `AgentTeam(..., require_plan=True)` 并回填；
- `TaskResult` 新增 `multiagent / subagents / sub_input / sub_output`，轨迹新增
  `multiagent` 与 `subagent_turn`，报告多一行子 agent 统计；
- 启用时任务消息追加 `DELEGATION_HINT`（先 `plan_tasks`、派活带 `files`、`wait` 后集成）；
  `GRADUS_MA_FORCE=1` 仅用于冒烟。

**冒烟数据**（`pytest-dev__pytest-10081`，强制派活）：

| 项 | 单 agent | 多 agent |
| --- | --- | --- |
| 判定 | resolved | resolved |
| 主轮 / 子轮 | 21 / — | 59 / 49 |
| 输入 | 211,928 | 971,298 + 772,578 |
| 墙钟 | 4.5m | 19.8m |

结论：**多 agent 不省 token**（总量是各会话相加），省的是墙钟与单会话上下文；
小任务上派活纯属额外开销，只有"独立、够大、父 agent 不再重复做"的子任务才划算。

### 5. 本轮修掉的 bug

- `Session.spawn` 引用了不存在的 `skill_usage` 局部变量 → 任何真实派活都 `NameError`；
- `WaitTool` 不传超时会把 `None` 透给 `asyncio.wait`（无限等）；
- 并发 `spawn` 名字撞车（先算名字后自增）；
- `aclose` 撞上 in-flight `spawn` 会漏子 agent；
- `TraceWriter.record(kind=...)` 与 `AgentError.kind` 撞名。

### 6. 仍未做

- 子 agent 的 token 尚未计入**主会话预算**（bench 里只记账，不设上限）；
- 消息仍是自由文本 + kind，没有 request/response 配对、广播与订阅；
- 没有"计划完整性"校验（是否覆盖目标、叶子数量是否合理）；
- `max_agents` 是总数上限，不是并发上限。

