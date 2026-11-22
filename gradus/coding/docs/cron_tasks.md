# Cron Tasks — 按时间启动一次对话

> *"闹钟替用户按下回车"* —— 任务表记住「什么时候、跑什么」，时钟每秒看一次表，到点就把提示词跑成一轮。
>
> **Harness 层**：调度在宿主的事件循环里，执行复用 `Session.prompt` 的那条管道。

---

## 问题

`background tasks` 解决的是「命令开始之后怎么跑」：耗时命令放后台，不阻塞当前轮次。它不记录「某项工作该在什么时间开始」，也没有东西持续看表。

「每天早上 9 点跑一遍测试」「每 30 分钟看一次 CI」这类需求，靠现有的东西只能靠用户自己每次到点重发 prompt。

需要的是三件独立的事：

1. 记住任务：什么时候、跑什么、跑到哪一步了；
2. 看时间：到点了要有人发现；
3. 执行：发现之后把提示词跑成一轮完整对话。

---

## 解决方案

四层，各自的职责互不重叠：

| 层 | 文件 | 负责 | 不负责 |
|---|---|---|---|
| 任务表与 cron | `coding/schedule.py` | 解析、匹配、状态流转、`schedules.json` 读写 | 不碰 asyncio，不认识 `Session` |
| 网关 | `coding/session.py` | `take_due` / `pending_jobs` / `prompt_scheduled` | 不主动跑，不问时间 |
| 驱动 | `host/serve.py` | 每秒看表，空闲时点火 | 不判断 cron，不改任务状态 |
| 工具 | `coding/tools/schedule.py` | 增删查任务 | 从不执行任务 |

一次触发的完整路径：

```
09:00:00  时钟 tick → schedule.due() 标记 pending_delivery=True 并落盘
          会话空闲 → create_task(run_scheduled(job))     ← 不 await
09:00:00  run_scheduled 开始：跑 [Scheduled] <prompt>
09:00:03  首个 TurnEnd → schedule.ack(job.id)            销账
09:03:10  AgentDone → 前端收到 idle
```

---

## 工作原理

### ScheduledJob：一条任务有九个字段

```python
@dataclass
class ScheduledJob:
    id: str                       # job_xxxxxxxx
    cron: str                     # 五段式表达式
    prompt: str                   # 到点后要跑的提示词
    recurring: bool = True        # False 表示跑完就删
    durable: bool = True          # 只决定「重启后还在不在」
    pending_delivery: bool = False  # 到点了、还没被模型接收
    last_fired: str | None = None   # "2026-11-10 09:00"，防止同一分钟重复标记
    attempts: int = 0             # 本轮失败次数
    last_error: str | None = None
```

`pending_delivery` 是整套设计里最关键的一个字段，下面会反复用到它。

### CronExpression：解析一次，之后只查集合

```text
分钟  小时  日  月  星期
  *    *   *   *   *      每分钟
  0    9   *   *   *      每天 09:00
 */5   *   *   *   *      每 5 分钟
  0    9   *   *  1-5     工作日 09:00
```

支持 `*`、`*/N`、`N`、`a-b`、`N,M,...`；不支持名称、`@daily` 别名、`a-b/N`。

解析的产物是五个 `frozenset[int]`，匹配就是 `in` 查询：

```python
def matches(self, moment: datetime) -> bool:
    if moment.minute not in self.minutes: return False
    if moment.hour not in self.hours: return False
    if moment.month not in self.months: return False
    # 日与星期都被限定时取「或」，任一为 * 时只看另一个
```

这与 s12 有个实质差别：那边 `validate_cron` 和 `cron_matches` 是两套代码，校验在写入口、匹配假设数据已经合法。这里解析即校验——`parse()` 失败就抛 `ScheduleError`，不可能出现「存进去合法、跑的时候解不出来」。

星期以 0 为周日（标准 cron），与 `datetime.weekday()`（周一为 0）差一位，转换在 `matches` 里做：

```python
weekday_matches = (moment.weekday() + 1) % 7 in self.weekdays
```

### due()：只标记，不执行

```python
def due(self, moment=None) -> tuple[ScheduledJob, ...]:
    now = moment or datetime.now()
    marker = now.strftime(MINUTE_FORMAT)      # "2026-11-10 09:00"

    for job in self._jobs.values():
        if job.pending_delivery or job.last_fired == marker:
            continue                           # 已在待交付 / 这一分钟已标记过
        if not self._expression_for(job.cron).matches(now):
            continue

        job.pending_delivery = True
        job.last_fired = marker
        job.attempts = 0                       # 新的一轮，重试计数归零
        fired.append(job)

    if fired:
        self._write_quiet()                    # 先落盘，再让上层去跑
    return tuple(fired)
```

要点：

- 它是**同步方法**，没有一行 `await`，所以整段是原子的，不需要锁；
- 它**不启动任何东西**，返回一个元组，用不用由调用层决定；
- 先写盘再返回：进程在「到点」和「跑完」之间退出，重启后任务仍是待交付状态；
- `_write_quiet()` 吞掉 `OSError` 只记进 `warnings`——它由时钟调用，不能因为磁盘问题让时钟挂掉。

### 没有队列：pending_delivery 就是队列

s12 用两个内存容器：`scheduled_jobs`（表）+ `cron_queue`（队列）。队列里装的是任务对象的**引用**，于是「`pending_delivery == True`」和「在 `cron_queue` 里」是同一个事实的两份记录，必须每个转移函数手工对齐。漏掉一处的后果是致命的：`pending` 但不在队列 → 永远不会被取走 → 任务消失。

这里去掉队列，只留字段：

```python
def pending(self) -> tuple[ScheduledJob, ...]:
    return tuple(job for job in self._jobs.values() if job.pending_delivery)
```

语义上它就是一个队列视图：

| 队列操作 | 实现 |
|---|---|
| `put(job)` | `due()` 把 `pending_delivery` 置 `True` |
| 队列内容 | 所有 `pending_delivery is True` 的任务 |
| `qsize()` / 遍历 | `pending()` |
| `get()` 出队 | `ack()` 把标记清回 `False` |

区别只有一点：出队不是 `pop`，是**清标记**。任务留在表里，`last_fired`、`attempts` 这些元数据都不丢，下次到点接着用。而「失败放回队列」这件事也不需要代码——失败只是**不清标记**。

不变式从「两处必须一致」变成「一处自己成立」，`restore_cron_jobs()` 这种回滚函数在这里无从谈起。

### 五个状态

| 状态 | 怎么进 | 怎么出 |
|---|---|---|
| 未到点 | `add()` 之后、非匹配分钟 | `due()` 命中 → 待交付 |
| 待交付 | `due()` 标记 | `ack()` / `fail()` 用尽 / `cancel()` |
| 待重试 | `fail()` 且 `attempts < 3` | 下一个 tick 重新点火 |
| 重试用尽 | `fail()` 到 `max_attempts` | 等下一次 cron 匹配（一次性任务直接删） |
| 已删除 | `cancel()` / 一次性任务跑完 | — |

**刻意没有 `running` 状态。** 一旦引入它，就必须回答「进程在 running 状态崩溃了怎么办」，答案永远是一套心跳 / 租约 / 超时回收。代价是任务可能被重复执行——这里接受重复，换永不卡死。

重复为什么不会失控：`pending_delivery` 在 `ack()` 之前一直是 `True`，`due()` 第一行就跳过它。所以这个字段同时兼任了「待交付」和「在飞」两个意思。

### 销账：放在首个 TurnEnd

```python
# session._run
async for event in events:
    if job is not None and not settled and isinstance(event, TurnEnd):
        self._settle(job, None)      # ack
        settled = True
```

判定标准是「模型产出了一轮」，不是「整次运行结束」。理由：只要模型已经接到这条提示词并开始干活，就该记账；之后再失败（比如第二个工具调用超时）属于这一轮的运行结果，不该让调度器重跑一遍。

`ack()` 的动作很小：

```python
job.attempts = 0
job.last_error = None
if job.recurring:
    job.pending_delivery = False     # 周期任务等下一次匹配
else:
    self._jobs.pop(job_id, None)     # 一次性任务删掉
```

### 失败与取消

```python
def fail(self, job_id: str, reason: str) -> None:
    job.attempts += 1
    job.last_error = reason
    if job.attempts >= self._max_attempts:      # 默认 3
        if job.recurring:
            job.pending_delivery = False        # 放弃这一分钟，等下次匹配
        else:
            self._jobs.pop(job_id, None)
```

没有上限的后果是热循环：模型服务挂了的十分钟里，每秒重试一次，烧 600 次请求。

**取消不计数。** `_run` 里对 `AgentError(kind="aborted")` 单独处理：

```python
if job is not None and not settled:
    if event.kind != "aborted":
        self._settle(job, event.message)
    settled = True
```

取消既不算成功也不算失败，任务保持待交付，下一次 tick 重新点火。这条约定和 `abort()` 的语义一致：用户停的是「这一轮」，不是「这条任务」。

### 持久化：只写 durable 的，先写临时文件

```python
payload = [asdict(job) for job in self._jobs.values() if job.durable]
temporary = self._path.with_name(f"{self._path.name}.{os.getpid()}.tmp")
temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
os.replace(temporary, self._path)
```

- 临时文件名带 pid，多进程同时写也不会互相踩；
- `os.replace` 是原子的，读者要么看到旧文件、要么看到新文件，不会看到半个；
- 写失败在 `add()` / `cancel()` 里回滚内存改动，在 `due()` / `ack()` / `fail()` 里降级成一条 warning。

**`durable` 只决定「重启后还在不在」，不决定「会不会触发」。** `due()` 完全不看这个字段。

| `durable` | `recurring` | 重启后 | 典型用途 |
|---|---|---|---|
| True | True | 还在，继续按 cron 触发 | 每天早上的例行检查 |
| True | False | 还在（没跑就在） | 明天下午提醒我一次 |
| False | True | 消失 | 本次会话内的临时轮询 |
| False | False | 消失 | 试用一下 |

加载时逐条校验，坏条目跳过并记 warning，不让一个坏条目挡住整个文件：

```python
for item in payload:
    try:
        job = _job_from_payload(item)
    except ScheduleError as exc:
        self._warnings.append(f"skipped saved job: {exc}")
        continue
    self._jobs[job.id] = job
```

因为 `pending_delivery` 也落盘，崩溃恢复是天然的：重启后 `pending()` 里还有那条任务，时钟一起就会把它跑掉。这就是「至少一次」交付。

### 驱动：一个时钟协程，每秒一轮

```python
async def clock(self) -> None:
    while True:
        try:
            self.tick()
        except Exception:
            pass
        await asyncio.sleep(1)

def tick(self) -> None:
    job = self._next_job()
    if job is not None:
        self.run_task = asyncio.create_task(self.run_scheduled(job))

def _next_job(self) -> ScheduledJob | None:
    session = self.session
    if session is None: return None
    session.take_due()                                  # 先标记，不管跑不跑
    if self.run_task is not None and not self.run_task.done(): return None
    if session.state != "idle": return None
    pending = session.pending_jobs()
    return pending[0] if pending else None
```

三段职责严格分开：

- **看表**：`take_due()` 无条件先做。会话忙着也照标——「到点了」这个事实不能因为忙就丢掉；
- **点火**：`create_task` 立刻返回，时钟继续转；
- **执行**：`run_scheduled` 在另一条协程里跑完整一轮。

如果点火改成 `await session.prompt_scheduled(job)`，时钟会暂停在这一行的整个运行期间。设 A 在 9:01 跑 9 分钟，B 在 9:02、C 在 9:05：

| 时刻 | `await` | `create_task` |
|---|---|---|
| 9:02 | 循环卡在 A 里，`due()` 没被调用 | B 被标记住（忙，先不跑） |
| 9:05 | 同上，C 也没被看到 | C 被标记住 |
| 9:10 | 恢复，`due()` 用 9:10 匹配，B/C 都不命中 → **永久丢失** | 空闲后 B、C 依次补跑 |

丢的原因不是慢，是 `due()` 只问「现在这一分钟」，从不补跑。所以「到点了」必须在那一分钟里被记下来一次。

`run_task` 复用用户回合的那个字段，于是三件事免费获得：`session/prompt` 在运行中会被丢弃（一次只跑一轮）、`switch()` 会先 abort、`shutdown()` 会等它收尾。

时钟的生命周期跟着 `Connection`：

- `connect_session()` 结尾 `start_clock()`；
- `shutdown()` 第一行 `stop_clock()`——否则关会话的同时它还在点火下一条；
- 切换工作区走 `connect_session`，旧会话先 `shutdown`，旧时钟自然停。

### 工具面

| 工具 | 权限 | 说明 |
|---|---|---|
| `schedule_task(cron, prompt, recurring)` | 询问（未知工具分支） | 创建的任务会在无人值守时跑一整轮，值得先确认 |
| `manage_schedules(action="list"｜"cancel", job_id)` | 放行 | 只动任务表，不会立刻执行任何东西 |

两个工具都只读写任务表。`add()` 有一个真实调用方之后，`schedules.json` 才会被创建。

---

## 与 learn-claude-code s12 的对照

| | s12 | 本章 |
|---|---|---|
| 内存状态 | `scheduled_jobs` + `cron_queue` 两份，手工对齐 | 只有 `_jobs`，`pending_delivery` 兜住队列语义 |
| 执行载体 | `cron_scheduler_loop`（1s 打标记）+ `queue_processor_loop`（0.2s 抢锁执行） | 一个时钟协程（1s），执行交给 `create_task` |
| 并发保护 | `agent_lock` | 单会话单 `run_task`，同一事件循环线程，不需要锁 |
| 解析与匹配 | `validate_cron` / `cron_matches` 两套 | 解析成 `frozenset`，匹配即查表 |
| 销账时机 | 第一个 `messages.create` 之后 | 首个 `TurnEnd` 事件 |
| 失败回滚 | `restore_cron_jobs()` 把任务塞回队列 | 不清标记即可，不需要恢复代码 |
| 失败上限 | 无 | 3 次，超了等下次匹配 |
| `*/N` 语义 | `value % N == 0` | `range(low, high+1, step)` |
| 运行载体 | 模块级全局状态 + 线程 | `Session` 实例方法 + 协程 |

s12 必须用两个线程：`input()`、同步 SDK、`subprocess.communicate()` 全是阻塞调用，只能靠线程并行。本章最里层都有 async 版本，一个时钟协程就够——但**看表与执行必须分开**这条结论不变，只是从「两个线程」变成「一次 `create_task`」。

---

## 已知边界

- **只在 Web 宿主里响**。CLI 用 `input()` 读终端，会停住整个事件循环，时钟起不来；
- **多连接会重复触发**。每个连接一份 `Schedule` 内存快照，同一个 workspace 开两个页面就是两份表，各自写各自的 `schedules.json`。要支持得加文件锁或读前重载；
- **不补跑**。9:05 启动的进程不会补做 9:00 的任务——`due()` 只看当前分钟；
- **只有本地时间**，没有时区字段；
- **待交付按建表顺序取**，不是按到点先后。同一分钟多条任务同时待交付时，顺序没有意义保证。

---

## 文件与测试

| 文件 | 内容 |
|---|---|
| `coding/schedule.py` | `CronExpression`、`ScheduledJob`、`Schedule`（add / cancel / jobs / pending / due / ack / fail / 读写） |
| `coding/session.py` | `take_due`、`pending_jobs`、`prompt_scheduled`、`_settle`，以及 `_run` 里的销账与取消分支 |
| `coding/tools/schedule.py` | `schedule_task`、`manage_schedules` |
| `host/serve.py` | `clock` / `tick` / `_next_job` / `run_scheduled` / `start_clock` / `stop_clock` |

`coding/tests/test_schedule.py`（解析、状态流转、落盘、Session 级销账与取消、两个工具）与 `host/tests/test_clock.py`（`_next_job` 的四种放行条件）共 40 个用例。
