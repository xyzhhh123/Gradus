# ai 层实施计划

## 目标与边界

**目标**：把四家厂商的线上格式统一成一套内部协议（1 种消息形状 + 12 个事件），
对上只暴露 `Request` 与 `Provider.stream()`。

**边界**：只改 `gradus/ai/`。不碰 `agent/`、`coding/`，不建它们的文件，也不替它们做决定。
涉及跨层的结论（历史如何落盘、压缩怎么触发）只记录在本文档，不在本层实现。

**依赖方向**（不可逆）：

```
providers/*  ->  base.py / stream_assembler.py  ->  types.py
     |
     +-------->  transport.py  ->  httpx
agent 层     ->  base.py / types.py
```

`types.py` 只允许 import 标准库；`transport.py` 是唯一允许碰网络和 httpx 的文件。

## 里程碑

| 编号 | 内容 | 状态 |
| --- | --- | --- |
| M0 | `types.py`：消息、内容块、`Usage`、`StopReason`、12 个事件 | 完成 |
| M1 | `base.py`：`ToolSpec`、`ModelSpec`、`Request`、`Provider` 协议 + 契约注释 | 完成 |
| M2 | `fake.py`：剧本驱动，覆盖正常 / 截断 / 中断三条路径 | 完成 |
| M3 | 事件累加器 `stream_assembler.py`，`fake.py` 已改用它 | 完成 |
| M4 | 传输层：HTTP 客户端、SSE 解析、超时与重试、错误映射 | 完成 |
| M5 | `openai_compat`：第一个真实适配器（DeepSeek） | 完成 |
| M6 | `anthropic`：请求侧约束最多的一家（角色交替、signature 回传） | 完成 |
| M7 | `gemini`：合成缺失的 delta | 完成 |
| M8 | `openai_responses`：item / part / delta 三层折叠 | 完成 |
| M9 | 录制回放：真实 SSE 样本存成 fixture，离线断言解析正确性 | 收尾：9 条真实样本已覆盖三种协议；`gemini` 缺 key 暂不录，`ai` 层到此告一段落 |

顺序理由：先让事件流离线自洽（M2），再抽公共累积逻辑（M3），最后才接网络（M4 起）。
反过来做的话，协议缺陷会被网络问题掩盖。

## 完成定义

每个里程碑结束时必须同时满足：

1. `uv run python -c "import gradus.ai.<模块>"` 通过，且 `types.py` 的 import 清单为空
2. 事件序列符合契约：`StreamStart` 打头，块按三阶段成对出现，结尾恰好一个
   `StreamDone` 或 `StreamError`
3. `partial` 是快照，不随后续 delta 变化
4. `content_index` 由本层从 0 分配，与厂商索引无关
5. 与上一轮相比，本文档的「进度检查」与「偏差记录」都已更新

## 进度检查

### 累计进度（更新至 2026-10-30）

**已完成**
- `types.py`：3 种消息、3 种内容块、`Usage`（支持 `+`）、7 个 `StopReason`、12 个事件
- `base.py`：请求侧类型 + `Provider` 协议，4 条契约规则写进注释
- `stream_assembler.py`：块累积、`partial` 快照、`content_index` 分配、用量合并、
  `done()` / `fail()` 自动补 End 事件（M3）
- `providers/fake.py`：剧本 → 12 事件，多剧本按调用顺序消费；累积逻辑已交给 assembler
- `tests/test_fake.py`：7 条断言固化 fake 的行为，可离线复跑
- `transport.py`：`parse_sse()` 纯函数切分 SSE、`post_sse()` 流式 POST，超时与重试，
  一切失败归一为 `TransportFailure`（M4）
- `tests/test_transport.py`：14 条断言，SSE 解析 9 条 + 传输 5 条，走 `httpx.MockTransport`
- `providers/openai_compat.py`：请求侧四个纯函数 + `stream()` 全链路（M5）。
  `delta.content` / `reasoning_content` 直接喂 assembler；`tool_calls` 按 `index`
  缓冲到流结束再重放；`finish_reason` 与 `usage` 各有一张映射
- `tests/test_openai_compat.py`：28 条断言，请求侧 13 条 + 响应侧 15 条
  （文本流、工具分片、并行工具、reasoning、usage、length、TransportFailure、坏 JSON、URL/鉴权头）
- `providers/anthropic.py`：请求侧四个纯函数 + `stream()` 按 SSE 事件名分发（M6）。
  相邻同角色合并保证交替；首条不是 `user` 时在发请求前直接拦成 `StreamError`；
  `signature_delta` 累积到块结束时一次给出；`content_block_stop` 三个 `end_*`
  连调，靠幂等让当前块生效
- `tests/test_anthropic.py`：19 条断言，请求侧 7 条 + 响应侧 12 条
- `providers/gemini.py`：请求侧四个纯函数 + `stream()`（M7）。`contents` 只有 `user`/`model`，
  先从历史建 `id -> name` 表回填 `functionResponse`；`functionCall` 一次到齐，现场合成
  三段事件并生成 `call_{n}`；`thought` 标记映射到 `ThinkingBlock`；
  `promptFeedback.blockReason` 走 `done("filtered")` 而不是 `StreamError`
- `tests/test_gemini.py`：19 条断言，请求侧 7 条 + 响应侧 12 条
- `providers/openai_responses.py`：请求侧四个纯函数 + `stream()`（M8）。`input` 是 item 数组，
  一条 `AssistantMessage` 拆成「一个 message item + N 个 `function_call`」；
  工具声明是扁平结构，不再有 `function` 包一层；`store` 恒为 `false`（不走服务端会话）。
  响应侧把 item / part / delta 三层折叠成内部的两层，丢掉 `output_index`
- `tests/test_openai_responses.py`：20 条断言，请求侧 8 条 + 响应侧 12 条
- 逐条对照四个适配器后补齐三处共性缺口：拒答内容转成文本块并把 `stop_reason` 记为
  `filtered`（D18）、`is_error` 在两种 OpenAI 形状里加 `Error: ` 前缀（D19）、
  `openai_compat` 补上多候选 `index` 过滤与流内 `{"error": ...}` 识别
- 测试去重：`openai_compat` 的 system / `max_tokens` / `temperature` 三对各合成一条，
  `anthropic` 的 `max_tokens` 一对合成一条；删掉 `fake` 里与"截断保留 raw"完全重叠的用例、
  `gemini` 里名字与断言矛盾的 `generationConfig` 用例（有用的断言并入相邻用例）。105 → 99
- 补三处能力：中断时重放工具骨架（D20）、拒答单独成块（`TextBlock.refusal`，D21）、
  `openai_responses` 解析 reasoning（D22）；`is_error` 从 `Error: ` 前缀改为 `{"error": ...}`（D19 修订）
- M9 回放框架：`tests/fixtures/*.sse` 存原始流，`tests/test_replay.py` 走
  「文件 → `parse_sse` → 适配器」的真实链路；先用一条手写样本跑通，等真实流回填。
  配套两个手动脚本：`scripts/record.py` 录制（key 只读环境变量）、`scripts/replay.py` 打印事件
- 思考强度：`base.py` 新增 `ReasoningLevel`（minimal / low / medium / high）与 `THINKING_BUDGETS`，
  `Request.reasoning` 默认 `None`；四个适配器各翻译一次——`reasoning_effort`（Chat Completions）、
  `thinking.budget_tokens` 并把 `max_tokens` 抬到预算之上（Messages）、`thinkingConfig`
  （generateContent）、`reasoning{effort, summary}`（Responses）（D23）
- M9 真实样本：用 `scripts/record.py` 录下 Kimi k2.6 的三种协议 × 三个场景（纯文本 / 单工具 /
  两个并行工具），共 9 条，`test_replay.py` 每条一个用例：
  `<protocol>_kimi_text.sse`、`_thinking_tool.sse`、`_parallel_tools.sse`
- 真实样本当场暴露一处差异：Kimi 把 `usage` 放在 `choices[0]` 里，适配器只读顶层导致用量全 0；
  改为顶层没有时读 choice 内的，两处都有只认顶层（D24，P-09）
- M10 异步化：`Provider.stream()` 从同步迭代器改为 `AsyncIterator`，`post_sse` 改用
  `httpx.AsyncClient` + `async for aiter_lines()`，退避改用 `await asyncio.sleep`；
  `parse_sse` 保留同步版给 fixture，与异步版共用 `_SseParser` 状态机（D28）
- M11 交接三项：A 适配器可注入 client（完成，D29）、B Anthropic 显式缓存断点（形状完成，D30）、
  C `prompt_cache_key` 透传（待办，等会话 id，见「未覆盖」）
- 环境：`Gradus/pyproject.toml` + `uv.lock`，Python 3.13，运行依赖 httpx，dev 依赖只有 pytest
- `README.md` / `README.zh.md`：对外入口（定位、快速开始、数据模型、目录与职责表），两份互链
- `docs/design.md`（中文）：逐文件实现说明，从 README 拆出

**验证结果**
- 多轮剧本：`content_index` 为 0/1/2，第二轮重新从 0 起算
- 快照独立性：`TextStart.partial` 为空串，`TextEnd.partial` 为完整文本
- 截断：`finish: length` 且 `arguments == {}`
- 中断：`StreamError(aborted)`，`partial` 保留已产出内容
- 自动补结束：工具块未显式关闭时，`done()` 先发 `ToolCallEnd` 再发 `StreamDone`
- 以上五条已固化为 `tests/test_fake.py` 的 7 个用例
- SSE 解析：空行分记录、多行 `data:` 拼接、`:` 注释与未知字段忽略、CRLF、
  空 data 交付、无 data 记录跳过、`[DONE]` 原样、冒号后只剥一个空格
- 传输：正常流派、429 后重试成功（2 次调用）、401 不重试、连接失败重试用尽（3 次调用）、
  非 UTF-8 字节不抛异常
- `uv run python -m gradus.ai.tests.adapter`：126 passed
  （anthropic 23 + fake 7 + gemini 20 + openai_compat 30 + openai_responses 21 +
  replay 10 + transport 14 + factory 1）
- 缓存断点：system / 最后一个工具 / 最后一条消息各一个，`thinking` 跳过，总数 3；
  离线只验证形状，真实命中率待官方 key
- client 注入：四种协议连续两次 `stream()` 都走注入的 client，结束后未被关闭；不注入时行为不变
- 思考强度：`reasoning=None` 时四个请求体都不出现相关字段；`high` / `low` / `medium` 分别落到
  各自的厂商形状，且 `anthropic` 的 `max_tokens` 大于 `budget_tokens`
- 回放链路：`replay` 打印出 `ThinkingStart` → 多片 `ThinkingDelta` → `ThinkingEnd` →
  工具三段 → `StreamDone(tool_use)`；修复后 `usage` 为 65 / 44（缓存命中 65），不再是 0

**未覆盖**（按「谁来补 / 何时能验证」记，避免下次重新推一遍）

`ai` 层：

- **Anthropic 缓存断点已打，但真实命中率未验证**：三处断点（system / 最后一个工具 /
  最后一条消息的最后一个可缓存块）已实现（D30），离线只验证了形状。还缺两件事：
  ① 用真实 Anthropic key 确认官方端点命中（`cache_read_input_tokens > 0`）；
  ② 用 Kimi 的 Anthropic 兼容口冒烟，确认它接受 `system` 块数组与 `cache_control`
  （不认的话会回 400，需要按端点回退到字符串 system）。另外注意最小可缓存长度
  （Sonnet 4.5 为 1024 token，Sonnet 4.6 / Haiku 为 2048），短会话命中不了属正常
- **`prompt_cache_key` 透传（交接任务 C，未做）**：计划给 `Request` 加
  `prompt_cache_key: str | None = None`，`openai_compat` 与 `openai_responses` 在非空时写入
  请求体，Anthropic / Gemini 不写。**前置条件是稳定会话 id**——`agent` 层还没定怎么生成，
  现在发常量会让所有用户撞一个桶、发随机值等于没有，所以等会话 id 落地后再做。
  届时的兜底：DeepSeek / Kimi 兼容口若因未知字段回 400，改为按 `ModelSpec` 开关控制，
  并把这条偏差记进 D 表，不要静默删需求。长保留 `prompt_cache_retention: "24h"` 同批处理
- `openai_responses` 的 reasoning item 已能解析成 `ThinkingBlock`，但**回传**（续推理）仍缺真实样本；
  录到的 Kimi 流里也没有 `encrypted_content`，`signature` 一直是 `None`（D22）
- `gemini` 真实流待录（缺 key；`--url` 要带模型名），官方 Anthropic / OpenAI 端点也尚未验证
- 一条 assistant 消息里出现多个 `output_text` part 时是否该拆成多个 `TextBlock`，缺真实样本

`agent` 层（不属于本层，列在这里免得上层重推）：

- 上下文压缩：阈值触发 → 摘要旧消息 → 保留近期原文（Pi 的做法见
  `pi-agent/docs/concepts/context.md`）
- 缓存命中率统计与展示：`Usage` 已有 `cache_read_tokens`，缺消费者
- 按模型维护「认不认 `reasoning_effort` 这类字段」：现在 400 只上报、不降级（D23）

## 偏差记录

| 编号 | 原规划 | 实际 | 处理 |
| --- | --- | --- | --- |
| D1 | `partial` 的累积逻辑待定抽不抽公共类 | 抽出 `stream_assembler.py`，`fake.py` 已改用它 | 已决：四家适配器直接复用，不再各写一份 |
| D2 | 适配器构造参数为单个 `script` | 改为 `scripts` 列表，一份剧本一次调用 | 保留：多轮（先 `tool_use` 再 `stop`）必须有此能力 |
| D3 | `StopReason` 六个值 | 增加 `filtered`，共七个 | 保留：`content_filter` / `refusal` / `SAFETY` 无处可归 |
| D4 | 计划留 `Provider.on_event` 钩子用于 trace | 未加，`base.py` 目前只有 `stream()` | 待决：trace 是调试功能，可在 M5 之后再加，不阻塞 |
| D5 | 建包时创建 `__init__.py` | 用户删除，现依赖 namespace package | 保留：import 已验证可用，不重建 |
| D6 | 曾把骨架同时建到 `agent/`、`coding/` | 已删除，仅保留 `ai/` | 已纠正：本层只改 `ai/` |
| D7 | 文件名 `event_builder.py` / `EventBuilder` | 改为 `stream_assembler.py` / `StreamAssembler` | 已改：原名字不体现"把增量流装配成事件"的含义 |
| D8 | 计划未安排自动化测试，验证靠一次性脚本 | 在 M3 之后插入 `tests/test_fake.py` | 已补：用例用 unittest 写法，pytest 只作运行器 |
| D9 | 环境未指定，靠系统 `python` | 改用 uv：`pyproject.toml` + `uv.lock` + `.venv`，锁 Python 3.13 | 保留：`uv run pytest` 已验证，后续命令统一走 `uv run` |
| D10 | M4 的 HTTP 客户端未指定，候选 `urllib` 与 `httpx` | 选 `httpx`：分阶段超时、连接池、可注入 `MockTransport` | 保留：代价是多一个运行依赖，换来传输层可完全离线测试 |
| D11 | 计划未写明 SSE 重试的边界 | 只在「尚未吐出任何事件」时重试，且只对 408/409/425/429/5xx 与连接/超时失败 | 保留：避免半截内容叠加；流中断直接归一为 `TransportFailure` |
| D12 | 数据模型存在重复入口与死代码：`SystemMessage` 与 `Request.system` 双入口、`Request.model` 与 `ModelSpec.model` 重叠、`TransportFailure.kind` 恒为 `"error"`、assembler 有 `message` / `usage` / `open_block` 三个无人使用的 property | 删 `SystemMessage`（system 只走 `Request.system`）、删 `Request.model`、删 `TransportFailure.kind`、删那三个 property；四个适配器注释的映射行改为 `Request.system` | 已改：`max_output_tokens` 保留（本次覆盖值 vs 模型上限，语义不同）；`uv run pytest` 20 passed |
| D13 | 测试只有 `uv run pytest` 一条路径，没有统一入口 | 新增 `tests/adapter.py`：不写断言、不导入 gradus 模块，按 `MODULES` 把各 `test_*.py` 的用例装成 `TestSuite`，`uv run python -m gradus.ai.tests.adapter` 一次跑完 | 保留：新增测试文件只需加一行模块路径；不匹配 `test_*.py` 收集规则，两条命令不重复执行 |
| D14 | `ModelSpec.api_key_env` 让适配层看起来该自己去读环境变量，与本层"只做协议翻译"的边界冲突 | 删 `api_key_env`；改为 Provider 构造时传 `api_key: str`（默认空串 = 无需鉴权，如本地 Ollama）；四个适配器注释里的 `<XXX_API_KEY>` 改为 `<api_key>` | 保留：对齐 Pi 的分法（auth 解析在上层，适配层只收凭证值）。代价：Provider 从无状态配置变成带凭证的实例，换 key 要新建实例；当前单 key 场景无影响 |
| D15 | 三种块应同等对待地三阶段流式 | 工具调用在适配器里无条件缓冲到流结束再重放：`ToolCallDelta` 只出现一次且内容完整 | 保留：`assembler` 同一时刻只能开一个块，而厂商会用 `index` 交错给出并行调用；"会不会交错"在流式协议里无法提前判断，事件也无法回滚，只能选保守。触发改动条件是"需要工具参数实时预览"，届时要把 `_open` / `_index` / `_tool_raw` 三个单值改成按 index 的字典，并同步改所有消费方的事件累加写法 |
| D15-a | 同上 | `fake` 与 `anthropic` 实时分片发 `ToolCallDelta`，`openai_compat` 末尾一次发；三者最终消息相同但事件时序不同 | 保留：`anthropic` 的块严格串行、`fake` 的剧本天然顺序，都不需要缓冲。若将来有测试或 UI 依赖 delta 出现在流中间，`openai_compat` 会有行为差异 |
| D15-b | 同上 | 流中断时，已产出的文本块保留在 `partial` 里，`openai_compat` 未完成的工具调用**完全丢弃**（`pending` 在适配器，从没进过 assembler） | 保留：半截 JSON 无法使用，丢弃比留下更好的；但与文本块的行为不一致 |
| D16 | `partial` 对工具块没有进度信息（`arguments` 全程为 `{}`），中断时无法区分"无参数工具"与"参数被截断" | 给 `ToolCallBlock` 加 `raw: str`：`tool_arguments` 每片同步到块上，`end_tool` 在没走过分片时用 `json.dumps` 补出 | 保留：`raw` 是厂商原文、`arguments` 是解析结果，end 之后两者一致；回传厂商一律用 `arguments`，`raw` 只用于观察与诊断 |
| D17 | `openai_responses` 原本跟 `anthropic` / `fake` 一样实时发工具事件：收到 `output_item.added` 就 `start_tool`，收到参数 delta 就 `tool_arguments` | 该协议的并行调用是多个平级 item，参数事件按 `item_id` 交错到达；`assembler` 开新块会关旧块，第一个调用被以 `{}` 提前关闭，之后它的参数全部丢失（两个并列调用的参数都变成空 dict）。改为与 `openai_compat` 相同的缓冲：按 `item.id` 存槽位，`function_call_arguments.done` 与 `output_item.done` 的完整 JSON 覆盖，流结束后按槽位顺序重放 | 保留：D15 的结论适用于这一家。代价是工具事件都出现在文本之后；好处是 `ToolCallDelta` 只有一条且内容完整，且能直接用上游给好的完整 JSON，不必自己拼 |
| D18 | 拒答内容没有对接，两家 OpenAI 形状协议把拒答放在独立字段里（不在正文） | `openai_compat` 读 `delta.refusal`，`openai_responses` 读 `refusal` part 与 `response.refusal.delta`；两者都把拒答当文本块发出去，同时把 `stop_reason` 记成 `filtered` | 保留：不吞掉用户该看到的话，也不让上层把拒答当成正常回答。拒答与正文的区分后来由 D21 的 `refusal` 字段补上 |
| D19 | `ToolResultMessage.is_error` 在两种 OpenAI 形状里无处映射，协议没有错误位 | 错误结果包成 `{"error": ...}`，成功结果保持原文；对照：`anthropic` 用原生 `is_error`，`gemini` 用 `{"error": ...}` | 保留：模型看到 JSON 对象就能区分失败，也不影响成功结果的原文。代价是失败结果的形态与成功不对称（一次修订：原实现是 `Error: ` 前缀，无法与本身以该前缀开头的输出区分） |
| D20 | 中断时工具调用直接丢弃（D15-b） | 改为在 `fail()` 之前把 `pending` 里已有 `name` 的槽位重放成骨架：`start_tool` + 参数原文 + `end_tool`，`arguments` 为空 dict，是否执行由 `stop_reason` 决定 | 保留：`partial` 能看到"正在调用的工具"，代价是出现 `arguments == {}` 的块，必须靠 `stop_reason` 拒绝执行 |
| D21 | 拒答内容与正文无法区分 | 给 `TextBlock` 加 `refusal: bool = False`，`assembler.start_text(refusal=...)` 只改块上的标记，事件形状与数量不变 | 保留：比新增 `RefusalBlock`（要加 3 个事件）侵入性小得多 |
| D22 | `openai_responses` 不解析 `reasoning` item | 接入 `reasoning_summary_text.delta` / `reasoning_text.delta` → `thinking()`，`output_item.done` 时把 `encrypted_content` 存进 `ThinkingBlock.signature`；**暂不把 reasoning item 回传厂商** | 保留：回传形状缺真实样本，写错会 400，等 M9 |
| D23 | 未规划思考强度，上层无法让模型"少想"或"多想" | 沿用 Pi 的分层：`ai` 只做翻译。`Request.reasoning` 取四档（minimal / low / medium / high），预算表 `THINKING_BUDGETS` 抄 Pi 的 `DEFAULT_THINKING_BUDGETS`（1024 / 2048 / 8192 / 16384）；四家分别翻成 `reasoning_effort`、`thinking{budget_tokens}`、`thinkingConfig{thinkingBudget, includeThoughts}`、`reasoning{effort, summary}` | 保留：档位比 Pi 少两档（xhigh / max），够区分"少想/多想"即可，加档只是改表。两处代价：① 厂商支不支持某一档由厂商自己决定，本层不查模型目录，不认的会 400，所以只有显式设置时才发；② `anthropic` 要求预算小于 `max_tokens`，适配器把 `max_tokens` 抬到 `budget + 1024`，等于由档位隐含改写了输出上限 |
| D24 | 按 OpenAI 文档，`usage` 只在顶层出现（`stream_options.include_usage` 打开时） | Kimi k2.6 把 `usage` 放进 `choices[0]`，与 `finish_reason` 同一个 chunk。改为顶层没有时读 choice 内的，两处都有只认顶层，避免 `Usage.__add__` 重复累加 | 保留：多一个兜底分支，代价是"用量可能在两个位置"这条事实必须写进注释，不能让后来者以为只有顶层 |
| D25 | `Usage` 的字段假设四家语义一致 | 真实流对比同一条 prompt：`openai_compat` 报 `in=18 cache=0`，`openai_responses` 报 `in=18 cache=18`（`prompt_tokens` 含缓存，`cached_tokens` 是子集），`anthropic` 报 `in=0 cache=18`（`input_tokens` 不含缓存，相加才是总输入） | 保留②：适配器按**协议**归一化，出口统一成「总输入」。OpenAI 系原样即对，只有 `anthropic` 要加 `input + cache_read + cache_write`（`with_total_input`，只包在 `set_usage` 外层，不能进 `merge_usage`，否则累计值会重复加）。否掉①（保持原义）的原因：上层要算总输入必须按协议分支，等于把厂商知识漏到 `agent` 层；否掉「按 model 配置开关」的原因：账法是协议事实，写进 `ModelSpec` 就是每个模型条目都要维护、写错还静默。残留歧义：`cache_read_tokens == 0` 无法区分「真没命中」与「厂商不报」 |
| D26 | `add_usage` 一律累加，默认各事件报的是增量 | Anthropic 的 `message_delta` 报的是"截至目前的累计值"：官方只重复 `output_tokens`，Kimi 会把完整 usage 再报一遍，累加后 input / cache 翻倍、output 多 1。新增 `assembler.set_usage`（整体替换），`anthropic` 用 `merge_usage` 逐字段覆盖 | 保留：只改 `anthropic`，其余三家仍用 `add_usage`（用量只出现一次或本身就是分段增量）。代价是合并策略按协议不同，注释必须写明，否则下一个人会以为是统一规则 |
| D27 | D5：不建 `__init__.py`，依赖 namespace package | 新增 `providers/__init__.py`，提供 `create_provider(spec, api_key)` 与 `CLASSES` 映射表：否则上层要按 `spec.provider` 分四路 import 具体类，新增一家得改两处 | 保留（D5 的例外，仅此一个 `__init__.py`）：映射表只留一份。位置只能在 `providers/` 下——依赖方向是 `providers -> base`，写进 `base.py` 会成环。`fake` 不进工厂，它按剧本构造 `(spec, scripts)` |
| D28 | 整条链路是同步迭代器 | 改为异步：`Provider.stream()` 返回 `AsyncIterator`，`post_sse` 用 `httpx.AsyncClient`，退避用 `await asyncio.sleep`；`CancelledError` 是唯一允许穿透的异常 | 保留：用户目标是多用户，等 `agent` 写完再改等于重写。一次性改到底、不留同步分支。`parse_sse` 只服务 fixture 与回放，仍同步；两个入口共用 `_SseParser`，切分规则只有一份。`client` 可注入且由调用方关闭，但适配器还没有接收 client 的入口，等 `agent` 有会话生命周期时再接（见「未覆盖」）。代价：工具执行器、UI 消费端都必须是 async；测试改成 `StreamTestCase`（`unittest.IsolatedAsyncioTestCase`）+ `async def test_*`，用 `consume()` 边收边断言、不先收集成列表，不引入 `pytest-asyncio` |
| D29 | 四个适配器只接 `(spec, api_key)`，每次 `stream()` 自建并关闭 `AsyncClient`；D28 却写了"上层能共享连接池"，实际没有入口 | `create_provider(spec, api_key="", client=None)`；四个适配器构造函数同样加可选 `client`，`stream()` 透传给 `post_sse` | 保留：注入的 client 由调用方关闭，`None` 时保持原行为（`post_sse` 自建并 `aclose`）。`httpx` 只出现在 `providers/*` 与 `transport.py`，`base.py` 不 import。新增 `test_provider_factory.py`：四种协议各注入一个 `MockTransport` client，连续两次 `stream()` 都命中它，结束后 `is_closed` 仍为 `False` |
| D30 | Anthropic 从未发 `cache_control`，官方端点命中率恒为 0（Kimi 兼容口自动缓存掩盖了问题） | `system` 改成 text 块数组并在末尾打点；最后一个 tool 打点；最后一条消息的最后一个可缓存块打点 | 保留：前缀按 tools → system → messages 累积，三处都打在段末，最多 3 个（官方上限 4）。可显式标记的类型是 text / tool_use / tool_result；`thinking` 不能带，遇到往回找（交接里担心的 `tool_result` 实际是支持的）。最小可缓存长度 1024（Sonnet 4.5）/ 2048（Sonnet 4.6、Haiku），短会话不命中属正常。真实命中率待官方 key；Kimi 兼容口是否接受块数组待冒烟，不认则按端点回退字符串 system |

## 下一轮检查时要做的事

1. 逐条核对上表 D1 / D4 是否已决
2. 确认没有代码越界到 `agent/`、`coding/`
3. 确认 `types.py` 仍未引入外部依赖
4. 新增偏差，无论大小，都写进「偏差记录」而不是静默改掉
