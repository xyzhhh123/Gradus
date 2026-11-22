# Background Tasks — 慢命令放后台，循环继续跑

> *"慢操作放后台，Agent Loop 继续运行"* —— 命令交给任务表，占位结果立即返回，完成时补一条通知。
>
> **Harness 层**：异步执行，不阻塞主循环。

---

## 问题

`read_file`、`git status` 这种调用等待不明显。但装依赖、跑全量测试、构建项目要几分钟。同步执行时，整个 Agent Loop 停在这一次工具调用上：本轮后续的工具调用不能开始，下一轮请求也发不出去。

如果后续工作并不依赖这条命令，这个等待就是纯损失。模型想做的其实是「把它跑起来，我待会儿看结果」。

本章要解决的是：让耗时的 shell 命令在后台跑，当前轮次立刻拿到一个可引用的任务号，完成后结果自动回到对话里。

---

## 解决方案

不新增工具，只给 `bash` / `powershell` 加一个参数：

```
run_in_background: true   →  交给 BackgroundTasks，立即返回 bg_0001
run_in_background 省略/假   →  同步执行，等命令结束
```

完成的命令会生成一条 `<task_notification>`，由 `Session` 注入成一条带 `origin="harness"` 的用户消息。

| | 同步 | 后台 |
|---|---|---|
| 慢命令 | 当前工具调用被阻塞 | 独立 asyncio task 执行 |
| Agent Loop | 等命令返回 | 拿到占位结果后继续 |
| 返回值 | 命令输出 | 任务号 + 日志路径 |
| 结果送达 | 当轮工具结果 | 一条 `task_notification` 消息 |
| 判定标准 | — | `run_in_background is True` |

---

## 工作原理

### 判定只有一条：显式参数

`tools/shell.py` 里只有一句判断：

```python
if arguments.get("run_in_background") is True:
    return await self._start_background(command)
```

不按 `install`、`build`、`test` 这些关键词猜。关键词猜错的代价是双向的：该后台的没后台（白等），不该后台的被丢到后台（模型拿不到输出却以为拿到了）。是否后台由调用方显式决定。

`is True` 而不是真值判断：`"false"` 这种字符串不会意外打开后台。

### 占位结果就是这个调用的正确答案

```python
ToolResult(
    f"[background {task.id} started]\n"
    f"status: running\n"
    f"log: {task.log_path}\n"
    "A <task_notification> arrives when it finishes; "
    "call check_background to inspect it earlier.",
    details={"background_task_id": task.id, "status": "running"},
)
```

这不是伪造。模型问的是「把它跑起来」，答案就是「已启动，任务号 bg_0001」。跟调用一个异步 HTTP 接口拿到 job id 是同一件事。

协议上这也必须如此：一个 `tool_use` 有且只有一个 `tool_result`，且 `tool_result` 必须紧跟在它后面。占位结果当场把这次配对结清，之后的完成通知才有地方放——它不能是第二条 `tool_result`，那会被 provider 以重复 `tool_use_id` 拒绝。

### BackgroundTasks：任务表

`coding/background.py` 一个会话一张表，与会话同寿命：

```python
self._tasks: dict[str, BackgroundTask] = {}   # 可观察状态，按 id
self._order: deque[str] = deque()             # 建表顺序，用于淘汰
self._running: dict[str, asyncio.Task]        # 在跑的协程
self._processes: dict[str, Any]               # 在跑的进程，用于 kill
```

`start()` 先登记再起协程，立刻返回：

```python
task_id = f"bg_{self._counter:04d}"
self._tasks[task_id] = task
self._running[task_id] = asyncio.create_task(self._run(task, argv, cwd))
return task
```

任务状态只有四个：`running` / `completed` / `failed` / `killed`。`killed` 覆盖超时和取消两种来源，对模型来说它们没有区别——都没有正常结束。

### 输出边读边写

不用 `process.communicate()`。它会把全部输出攒进内存再一次性返回，输出 1GB 就占 1GB 内存，截断发生在内存已经被吃掉之后。

这里把 stdout 流式读进 `_LogWriter`：

| 区段 | 去哪 | 大小 |
|---|---|---|
| 前 64KB | 直接写日志文件 | `LOG_HEAD_CHARS` |
| 中间 | 只累加 `omitted` 字符数 | 0 |
| 最后 256KB | 内存里滚动 | `LOG_TAIL_CHARS` |

`close()` 时把省略长度和尾部一起追加到文件末尾，所以日志文件约 320KB 封顶，进程内存也不随输出增长。通知和 `check_background` 读的都是这份尾部。

读块的写法要兼容两条路径（`_read_chunk`）：`asyncio.StreamReader.readline()` 遇到没有换行的超长行会抛 `ValueError`，退化成定长 `read(65536)`；Windows 管道的 `read()` 不接受长度参数，再退化成裸 `read()`。

### 完成通知怎么进对话

`_run` 的 `finally` 里调用 sink：

```python
if not self._closed and self._sink is not None:
    self._sink(self._notice(task, tail))
```

`Session` 在构造时把它绑到自己的 `_notify` 上，于是分两种情况：

```
任务完成
  ├─ Agent 正在运行 → Agent.notify(text) → 推进本次运行的 steering 队列
  │                     （下一轮请求前取到，表现为一次中途插话）
  └─ Agent 空闲       → Session._notices.append(text)
                        （下一次运行开头合并补送，一次请求只带一条）
```

这就是「在哪里检查任务是否完成」的答案：**没有轮询**。后台任务自己在完成的瞬间推消息，检测点是事件驱动的。

两条路径的载体都是 `UserMessage(origin="harness")`，不是 `ToolResultMessage`：

- `ToolResultMessage` 有配对义务，第二次应答同一个 `tool_use_id` 会被 provider 拒绝；
- `origin` 只存在于内部，不上 wire，四家 provider 适配器零改动；
- 会话落盘和压缩摘要据此把它标成 `System notice`，不会当成用户发言。

### 模型的三条路径

| 场景 | 用什么 |
|---|---|
| 命令还在跑，想看中间输出 | `check_background`（一个固定工具，不走检索） |
| 命令跑完了 | 自动到达的 `<task_notification>` |
| 上次运行留下的任务 | 运行开头补送的合并通知，或 `check_background` |

`check_background` 必须是固定工具（direct），不能走 `tool_search` 检索：没有 MCP 时 `tool_search` 根本不存在（只有存在 deferred 工具才注册），而 `tool_search` 按空格切词打分，中文查询命中率接近零。模型缺的不是「发现能力」，是「查状态」。

### 三条资源约束

| 约束 | 值 | 超了怎么办 |
|---|---|---|
| 同时运行数 | 4 | 直接报错，不排队 |
| 单命令存活 | 1800s | 杀掉，按完成发通知 |
| 保留任务数 | 50 | 删最旧的已完成任务和它的日志 |

不排队的理由：排队的任务对模型不可见——它既查不到，也不会收到通知，等于黑洞。报错至少能让模型知道「现在开不了，先等一个」。

超时算完成：保证「启动过的任务一定会回来」，否则模型永远在等一条不会到的通知。

### 关闭顺序

`Session.aclose()` 里 `background.aclose()` 必须排在 `workspace_copy.sync()` 之前——后台命令可能还在往沙箱副本里写，先合回会丢产物。

`aclose()` 本身：kill 所有进程 → cancel 所有协程 → 给还没开始执行就被取消的任务补 `killed` 状态。

而 `abort()` 不碰后台任务。它取消的是一次运行，不是会话，命令继续跑，结果留在日志里。

---

## 与 learn-claude-code s11 的对照

| | s11 | 本章 |
|---|---|---|
| 执行载体 | `threading.Thread(daemon=True)` + `subprocess.Popen` | `asyncio.create_task` + 沙箱 `env.spawn` |
| 输出收集 | `communicate()` 全量进内存再截断 | 流式写盘，内存恒定约 320KB |
| 完成通知 | 自由文本 `<task_notification>` 作为 user 消息 | 同一形态，但标 `origin="harness"` 区分来源 |
| 跨运行的结果 | 存在模块级全局里，靠 `check_background` 捞 | `Session._notices` 缓冲，下次运行开头合并补送 |
| 结果可见性 | 只给 `result[:500]` | 尾部 40 行进通知，完整尾部走 `check_background` |
| 并发上限 | 无 | 4，超了报错不排队 |

相同的是三件关键决定：显式参数、占位结果、通知不复用 `tool_use_id`。

用协程而不用线程，是因为这里最里层的调用（`env.spawn` / `readline`）都有 async 版本，走事件循环就够；线程只在「最里层是同步阻塞且没有 async 替代」时才需要，那是 s12 的处境。

---

## 文件与测试

| 文件 | 内容 |
|---|---|
| `coding/background.py` | 任务表、执行、`_LogWriter`、通知格式 |
| `coding/tools/shell.py` | `run_in_background` 参数与分流 |
| `coding/tools/background.py` | `check_background`：只读查询 |
| `coding/session.py` | 建表、绑定 sink、`_notices` 缓冲与补送、关闭顺序 |
| `agent/agent.py` | `notify()`：往 steering 队列推带标记的消息 |

`coding/tests/test_background.py`、`agent/tests/test_notify.py` 覆盖：显式判定、并发上限、超时、日志头尾截断、通知格式、运行中插话与空闲补送。
