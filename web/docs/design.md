# Gradus Web 设计

给 Gradus 加浏览器界面。前端是独立的 TypeScript 应用，Python 侧新增一个 Host 适配层，两者通过一条事件流连接。

- 前端：`Gradus/web/`，TypeScript + React + Vite + **Cordis**
- Host：`Gradus/gradus/host/`，只做协议、转发与安全，不改 `coding` 及以下任何一层
- 参考：`deepseek-harness` 的 Web Client（下称 dsh）与它的 `vendor/cordis`

## 0. 结论

1. **前端独立成顶层目录 `Gradus/web/`**，不放进 `coding/`。它是另一个产品入口，语言与构建链都不同。
2. **浏览器侧使用 Cordis 作为唯一装配机制**。理由见 §2，不是"因为它有趣"，而是这个应用正好有四个问题需要它解决。
3. **Host 侧单独成层 `gradus/host/`**，依赖方向是 `host -> coding -> agent -> ai`，不反向。
4. 分层、事件 fold、slot 声明制照抄 dsh；动态模块加载与代码生成不抄。

## 1. 为什么不在 `coding/` 里

`coding` 是 CLI 产品层：它决定用哪个模型、拿哪些工具、在什么权限下跑，输出给终端。

Web 是**同级的另一个产品入口**，共用 `coding.Session`，但有自己的传输、状态模型和渲染。

```text
Gradus/
├── gradus/                # Python 包
│   ├── ai/                # 协议与 provider
│   ├── agent/             # 循环与工具执行
│   ├── coding/            # CLI 产品层（现有）
│   └── host/              # 新增：Web 后端适配层
└── web/                   # 新增：浏览器应用（TS）
    ├── docs/
    ├── src/
    └── package.json
```

依赖方向：

```text
web (TS)  --JSON-->  host (py)  -->  coding  -->  agent  -->  ai
```

`host` 只允许做三件事：把 `AgentEvent` 序列化、把命令转成 `Session` 调用、把确认请求转成 UI 消息。任何业务判断都不能写在 `host` 里。

## 2. 为什么用 Cordis

上一版设计说"功能不到十个，不值得"，这是按最小复杂度做的判断，不是事实。重新核对后，这个应用恰好有四个问题正好是 Cordis 的形状。

### 2.1 启动顺序

没有 Cordis 时，`main.tsx` 必须手写：

```ts
const conn = await openConnection(url)
const store = createStore(conn)
const slots = createSlots()
registerBuiltinSlots(slots)
const root = mount(store, slots)
```

七行里藏着六条隐含依赖：store 需要 conn，视图需要 store，工具卡片需要 slots 已声明，renderer 需要全部就绪。加一个功能就要想"插在第几行"。

用 Cordis 后：

```ts
ctx.plugin(connectionPlugin)
ctx.plugin(storePlugin)      // inject: ['connection']
ctx.plugin(slotsPlugin)
ctx.plugin(rendererPlugin)   // inject: ['slots', 'sessions']
ctx.plugin(toolCardsPlugin)  // inject: ['slots']
```

**不需要写顺序。** 顺序由 `inject` 推出来，而且推错会表现为 PENDING（能看见），不是运行时 undefined（看不见）。

### 2.2 可替换的传输

`connection` 是一个服务。开发 UI 时用一个 `replay-connection` 顶替它，从录制好的事件文件里吐帧，前端不连后端也能跑。

这不是设想：Gradus 的 `ai` 层已经有 `scripts/replay.py` 的做法，把这套思路搬到 UI 测试上是顺的。没有服务名，替换就得改 `main.tsx` 的 import。

### 2.3 插槽的等待问题

工具卡片要往 `tool.call.view` 注册，但这个渲染位由布局组件声明。声明什么时候发生不确定。

dsh 的解法是 `ctx.slots.inject(key, callback)`：等真正的声明出现，声明塌了贡献自动撤掉，重新声明后再跑一遍。

这个能力建立在 Cordis 的 effect 生命周期上。自己做要么写轮询，要么写事件订阅 + 手动解绑。

### 2.4 开发期热替换

改一个工具卡片的样式，Cordis 的 `restart()` 能单独把它卸掉重装，不需要刷新页面。没有这层，就得整页 reload，会话状态全丢。

### 2.5 代价，必须写清楚

| 代价 | 表现 | 缓解 |
|---|---|---|
| 多一层间接 | 找不到某个 UI 元素来自哪个插件 | 插件 id 与目录同名 |
| 组件容易偷看 ctx | 状态来源说不清 | 硬规则：组件只见 props |
| 谁没起来不好查 | 页面空白，控制台没报错 | 启动后打印每个 fiber 的 state |
| 依赖不直观 | 读代码要跳几个文件 | 每个插件头部列出 inject 与 provides |

### 2.6 什么时候不用

如果将来前端缩成"一个页面 + 两块卡片"，把 Cordis 删掉，退回 §2.1 的七行代码。判断标准只有一条：**手写启动代码超过三十行，就该用它。**

## 3. 借 dsh 的哪些内容

| dsh 里的东西 | 出处 | 我们怎么用 |
|---|---|---|
| 分层与单向依赖 | `docs/subsystems/web-client.md` §Layers and ownership | 原样照搬 |
| Host 掌事实源，Client 只做投影 | 同上 §Client models | 会话状态只由 Python 侧决定 |
| 事件 → 上下文 → 视图快照 | `docs/subsystems/conversation.md` | 用一个纯函数 `reduce(frame, state)` |
| 渲染不扫描全量事件 | 同上 §Replayable event families | 增量累积，不做全量重算 |
| Slot 声明 + 授权 | `docs/subsystems/slots.md` §Declaration and lifecycle | 未声明就注册 → 直接报错 |
| `slots.inject(key, cb)` | 同上 | 跨插件贡献渲染位 |
| 组件永远看不到 ctx | `packages/client/AGENTS.md` §ctx discipline | 组件只接收 props |
| 重连 = 新基线 + 增量 | `web-client.md` §Reconnection | 每代连接先发快照 |
| 纯展示状态不进会话记录 | `web-client.md` §Layering red lines | 面板宽度只存浏览器 |
| Cordis 的 `Context` / `inject` / `effect` | `vendor/cordis/src` | 见 §4 |

不抄：

| 东西 | 原因 |
|---|---|
| Client Modules 动态加载 | 收益是插件市场，需要模块表 + 启动审计，当前没有第三方插件 |
| typert 代码生成 | 那是给 40+ 包共享类型用的；我们一份 JSON schema 够用 |
| Conversation target registry | dsh 有 chat / trajectory 两套视图，我们只有一套 |
| 多会话并行窗口 | 第一版单会话 |

## 4. 浏览器侧

### 4.1 启动

```ts
// main.tsx
import { Context } from '@deepseek-ai/cordis'

const ctx = new Context()
for (const plugin of plugins) ctx.plugin(plugin)
```

没有 `await` 链，没有顺序。启动结束后打印一份状态表：

```text
connection      ACTIVE
sessions        ACTIVE
slots           ACTIVE
renderer        ACTIVE
tool-cards-bash ACTIVE
approval        PENDING   missing: sessions
```

最后一行就是"为什么某个功能没出现"的答案。

Cordis 来源：dsh 仓库的 `deepseek-harness/vendor/cordis`（`@deepseek-ai/cordis` 4.1.0，MIT）。npm 拉不到时直接复制该目录，或作为本地 workspace 依赖引用。

### 4.2 插件清单

| 插件 id | inject | provides | 职责 |
|---|---|---|---|
| `connection` | — | `connection` | WebSocket、重连、序号补洞 |
| `sessions` | `connection` | `sessions` | 事件 fold 成状态，提供订阅 |
| `slots` | — | `slots` | 声明与注册表 |
| `renderer` | `slots`, `sessions` | — | 唯一调用 `renderSlot('root')` 的地方 |
| `layout` | `slots` | — | 声明 `app.root`、`shell.blocks`，注册 Shell |
| `conversation` | `slots`, `sessions` | — | 声明 `conversation.turn`、`tool.call.view`，贡献会话块 |
| `composer` | `slots` | — | 输入框、发送、取消 |
| `approval` | `slots` | — | 确认弹窗 |
| `tool-bash` | `slots` | — | bash 结果卡片 |
| `tool-fs` | `slots` | — | 读写文件卡片 |
| `status-bar` | `slots` | — | 沙箱警告、连接状态 |

只有 `renderer` 与 `conversation` 需要 `sessions`：前者要把快照转成 hook，后者要区分「历史消息」与「实时轮次」。其余视图只接收 props。

依赖关系是真实的，不是摆设：

```text
connection ──> sessions ──> renderer
                    │
                    └──────> conversation / composer / approval / status-bar

slots ────────────> layout ──> renderer
   │
   └──> tool-bash / tool-fs / conversation
```

`tool-bash` 没有 `inject: ['sessions']`，因为它只画卡片，不读会话状态。这份清单本身就能说明"谁的改动会影响谁"。

### 4.3 服务契约

浏览器侧只需要五个服务，全部由自己定义：

| 服务名 | 形态 | 说明 |
|---|---|---|
| `connection` | `{ send(cmd), onFrame(cb), status }` | 唯一接触 WebSocket 的地方 |
| `sessions` | `{ getSnapshot(), subscribe(cb), prompt(text), cancel() }` | 唯一持有会话状态的地方 |
| `slots` | `{ declare(name), register(...), inject(key, cb) }` | 渲染位注册表 |
| `layout` | `{ header, footer }` | 布局锚点，供 slots 使用 |
| `theme` | `{ tokens }` | 可选，纯展示 |

方法不定义在组件里，定义在服务上；组件通过 props 拿回调。

### 4.4 组件纪律

照抄 dsh 的一条硬规则：

> 组件（`.tsx`）不接触 `ctx`，不 import 服务类，不读 React context。所有数据和回调通过 props 传入。

`ctx` 只存在于两处：

1. 插件的 `apply` 函数体；
2. 由 `apply` 闭包创建的 `inject` 面（把服务方法转成普通回调和只读数据）。

违反这条的典型症状：某个组件直接 `ctx.sessions.getSnapshot()`，然后你会发现它不会随状态更新——因为 `ctx` 不是响应式的。

### 4.5 slot 作为服务

`slots` 是普通服务，注册要跟随 Cordis 的 effect 生命周期：

```ts
export const inject = ['slots']

export function apply(ctx: Context) {
  ctx.effect(() => ctx.slots.register({
    name: 'tool.call.view',
    key: 'bash',
    order: 100,
  }, BashCard))
}
```

`ctx.effect` 的返回值是 disposer，插件卸载时自动撤销注册。不需要在组件里写 `useEffect` 清理。

首版渲染位：

| 渲染位 | 基数 | 谁声明 | 谁贡献 |
|---|---|---|---|
| `app.root` | single | `layout` | `layout`（Shell） |
| `shell.blocks` | list | `layout` | `status-bar`、`conversation`、`composer`、`approval` |
| `conversation.turn` | list | `conversation` | `conversation`（默认轮次） |
| `tool.call.view` | keyed（工具名） | `conversation` | `tool-bash`、`tool-fs` |

跨插件贡献一律走 `ctx.slots.inject(key, cb)`，不能直接 `register`。

这一点在骨架里实际踩到过：`tool-bash` 只 inject `slots`，比声明 `tool.call.view` 的 `conversation` 更早启动，直接 `register` 抛 `slot "tool.call.view" is not declared`，整个 fiber 变成 FAILED，卡片退化成原始 JSON。改成 `inject` 后由声明方唤醒，问题消失。

## 5. Host 侧

### 5.1 目录

```text
gradus/host/
├── serve.py            # 命令行入口、WebSocket、静态文件、确认桥
├── wire.py             # AgentEvent / 命令 的 JSON 编解码
└── tests/test_wire.py  # 只测翻译层，不起服务
```

依赖只往一个方向：`host` import `coding`，`coding` 不 import `host`。

新增依赖（`[project.optional-dependencies].web`）：`starlette` + `uvicorn` + `websockets`。
`uvicorn` 本身不带 WebSocket 实现，缺 `websockets` 时升级请求会被当成普通 HTTP，浏览器只看到 1006。

运行：

```bash
cd Gradus
uv sync --extra web
cd web && npm install && npm run build && cd ..
uv run --extra web gradus-serve --port 8765
```

启动后打印带 token 的地址，例如 `http://127.0.0.1:8765/?token=...`。前端会把这个 token 拼到 `/ws`；缺 token 时握手直接 4401（浏览器表现为 403）。

参数：

```text
gradus-serve [--host 127.0.0.1] [--port 8765] [--workspace DIR]
             [--config FILE] [--session-dir DIR] [--mcp FILE]
             [--token TOKEN] [--confirm-timeout 120]
```

### 5.2 帧格式

```json
{ "seq": 42, "type": "agent/event", "payload": { } }
```

服务端发出：

| type | 说明 |
|---|---|
| `session/snapshot` | 连接建立后的第一帧 |
| `session/message` | 服务端回显一条已入库的消息（用户输入），前端按 `id` 去重 |
| `agent/event` | 一条 `AgentEvent` |
| `interaction/confirm` | 需要确认，带 `id` |
| `session/status` | `idle` / `running` / `error` |

客户端发出：

| type | 说明 |
|---|---|
| `session/prompt` | `{ text }` |
| `session/cancel` | 取消当前运行 |
| `interaction/answer` | `{ id, ok }` |

### 5.3 AgentEvent 映射

`gradus/agent/types.py` 的 `AgentEvent` 是 9 个 dataclass 的 union：

| Gradus 事件 | wire type | UI 动作 |
|---|---|---|
| `AgentStart` | `agent/start` | 状态 running |
| `TurnStart` | `turn/start` | 新建一轮 |
| `AssistantEvent` | `assistant/delta` | 追加文本 |
| `ToolExecutionStart` | `tool/start` | 插入 pending 卡片 |
| `ToolExecutionUpdate` | `tool/update` | 按 `call.id` 追加 `partial` |
| `ToolExecutionEnd` | `tool/end` | 卡片完成，附结果与耗时 |
| `TurnEnd` | `turn/end` | 固化本轮 |
| `AgentDone` | `agent/done` | 状态 idle |
| `AgentError` | `agent/error` | 状态 error |

序列化只白名单字段，不 `asdict` 全量导出；`ToolCallBlock.id` 必须保留；`AgentError.partial` 单独标记。

### 5.4 确认与取消

**确认**：`confirm` 是 `async (question) -> bool`（`cli.py` L97）。Host 生成 `id`，发 `interaction/confirm`，挂起 Future，等回答或超时。

- 超时默认拒绝，不许默认同意；
- UI 未连接时按现有策略失败，不许静默放行；
- 问题原文透传。

**取消**：关闭 `Session.prompt` 的异步迭代器，等 `AgentError(kind="aborted")` 落地，再推 `session/status`。

### 5.5 已知缺口：没有事件级持久化

`SessionFile` 只保存 `messages`（`session.py` L594）与压缩记录，没有逐事件日志。

后果：实时能画工具卡片，重开历史只有消息。

第一版选"历史只显示消息"，把"事件日志"记入 `coding_plan.md` 的待定列。补的时候是一次性改动：新增事件文件、写入路径、回放测试，所有视图都受益。

### 5.6 安全

1. 默认只绑 `127.0.0.1`。
2. 一次性 token，启动时打印完整 URL；校验失败 403。
3. `Origin` 校验，只允许同源。
4. `sandbox_warning()` 非空时 UI 顶部常驻横幅。
5. 确认弹窗只能由点击触达，禁止"全部同意"默认值。
6. 不在前端暴露 API key、环境变量全量、`~/.gradus/trusted.json`。

## 6. 传输与重连

单通道 WebSocket，单调 `seq`。

连接建立后第一帧固定为快照：

```json
{
  "seq": 0,
  "type": "session/snapshot",
  "payload": {
    "messages": [ ],
    "status": "idle",
    "tools": ["bash", "read_file"],
    "sandbox": { "backend": "restricted", "warning": "network NOT isolated" }
  }
}
```

规则：

1. 每一代连接都从快照重建，不复用旧状态。
2. 快照之后从 `seq + 1` 开始；发现跳号就整代重建，不做局部修补。
3. 断开期间不可回放，UI 明确显示"连接断开"。
4. 重连后工具卡片不可还原（除非补齐 §5.5）。

## 7. 状态 fold

`sessions` 插件内部持有状态，暴露快照与订阅：

```ts
class SessionState {
  private state: Snapshot
  private listeners = new Set<() => void>()

  apply(frame: Frame) {
    const next = reduce(this.state, frame)   // 纯函数
    if (next === this.state) return
    this.state = next
    this.listeners.forEach(fn => fn())
  }

  getSnapshot = () => this.state
  subscribe = (fn: () => void) => { this.listeners.add(fn); return () => this.listeners.delete(fn) }
}
```

三条必须成立的约束：

1. 状态未变时 `getSnapshot()` 返回同一个引用，否则 React 会无限重渲染。
2. `reduce` 是纯函数，同一帧序列必须得到同一状态。
3. `tool/update` 必须按 `call.id` 归属，不能按到达顺序写给最后一张卡片（并行批次会交错）。

React 绑定只在一个地方：`renderer` 插件里用 `useSyncExternalStore` 把 `sessions.subscribe` 转成 hook，再通过 slot 的 props 分发。组件不自己订阅。

## 8. 分期

| 阶段 | 内容 | 完成标准 |
|---|---|---|
| P0 | Cordis 启动 + connection + sessions + renderer + 对话 + 确认 | 浏览器能对话、点确认、取消 |
| P1 | slots 服务 + 工具卡片 | `tool.call.view` 按工具名分卡片 |
| P2 | replay-connection | UI 测试不连后端 |
| P3 | 事件日志 | 重开历史能还原工具卡片 |
| P4 | 多会话 | 侧栏切换，每会话独立连接 |

P0 验收必须包含：启动状态表打印、断线重连、确认超时拒绝、沙箱警告可见。

## 9. 明确不做

- 不做多客户端同步（两标签页同时操作同一会话）。
- 不做移动端布局。
- 不让前端直接读写文件或起进程。
- 不把纯展示状态写进会话文件。
- 不实现 dsh 的 Client Modules 动态加载与 typert 代码生成。
- 不为没有调用方的能力预留抽象。

## 10. 与 dsh 的差异

| 维度 | dsh | Gradus |
|---|---|---|
| 后端语言 | TypeScript（同构） | Python（异构，靠 JSON） |
| 传输 | WebSocket mux + 逻辑流 + typert | WebSocket 单流 + 手写 schema |
| 客户端容器 | Cordis | Cordis（同一套机制） |
| 服务数量 | 数十个 | 5 个 |
| 插件数量 | 40+ | 11 个 |
| Slot 基数 | 5 种 | 3 种（single / list / keyed） |
| 会话持久化 | 事件级 | 消息级（见 §5.5） |
| 模块加载 | 动态模块表 + 启动审计 | Vite 静态打包 |
| 前端规模 | 数十个功能包 | 一个应用 |

结论：**装配机制相同，规模差一到两个数量级。** 抄机制与纪律，不抄基础设施。
