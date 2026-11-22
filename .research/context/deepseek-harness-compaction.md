# DSH 上下文压缩与会话存储（源码级笔记）

- 仓库：`H:\Desktop\LLM learning\AgentLearn\deepseek-harness`（TypeScript monorepo）
- 证据：`packages/**/src/**/*.ts` 为主；`cordis.patch.yml`（仓内组合清单）仅用于判定「默认安装了什么」，每次引用都标 `yml` 提醒它不是 TS。
- 路径均相对仓库根，写作 `路径:行号`；同一小节内后续只写 `:行号` 者，指该小节最近一次给出的完整路径；以 `src/...` 起头者相对该包目录（紧随其前的完整包路径）。
- 引用块是 TS 文档注释/代码原文，紧随中文解释；「未找到实现」= 已在该范围内检索而无对应代码。
- 按要求未执行代码/测试，未改任何源码（仅新建本文件）。

## 包与职责

| 包 | 位置 | 职责（源码定位） |
|---|---|---|
| `@deepseek-ai/dsh-compaction` | `packages/compaction/compaction/` | 压缩**能力缝**：`ctx.compaction` 抽象服务、`CompactionResult`、`compaction/*` 事件声明、checkpoint 标记 |
| `@deepseek-ai/dsh-compaction-basic` | `packages/compaction/compaction-basic/` | 默认后端：阈值策略 + 触发监听 + LLM 摘要 + 提交事务 |
| `@deepseek-ai/dsh-compaction-tool-result-pruner` | `packages/compaction/compaction-tool-result-pruner/` | 与模型无关的工具结果头/尾修剪（`ctx.toolResultPruner`） |
| `@deepseek-ai/dsh-command-compact` | `packages/compaction/command-compact/` | 人类 `/compact` 命令 |
| `@deepseek-ai/dsh-compaction-image-offload` | `packages/compaction/compaction-image-offload/` | 相邻机制：超图片预算时把最老图片替换为占位文本并重试 |
| `@deepseek-ai/dsh-token-meter` | `packages/llm/token-meter/` | 唯一 token 计量服务 `ctx.tokenMeter` |
| `@deepseek-ai/dsh-session` | `packages/core/session/` | 事件溯源日志 `Session`/`SessionStore` + surface 折叠 |
| `@deepseek-ai/dsh-system-prompt` | `packages/core/system-prompt/` | 有序 system sections / 动态 contexts / 工具 schema 组装注册表 |
| `@deepseek-ai/dsh-skill` + `dsh-tool-skill` | `packages/skill/skill/`、`packages/skill/tool-skill/` | 技能目录注册表 + 目录注入与 `skill` 装载工具 |
| `@deepseek-ai/dsh-spill-policy` | `packages/spill/spill-policy/` | 工具结果按 token 预算外溢到文件（相邻的「输出截断」） |
| `@deepseek-ai/dsh-session-projection-cache` | `packages/session/session-projection-cache/` | 「投影检查点缓存」`ctx.sessionProjectionCache`；**不是** prompt cache |

- 服务定义头注释（`packages/compaction/compaction/src/index.ts:1-8`）：
  > ``Compaction Service Definition (`ctx.compaction`): providers decide when to compact and replace a history range with one summary node by subclassing {@link CompactionEngine}.``
  → 压缩的「何时压」由 provider（后端插件）决定，接口本身不做压缩。
- 关于 cache：全仓 **未找到** 名为 prompt-cache 的包；cache 概念只出现在 (a) `session-projection-cache` 的折叠捷径、(b) 摘要调用复用前缀 KV cache 的注释（`packages/compaction/compaction-basic/src/summarizer.ts:25-31`）、(c) 适配器 `replayState`。**未找到实现**：按 token 计价的 prompt cache 管理器。

## token 计量

- 计量者是单例服务 `TokenMeter`，服务名 `tokenMeter`，硬依赖投影表（`packages/llm/token-meter/src/index.ts:87-101`）：
  > `/** Replay owner for one service-wide estimator and isolated per-session folds. */`
  `static inject = ['sessionProjections']`（`:92`）。配置 schema 为空且拒收任何键（`:90`、`:74-78`）——**估算器无参数可调**。
- 算法是**固定启发式**，不是 tiktoken（全仓 `grep tiktoken|tokenizer` 在 llm/token-meter 下无命中）：
  > `/** Fixed text-density estimate used until exact tokenization is needed. */ const CHARS_PER_TOKEN = 4`（`src/estimate.ts:12-13`）
  > `/** Per-block structural overhead for JSON framing and type tags. */ const BLOCK_OVERHEAD = 4`（`:15-16`）
  > `/** Role-field framing overhead added to every priced message. */ export const ROLE_OVERHEAD = 4`（`:18-19`）
  - text/reasoning：`ceil(text.length / 4) + 4`；tool-call：`ceil(name/4) + ceil(arguments/4) + 4`；其余块按 JSON 结构价（`:42-63`）。
  - system prompt：`ceil(chars/4) + 4`，无 per-block overhead（`:73-80`）；工具 schema：`ceil(JSON.length/4) + 4`（`:99-102`）。
- 三类基线（`packages/llm/token-meter/src/types.ts:16-19`）：
  > `export type TokenMeasurementBaseline = | { kind: 'none'; tokens: 0 } | { kind: 'estimated'; tokens: number } | { kind: 'usage'; tokens: number; usage: Readonly<TokenUsage> }`
- 计量结果结构（`types.ts:22-54`）：`logRevision`、`baseline`、`surfaceDeltaTokens`、`totalTokens`、`surfaceTokens`、`nodes[]`；每个节点同时给**路由价** `tokens` 与**固定启发式价** `heuristicTokens`（`:38-54`）。
- 计算入口 `measure(session, requestHeader?)`（`src/index.ts:151-196`），文档（`:131-150`）：
  > `Provider usage is reused only when the latest successful call's canonical request envelope matches {@link requestHeader} and its total is no lower than that call's full route-priced anchor; otherwise the complete envelope and surface are repriced.`
  - 复用条件 `optionalHeaderEquals(...)` 且 `usageTokens(usage) >= estimatedAnchorTokens`（`:163-176`）；`usageTokens` = `input + cacheRead + cacheWrite + output`（`:57-62`）。
  - `totalTokens = Math.max(0, baseline.tokens + surfaceDeltaTokens)`（`:192`）；每次调用克隆节点，复杂度 O(surface)（`:144-145`）。
- provider usage 的锚点由投影记录（`src/measurement-projection.ts:23-32`、`:61-77`）：只在 `assistant/message` 且已有 `request/header` 时记录 `usage`，否则用启发式价。
- 路由差异：图片按适配器声明的视觉 token 计价并替换结构价（`src/route-pricing.ts:47-52`、`:63-70`），文件按 `fileRequestText` 句柄文本计（`:57-62`）；`ctx.llm.imageRequestPricing(...)` 缺失时退回启发式（`src/index.ts:198-203`）。
- 传递路径：压缩策略读 `ctx.tokenMeter.measure(session, header)`（`compaction-basic/src/index.ts:325`、`:348`、`:363`、`:420`）；修剪器用 `estimateMessage` 记 shadow price（`compaction-tool-result-pruner/src/index.ts:163`）；UI/遥测读投影 `contextPressure(session)`（`token-meter/src/index.ts:122-128`）。

## 触发与溢出处理

- 自动触发只由 `compaction-basic` 注册，且**仅当 `auto`**（`packages/compaction/compaction-basic/src/index.ts:136-140`）：
  > Register between-step pressure, model-switch pressure, and request-overflow recovery. Pre-step and overflow listeners dispatch through compactIfNeeded; model switches price the proposed header directly. All summaries use summarize.（`:142-146`）
  三条入口：
  1. `agent/pre-step`（`:157-175`）→ `compactIfNeeded(agent, 'pressure', signal)`；任何异常只 `logger.warn` 并 `next()`，**不打断 turn**（`:165-174`）。
  2. `agent/request`（`:183-203`，`{ prepend: true }`）→ 路由 provider/model 变化时用候选 header 试算；失败抛 `MODEL_SWITCH_COMPACTION_FAILED`（`:195-200`）。
  3. `agent/request-error`（`:213-257`）→ **仅当** `failure.code === CONTEXT_WINDOW_EXCEEDED_CODE`（`:217`）。
- **两种判定并存：主动阈值比较 + provider 溢出错误**。
  - 阈值公式（`compaction-basic/src/config.ts:153-217`）：
    > `const messageBudgetTokens = contextWindow - reservedCompletionTokens`（`:172`）
    > `const pressureBudgetTokens = messageBudgetTokens - policy.headroomTokens`（`:181`）
    > `const thresholdTokens = Math.floor(Math.min(contextWindow * policy.thresholdRatio, pressureBudgetTokens,))`（`:191-194`）
    > `const retainTokens = policy.retainTokens === undefined ? Math.floor(messageBudgetTokens * policy.retainRatio) : policy.retainTokens`（`:195-197`）
  - 默认常量：`DEFAULT_THRESHOLD_RATIO = 0.8`（`:19-20`）、`DEFAULT_RETAIN_RATIO = 0.16`（`:22-23`）、`headroomTokens ?? 65_536`（`:75`）、`maxTokens = config.maxTokens ?? headroomTokens`（`:76`，即默认 65536）、`compactionRetries ?? 1`（`:105`）、`maxOverflowRetries ?? 1`（`:106`）。
  - `reservedCompletionTokens = header.config.maxTokens ?? info.defaultMaxTokens ?? 0`（`compaction-basic/src/index.ts:65-67`）。
  - 比较点：`if (measurement.totalTokens < spec.thresholdTokens) return null`（`index.ts:342`、`:350`）。
  - 缺少适配器 `contextWindow` 直接报配置错（`:330-336`）：
    > `compaction-basic: no context capacity for ${targetKey}; configure contextWindow on that adapter model`
- 溢出错误识别走「结构化 code + 供应商文案分类器」：
  > `/** Canonical provider-neutral code for a model request rejected because its context window was exceeded. */ export const CONTEXT_WINDOW_EXCEEDED_CODE = 'CONTEXT_WINDOW_EXCEEDED'`（`packages/llm/llm/src/error.ts:24-25`）
  > `export function isContextWindowExceededError(detail: string): boolean`（`:83`），由 5 条正则组成：`STRUCTURED_CONTEXT_OVERFLOW`（`:54-58`，匹配 `context_length_exceeded` / `context window limit exceeded` 等）、`maximum/max context length|window`（`:85`）、`TOO_LARGE_FOR_CONTEXT`（`:61-66`）、`too long for this model`（`:87`）、`EXCEEDS_MODEL_CONTEXT`（`:69-74`）。
  - 适配器落 code：`packages/llm/llm-deepseek/src/transport.ts:32`（`else if (isContextWindowExceededError(detail)) code = 'CONTEXT_WINDOW_EXCEEDED'`）；`packages/llm/llm-pi-ai/src/stream.ts:80-90`（pi-ai `isContextOverflow` 或 harness 分类器任一命中即 `CONTEXT_WINDOW_EXCEEDED`）。
- 溢出恢复流程（`compaction-basic/src/index.ts:213-257`）：`overflowRetries` 每 agent 计数（`:133`，`WeakMap`），超过 `maxOverflowRetries` 就 `next()` 放弃（`:222-223`）；压缩后必须看到 `surface.replaceGeneration` 增长才返回 `{ kind: 'retry' }`（`:225`、`:252-256`）；若摘要阶段失败但修剪已落盘，也允许重试（`:232-242`）。`agent/status` 变 idle 或产生新 `assistant/message` 时清零计数（`:177-179`、`:207-211`）。
- 重试语义在循环侧：`agent/request-error` 瀑布返回 `retry` 后 `continue` 重发同一请求（`packages/core/agent-loop/src/agent.ts:548-563`）。
- **溢出代理 / fallback provider 路由：未找到实现**。全仓 `grep 'fallbackProvider|overflowProxy|overflowFallback|fallbackModel'` 在 `packages/**/*.ts` 下 0 命中；溢出后唯一的「换路由」是 `modelPolicies`（按 provider/model 给不同阈值，`config.ts:118-139`），那是策略表不是路由回退。

## 压缩算法

- 触发后的步骤顺序（与模型无关的修剪**先于**摘要）：
  - 主动压力路径 `compactPressure`（`compaction-basic/src/index.ts:316-371`）：`measure` → `resolveModelInfo` → 断言无进行中压缩 → 解析 spec → **低于阈值直接返回**（`:342`）→ `prune.pruneSession(session)` 并**重新计量**（`:344-349`）→ 仍低于则返回（`:350`）→ 循环 `selectCompactableRange(..., retainTokens)`（`:354`）→ `compactRegion`（`:362`）→ 重新计量（`:363`）→ 低于阈值返回，否则再试一轮；耗尽 `compactionRetries` 后抛错（`:367-370`）。
    > `// Once pressure qualifies, land the model-free pass before choosing a summary range, then remeasure through the singleton replay fold.`（`:344-345`）
  - 溢出路径 `compactIfNeeded(..., 'context-overflow')`（`:302-308`）：`toolResultPruner.pruneSession`（`:303`）→ `measure`（`:304`）→ `selectCompactableRange(session, measurement, 0)`（`:305`，**保留尾部预算为 0**）→ `compactRegion`（`:307`）。
    > `overflow bypasses the normal threshold and retained-tail policy so it can force one useful balanced reduction.`（`:283-287`）
- 「与模型无关的修剪」修剪的是**工具输出**（不是早期消息）：`ToolResultPruner` 遍历当前 surface 的 `tool/result` 节点（`compaction-tool-result-pruner/src/index.ts:136-143`），仅当文本 code point 数 > `thresholdChars` 时去掉中段、保留头 4096 / 尾 1024 字符（`src/config.ts:10-14`）：
  > `export const PRUNE_MARKER = '\n\n[... tool result middle pruned ...]\n\n'`（`config.ts:7`）
  - 切片按 Unicode code point（`index.ts:76-82`），逐块重建并插入一次 marker（`:83-122`）；随后**先追加** `compaction/prune`（带 `shadowedTokenCount = estimateMessage(original)`），**再追加** `tool/result` 替换（`{ op:'replace', startSeq:seq, endSeq:seq }`，`sourceEventSeqs:[seq]`）（`:157-171`）。
- 范围选择（`compaction-basic/src/region.ts:117-155`）：从尾部累加 `pricedNodes[i].tokens` 直到 ≥ `retainTokens` 得 `keepFromIdx`（`:133-140`）；`system/message` 位于 surface 0 时永不进范围（`:131`）；再向前回退直到 `toolPairingBalancedBefore` 成立（`:143-147`）；返回 `[surface[firstIdx], surface[keepFromIdx-1]]`（`:151-154`）。无安全范围返回 `null`（`:141`、`:148`）。
- 边界校验（`:337-358`）：start/end 必须存在且顺序正确，start 需 `toolPairingBalancedBefore`、end 需 `toolPairingBalancedAfter`（`:349-355`）——即**不得切开 assistant tool-call 与 tool/result 配对，也不得在半开 step 处收尾**。
- 摘要 prompt（`compaction-basic/src/summarizer.ts:32-67`）是**追加在重放前缀之后的最后一条 user 消息**，不是独立 summarizer system prompt：
  > `The summarization directive, delivered as the FINAL user message after the replayed conversation rather than as a distinct summarizer system prompt. Keeping the conversation's own system prompt, tools, and message prefix in front of it makes the auxiliary call a genuine prefix of the last routed request, so the provider's KV cache is reused instead of invalidated.`（`:25-31`）
  正文首句与结构（`:33-59`）：`You are now acting as a compaction engine for this AI coding assistant. Condense the conversation ABOVE into a structured checkpoint...`，固定 8 个 Markdown 小节：Primary Request and Intent / Key Technical Concepts / Files and Code / Errors and Fixes / Pending Jobs / Current Work / Next Step / Critical Context，空节写 `(none)`。
  规则（`:61-66`）：保留精确路径/命令/错误串/标识符/数值/签名；忠实记录用户纠正；`- Do NOT mention this summarization request or that the context was compacted.`；如果已有 `<compacted-summary>` 块则视为 PRIOR checkpoint，合并而非照搬（`:66`）。
- 摘要后端选择（`summarizer.ts:127-142`）：`config.summarizationProvider/Model` → 最新已路由 `requestHeader().config` → `agent.options`，全缺则抛错。调用方式（`:152-163`）：`ctx.llm.stream({ provider, model, messages, toolHistory, tools, maxTokens, sessionId, purpose: 'compaction', signal })`。
- 摘要失败是**失败关闭**：`error`/`aborted` 转 `LlmError`，`max-tokens` 报 `summarization truncated at the token cap (incomplete checkpoint)`（`:197-211`）；含图片输出直接 `UNSUPPORTED_CONTENT`（`:214-221`）；正文全空报 `summarization produced no text summary content`（`:169-171`）。
- 摘要后必须**确实变小**才提交（`region.ts:410-422`）：
  > `summary is not smaller than the shadowed content (${framedSummaryTokenCount} estimated framed tokens >= ${shadowedRouteTokenCount})`
- 摘要失败可被插件修复并重试：`compaction/summary-error` 瀑布（`compaction/src/index.ts:93-107`）→ `region.ts:397-409` 重新 `prepareCompaction` 后再调摘要。`compaction-image-offload` 就是该钩子的使用者（`compaction-image-offload/src/index.ts:33-40`）。
- 并发/加锁：`compaction/start` 在第一次 await 之前同步追加（`region.ts:204-210`），它本身就是锁；`inspectCompactionEntryState` 倒扫日志找未配对的 start 与 open turn（`region.ts:566-599`），重复进入抛 `busy`（`:308-320`）。

## 产物与历史形态

- 摘要**不是插入新消息、也不是替换全部历史**，而是用一条 `user/message` 在 surface 上做**位置替换**（splice）。四个事件的顺序（`compaction-basic/src/region.ts:471-522`）：
  1. `compaction/start`（log-only，`turn` 为当前 turn 或 `null` 表示跨 turn 手动事务）——`compaction/src/types.ts:24`；
  2. `compaction/summary`（log-only，携带 `summary`、`rawOutput`、`shadowedRange`、`shadowedSeqs`、`shadowedTokenCount`、`provider`、`model`、`maxTokens?`、`usage?`）——`types.ts:34-67`，写入处 `region.ts:491-505`；
  3. `user/message`（真正的 surface 变更）：`surfaceOp: { op:'replace', startSeq:start, endSeq:end }`、`sourceEventSeqs: [startEvent.seq, summaryEvent.seq, ...shadowedSeqs]`——`region.ts:506-509`；
  4. `compaction/end`（log-only，成功 `region.ts:237`、失败带 `error` `:245`）——`types.ts:72`。
- 类型注释把这条契约写死（`types.ts:26-33`）：
  > ``Completed summary, its inputs, and its model call facts — log-only, no surfaceOp. The summary content is in `data.summary`; the actual surface replacement is performed by the immediately following `user/message` event that shadows the compacted range. That adjacency is contractual...``
- 替换消息的**来源标记**可跨后端识别（`compaction/src/checkpoint.ts:19-25`、`:33-42`）：
  > `const COMPACT_CHECKPOINT_MARKER = Object.freeze({ kind: 'compact-checkpoint' } as const)` → `source.kind === 'compact-checkpoint'`（`:49-51`）
  并通过模块合并进入 LLM 的 `MessageSourceMap`（`compaction/src/index.ts:25-29`）。
- 摘要文本被框架包裹（`summarizer.ts:22-23`、`:70-71`、`:188-194`）：`CHECKPOINT_PREAMBLE` + `<compacted-summary>` + 模型输出 + `</compacted-summary>`；preamble 要求「Treat the captured context as established background... without acknowledging this checkpoint.」
- 模型真正看到的形态：`deriveEventMessage` 对 `user/message` 直接返回 `event.data`（`packages/core/session/src/surface.ts:129-135`），`deriveMessages()` 沿 surface 节点投影（`packages/core/session/src/index.ts:981-1020`），请求就用它（`packages/core/agent-loop/src/agent.ts:728-742`）——因此 N 个旧节点在模型侧等价于 1 条 user 消息。
- 原记录**保留**：替换只在 surface 遮蔽，日志仍 append-only；`isAppendSurfaceEvent` 注释明确区分「模型可见面」与「人类 transcript」（`surface.ts:78-93`）。
- 两个 token 计数语义不同：`compaction/summary.shadowedTokenCount` 用固定启发式（`region.ts:376-381`），而「是否更小」的判据用路由价（`:381`、`:417-422`）。
- 手动结果回报：`Compacted ${n} history items (~${tokens} tokens)`（`command-compact/src/index.ts:68-73`）。

## 会话存储与重建

- 权威数据结构是 append-only 事件日志（`packages/core/session/src/types.ts:281-287`）：
  > `The merge-extensible, append-only source of truth for an agent interaction. Message history is derived from this log. Every event is lossless JSON and sequence numbers stay contiguous.`
- 事件类型：`SessionEventMap` 声明（`types.ts:287-436`）含 `turn/start|end`、`step/start|end`、`user/message`、`developer/message`、`system/message`、`assistant/message`、`assistant/attempt`、`tool/call`、`tool/result`、`request/header`、`request/context`、`session/end-seed`；插件合并出 `compaction/start|summary|end|prune`、`image/offload` 等。运行期白名单 `KNOWN_SESSION_EVENT_TYPES`（`src/known-event-types.ts:22-86`）中压缩相关在 `:32-35`。
- 事件信封（`types.ts:501-524`）：`type` / `seq` / `time` / `data`，外加仅 5 类 surface 事件才允许的 `surfaceOp` 与 `sourceEventSeqs`（`:447-462`、`:470-472`）。未知类型必须带 `ignorable: true`，否则读取端拒绝重建（`:509-519`）。
- 写入：`Session.append()` 校验 JSON 可序列化 → 深冻结 → `seq = log.length`（`packages/core/session/src/index.ts:806-837`）；`commit` 先 `surfaceManager.validateNext(event)` 再入日志（`:892-893`）。
- 重建：`foldSurface(events)` 纯函数重放（`surface.ts:594-609`）；append → `nodes.push`，replace → `nodes.splice(startIdx, endIdx-startIdx+1, seq)` 并 `replaceGeneration++ / contentGeneration++`（`:567-577`）；增量版 `SurfaceManager` 实现同一语义（`:611-696`）。
- **压缩事件如何参与重建**：`compaction/*` 是 log-only，禁止携带 `surfaceOp/sourceEventSeqs`（`surface.ts:302-327` 抛错），因此它们不产生消息；它们的作用是 (a) 锁与事务括号、(b) shadow price 语义（`types.ts:73-89` 的协议注释）、(c) 子序列校验用的 owner 关系。
- 替换的合法性强校验：`sourceEventSeqs` 必须覆盖被遮蔽的每个节点（`surface.ts:361-364`）、surface 0 的 system prompt 只能被 `system/message` 原位重写（`:488-507`）、`tool/result` 替换只许改 content（`:456-485`）。
- 持久化格式：JSONL，一代一个文件 `vN`（`packages/session/session-persistence-jsonl/src/format.ts:33-37`），物理编码 `zstd | none`（`:30-31`）；首条记录是 `type:'session'` 头（`HeaderLine` 与 `toHeaderLine`，`:79-93`、`:112-119`）。
- 已发布 v4 关系校验把压缩括号纳入不变式（`packages/session/session-format-v3-to-v4/src/relationships.ts:9-14`、`:226-263`）：
  > `compaction/start overlaps an open compaction`（`:230`）、`compaction/summary repeats`（`:239`）、`successful compaction/end requires one summary`（`:243`）、`${event.type} crosses an open compaction`（`:271`）、`shadowedSeqs do not name an exact current surface span`（`:261`）、`compaction cannot shadow the protected system head`（`:263`）。
- 恢复/分叉：`Session.fromRestore`（`core/session/src/index.ts:611-627`）要求种子事件 `seq` 从 0 连续（`:641-657`）；`ctx.sessions.flush(session)` 是唯一的持久化检查点入口（`:1330-1360`），手动压缩用它确认落盘（`compaction-basic/src/index.ts:434-437`）。
- 同一日志派生多条视图：`requestHeader()`（`index.ts:914-937`）、`toolHistory()`（`:958-972`）、`deriveMessages()`（`:974-1020`）都是对同一日志的折叠/增量缓存。

## 插件接口

- 抽象服务（`packages/compaction/compaction/src/index.ts:110-122`）：
  > ``Abstract compaction service. Implementations own trigger policy, retention, and summarization, and may consume a separate measurement service... Load one implementation per context as `ctx.compaction`.``（`:110-117`）
  `export abstract class CompactionEngine extends Service { constructor(ctx: Context) { super(ctx, 'compaction') } }`（`:119-122`）。
- 三个必须实现的方法（签名原文）：
  - `abstract compactIfNeeded(agent: CompactionAgentContext, trigger: CompactionTrigger, signal: AbortSignal): Promise<CompactionResult | null>`（`:136-140`）；`trigger` 为 `'pressure' | 'context-overflow'`（`:31-32`）。
  - `abstract compactNow(agent: ManualCompactAgentContext, signal: AbortSignal, sourceCommandId?: CommandId): Promise<CompactionResult | null>`（`:162-166`）。
  - `abstract compactRegion(start: SessionSeq, end: SessionSeq, agent: CompactionAgentContext, signal?: AbortSignal): Promise<CompactionResult>`（`:187-192`）。
- 上下文契约：`CompactionAgentContext = { session: Session; options: { provider?, model? } }`（`:66-70`）；手动版另需 `runMaintenance<T>(task)`（`:72-86`），即「仅在 idle 时串行执行并延后唤醒输入」。
- 第三方后端必须做的事（注释原文汇总）：用 `compactCheckpointSource(compactionId, sourceCommandId?)` 构造替换 user 消息（`:110-116`、`:168-178`）；用 `toolPairingBalancedBefore/After` 校验边界（`:176-179`）；摘要若走模型必须转发 `signal`（`:132-133`、`:183`）；错误分类用 `ManualCompactionError` 的 `busy|cancelled|changed|summary|commit|persistence`（`:35-64`）。
- 返回值形状 `CompactionResult`（`types.ts:93-119`）：`compactionId`、`sourceCommandId?`、`startSeq`、`summarySeq`、`endSeq`、`summary`、`shadowedRange`、`shadowedSeqs`、`shadowedTokenCount`。注意位置语义（`:107-114`）：
  > ``A surface-POSITION span, not a numeric seq interval — after a prior replace lands a fresh high-seq summary node at an older range's position, `start` can be GREATER than `end`.``
- 注册与覆盖：Cordis 服务，一个 context 只允许一个实现（`:116-117`）；默认组合在 base 层注册一次（`packages/bundle/base/cordis.patch.yml:355-356`，yml），web-app 则在 host 层禁用、改由每个 preset 分组隔离挂载（`packages/bundle/web-app/cordis.patch.yml:550-565`，`packages/bundle/web-app/presets/standard.patch.yml:69-85` —— `isolate: { compaction: true, toolResultPruner: true }`）。
- 可选协作服务用 `ctx.get('toolResultPruner')`（`compaction-basic/src/index.ts:17-18`、`:303`、`:326`），缺失即跳过修剪——**修剪器是可选兄弟服务，不是编译期依赖**。
- 扩展点还包括：`compaction/summary-error` 瀑布（`:93-107`）允许插件「durably 修改被选中的输入后重试」。

## 默认是否自动压缩（定论）

- `compaction-basic` 的 `auto` 默认 `true`（`compaction-basic/src/config.ts:108`）：
  > ``auto: config.auto ?? true``（`config.ts:108`）；`types.ts:49-50` 的字段注释：``/** Enable automatic step-boundary pressure and overflow-recovery listeners. Defaults to `true`. */ auto?: boolean``
- 构造函数按此注册监听（`compaction-basic/src/index.ts:136-140`）：`if (this.config.auto) this._registerAutomaticCompaction()`，而全部自动触发代码就在 `:147-258`。
- base 组合（默认 CLI `dsh` 的基座）**未加配置、未禁用**地挂载了它（`packages/bundle/base/cordis.patch.yml:352-361`，yml）：
  ```yaml
  - id: token-meter
    name: '@deepseek-ai/dsh-token-meter'
  - id: compaction-basic
    name: '@deepseek-ai/dsh-compaction-basic'
  ```
  headless 明确「directly over dsh-base」（`packages/bundle/headless/cordis.patch.yml:1`）；`sdk-minimal` 同样无配置挂载（`packages/bundle/sdk-minimal/cordis.patch.yml:64-65`）。
- Web/桌面形态：host 层把 `compaction-basic`、`command-compact`、`tool-result-pruner` 置 `disabled: true`（`packages/bundle/web-app/cordis.patch.yml:557-564`），改由每个 agent preset 分组挂载（`presets/standard.patch.yml:69-85`、`presets/ptc.patch.yml:69-81`、`presets/minimal.patch.yml:24-31`）——**默认仍在 preset 内启用**，只是所有权从 host 移到 preset。
- 源码定论：**默认安装的包里存在自动触发压缩的代码路径，且默认开启；不是「必须显式启用/安装」。** 唯一「什么都不压缩」的情形是组合里只挂 `dsh-compaction` 而没有 backend —— 该包 `src/index.ts` 全 195 行只有抽象声明与 3 个 abstract 方法，无任何 `ctx.on(...)` 触发代码（`compaction/src/index.ts:119-193`），文档注释也说明 provider 才决定何时压缩（`:1-8`）。
- 因此社区说法「内置没有自动压缩」**与源码不符**；白皮书「压缩由插件提供」在架构上成立（实现确实在 backend 插件），但该插件是默认装上且默认开自动触发的。

## 修剪与工具输出

| 机制 | 位置 | 关键数值 | 是否改写日志 |
|---|---|---|---|
| 上下文修剪（trim） | **未找到实现** | — | — |
| 工具结果修剪 | `packages/compaction/compaction-tool-result-pruner/src/config.ts:10-14` | `thresholdChars 8192` / `headChars 4096` / `tailChars 1024` | 追加 `compaction/prune` + `tool/result` 替换（不删旧事件） |
| 工具输出外溢 | `packages/spill/spill-policy/src/index.ts:26-35`、`:92-127` | base 组合 `maxInlineTokens: 12500`（`packages/bundle/base/cordis.patch.yml:422-425`，yml）；省略则该插件不生效（`:50-51`） | 在工具完成时改写模型可见结果（含图片就外溢为路径文本） |
| 技能目录注入 | `packages/skill/tool-skill/src/index.ts:217-255`、`:258-287` | 描述截断 `DEFAULT_CATALOG_DESCRIPTION_MAX_LENGTH = 500`（`:28`、`:411-413`） | 以 user 角色 `<system-reminder>` 追加 |

- 「trim」：全仓无 `trimHistory`/`historyTrim`/`contextTrim` 类实现，`trim` 命中都是字符串处理。DSH 的「模型无关修剪」就是 `tool-result-pruner`。**未找到独立的历史 trim 策略实现**。
- 修剪器只处理**当前 surface 上**的 `tool/result`（`index.ts:136-143`），只改 text 块，富块保序（`:93-111`），配置约束 `headChars + marker + tailChars ≤ thresholdChars`（`config.ts:55-63`）。
- 外溢（spill）在工具结算时按 token 预算裁剪：估算价 > `maxInlineTokens` 时把全文存入 spill store，内联保留 `head + '\n\n[...]\n\n' + tail + 通知`（`index.ts:42`、`:99-127`）；无法恢复的失败一律保留原内联内容并 warn（`:128-130`）。
- 技能目录注入：不是 system prompt section，而是 `agent/pre-step` 里以 user 消息追加（`tool-skill/src/index.ts:217-255`），内容形如 `<system-reminder> ... <available_skills>`，每条目录行是「反引号包住的技能名 + 冒号 + 描述」（`:339-341`）；catalog 变化时发「替换整份目录」的更新消息（`:289-322`）；digest 未变则不重复注入（`:235-240`）。
- system prompt 侧只负责有序 sections / 动态 contexts 的组装，`PromptContext` 的定位（`packages/core/system-prompt/src/index.ts:78-90`）：
  > `/** Dynamic model context materialized as a durable user-role snapshot. */`（`:78`）
  即动态上下文落成 user 角色快照，与压缩替换的 user 消息同层。

## 与白皮书的冲突

1. **「token 计量驱动」→ 需改写**。驱动判定的是**固定 4 字符/token 启发式 + provider usage 锚点的混合估算**（`packages/llm/token-meter/src/estimate.ts:12-19`、`packages/llm/token-meter/src/index.ts:163-176`），没有 tokenizer，也不保证与真实计数一致。白皮书若暗示「精确计量驱动」，与源码不符；「估算驱动」才准确。
2. **「检测溢出错误 CONTEXT_WINDOW_EXCEEDED；具体阈值公开资料未给出」→ 前半成立、后半与源码不符**。code 常量确实存在于 `packages/llm/llm/src/error.ts:25`，并有 5 条供应商文案正则（`:53-89`）与适配器落码（`packages/llm/llm-deepseek/src/transport.ts:32`、`packages/llm/llm-pi-ai/src/stream.ts:80-90`）。阈值并非未知：`floor(min(W×0.8, W−O−65536))`，保留尾部 `floor((W−O)×0.16)`，`maxOverflowRetries=1`（`packages/compaction/compaction-basic/src/config.ts:75-76`、`:105-108`、`:191-197`）。
3. **「修剪历史 + 可选摘要；失败路由到溢出代理」→ 前两句成立，末句未找到实现**。修剪的是工具输出（`tool-result-pruner`），摘要在其后（`packages/compaction/compaction-basic/src/index.ts:344-350`、`:303-307`）。**未找到「溢出代理」/fallback provider 路由**；溢出后是同模型、有次数上限的重试（`:222-223`、`:255-256`，`packages/core/agent-loop/src/agent.ts:548-563`）。
4. **「摘要（由插件实现）」→ 成立（更精确）**。摘要在 backend 插件（`compaction-basic`）里，但接口抽象在 `dsh-compaction`；且它是复用对话前缀（system + tools + messages）的一次性 `ctx.llm.stream(purpose:'compaction')` 调用（`packages/compaction/compaction-basic/src/summarizer.ts:25-31`、`:144-163`），不是独立摘要 prompt 模板。
5. **「事件溯源会话存储，可由事件日志重建」→ 成立（范围更大）**。不仅消息可重建，surface 的 replace/splice 与 `replaceGeneration` 也可确定性重放（`packages/core/session/src/surface.ts:594-609`、`:567-577`）；压缩事件本身 log-only，不改变 surface（`:302-327`）。已发布 v4 关系校验还把压缩括号纳入不变式（`packages/session/session-format-v3-to-v4/src/relationships.ts:226-263`）。
6. **「压缩由插件提供 vs 内置无自动压缩」→ 前者成立、后者不成立**。能力确由插件承载，但默认基座就挂载 `compaction-basic` 且 `auto` 默认 `true`（`packages/compaction/compaction-basic/src/config.ts:108`、`packages/bundle/base/cordis.patch.yml:352-361`，yml），自动触发代码无条件在该插件内（`packages/compaction/compaction-basic/src/index.ts:139-140`、`:147-258`）。社区「内置没有自动压缩」与源码不符。

冲突统计：6 条核对中 **3 条与源码不符**（#2 的阈值部分、#3 的溢出代理、#6 的自动压缩）、**1 条需改写成估算口径**（#1）、**2 条成立**（#4、#5）。

## 值得一提的细节

- **摘要调用是 KV-cache 友好的前缀复用**：它重放 system head、header 里的工具 schema、被遮蔽区间自身投影出的消息，只把指令放最后（`packages/compaction/compaction-basic/src/region.ts:544-563`、`packages/compaction/compaction-basic/src/summarizer.ts:25-31`）。这与「另起一个 summarizer 系统提示」是不同设计。
- **两条 token 价并存且不可混用**：`shadowedTokenCount` 用固定启发式（重放 fold 必须自洽，`region.ts:376-381`），阈值/保留/收缩判据用路由价（`:381`、`:417-422`）；注释解释「a pure consumer can subtract it without retaining per-node prices」（`packages/compaction/compaction/src/types.ts:73-89`）。
- **surface 位置 ≠ seq 顺序**：`CompactionResult.shadowedRange` 是位置跨度，替换后可能出现 `start > end`（`packages/compaction/compaction/src/types.ts:107-114`）；这对任何自建索引都是陷阱。
- **压缩锁是日志标记而非内存 mutex**：`compaction/start` 先落盘再 await，`compaction/end` 恰好一次；失败的 close 故意留下未配对 start 作为 busy 信号（`region.ts:157-172`、`:240-251`）。
- **`max-tokens` 的摘要视为失败**，因为 checkpoint 不完整（`packages/compaction/compaction-basic/src/summarizer.ts:203-207`）；摘要含图直接拒绝（`:214-221`）。
- **手动 `/compact` 可在阈值以下触发**，且必须在 idle 上通过 `runMaintenance` 串行化并 `flush`（`packages/compaction/compaction-basic/src/index.ts:407-459`、`packages/compaction/command-compact/src/index.ts:59-73`）。
- **图片用另一条恢复路径**：`IMAGE_OFFLOAD_REQUIRED` → `agent/request-error` 里做持久 surface 修复并 `retry`，不消耗重试预算、不写 `llm/retry` 事件（`packages/compaction/compaction-image-offload/src/index.ts:27-32`）；摘要阶段的同一错误走 `compaction/summary-error`（`:33-40`）。
- **投影检查点缓存只缓存折叠结果，不是权威**：`seq` 表明陈旧度，`ver` 不匹配就丢弃重放（`packages/session/session-projection-cache/src/index.ts:1-15`）；token-meter 在 `headerSeq`/`anchorHeaderSeq` 与检查点不一致时会整段重算（`packages/llm/token-meter/src/index.ts:262-268`）。
- **`KNOWN_SESSION_EVENT_TYPES` 是生成文件**，改事件类型需跑 `pnpm run gen-persistence-catalog`（`packages/core/session/src/known-event-types.ts:1-6`）。
- **单位陷阱**：修剪按 Unicode code point（`packages/compaction/compaction-tool-result-pruner/src/config.ts:22-29`、`packages/compaction/compaction-tool-result-pruner/src/index.ts:76-82`），token 估算按 UTF-16 `length`（`packages/llm/token-meter/src/estimate.ts:48`）；同一文本两者口径不同。
- 未找到实现清单（明确标注）：tiktoken/精确分词器、上下文 trim 策略、溢出代理/fallback provider、prompt-cache 管理器、按模型的自动「降级路由」。
