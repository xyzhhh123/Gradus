# `ai` 层实现说明

[返回 README](../README.zh.md)

README 讲这一层对外是什么形状；这里讲每个文件内部怎么做的：各协议的字段映射、事件分发、
边界行为与已知取舍。只在改对应文件时才需要读。

## 逐文件说明

### `types.py`

即 README「数据模型」一节那套内部协议。除标准库外不 import 任何东西。
`stop_reason` 七个值各自对应的上层动作，写在源码 `StopReason` 上方的注释里，改一处即可。

### `base.py`

请求侧类型与 `Provider` 协议。没有实现。

### `stream_assembler.py`

四个适配器共用。负责开关内容块、分配 `content_index`、为每个事件深拷贝 `partial`、
合并 `Usage`，并在 `done()` / `fail()` 发出终止事件前补上未关闭块的 End 事件。
适配器只负责把厂商 SSE 翻译成对 assembler 的调用。

### `transport.py`

网络与解析的边界，不涉及任何厂商语义。两个公开入口：

| 入口 | 说明 |
| --- | --- |
| `parse_sse(lines) -> Iterator[SseEvent]` | 纯函数、同步。空行分记录、`:` 注释、多行 `data:` 用 `\n` 拼接、`[DONE]` 原样保留。可拿录制文本离线断言。 |
| `post_sse(url, headers=, payload=, client=) -> AsyncIterator[SseEvent \| TransportFailure]` | 异步流式 POST。除 `asyncio.CancelledError` 外不抛异常；429 / 5xx / 连接失败按指数退避重试，且只在「尚未吐出任何事件」时重试，避免内容重复。 |

两个入口共用内部状态机 `_SseParser`：同步 `parse_sse` 与 `_parse_sse_async` 只是喂数据的方式不同，
切分规则只有一份。

`SseEvent` 只有 `event` 与 `data`；`TransportFailure` 只有 `message` 与 `status_code`。
把它变成 `StreamError` 是适配器的事。

`client` 可注入：测试用 `httpx.MockTransport` 就能完全离线；上层注入一个长期存活的
`httpx.AsyncClient` 即可共享连接池，注入的 client 由调用方关闭。不注入时每次 `post_sse`
自建一个并在结束时 `aclose()`。明确不做：代理认证、HTTP/2 调优、按厂商分级重试。

### `envfile.py`

一个函数：`load_env_file(path)` 把 `.env` 里的键写进 `os.environ`。规则是 **文件优先**
（同名变量被文件覆盖）、空值跳过、去两侧引号、按 `utf-8-sig` 读（兼容 BOM）、不展开变量、
不处理多行值。

放在 `ai` 而不是 `coding`：调用方是 `scripts/record.py`（在 `ai`）与 coding CLI，而依赖方向
只允许 `coding -> ai`；放 `coding` 会让 `ai` 反向依赖。本模块不碰网络、不碰协议。

### `services.py`

一张表 `SERVICES`：服务商名 -> `(协议, base_url)`。`coding` 用它把配置里的 `service`
展开成 `provider` + `base_url`；`scripts/record.py` 用它拼录制端点（`base_url` + 协议路径）。
同名的服务商出现在多个协议下时带协议后缀（`kimi-compat` / `kimi-anthropic` /
`kimi-responses`），避免「一家平台」的误解。`gemini` 的端点里带模型名，不在这张表里。

放在 `ai` 的理由与 `envfile.py` 相同：`record.py` 在 `ai` 内，依赖方向只允许
`coding -> ai`，两边共用一张表才不会各写一份。

### `providers/__init__.py`

上层唯一的构造入口 `create_provider(spec, api_key, client=None) -> Provider`：按 `spec.provider`
在 `CLASSES` 里选实现。映射表只留这一份，新增一家改一行。工厂放这里而不是 `base.py`
是因为依赖方向 `providers -> base`，写进 `base` 会成环；也不代表上层该 import 具体类。

`client` 用来复用连接池：注入一个长活的 `httpx.AsyncClient`，多次 `stream()` 共用同一批连接。
**所有权在注入方**——本层不会 `aclose()` 它；`None` 时每次 `stream()` 自建并在结束时关闭（D29）。

`fake` 不走工厂——它按剧本构造，签名是 `(spec, scripts)`，只在测试里直接实例化。

### `providers/openai_compat.py`

`POST /chat/completions` 形状。system 放 `messages[0]`，工具参数是按 `index` 分片的
JSON 字符串，工具结果用 `role="tool"`，推理内容走 `reasoning_content`。

请求侧是三个纯函数：

| 函数 | 作用 |
| --- | --- |
| `build_payload(request, spec)` | 整体请求体；`stream` / `stream_options` 固定打开，`tools` 为空时不发 |
| `build_messages(request)` | `system` 落在 `messages[0]`，三种 `Message` 各翻一次 |
| `build_assistant(message)` | 多个 `TextBlock` 合成一个字符串；带工具调用时 `content` 给 `null`；`ThinkingBlock` 不回传 |

`Request.reasoning` 翻成 `reasoning_effort`，且只在设置了档位时才发 —— 不认这个字段的兼容厂商会回 400。

响应侧是 `stream()`，链路为 `post_sse` → `json.loads` → 分发到 assembler：

| 厂商字段 | 去向 |
| --- | --- |
| `delta.content` | `assembler.text()` |
| `delta.reasoning_content` | `assembler.thinking()` |
| `delta.refusal` | 先关正文块，再开一个 `refusal=True` 的文本块；`stop_reason` 记成 `filtered` |
| `delta.tool_calls[].index` | 按 index 缓冲，流结束后依次开块、喂参数、收尾 |
| `choices[].index != 0` | 直接跳过，避免多候选串进一条消息 |
| `finish_reason` | `StopReason`（`tool_calls` → `tool_use`，`content_filter` → `filtered`） |
| `usage` | `Usage`，`prompt_tokens_details.cached_tokens` 计入 `cache_read_tokens` |
| `choices[].usage` | 同样的映射，仅当顶层没有时采用（Kimi 把它放在这里） |
| 流内 `{"error": ...}` | `StreamError`（部分兼容端点会在 200 里发错误对象） |

**工具调用为什么攒到最后发**：`assembler` 同一时刻只能开一个块（`_open` 是单值），而厂商会用 `index` 交错给出多个并行调用。参数本来也要拼完才能用，所以先按 index 建槽位收碎片，流结束再按顺序发 `ToolCallStart` / `ToolCallDelta` / `ToolCallEnd`——因此每个工具调用只产出一个 `ToolCallDelta`，内容已是完整 JSON（与 `openai_responses` 相同，见 D15 / D17）。

工具结果本协议没有 `is_error` 字段，错误结果包成 `{"error": ...}`，与成功结果的原文区分开（D19）。
`TransportFailure`、坏 JSON、非对象 JSON（如 `[]`）、缺字段一律变成 `StreamError`，不抛异常；中断前会把已攒下的工具补发成骨架，`partial` 里能看到调用了什么。

### `providers/anthropic.py`

`POST /v1/messages` 形状。`system` 是顶层字段，角色必须交替，工具结果包成 `tool_result`
block 放进 user 消息，工具参数是对象，thinking 块带需要回传的 `signature`。

**缓存断点**：官方要求显式 `cache_control` 才写缓存，否则命中率恒为 0。前缀按
`tools -> system -> messages` 累积，所以三处都打在各自末尾：system 文本块、最后一个工具、
最后一条消息的最后一个可缓存块（最多 3 个，上限 4）。可显式标记的类型是
`text` / `tool_use` / `tool_result`，`thinking` 不能带，遇到就往前找（D30）。
注意最小可缓存长度（Sonnet 4.5 为 1024 token，Sonnet 4.6 / Haiku 为 2048），
短会话打了点也不会命中；官方端点的真实命中率尚未验证。

请求侧的四个纯函数：

| 函数 | 作用 |
| --- | --- |
| `build_payload(request, spec)` | `system` 放顶层（text 块数组 + `cache_control`）；`max_tokens` 必填；`tool_choice` 是 `{"type": "auto"}` 对象 |
| `build_messages(request)` | 相邻同角色合并，保证 `user` / `assistant` 严格交替 |
| `build_assistant_blocks(message)` | 块顺序保留，`ThinkingBlock.signature` 原样回传，工具调用转 `tool_use` + `input` 对象 |
| `build_tool(tool)` | 用 `input_schema`，不是 `parameters` |

`Request.reasoning` 按 `THINKING_BUDGETS` 翻成 `thinking {type: "enabled", budget_tokens}`，同时把
`max_tokens` 抬到 `budget + 1024` —— 预算不小于 `max_tokens` 时请求会被直接拒掉。

响应侧按事件名分发，六个分支：

| 事件 | 处理 |
| --- | --- |
| `message_start` | 取 input 用量（含两个 cache 字段）；出口把"未缓存输入 + 缓存读 + 缓存写"合成总输入 |
| `content_block_start` | 按块类型开文本 / 思考 / 工具块 |
| `content_block_delta` | `text_delta` / `thinking_delta` / `signature_delta`（累积）/ `input_json_delta` |
| `content_block_stop` | 三个 `end_*` 连调，靠幂等让当前块生效 |
| `message_delta` | 取 `stop_reason` 与累计用量，逐字段覆盖：官方只重复 `output_tokens`，兼容端点会整份再报一遍 |
| `ping` / `error` | 忽略 / 转 `StreamError` |

和 `openai_compat` 的关键差异：**本厂商的块是严格串行的**（`content_block_start/stop` 成对、`index` 递增），所以工具调用不需要缓冲，可以实时发 `ToolCallDelta`。

发请求前还有一道本地校验：`messages` 的第一条必须是 `user`（`ToolResultMessage` 也算，它映射为 user）。以 `assistant` 开头会直接变成 `StreamError`，不发请求 —— 否则只能拿到一句笼统的 HTTP 400。

### `providers/gemini.py`

`streamGenerateContent` 形状。`contents`/`parts` 结构，只有 `user` 和 `model` 两种角色；
`functionCall` 在一个 chunk 里完整到达，start/end 事件由适配器合成；`functionResponse`
靠 name 匹配，没有 call id。

请求侧的四个纯函数：

| 函数 | 作用 |
| --- | --- |
| `build_payload(request, spec)` | `system` → `systemInstruction`；`temperature` / `maxOutputTokens` 进 `generationConfig` |
| `build_contents(request)` | 角色只有 `user` / `model`；相邻同角色合并；先从历史建 `id -> name` 表，工具结果按 name 回填 |
| `build_model_parts(message)` | `TextBlock` → `{"text"}`；`ThinkingBlock` → `{"text", "thought": true}`；`ToolCallBlock` → `{"functionCall": {"name", "args"}}` |
| `build_tool(tool)` | 包一层 `{"functionDeclarations": [...]}` |

`Request.reasoning` 翻成 `generationConfig.thinkingConfig {thinkingBudget, includeThoughts: true}`，
带上 `includeThoughts` 之后思考内容才会以 `thought: true` 的 part 返回。

响应侧的特点：

| 情况 | 处理 |
| --- | --- |
| `parts[].text` | 增量喂 `assembler.text()` |
| `parts[]` 带 `thought: true` | 喂 `assembler.thinking()`，`thoughtSignature` 记下来给 `end_thinking` |
| `parts[].functionCall` | **一次到齐**，适配器现场合成 `start_tool` → `tool_arguments(json.dumps)` → `end_tool` |
| 没有 call id | 合成 `call_{n}` |
| 纯工具调用无 `finishReason` | 按内容判定 `stop_reason = "tool_use"`，但只在 `stop_reason` 仍是 `stop` 时生效，截断/安全拦截优先 |
| 多条候选 | 只处理 `index == 0`，避免多条候选串进同一条消息 |
| `promptFeedback.blockReason` | `done("filtered")`，不是 `StreamError`（不是失败，是没给答案） |
| `MALFORMED_FUNCTION_CALL` | `StreamError("error")` |

`thoughtsTokenCount` 计入 `output_tokens`，不单列。

### `providers/openai_responses.py`

Responses API，与 Chat Completions 是两套协议，不是换个 `base_url` 就能切。`input` 是
item 数组，消息与工具调用平级；三层事件流（item / part / delta）要折叠成内部协议的两层。

请求侧的三个纯函数：

| 函数 | 作用 |
| --- | --- |
| `build_payload(request, spec)` | `system` → `instructions`；`store` 恒为 `false`（不用服务端会话） |
| `build_input(request)` | 一条 `AssistantMessage` 会被拆成「一个 message item + N 个 `function_call`」，因为工具调用与消息平级 |
| `build_tool(tool)` | 扁平结构，没有 `function` 包一层 |

`Request.reasoning` 翻成 `reasoning {effort, summary: "auto"}`；正是 `summary: auto` 让上游返回
本适配器读取的 `reasoning_summary_text` 增量。

响应侧把三层折叠成两层：

| 厂商事件 | 内部动作 |
| --- | --- |
| `response.output_item.added`（`function_call`） | 按 `item.id` 建槽位，不产出事件 |
| `response.content_part.added`（`output_text`） | `start_text()` |
| `response.output_text.delta` | `text(delta)` |
| `response.content_part.added`（`refusal`）/ `response.refusal.delta` | 单独开一个 `refusal=True` 的文本块，`stop_reason` 记成 `filtered` |
| `response.reasoning_summary_text.delta` / `reasoning_text.delta` | `thinking(delta)` |
| `response.output_item.done`（`reasoning`） | `end_thinking(encrypted_content)`，存进 `signature` |
| `response.content_part.done` | `end_text()` |
| `response.function_call_arguments.delta` | 按 `item_id` 追加到槽位 |
| `response.function_call_arguments.done` | 用上游给的完整 `arguments` 覆盖槽位 |
| `response.output_item.done` | 工具 item 用完整 `arguments` 覆盖槽位 |
| 流结束后 | 按槽位顺序重放三段工具事件 |
| `response.completed` / `response.incomplete` | 取 `usage`、判 `length` / `filtered` |
| `response.failed` / `error` | `StreamError` |

折叠的核心是**丢掉 `output_index` 这一层**：内部协议只有一个块在长，不需要知道"这是第几个 item"。

工具调用必须缓冲：并行调用是多个平级 item，参数事件按 `item_id` 交替到达，而
`assembler` 同一时刻只能开一个块。流式期间只写 `pending` 字典，结束后按顺序重放
`start_tool` / `tool_arguments` / `end_tool`，所以 `ToolCallDelta` 只出现一次且内容完整
（与 `openai_compat` 相同，见 D15 / D17）。

`reasoning` item 的思考内容会解析成 `ThinkingBlock`，`encrypted_content` 存进 `signature`；
但暂时不把 reasoning item 回传厂商，回传形状等 M9 用真实样本确认。

工具结果同样没有 `is_error` 字段，错误结果包成 `{"error": ...}`（D19）。中断时已攒下的调用会补发成骨架，`partial` 里能看到调用了什么。

### `providers/fake.py`

不是厂商。按剧本直接产出内部事件，不联网。有了它，12 个事件、`length` 截断、`aborted`
中断这些路径才能离线断言。真实厂商解析的正确性另由录制的 SSE 回放样本覆盖。

### `tests/test_anthropic.py`

23 条断言，请求侧 10 条 + 响应侧 13 条。

- 请求侧：`system` 在顶层且是带 `cache_control` 的块数组、空 system 省略、`max_tokens` 回退、
  `input_schema`、`tool_choice` 是对象、**连续工具结果合并成一条 user**、块顺序与 `signature` 回传、
  `reasoning` → thinking 预算且 `max_tokens` 高于预算、**三处缓存断点**（system / 最后一个工具 /
  最后一条消息）、`tool_result` 可打点、`thinking` 跳过、断点总数不超过 3
- 响应侧：文本流、用量跨两个事件合并且不重复累加、thinking + `signature_delta`、
  `input_json_delta` 拼参数、`ping` 忽略、`max_tokens` → `length`、`error` 事件、
  `TransportFailure`、URL 与 `x-api-key`、**首条不是 user 时本地拦截**、
  `ToolResultMessage` 开头合法、非对象 JSON 收敛成 `StreamError`

### `tests/test_fake.py`

把上面这些路径固化成可复跑的断言。用例仍用 `unittest` 的类写法，由 `pytest` 负责运行；
凡是要消费 `stream()` 的用例都继承 `tests/support.py` 的 `StreamTestCase`：
`async def test_*` 里用 `await self.consume(...)` 边收边断言，不把整条流先收成列表，
事件的数量与先后顺序因此也在断言范围内。
覆盖：完整事件序列与 `content_index`、`partial` 快照独立性、工具参数 `raw` 随分片增长、
参数截断留空 dict 但保留 `raw`、中断保留内容、多剧本按序消费、用量累加。

```bash
uv run pytest
```

### `tests/test_gemini.py`

20 条断言，请求侧 7 条 + 响应侧 13 条。

- 请求侧：`systemInstruction`、角色只用 `user`/`model`、空 content 跳过、工具结果靠 name 匹配、
  `is_error` → `error` 键、`functionDeclarations`、`generationConfig`、
  `reasoning` → `thinkingConfig` 且带 `includeThoughts`
- 响应侧：文本流、`functionCall` 合成三段且 id 为 `call_0`、thought + signature、
  `MAX_TOKENS` → `length`、`SAFETY` → `filtered`、`blockReason` 走 `done("filtered")`、
  `MALFORMED_FUNCTION_CALL` → `StreamError`、thoughts 计入 output、URL 与 `x-goog-api-key`、
  多条候选只取 `index 0`、`MAX_TOKENS` 不被工具调用覆盖

### `tests/test_openai_compat.py`

30 条断言，请求侧 13 条 + 响应侧 17 条。

- `build_payload`：system 落位、空 system 省略、`stream` 恒开、`max_tokens` 回退到
  `ModelSpec`、`temperature` 为 `None` 时不出现、`reasoning_effort` 未设置时不出现、
  `tools` 为空时不发、`tools` 形状
- `build_assistant`：纯文本、多个文本块合并、纯工具调用时 `content` 为 `null`、
  `ThinkingBlock` 不回传
- `ToolResultMessage` → `role="tool"`，错误结果包成 `{"error": ...}`
- 响应侧：文本流、工具分片拼接、并行工具按 index 分开、reasoning、usage（顶层与
  `choices[0]` 两处都要认，且只算一次）、`length`、拒答 → `filtered` 且单独成块、
  中断保留工具骨架、多候选只取 `index 0`、流内 `error`、`TransportFailure`、坏 JSON、非对象 JSON、URL 与鉴权头

### `tests/test_openai_responses.py`

21 条断言，请求侧 8 条 + 响应侧 13 条。

- 请求侧：`instructions`、`store` 恒为 false、user item、助手消息拆成 message + `function_call`、
  `function_call_output`（错误结果包成 `{"error": ...}`）、扁平 tools、`reasoning` → `{effort, summary}`
- 响应侧：文本流、三层事件折叠成三段工具事件、**并行两个调用按 `item_id` 分开重放**、
  拒答 → `filtered` 且单独成块、reasoning → `ThinkingBlock` 带 `signature`、
  中断保留工具骨架、`incomplete` → `length`、`response.failed`、`error` 事件、
  usage 的 `cached_tokens`、`TransportFailure`、URL 与 `Bearer`

### `tests/test_provider_factory.py`

1 条用例，跑四次（四种协议各一次 `subTest`）：注入一个带 `httpx.MockTransport` 的
`httpx.AsyncClient`，连续两次 `stream()` 都应打到这个 client 的 handler 上，
且结束后 `client.is_closed` 仍为 `False`。这条钉住 D29 的两半——连接确实被复用、
所有权确实没被本层拿走。

### `tests/test_replay.py`

整段原始流的回放测试。`fixtures/*.sse` 存厂商响应原文，`replay_events()` 把文件逐行交给
`parse_sse()` 切成事件，再喂给适配器，所以走的是「文件 → SSE 解析 → 适配器」的真实链路，
只省掉网络。

与 `test_<provider>.py` 的分工：后者用**手写**的 `SseEvent` 列表逐条验证分支，粒度细、定位快；
本文件只验证「一整段真实流能被完整吃下去」。目前十条样本：`anthropic_text_tool.sse` 是照协议
手写的；其余九条是用 `scripts/record.py` 录的 Kimi k2.6 真实流——三种协议 × 三个场景：
`_text`（不给工具，纯文本回答）、`_thinking_tool`（单工具）、`_parallel_tools`（一次两个调用）。
真实样本整份覆盖、不要手改，钉住它的断言（例如 `usage` 为 65 / 44、缓存 65）
跟着重录一起改。

### `tests/test_transport.py`

SSE 解析的 9 条断言（单条记录、多行 data、注释与未知字段、CRLF、空 data、`[DONE]` 哨兵）
加上 `post_sse` 的 5 条：正常流派、429 后重试成功、401 不重试、连接失败重试用尽、
非 UTF-8 字节不抛异常。全部走 `httpx.MockTransport`，不碰网络。

### `tests/adapter.py`

测试总入口。不写断言、不导入任何 `gradus` 模块，只按 `MODULES` 的顺序把各 `test_*.py`
的用例装成一个 `TestSuite`。新增测试文件时，加一行模块路径即可。

| 命令 | 用途 |
| --- | --- |
| `uv run python -m gradus.ai.tests.adapter` | 一次跑完全部用例，逐条打印用例名 |
| `uv run pytest` | 按文件收集，跑的是同一批用例 |

`adapter.py` 不匹配 pytest 的 `test_*.py` 收集规则，所以两条命令不会互相重复执行。

### `scripts/record.py` / `scripts/replay.py`

两个手动工具，不进测试流程。

| 命令 | 用途 |
| --- | --- |
| `uv run python -m gradus.ai.scripts.record <provider> <body.json> [--out 路径]` | 按 provider 拼端点与鉴权头；key 先读 `.env`（默认 `./.env`，可用 `--env` 指定），文件里没有的再从环境变量兜底；`.env` 解析在 `gradus/ai/envfile.py`，与 coding CLI 共用同一份规则（文件优先、空值跳过）；输出默认命名为 `scripts/recorded/<协议>_<模型>_<时间戳>.sse`，也可用 `--out` 指定；边收边写 `.tmp`，成功后再改名，避免留下半截样本 |
| `uv run python -m gradus.ai.scripts.replay <provider> <fixture>` | 读 fixture → `parse_sse()` → 顶掉适配器的 `post_sse` → 打印 12 种事件与最终消息 |

`record` 的两个约定：请求体必须自己提供（且 `stream` 为 `true`），`scripts/bodies/` 里存着现成的，文件名与对应的 `--service` 同名（如 `kimi-compat.json`）；Gemini 必须用 `--url` 指定带模型名的完整地址。非 200、连接失败时打印原因并退出，不写文件。

录制产物落到 `scripts/recorded/`，命名是 `<协议>_<模型>_<时间戳>.sse`（模型名取自请求体，缺了就是 `unknown`）。用 `replay` 看的就是这份文件，事件没问题后再挪进 `tests/fixtures/` 并改名 `<协议>_<场景>.sse` —— 只有验证过的流才值得变成测试数据，而 fixture 用稳定名字，不带时间戳。

`--service` 是常用服务商的快捷名，只替换地址，协议与 key 名都不变；协议写错会直接报错。同一家出现在多个协议下时，每个名字都带协议后缀（`kimi-compat` / `kimi-anthropic` / `kimi-responses`），避免 `kimi` 这种看起来像"整个平台"其实只是一种形状的名字；只有一种协议的服务保持原名。

| 协议 | 可用的 `--service` |
| --- | --- |
| `openai_compat` | `deepseek`（默认）、`kimi-compat`、`zhipu-compat`、`qwen`、`siliconflow`、`ark` |
| `anthropic` | `anthropic`（默认）、`kimi-anthropic`、`zhipu-anthropic` |
| `openai_responses` | `openai`（默认）、`kimi-responses` |
| `gemini` | 无，只能 `--url` |

```powershell
uv run python -m gradus.ai.scripts.record openai_compat gradus/ai/scripts/bodies/kimi-compat.json --service kimi-compat
```

