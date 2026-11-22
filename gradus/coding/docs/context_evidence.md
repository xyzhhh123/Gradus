# 上下文管理：证据索引

本文件只放**可核对的引用**，不放叙述。思路与结论在 `context_reference.md`。

代码类证据均为本地仓库源码，行号对应该版本的 HEAD；Claude Code 为公开 npm 包的打包产物，只作行为级引用。

## 0. 版本

| 对象 | 位置 | 版本 |
| --- | --- | --- |
| codex | `codex/codex-rs` | `ddd1812`（2026-11-06） |
| hermes-agent | `hermes-agent` | `c314285f07`（2026-11-03） |
| learn-claude-code | `learn-claude-code` | `0dcafa2`（2026-08-27） |
| pi-agent | `pi-agent/pi` | `39b5d9d`（2026-05-26） |
| deepseek-reasonix | `deepseek-reasonix` | `cd20bb50b`（2026-11-11） |
| DeepSeek Harness | `deepseek-harness/packages` | `fcc0ad4621`（2026-11-04） |
| Claude Code | `@anthropic-ai/claude-code@2.0.0` 的 `cli.js` | 打包产物，非源码 |

## 1. 数值速查

| | codex | hermes | learn-claude-code | pi | reasonix | DSH | Claude Code |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 触发线 | 窗口 × 90%（可下调）；硬上限 窗口 × 95%；后台预压 65% | `(窗口 − 输出预留) × pct`；pct 默认 0.50、<512K 窗口抬到 0.75；下限 64K | 固定 50,000 字符 | 窗口 − 16,384 | 窗口 × 0.90 | `min(窗口 × 0.8, 窗口 − 输出预留 − 65,536)` | 窗口 − 13,000 |
| 保留 | 用户消息共享 20K token / 服务端保留项共享 64K | 头 3 条（首压后 0）+ 尾部 2.5% 夹 10K–25K，再夹 `0.5×触发线`、`0.2×窗口` | 头 3 + 尾 46 | 尾部 20,000 token | 活跃轮次的用户消息 | `(窗口 − 输出预留) × 0.16` | 未观测到 |
| 单条工具输出 | 10,000 token + 1 MiB 字节缓冲 | 100,000 字符（MCP 50,000） | bash 50,000 字符 | 2,000 行 / 50KB | 32KB | 独立 spill 策略 | 打包产物中只有截断文案 |
| 缓存 | `prompt_cache_key` + TTL | 4 个断点 | 无 | 键 = 会话 id；3 个断点 | 无标记 | 零标记 | 每条消息最后一块 + system |

## 2. 路径与行号

| 主题 | codex | hermes-agent | learn-claude-code | pi-agent | deepseek-reasonix | DSH |
| --- | --- | --- | --- | --- | --- | --- |
| 阈值 | `protocol/src/openai_models.rs:568-590`；`core/src/session/context_window.rs:26-130`；`core/src/config/mod.rs:238` | `agent/context_compressor.py:2660-2810`、`:1149-1150` | `s08_context_compact/code.py:248-253` | `core/compaction/compaction.ts:126-130`、`:267-270` | `internal/runtime/agent/compact.go:22-63` | `compaction/compaction-basic/src/config.ts:20-23`、`:153-217` |
| 压缩流水线 | `core/src/compact.rs:178-472`；`compact_remote_v2.rs:173-541`；`compact_token_budget.rs:18-66` | `context_compressor.py:3286-3324`、`:5438-5546` | `s08/code.py:509-520` | `compaction.ts:446-501`、`:859-1057` | `compact_archive.go:36-66`；`compact_projection.go:389` | `compaction-basic/src/index.ts:316-371`；`region.ts:117-155` |
| 保留与切点 | `compact.rs:64`、`:411`、`:745-846`；`compact_remote_v2.rs:93`、`:521-541` | `:4571-4600`；`context_compressor_tail.py:23-38` | `:391-410` | `compaction.ts:351-364`、`:491-500` | `compact.go:250-295`；`compact_group.go:18-75` | `region.ts:117-155`、`:410-422` |
| 接受条件 | 无 | 无 | 无 | 无 | `compact_projection.go:540-581` | `region.ts:410-422`（仅「必须更小」） |
| 工具输出 | `core/src/unified_exec/mod.rs:74-75`；`utils/head-tail-buffer/src/lib.rs:18-19` | `tools/budget_config.py:13-20`；`tools/tool_result_storage.py:364-381` | `s08/code.py:340-379`、`:437-456` | `core/tools/truncate.ts:11-12`；`utils/output-files.ts:14-33` | `tool_output_spill.go:53-58`、`:225-258` | `packages/spill/*` |
| 持久化与恢复 | `session/rollout_reconstruction.rs:70-91` | `hermes_state_messages.py:57`、`:73`、`:1679-1687` | 无 | `core/session-manager.ts:482-510` | `projection.go:199-210`；`compaction_state.go:184-204` | `core/session/src/surface.ts:302-364`、`:594-609` |
| 缓存 | `core/src/client.rs:720-727`；`compact.rs:365-374` | `agent/prompt_caching.py:1-6`、`:184-214`、`:364-374` | 未实现 | `core/cache-warmer.ts:19-32`；`cache-stats.ts:10-112` | `usage_anchor.go:8-47`；`context_usage.go:71` | `llm/llm-deepseek/src/serialize.ts:152-168`；`system-prompt/src/index.ts:182`、`:248-261` |
| 记忆 | `ext/memories/src/prompts.rs:31-64`；`lib.rs:16` | `agent/agent_init.py:1247-1248` | `s09/code.py:280-349` | 无 | `state/memory/doc.go:9-13`；`memory.go:164-167` | `skill/tool-skill/src/index.ts:28` |
| 反抖动 / 失败 | `session/turn.rs:917-947`（仅审查路径重试一次） | `context_compressor.py:3028-3035`、`:2735`、`:3072-3120`、`:3514-3580` | `:540-544`（重试一次） | `compaction.ts:581-593`（截断即报错） | 无（用接受条件替代） | `compaction-basic/src/index.ts:213-257` |

## 3. 关键设计的出处

| 结论 | 出处 |
| --- | --- |
| codex 的 95% 是「可用窗口/硬上限」，90% 是可覆盖的软触发线 | `protocol/src/openai_models.rs:413`、`:573-590`；`core/src/session/context_window.rs:84-110` |
| codex 换窗口模式刻意复用压缩生命周期 | `core/src/compact_token_budget.rs:18-20` |
| codex 普通回合超窗不自动重试 | `core/src/session/turn.rs:2027-2031` |
| hermes 的 60/300/900 是超时冷却，熔断恢复是单值 300 秒 | `context_compressor.py:799`、`:4147-4150` vs `:2735`、`:3072-3120` |
| hermes 的尾部预算是三重夹取 | `context_compressor_tail.py:23-38`；`context_compressor.py:928`、`:2240` |
| learn-claude-code 前两步每轮无条件执行 | `s08/code.py:509-520` |
| learn-claude-code 被动路径保留尾部 5 条 | `s08/code.py:497-507` |
| learn-claude-code 中文计量在 s15 偏差约 6 倍 | `s15_integrated_harness/code.py:1932` vs `s08/code.py:263` |
| pi 切点要对齐轮次起点 | `compaction.ts:492-500` |
| pi 缓存读写计入上下文 | `compaction.ts:140-142` |
| reasonix 输出预算刻意不参与触发线 | `compact.go:45-48` |
| reasonix 触发线永不豁免、50% 可豁免 | `compact_projection.go:553-579`；`context_manager.go:143-147` |
| reasonix 锚点绑定投影版本 | `usage_anchor.go:8-47` |
| reasonix 事实索引刻意不进前缀 | `state/memory/memory.go:164-167` |
| DSH 保留预算 < 触发线是加载期断言 | `compaction-basic/src/config.ts:191-204`、`:230-241` |
| DSH 摘要是「重放前缀之后的最后一条 user 消息」 | `compaction-basic/src/summarizer.ts:25-67` |
| DSH 修剪只动工具输出中段 | `compaction-tool-result-pruner/src/config.ts:7-14` |
| DSH 已发布 v4 把压缩括号写成不变式 | `session/session-format-v3-to-v4/src/relationships.ts:226-271` |
| Gradus 保留预算与触发线无约束 | `gradus/coding/context.py:45-46`、`:169-176`、`:179-198` |
| Gradus 压缩后重建 Agent | `gradus/coding/session.py:1363-1393` |
| Gradus 运行中视图与恢复重放是两套实现 | `session.py:1033` vs `:597-602` |
| Gradus 摘要请求用独立 system 前缀 | `gradus/coding/context.py:262-287` |
| Gradus 已有 3 个缓存断点 | `gradus/ai/providers/anthropic.py:139-147`、`:164-192` |

## 4. Claude Code 的打包产物引用

来源：`@anthropic-ai/claude-code@2.0.0` 的 `cli.js`（8.92 MB），只读取字符串与常量，未反编译。复现方式见 `claude-code/NOTES.md`。

| 观察 | 依据类型 |
| --- | --- |
| 自动压缩线 = 窗口 − 13,000 | 常量 `13000 / 20000 / 20000` + `percentLeft` 计算函数同时可知 |
| 剩余百分比以「窗口 − 13,000」为分母；关掉自动压缩时退回裸窗口 | 同上函数的分支 |
| 压缩触点不止一次，含工具结果级的微压缩 | 埋点字段 `{toolsCompacted, totalUncompactedTokens, tokensAfterCompaction, tokensSaved, triggerType}` |
| 摘要提示词为九节结构，先 `<analysis>` 后 `<summary>` | 模板字符串原文 |
| 压缩会在历史里留一条 `compact_boundary` 系统消息，含 `{trigger, preTokens}` | 消息构造函数与类型判断函数 |
| 恢复时从最后一条 `compact_boundary` 之后取消息 | 反向扫描后切片的实现 |
| 缓存断点打在每条 user/assistant 消息的最后一块与 system 上 | `cache_control: {type:"ephemeral"}` 出现位置 |
| 用户可定制压缩聚焦点 | 提示词末尾的 `## Compact Instructions` / `# Summary instructions` 示例 |

**未观测到**（不做推测）：压缩后保留多少条、微压缩阈值、压缩失败处理、system 提示的完整装配顺序。
