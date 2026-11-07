# Cordis 笔记

记录 `deepseek-harness` 所用插件框架 Cordis 的工作方式，以及它与 Gradus 现有装配方式的对照。

- 源码位置：`deepseek-harness/vendor/cordis`
- 包名与版本：`@deepseek-ai/cordis` 4.1.0（上游为 Shigma 的 cordis，MIT）
- 性质：TypeScript 库，不是运行时、不是脚手架。它解决的问题只有一个——**让一堆互不直接调用的模块按依赖关系启动、停用和替换**。

## 1. 它解决什么

普通写法里，「谁先启动」由代码行号决定：

```ts
const fs = await openFs()
const llm = await connectLlm()
const tools = new Tools(fs, llm)
```

这段代码有三个隐含限制：

1. 想换掉 `fs`，必须改这一行。
2. 想在运行时关掉 `tools`，没有对应操作。
3. 想只重启 `tools`，得把整段重跑。

Cordis 的做法是把这四行拆成四个插件，每个插件只写「我要什么」（`inject`）和「我提供什么」（`provide`），**不写调用顺序**。顺序由框架在运行时推出来。

## 2. 插件不等于实体

dsh 里这两件事是分开的：

| 东西 | 身份 | 位置 |
|---|---|---|
| `Agent` / `Session` | 类（数据类型） | `packages/core/agent`、`packages/core/session` |
| `agents` / `sessions` / `tools` | 服务（能力名字） | 同上，`super(ctx, 'xxx')` |
| `agent-loop` | 插件（跨实体编排） | `packages/core/agent-loop` |

`agent-loop` 的声明是：

```ts
static inject = ['agents', 'sessions', 'llm', 'tools', 'systemPrompt', 'sessionProjections']
```

它不「代表」任何一个实体，而是把多个实体拧在一起跑循环。

真正的拆分判据是下面四条，满足任意一条才值得拆成插件：

1. 可能有第二个实现（`fs-local` / `fs-sandbox` / `fs-ssh`）；
2. 可能被单独关掉（`plan-mode`、`session-log-deepseek`）；
3. 有自己的配置项（`cordis.yml` 里每一条都带 `config`）；
4. 有自己的生命周期（要 flush、要 abort、要 dispose）。

反过来，概念上是「实体」但换不掉、关不掉、没有配置的东西，就留在类里，不拆。

`packages/AGENTS.md` 里还有两条反向约束，防止拆得过细：

- **Require a current owner and need**：每个抽象必须绑定当前调用方。
- **Inverse smell**：只有内部一个调用方的公开服务方法是坏味道，应该用私有闭包。

结论：**服务要稳、要少；插件可以碎、可以多。** 服务是公共契约，插件是装配单元。

## 3. 两个基本概念

### 3.1 服务

一个有名字的能力。注册方式：

```ts
super(ctx, 'tools')            // Service 子类在构造函数里
ctx.provide('tools', value)   // 直接注册
```

同一作用域里一个名字只能有一份实现，重复注册会抛错（`reflect.ts:303-305`）。

### 3.2 依赖

写在插件的静态字段上，内容是**名字数组**：

```ts
static inject = ['fs', 'sessionProjections', 'systemPrompt']
```

关键点：**依赖的是一种能力，不是某个具体实现。** `inject = ['fs']` 的意思是「谁把 `fs` 这个名字注册了，我就用谁」。所以 `fs-local` 换成 `fs-sandbox` 时，消费方一行都不用改。

Gradus 里已经是同样的模式：`sandbox.py` 的 `BACKENDS` 字典按名字选实现（`local` / `restricted` / `appcontainer`）。区别只是我们**主动选**，Cordis **声明需求，由环境决定谁提供**。

## 4. 启动：广播 + 自己重算

整套机制可以拆成四个动作。

### 4.1 加载只登记，不执行

`new Fiber(...)` 做四件事（`fiber.ts:238-325`）：

1. 分配编号 `uid`；
2. 从父上下文派生一个子上下文；
3. 把 `inject` 里的配置挂到子上下文上（只是配置，不是依赖）；
4. 把 `dispose` 注册到父 fiber，并加入 `runtime.fibers`。

最后执行：

```ts
for (const name of Object.keys(this.inject)) this._checkImpl(name)
this._refresh()
```

**插件体一行都没跑。** 所以加载 40 个插件很快：40 次登记 + 40 次字符串计算。

### 4.2 epoch：判断「能不能跑」的唯一依据

`_refresh()`（`fiber.ts:617-628`）给每个插件算一个字符串：

```
自己的版本号 : 依赖1的 uid : 依赖2的 uid : ...
```

规则只有一条：**只要有一个依赖不在，整个字符串就是 `INACTIVE`。**

举例：`plan-mode` 的 `inject` 是 `['tools', 'systemPrompt', 'sessionProjections', 'agents']`，

- 四样齐了 → `"0:12:8:31:5"`
- 缺 `agents` → `INACTIVE`

三个输入分别由谁改变：

| 组成 | 何时变化 |
|---|---|
| 自己的版本号 `_revision` | `restart()` 时自增（`fiber.ts:768-774`） |
| 依赖的 `uid` | 该服务被重新注册时自增（`reflect.ts:302`） |
| 依赖缺失 | 字符串直接变成 `INACTIVE` |

### 4.3 `_setEpoch`：唯一的动作入口

`fiber.ts:630-646`，四个分支：

```ts
if (epoch === oldEpoch) return        // ① 没变 → 什么都不做
if (this._error) return               // ② 失败锁定，只有 update() 能解
this._runner.epoch = epoch
if (this.inertia) return              // ③ 当前正在加载/卸载 → 只记录，不动手
// ④ 决定动作
INACTIVE → 有值  :  _reload()   开始执行插件体
有值 → 其它      :  _unload()   卸载
```

- 分支 ① 是性能来源：广播一百次，只要字符串没变，就只做一次比较。
- 分支 ② 是失败隔离：某个插件抛错后不会被后续广播重新拉起来，必须显式 `update()`。
- 分支 ③ 是正确性来源：同一个插件上永远只有一个加载或卸载在跑，新意图只改字符串，等当前操作结束后自己再判断一次。

### 4.4 广播：由「跑完」触发

提供者真正对别人可见，不是在调用 `provide` 的那一刻，而是在它**执行完、状态切成 ACTIVE** 的那一刻（`fiber.ts:594-600`）：

```ts
if (oldState !== FiberState.ACTIVE && this.state !== FiberState.ACTIVE) return
for (const key of Reflect.ownKeys(this.ctx.reflect.store)) {
  const impl = this.ctx.reflect.store[key]
  if (impl.ctx.fiber !== this) continue
  this.ctx.isolate(impl.name, impl.isolate).reflect.notify([impl.name])
}
```

然后 `notify()`（`reflect.ts:335-348`）扫描所有插件：

```ts
for (const fiber of runtime.fibers) {
  if (!(name in fiber.inject)) continue
  fiber._checkImpl(name)
  fiber._refresh()
}
```

只处理 `inject` 里写了这个名字的插件，其余跳过。

这条「跑完才广播」的规则是全部正确性的基础：**消费者醒来时，提供者的初始化一定已经完成。**

## 5. 为什么必须「自己重算」

因为依赖是单向可见的。

`tools` 知道自己的名字是 `tools`，但它**不知道谁在用**。框架也没有维护「谁用了谁」的反向索引，只有提供者这一侧的 `dependents` 集合（在消费者加载时登记）。

所以提供者能做的只有广播，不能点名通知。消费者每次收到广播，只能把自己的整张清单重新数一遍。

## 6. 依赖被替换时的行为

这是 epoch 带 `uid` 的原因。

`fs-local` 被 `fs-sandbox` 顶替：

- 名字仍然叫 `fs`；
- 但新实现的 `uid` 不同；
- 消费者的字符串从 `"0:7"` 变成 `"0:23"`；
- 于是消费者**卸载后重新执行**，而不是继续用旧的对象。

如果没有 `uid`，只靠名字比对，这种替换会被完全忽略。

## 7. 卸载

`_unload()`（`fiber.ts:712-746`）分三步：

1. **等消费者走完**。只要自己提供的服务还有 `dependents`，就挂起等待。
2. **撤销自己的副作用**。`_disposables` 是插入有序的列表，清空时按**逆序**执行（`utils.ts:27-31`），即后注册的先撤销。
3. **给提供者减一**。把自己从每个依赖的 `dependents` 里移除；某个依赖减到零时唤醒它，让它继续卸载。

所以关闭顺序不是排出来的，是**引用计数收敛**出来的：使用者先走，提供者后走，谁都不需要提前知道别人。

## 8. 同层并发

两个插件若不在对方 `inject` 里，它们会在同一批广播后一起被唤醒。

`_reload()` 的第一行是 `await Promise.resolve()`，所以这一批插件都先让出，然后各自开始执行；每个执行到自己第一个 `await` 时再让位。

实际表现：

- 同一个插件内部，代码是顺序 `await` 的；
- 不同插件之间，是交错执行的；
- 谁先开始，取决于扫描顺序（`runtime.fibers` 的插入顺序，实际就是 `cordis.yml` 的行序），但这是实现细节，**不构成契约**。

真正的规则只有一条：**顺序由依赖强加，没有依赖就没有顺序。**

## 9. 没有调度器

需要明确它**没有**哪些东西：

| 拓扑排序里有的 | Cordis 里有吗 |
|---|---|
| 建好的依赖图 | 没有。依赖散落在各插件的 `inject` 里 |
| 入度计数 | 没有。用字符串比对代替 |
| 就绪队列 | 没有。没有待执行列表 |
| 调度循环 | 没有。推进由广播递归驱动 |
| 一次性排好的顺序 | 没有。每次变化都重算局部 |

代价是：

- 「谁先跑」不可预测，调试时不能当依据；
- 无法表达「我不需要你的服务，但我想让你先跑」——想排前面就得真的依赖它；
- 错误默认只进 `logger`，调用方不 `await fiber.await()` 就看不到。

收益是：

- 换实现、删插件、改配置，都只触发局部重算；
- 依赖消失会反向卸载，不需要重排整张图；
- 失败被隔离在单个插件内。

## 10. 与 Gradus 的对照

| Cordis | Gradus 现状 |
|---|---|
| `inject = ['fs', 'llm']` | `Agent(env=..., policy=..., mcp=...)` 构造函数参数 |
| `super(ctx, 'tools')` | `BACKENDS` 字典按名字选实现 |
| 广播 + 重算 epoch | 无，顺序写在 `Session.connect` 的函数体里 |
| `fiber.dispose()` | `await self._env.aclose()` |
| `dependents` 引用计数 | 关闭顺序手写在 `session.py` 的 `connect`/`close` 里 |

Gradus 的装配是四步：

```
_build_environment -> Policy -> connect_mcp -> Agent
```

每一步的输入确实是上一步的输出，所以顺序是真实存在的，只是被写死在四行代码里。

信息量完全相同，代码更短。因为：

- 只有四个组件；
- 没有任何一个存在「第二个实现 + 运行中替换」的诉求；
- 没有多入口、多权限模式带来的分支爆炸。

所以现阶段不引入类似框架。值得借鉴的只有三条与框架无关的纪律：

1. 能力通过**名字**暴露，不通过模块直接 import；
2. 依赖**显式声明**，不靠调用顺序暗示；
3. 副作用**可撤销**，注册时就要想清楚怎么撤。

## 11. 什么时候值得用

值得：

- 插件数量到几十个，人工维护调用顺序开始出错；
- 有多个入口或多个权限模式，同一套能力在不同场景下取舍不同；
- 需要运行中开关、替换某一块（HMR、插件市场、多租户）。

不值得：

- 组件少于十个，且顺序天然清晰；
- 没有替换需求，所有实现都是唯一的；
- 团队对 DI 的复杂度没有共识——它的调试成本远高于手写调用链。

## 12. 源码阅读顺序

不要从 `fiber.ts` 开头读，前 300 行是构造函数和 effect 细节，噪音很大。

建议顺序：

1. `service.ts`（115 行）：看 `super(ctx, name)` 如何注册名字。
2. `context.ts`（146 行）：看 `ctx` 如何按名字查服务。
3. `fiber.ts` 只读四段：
   - `_refresh()` 617-628
   - `_setEpoch()` 630-646
   - `_reload()` 681-710
   - `_unload()` 712-746
4. `reflect.ts` 只读三处：
   - `provide` 里的 `uid` 分配 302
   - `notify` 的遍历 335-348
   - `provide` 成功后是否立刻广播 308-313

读完这四段就能完整还原启动、替换、卸载三条路径，其余是 effect 回收、配置校验、诊断信息。

## 13. 常见误解

| 误解 | 实际 |
|---|---|
| 有一个调度器在排顺序 | 没有。每个插件自己盯依赖 |
| 就绪的插件排队一个个跑 | 同一批并发交错，只有同一插件内部是顺序的 |
| `inject` 写的是某个类 | 写的是服务名，实现可以换 |
| 插件就是实体 | 插件是装配单元；实体是类，由服务管理 |
| 依赖齐了会收到通知 | 只收到「有人上线了」，要自己重算整张清单 |
| 改了配置要重启整个进程 | 只重启 `_revision` 变化的那个插件 |

## 14. 一句话总结

**没有调度，只有「跑完广播，听到就自己重算」；没有顺序，只有「我用了你的输出」产生的先后。**
