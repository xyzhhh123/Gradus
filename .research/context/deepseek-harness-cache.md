# deepseek-harness 提示词装配与缓存纪律（源码级笔记）

路径均相对 `H:\Desktop\LLM learning\AgentLearn\deepseek-harness`。

**总结论**：DSH **没有任何客户端缓存标记**（无 `cache_control`、无 `prompt_cache_key`、无 `ephemeral`）。它把「前缀稳定」做成结构性纪律：请求是从会话日志**纯函数推导**出来的，system prompt 是表面节点 0，易变事实被赶到请求尾部，工具声明变更走 developer 消息追加。命中完全依赖服务端严格前缀缓存，客户端只负责「不要改前面」。

---

## 请求装配顺序

请求**没有**独立 `system` 字段——system prompt 就是 `messages[0]`：

> `packages/core/system-prompt/README.md:142` — "The rendered prompt reaches the model as a system-role message of derived history — surface node 0, or the latest system node after an in-history update — neither the loop request nor `request/header` carries a separate `system` field."

**构造点** `buildRequest()`，就是四个字段（`packages/core/agent-loop/src/agent.ts:735-743`）：

```ts
markAgentLoopRequest(Object.freeze({
  ...header.config, messages: boundaryMessages, toolHistory: session.toolHistory(),
  ...header.tools !== undefined ? { tools: header.tools } : {}, sessionId, signal,
}))
```

所以「拼装」= `{ config…, messages, toolHistory, tools }`，**不是** system→技能目录→历史→schema 的字符串串联。`messages` 来自 `session.deriveMessages()`（`agent.ts:728`），`tools` 来自上一轮 canonical `request/header` 快照。

**一步之内的时序**（`agent.ts:283-317` `preStep` + `435-479` `step`）：

1. `agent.ts:292` `tools.resolve(...)` 解析本步可见工具集。
2. `agent.ts:294` `systemPrompt.assemble({ ...assembleContextFor(this, signal), modelSelection, toolSet })` → 一次拿到 `sections/contexts/tools/variables`。
3. `agent.ts:296-306` `agent/pre-step` waterfall；**最内层默认实现**把 runtime context 快照拼在已认领用户消息之后 —— `agent.ts:301-304` `messages: context === undefined ? claimed : [...claimed, context]`。
4. `agent.ts:453-461` `SystemPromptProjection.project(renderedPrompt, …)` 决定 system 节点 append 还是 replace，再 `session.append('system/message', …)`。
5. `agent.ts:464-477` 追加认领的 user 消息（runtime-context 源的消息就地替换，不重复追加）。
6. `agent.ts:479` `buildRequest()` → `:666-691` 记 `request/header` → `:728-734` 推导并**深冻结**历史。

`messages` 数组顺序 = 会话**表面节点顺序**（= 日志 seq 序）：system 头在节点 0，其后是历史，本步新增的 runtime-context 与技能目录追加在**尾部**。

**「不能随意改」是否被注释规定**：没有字面禁令，但有等价强度的结构约束：

> `agent.ts:3-4` — "Default Agent driver over queued turns and step-boundary input. **Every request is derived from the session log.**"
> `tests/request-reconstruction.spec.ts:1-6` — "every request the loop sends is a pure function of the session log — messages derive at the step/start boundary and the header is the latest request/header snapshot. **Each request extends its predecessor** unless a logged compaction replacement or header change explains the difference."

---

## system prompt 分节机制

每节由**拥有该事实的插件**调 `SystemPrompt.section()` 注册（`system-prompt/src/index.ts:484-493`），位置取自一张**集中分配表**（`index.ts:136-172`）：

```ts
HARNESS_IDENTITY: -1000, DEPLOYMENT_PERSONA_PREFIX: 0, PLAN_POLICY: 500, TEAM_POLICY: 600,
PTC_ONLY: 800, FILE_REFERENCE: 900, TOOL_BASH: 1000 … TOOL_COMPUTER_USE: 3000,
MCP_SERVERS: 3100, TOOLS_SDK: 5000, DELIVERABLE_FILE_REFERENCES: 9000, STRUCTURED_OUTPUT: 9900,
// Local paths and endpoints follow reusable instructions.
HARNESS_SOURCE: 10000, WEB_SURFACE: 10100, DEPLOYMENT_PERSONA_SUFFIX: 10200,
```

`index.ts:168` 的注释解释了 10000/10100 为何排在 9900 之后：把可变环境压到最后，让前面那一大段可复用指令的字节前缀尽量长。

**稳定排序：order 升序，同号按名字 code-unit 序**（`index.ts:253-256`）：

```ts
/** Order prompt sections by their explicit placement, then deterministically by name. */
return a.order - b.order || compareNames(a.name, b.name)
```

`index.ts:248-251` —— "**Code-unit name comparison — locale-independent, so the order is identical on every machine.**"
契约写在字段 JSDoc（`index.ts:56-59`）："Sections are concatenated in ascending order. Equal orders use code-unit name order."

**注册顺序不是排序**：`assemble()` 先分层 merge（同名 scoped 覆盖 global），再统一排序（`index.ts:658` `sectionDefinitions = [...sectionByName.values()].sort(comparePromptSections)`）。拼接用两个换行、丢空节（`index.ts:296-301` `renderPrompt`：`.filter(t => t.length > 0).join('\n\n')`）。

**排序为何重要**（源码原话）：

> `system-prompt/README.md:156`（KV Cache effect）— "Prefix-stable while identity, persona, variables, section text, and order render identically: an unchanged rendering leaves the system nodes untouched, including at a new request series on a capable route … Any change may invalidate reuse from the first changed token; provider cache sharing and measured hit rates are not guaranteed."

**一步之内不重装配**——这是最省事的前缀保护：`refreshContext()` 只重算 runtime context，sections / tools / variables 保持已接受状态：

> `index.ts:578-587` — "Refresh accepted registered runtime facts for request admission … **Sections, tools, and interpolation variables retain the accepted assembly; their providers and waterfall do not rerun.**"

**分节不能改工具**：waterfall 拿到 `PromptPresentation`（无 `tools`），`PromptAssembly extends PromptPresentation { tools }` 在 waterfall **之后**才加（`index.ts:125-134`），由 `system-prompt/tests/tool-order.spec.ts:81-89` 钉住（`expect(assembly).not.toHaveProperty('tools')`，监听器塞的 tools 被忽略）。

---

## 技能目录注入

技能目录**不在 system prompt**，而是 `agent/pre-step` 监听器产出的持久 **user 角色消息**（`source.kind === 'skill-catalog'`），追加在请求尾部。

> `packages/skill/tool-skill/src/index.ts:235-245`
> ```ts
> if (history.visibleDigest === digest) { … }                      // 摘要未变：一个字节都不发
> if (!history.published && skills.length === 0 && !incomplete) { … }
> const catalog = history.published ? renderCatalogUpdate(...) : renderCatalogMessage(...)
> ```
> 摘要 = `digestCatalogEntries`（`:347-355`，sha256 over 每项 `JSON.stringify([name, description])` + `incomplete` 前缀）。

**什么时候注入**：首步注入一次完整目录；之后每步算摘要，未变则不发。技能**体**改动不进摘要——`tool-skill/tests/tool-skill.spec.ts:717` "keeps body-only edits out of the catalog and loads the latest body on demand"。

**注入多少 / 有无 token 预算**：**未找到总 token 预算**。只有**单条 description 字符上限**——`tool-skill/src/index.ts:28` `const DEFAULT_CATALOG_DESCRIPTION_MAX_LENGTH = 500`，配置项 `catalogDescriptionMaxLength`（`:64-72`），最小 3（`:82`），截断 `slice(0, maxLength - 3) + '...'`（`:411-414`）。条目格式 `- \`name\`: description`（`:339-341`），整体包在 `<system-reminder>`/`<available_skills>` 内（`:258-287`）。搜索词：`budget`、`token`、`maxLength`、`limit`——除上述外无总量控制。

**会不会导致前缀变化**：不会破坏已发出的前缀。集合变化时发**完整替换目录**（`renderCatalogUpdate`，`:289-`），删除全部技能时发空墓碑并声明 "Do not use names from earlier skill catalogs."（`:292-295`）。历史被 compaction 隐藏后下一步重建（`tool-skill.spec.ts:688`）。

监听器**注册顺序**被注释显式用来固定尾部顺序：

> `tool-skill/src/index.ts:170-174` — "…background first (workspace rules, runtime policy, the catalog), the material the model must act on last, closest to its answer. **Registration order makes that placement deterministic**: this listener registers before the catalog listener, so the waterfall hands it the catalog-bearing list to extend."

waterfall 里先注册者在外层、`await next()` 之后追加，故 `/技能名` 指令体（`:181-208`，先注册）落在目录（`:217-255`，后注册）**之后**。

---

## 缓存标记与命中统计

**客户端不发任何缓存标记。** 第一方 DeepSeek 适配器 wire body 逐字段可查（`packages/llm/llm-deepseek/src/serialize.ts:152-168`）：只有 `model / stream / messages / max_tokens / thinking / output_config / system / temperature / stop_sequences / tools`，工具项只有 `name / description / input_schema / defer_loading?`。无 `cache_control` 断点、无 `prompt_cache_key`、无 `ephemeral`。

搜索词：`cache`、`cacheControl`、`cache_control`、`ephemeral`、`prompt_cache`、`prompt_cache_key`、`cacheRetention`、`supportsCacheControlOnTools`。
- `prompt_cache_key` 仅出现在 `packages/subagent/subagent-codex/tests/responses-fixture.ts:71`（Responses API 响应 fixture 的回显字段，非 DSH 发送）。
- `cache_control` 只有 pi-ai 路线的**能力声明**，不是本仓发送逻辑：`llm-pi-ai/src/catalog.ts:442-452`（`/** Prompt-cache marker convention; openai-completions. */ cacheControlFormat`、`supportsLongCacheRetention`、`supportsCacheControlOnTools`）、`llm-pi-ai/src/config.ts:170-171`（`/** Prompt-cache retention preference. */ cacheRetention?: CacheRetention`），只透传给 pi-ai（`adapter.ts:150`）。

→ **未找到客户端缓存断点实现，判断为依赖服务端严格前缀缓存。**

**命中字段：`cacheReadTokens` / `cacheWriteTokens`，与 `inputTokens` 互斥**：

> `packages/llm/llm/src/types.ts:174-196` — "Counts are DISJOINT: `inputTokens` is uncached input only; cached input is reported separately as `cacheReadTokens`/`cacheWriteTokens` (billed input = sum of the three). Adapters whose providers fold cache hits into a total prompt count (DeepSeek's `prompt_tokens`) subtract them out."

适配器映射：`llm-deepseek/src/translate.ts:36` `cache_read_input_tokens → cacheReadTokens`、`cache_creation_input_tokens → cacheWriteTokens`。

展示层：
- 命中率 `packages/experimental/client-ui-stats/src/client/stats-usage.ts:58-68`，`Math.min(1, read / (input + read + write))`
- 汇总字段 `stats-totals.ts:25-26`：`cacheHit`、`cacheRead`
- 文案 `packages/client/ui-chat/src/client/chat/token-format.ts:59-67` `formatCacheHitPercent()`（`:60` "without rounding a partial hit to 100%"）
- 轨迹表 `packages/client/ui-trajectory/src/client/layout.ts:949` `cell.cacheRead = usage.cacheReadTokens`
- 累计 `packages/llm/token-meter/src/usage-projection.ts:41,79`

---

## 前缀稳定性的代码约束

**① 易变事实放尾部，绝不进 system prompt** —— 最硬的一条，注释直接点名缓存（`system-prompt/src/index.ts:177-185`）：

```ts
const CONTEXT_ORDERS = { WORKING_DIRECTORY: 100, SANDBOX_POLICY: 110, APPROVAL_POLICY: 115,
  SUBAGENT_DELEGATION: 120,
  // Process-specific paths and endpoints stay out of the cached system prompt.
  HARNESS_SOURCE: 200, WEB_SURFACE: 210,
```

落地实现 `harness-dev-context`：**同一事实注册两次**——system 段放「无路径/URL 的稳定散文」，runtime context 放真实值：

> `packages/context/harness-dev-context/src/index.ts:4-8` — "It registers **path- and URL-free prompt sections** and states the current implementation checkout and Web GUI URL as runtime context, so a process started from another checkout, port, or installation **leaves the system prompt unchanged**."
> 成对注册 `:134-157`（`getSectionOrder('HARNESS_SOURCE')` + `getContextOrder('HARNESS_SOURCE')`）；稳定散文 `:40`、`:51`；易变值只在 context `:144`、`:156`。

**② 中途改 prompt 的唯一合法方式是表面操作，判据是「能否保住前缀」**（`agent-loop/src/runtime-context.ts:57-67`）：

> "A capable continuing series appends changed nonempty text after the cached history. An incapable route, a cleared prompt, or a new series whose effective text differs instead normalizes the first system node … **A series start whose latest surviving node already holds the rendering emits nothing: moving the same text to node 0 would change the request prefix without changing the prompt.**"

对应判定（`:100-107`）：不可追加 / 空 / (新序列且文本不同) → 重写头部；`latest.text === rendered` → `return []`；否则 append。

测试把这条经济性写成断言：
> `tests/system-prompt-projection.spec.ts:188-190` — "The effective text is already current: **re-baselining node 0 would move the same text into the request's first message and invalidate the provider prefix.**" → `expect(projection.project('v2', NEW_SERIES)).toEqual([])`
> `:204` — "A new series already costs the cache, so the change folds into node 0."

**③ node 0 有结构级保护**（`packages/core/session/src/surface.ts:487-507`）：

> "Protect the system prompt at surface node 0. A replacement covering node 0 while that node is a `system/message` must itself be a `system/message` over exactly that node; later system nodes carry no protection and a compaction range may shadow them."
> 抛出：`'surface replace: node 0 holds the system prompt and may be rewritten only by a system/message over exactly that node'`

**④ 请求对象深冻结**（`agent.ts:726-734`）：

> `// canonicalHeader is shallow; append logs a detached snapshot, not these local values.` → `deepFreeze(header)`；逐条 `deepFreeze(message)` + `this.frozenMessages.add(message)`，最后 `Object.freeze(boundaryMessages)`。

**⑤ 工具 schema 变化只在路由不支持时才强推新序列**：`agent.ts:319-324` `toolsChanged()`；使用点 `agent.ts:453-458` 的 `startsSeries` 仅在 `preparedCall?.toolUpdate === undefined && toolsChanged(assembly.tools)` 时为真。

**⑥ 逐包 KV Cache 契约**（`agent-loop/README.md:164`）：

> "Prefix reuse requires earlier messages and declarations to **stay byte-identical** under the same provider and model route. An unchanged rendered prompt keeps the cached prefix: a capable route leaves retained in-history system nodes in place even when a new series starts … A prompt change that replaces a system node in place makes the request differ from that node's first token — in full when the node is node 0 — so the provider prefix cache misses from there; when the prepared call declares `systemPromptUpdate: 'in-history'`, a non-empty prompt change inside a continuing request series is appended after the cached history, so the prefix through that history stays reusable."
> `README.md:178` — "Ordinary history growth is append-only and preserves reusable entries. A surface replacement or compaction invalidates reuse from the first shadowed history token."

---

## 工具 schema 稳定性

**两层排序：入 header 的规范序，与上 wire 的追加序。**

**第 1 层 — `SystemPrompt.assemble()` 的规范序**（`system-prompt/src/index.ts:227-246`）：配 `toolOrder` 就按表，否则按名字字典序。

> "Apply configured tool order, inserting unlisted tools lexicographically at `TOOL_ORDER_REST`. Unknown configured names fail; known but restricted names may be absent."
> `index.ts:207-208` `export const TOOL_ORDER_REST = '<unlisted-tools>'`
> `index.ts:248` `/** Code-unit name comparison — locale-independent, so the order is identical on every machine. */`

**`<unlisted-tools>` 是动态工具的着陆槽**：第一方工具按名字钉死，所有未列出的（MCP、运行时注册）统一落进 rest 槽的固定位置。校验强制该标记存在且只出现一次（`:214-225`），未知名字在 assembly 期抛错（`:238-241`）。

为何必须字典序（`core/agent-loop/tests/tool-order.spec.ts:1-7`）：

> "…carries the assembly's canonical tool order (`toolOrder` config, or lexicographic name order), regardless of the order tool plugins happened to register in. **Registration order is a concurrent loading artifact and must not leak downstream.**"

`system-prompt/README.md:166` "reordering changes cache shape but not semantic content"；`:170` "Prefix-stable while the visible schema set, rendering, and order are unchanged. Registration, restriction, or reordering may invalidate reuse from the first changed schema token."

**第 2 层 — wire 上的 `tools` 是「基线 + 追加」**。支持 tool update 的路由用 `Map` 插入序重建声明表，基线工具原序在前，新工具 `set` 到尾部（`packages/llm/llm/src/content.ts:382-394`）：

```ts
const declarations = new Map(history.tools.map(tool => [tool.name, tool]))
for (const update of history.updates) for (const tool of update.additions) {
  if (!declarations.has(tool.name)) {
    // Later additions activate these definitions at their recorded position.
    declarations.set(tool.name, { ...tool, deferLoading: true })
  }
}
```
返回 `content.ts:506-509` `tools: [...declarations.values()]` → **上一次的 tools 数组始终是新数组的严格前缀**。

**动态加载 MCP 工具会不会改前缀**：不改历史前缀；声明层以 `deferLoading: true` 追加（schema 暂不载入上下文），消息层留一条 `tool-addition` developer 块。路由能力定义在 `llm/src/types.ts:442-451`：

> "`'addition-only'`: the model reads a `tool-addition` block in a later developer message as activating a tool declared with `deferLoading`, so **an added tool follows the cached history instead of rewriting the declaration list**. `'in-history'`: the model additionally reads `tool-removal` blocks … Absent means every request declares the complete current tool list."

**权衡点**（`content.ts:396-423`）：

> `/** Rebuild declarations on removals or additions to an empty set, where the request prefix changes. */`
> `let reset = available.size === 0 && additions.size > 0` … `if (reset) { baseline = [...available.values()]; retainedFrom = index + 1 }`

即**删除**（addition-only 无法表达 deactivate）或**从空集添加**才重建基线、承认一次失效；纯添加走追加。

真实 provider 上钉住（`apps/cli/tests/profiles/sdk/dynamic-tool-cache.e2e.ts:143-150`）：`added.body.messages.slice(0, before.body.messages.length)` 等于 `before.body.messages`；`added.body.tools.slice(0, initial.body.tools.length)` 等于 `initial.body.tools`；新工具 `{ name: sampleTool, defer_loading: true }`；消息里出现 `{ type: 'tool_addition', tool: { type: 'tool_reference', name: sampleTool } }`。

---

## 相关测试

| 断言 | 位置 |
|---|---|
| 前缀扩展工具函数（messages 严格前缀 + 无 `system` + tools 相等） | `core/agent-loop/tests/request-reconstruction.spec.ts:68-74` |
| 同 turn 多步 append-extends，请求深冻结 | 同上 `:88-112` |
| resume 后字节级连续（":814 byte-identical continuation"） | 同上 `:791-818` |
| 定理：任何请求都能仅凭日志逐字节重建 | 同上 `:850-897` |
| `deriveMessages` 确定性 / 重放一致 / 噪声不干扰 | `core/session/tests/properties.spec.ts:100-105`、`:115-124`、`:136-159` |
| 推导缓存：追加增量、替换重建 | `core/session/tests/derived-cache.spec.ts:27-90` |
| 工具序与注册序无关（assembly / loop 两级） | `core/system-prompt/tests/tool-order.spec.ts:34-43`；`core/agent-loop/tests/tool-order.spec.ts:73-99` |
| 新序列起点文本未变则不重写 node 0 | `agent-loop/tests/system-prompt-admission.spec.ts:126-158`（`:149` "Rewriting node 0 would invalidate the provider prefix."） |
| 工具变更仍保住整个前次请求为 wire 前缀 | 同上 `:226-260`（`:245-246`） |
| compaction 引起的序列起点保住前缀 | 同上 `:262-`（`:279` "miss the provider prefix cache from the first token"） |
| 真实 API：首请求后每步 `cacheReadTokens > 0` | `agent-loop/tests/request-cache.e2e.ts:94-99` |
| 真实 API：工具增删后仍命中 | `apps/cli/tests/profiles/sdk/dynamic-tool-cache.e2e.ts:146` |
| 技能目录稳定、body-only 编辑不重发 | `skill/tool-skill/tests/tool-skill.spec.ts:249`、`:717` |

主张的最直白表述（`request-cache.e2e.ts:16-23`）：

> "…must report `cacheReadTokens > 0` on every request after the first — the adapter maps the provider's `prompt_cache_hit_tokens`, and the per-step usage recorded on `assistant/message` events is the production observable for cache behavior (the reconstructability Agent Note's measurement layer: **prefix stability is corollary #1**)."

⚠️ 该注释引用的 "reconstructability Agent Note" 在 `.agents/notes/` 全树**未找到**（`corollary` 零命中），判断为失效引用。

---

## 值得一提的细节

1. **缓存块粒度常量（仅测试常量）**：`request-cache.e2e.ts:26-27` "cache-block granularity (64 tokens)"；`dynamic-tool-cache.e2e.ts:16-17` "Provider cache blocks may leave a short uncached suffix of an unchanged request." `const cacheSuffixTokens = 256`（断言允许尾部最多 256 token 未命中）。

2. **prompt 不在 header 里**：`agent-loop/README.md:95` "a changed envelope (config or tools — **the prompt is not part of the header**)"; reason 为 `initial` / `resume` / `change` / `series`（`agent.ts:675-691`）——prompt 变更走 `series`，因为它是 `messages[0]` 而非 envelope 字段。

3. **header 折叠被冻结**（`core/session/src/index.ts:929-933`）："a consumer mutating it in place … would desync every later comparison against the log, so mutation throws instead."

4. **工具增删在日志里是 `developer/message`**：`agent.ts:692-706` 拿名集差产出 `tool-addition`/`tool-removal`，`headerSeq` 只在**有新增**时挂上；`surface.ts:367-395` 校验每个 addition 必须在 `headerSeq` 旧 header 里恰好一个定义，且 `deferLoading` 若存在必须为 `true`。

5. **`deriveMessages()` 缓存语义**（`core/session/src/index.ts:991-996`）："CACHED: pure tail growth costs O(new nodes); a replacement or message projection rebuilds. The returned array is a fresh snapshot per call …; the `Message` objects in it are SHARED and **deep-frozen**."

6. **技能目录与 runtime context 都是 user 角色消息**（有意为之）：它们随会话变化，放进 system prompt 就会改 node 0、代价是整条前缀。以「追加 + 声明 supersede」存在——`runtime-context.ts:20` 的 `CLEARED` 文案、`system-prompt/src/index.ts:323` "This snapshot supersedes earlier runtime-context snapshots."

7. **`systemPromptUpdate` / `toolUpdate` 是路由能力而非全局策略**（`llm/src/types.ts:434-465`）：同一份代码在 capable 路由追加、在 incapable 路由重写头部；测试以 `it.each(['capable','plain'])`、`it.each(['in-history','addition-only'])` 分支覆盖（`system-prompt-admission.spec.ts:110-124`、`:187`）。

8. **明确「未找到」，不作推测**：
   - 未找到「会话中途不得改动 system prompt」的字面禁令。等价物是 node 0 的 surface 保护（`surface.ts:487-507`）+ 「文本未变则不发事件」的投影规则（`runtime-context.ts:63-66`）+ 前缀扩展测试。改动被允许，代价被计入决策函数。
   - 未找到技能目录的**总** token 预算（只有单条 description 的 500 字符上限）。
   - 未找到客户端缓存标记/断点 → 依赖服务端严格前缀缓存。
   - 未找到「工具 schema 必须稳定」的字面禁令；等价物是规范序 + `<unlisted-tools>` 槽 + 追加型 wire 声明 + `deferLoading`。
   - 未找到 `prompt_cache_key` 的发送实现。
   - 未找到名为 "reconstructability" 的 Agent Note 文件。
