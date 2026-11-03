# 本轮问题记录

范围：2026-10-29 起，对照四个适配器、审查契约与交接评审时发现并修复的问题。每条按「现象 / 触发条件 / 原因 / 解决」写（排查过程复杂的补一条「定位」，记下怎么从现象查到根因），代码细节以源码和 `ai_plan.md` 的偏差记录为准。

1. **P-01 并行 `function_call` 串块** — 2026-10-29
   - 现象：模型一次返回两个工具调用（比如同时读 `a.py` 和 `b.py`）时，最终消息里两个 `ToolCallBlock` 的 `arguments` 都是 `{}`，`raw` 是两段参数拼在一起的坏字符串。agent 看到 `stop_reason="tool_use"` 就去执行，两次都因缺 `path` 失败。
   - 触发条件：Responses 的并行调用是 `added(fc_1)` → `added(fc_2)` → `arguments.delta(fc_1)` → `arguments.delta(fc_2)` → … 按 `item_id` 交错；单个工具调用时顺序正常，看不出问题。
   - 原因：`assembler` 同一时刻只维护一个打开的块，`start_tool()` 会先关掉旧块；适配器又没按 `item_id` 过滤，任何到达的 delta 都会写到当前打开的块上。于是 `fc_1` 在 `added(fc_2)` 那一刻就被以空参数关闭，它后续的参数全部丢失，`fc_2` 收到的是两个调用混在一起的片段。
   - 解决：改成与 `openai_compat` 相同的缓冲。流式期间只按 `item.id` 写 `pending` 槽位，`function_call_arguments.done` 与 `output_item.done` 带的完整 JSON 直接覆盖；流结束后按槽位插入顺序重放 `start_tool` / `tool_arguments` / `end_tool`。新增 `test_parallel_function_calls_are_replayed_in_order`。代价是工具事件都出现在文本之后（D17）。

2. **P-02 拒答内容没对接** — 2026-10-29
   - 现象：模型拒答时，用户看到的是一条空消息 + `stop_reason="stop"`，拒答原话（"我不能帮你写这个"）完全丢失，上层以为模型什么都没说。
   - 触发条件：`openai_compat` 的 `delta.refusal`，Responses 的 `content_part`（`part.type == "refusal"`）与 `response.refusal.delta`。拒答文本不放在正文 `content` / `output_text` 里，所以原有的 `text()` 分支一次都不会被调用。
   - 原因：两家的分发链里都没有这两条分支，事件被 `elif` 链跳过，静默丢弃。
   - 解决：把拒答当文本块发出（用户该看到的话不能吞），并在 `stop_reason` 还是 `stop` 时记成 `filtered`；`length` / `content_filter` 更具体，不覆盖。两家各加一条测试（D18）。

3. **P-03 `is_error` 丢失** — 2026-10-29
   - 现象：工具执行失败（比如 `FileNotFoundError`）时，模型收到的工具结果与"工具成功返回了这段文字"完全一样，可能继续按成功路径往下推理。
   - 原因：Chat Completions 与 Responses 的协议里都没有工具结果的错误位，`build_messages` / `build_input` 只取了 `content`。对照之下 `anthropic` 用原生 `is_error` 字段、`gemini` 用 `{"error": ...}` 包一层，这两家漏了，属于四个适配器没有逐项对齐。
   - 解决：错误结果包成 `{"error": ...}`，成功结果保持原文，两个文件写法一致。这样模型看到 JSON 对象就能判定失败，也不怕工具输出本身以 `Error: ` 开头。各加一条请求侧测试（D19）。

4. **P-04 多候选没按 `index` 过滤** — 2026-10-29
   - 现象：服务端一次返回多条 `choices` 时，所有候选的文本、工具调用会混进同一条 `AssistantMessage`。
   - 原因：`openai_compat` 直接遍历 `chunk.get("choices")`；`gemini` 有 `candidate.get("index", 0) != 0` 的过滤，这里没有，两个适配器行为不一致。
   - 解决：加同一条 `index != 0` 就 `continue` 的过滤，并补一条用例钉住；正常情况（我们不传 `n`）只有一条候选，属于未设防而非现网故障。

5. **P-05 流内错误对象未识别** — 2026-10-29
   - 现象：有些兼容端点（代理、网关）在 HTTP 200 的流里发 `{"error": {...}}`，比如额度耗尽。客户端 `json.loads` 成功，但取不到 `choices`，循环空转到底，最后得到 `StreamDone(stop)` + 空消息，看起来像模型没说话。
   - 原因：只处理了正常 chunk 的形状，错误只依赖 HTTP 状态码（真实 OpenAI 确实如此），没有覆盖"200 里带错误"这一类实现。
   - 解决：解析后先检查 `chunk.get("error")`，命中就转 `StreamError` 并 `return`，新增一条用例。

6. **P-06 中断时工具调用丢失** — 2026-10-29
   - 现象：流式期间工具调用攒在适配器的 `pending` 里，中途断流（`TransportFailure`、解析失败、服务端错误）时，`partial` 保留了已产出的文本，工具调用却完全消失，连"刚才在调 read_file"都看不到。
   - 原因：缓冲是 D15 / D17 的结论——`assembler` 单块模型放不下交错的并行调用，只能先写字典、流末重放；但重放只写在正常收尾路径上，失败路径直接 `fail()` 返回。文本块和工具块的失败行为因此不一致。
   - 解决：把重放抽成 `replay_tool_calls` / `replay_function_calls`，三条失败路径（传输失败、流内 error、解析异常）都在 `fail()` 之前先调用它。骨架里 `arguments` 是空 dict、`raw` 是半截原文，是否执行由 `stop_reason` 决定。两家各加一条中断用例（D20）。

7. **P-07 拒答无法与正文区分** — 2026-10-29
   - 现象：把拒答当文本块发出后，上层拿到一条消息，只能靠 `stop_reason == "filtered"` 判断"这次是拒答"，没法把拒答单独换个样式或折叠起来；拒答与正文混在同一条消息里时更分不清。
   - 原因：内部协议只有 `TextBlock`，没有表达"这段是拒答"的位置。
   - 解决：给 `TextBlock` 加 `refusal: bool = False`，`assembler.start_text(refusal=True)` 只改块上的标记，事件的数量与形状都不变；两家在遇到拒答时先关正文块再开新块。比重开一个 `RefusalBlock`（要加 3 个事件）侵入性小得多（D21）。

8. **P-08 GPT 推理内容不可见** — 2026-10-29
   - 现象：`openai_responses` 跳过 `reasoning` item，GPT 推理模型的思考过程完全看不到，最终消息里只有正文和工具调用。
   - 原因：适配器只处理了 `function_call` 与 `output_text` 两类 item，`reasoning_summary_text.delta` / `reasoning_text.delta` 没有分支，`encrypted_content` 也没保存。
   - 解决：两类 reasoning delta 都喂 `assembler.thinking()`，`output_item.done` 时把 `encrypted_content` 写进 `ThinkingBlock.signature`，新增一条用例。**暂不回传** reasoning item——请求侧的形状缺真实样本，写错会直接 400，等 M9（D22）。

9. **P-09 Kimi 把 usage 放在 choice 里，用量读成 0** — 2026-10-29
   - 现象：M9 第一次录真实流（Kimi k2.6，`--service kimi-compat`；录之前 `kimi-latest` 回 404，改用 `GET /v1/models` 查到的 `kimi-k2.6`），`replay` 打出的事件全对——思考块、工具三段、`stop_reason=tool_use` 都在，唯独 `StreamDone` 是 `in=0 out=0 cache_read=0`；而原始 SSE 最后一片明明写着 `prompt_tokens: 65`、`completion_tokens: 44`、`cached_tokens: 65`、`reasoning_tokens: 26`。
   - 触发条件：Kimi（`api.moonshot.cn`，`kimi-k2.6`）把 `usage` 塞进 `choices[0]`，与 `finish_reason` 同一个 chunk；标准实现（DeepSeek 等）把它挂在顶层 `usage`。这类端点也不会补发 `choices: []` 的独立用量片。
   - 原因：适配器只在顶层取 `chunk.get("usage")`，choice 里那份从来没被读到，而 `for choice in choices` 循环里也不看这个字段。手写样本按标准位置写，所以录制前没有一条用例会红——这一层差异只有真实流能暴露。
   - 定位：`replay` 看到 `in=0` 后，先确认事件链本身没问题（思考与工具都对），再回到原始 fixture 里搜 `usage`，只在最后一行命中且位于 `choices[0]` 内，对照适配器的读取位置即确认漏读。
   - 解决：在 choice 循环里兜底读 `choice.get("usage")`，且只在顶层没有时采用，避免两处都有时被 `Usage.__add__` 重复累加；补一条用例同时钉住「只在 choice 里」与「两处都有只认顶层」。样本存成 `fixtures/openai_compat_kimi_thinking_tool.sse`（重录后带缓存命中），回放测试直接断言 65 / 44 / cache 65，改坏立刻失败（D24）。

10. **P-10 Anthropic 重复上报的用量被当成增量累加** — 2026-10-29
   - 现象：`anthropic` 的真实流回放后，`cache_read_tokens` 是 36、`input_tokens` 是 130，而原始 SSE 里 `message_start` 与 `message_delta` 各写一次 18 和 65 —— 数值整整翻倍。
   - 触发条件：Kimi 的 Anthropic 兼容端点在 `message_delta` 里把**完整** usage 再报一遍；官方 Anthropic 通常只重复 `output_tokens`，所以累加恰好正确，差异只在兼容端点暴露。
   - 原因：`assembler.add_usage` 一律累加。官方 `message_start` 还会给 `output_tokens: 1`，与 `message_delta` 的最终值相加会多 1；Kimi 给全量则每个 input 类字段都翻倍。这条和 P-09 同源：都是"手写样本按官方形状写，兼容端点换个位置或粒度就露馅"。
   - 定位：回放值恰好是原始值的两倍，回到 fixture 里搜 `input_tokens`，看到 `message_start` 与 `message_delta` 的数值完全相同，即可确认是重复上报。
   - 解决：给 `assembler` 增加 `set_usage`（整体替换），`anthropic` 改用 `merge_usage` 逐字段覆盖——`message_delta` 里出现了才覆盖，没出现就保留 `message_start` 的值。原先"累加"的用例改为同时验证「跨事件合并」与「重复上报不翻倍」，replay 断言随之改为 65 / 42、18、78（D26）。

11. **P-11 `input_tokens` 四家不同义，上层算不出总输入** — 2026-10-29
   - 现象：同一条 prompt，三家报的总输入都是 18，但字段对不上：`openai_compat` 是 `in=18 cache=0`，`openai_responses` 是 `in=18 cache=18`，`anthropic` 是 `in=0 cache=18`。上层若统一用 `input_tokens` 当总输入，anthropic 那条直接少算整个缓存部分；若统一用 `input + cache_read`，OpenAI 系又会 18+18 双算。
   - 触发条件：只要上层拿 `Usage` 做计费或统计就会遇到，与是否流式无关；单看一家永远发现不了，是横向对比三种协议的真实流才暴露。
   - 原因：`input_tokens` 这个内部字段沿用了"厂商原义"却没有声明是哪一种。OpenAI 系是「总量 + 子集标注」（`input_tokens` 已含 `cached_tokens`），Anthropic 是「分项」（`input_tokens` 不含缓存读写，三项相加才是总量）。另外 Anthropic 的总输入还要加 `cache_creation_input_tokens`，只加 `cache_read` 仍然少算。
   - 定位：把三条 fixture 的 `usage` 并排放，总输入都是 18 而 `input/cache` 的分配各不相同；再对照各家文档的字段定义即可确认是记账方式差异，不是适配器读错。
   - 解决：按协议归一化，出口统一成「总输入」——`anthropic` 加 `with_total_input`（`input + cache_read + cache_write`），其余三家原值即对；缓存两项保留原值，`未缓存输入 = input - cache_read - cache_write` 仍可还原。归一化只包在 `set_usage` 外层，不能进 `merge_usage`（它的 `base` 已是归一化值，再补会重复）。断言随之改为 anthropic text 0→18、text_tool 25→39（D25）。

12. **P-12 非对象 JSON 穿透异常** — 2026-11-02
   - 现象：上游返回合法 JSON 但根节点不是对象时（`[]`、`"ok"`、`[null]`），`json.loads` 成功，随后 `chunk.get(...)` 抛 `AttributeError`，直接穿出 `stream()`。契约写的是「除 `asyncio.CancelledError` 外不抛异常」，这条被破坏；上层若没包 `try/except`，一次畸形响应就能中断整个 agent 循环。
   - 触发条件：代理/网关在 200 的流里塞数组或标量，或厂商给出 `{"choices": [null]}` 这类异常形状。正常协议流不会出现，所以手写样本和真实回放都没覆盖到。
   - 原因：兜底只写了 `(json.JSONDecodeError, KeyError, TypeError)`。`JSONDecodeError` 只覆盖「解析失败」，不覆盖「解析成功但类型不对」；`AttributeError` 既不在元组里，也不是这几类的子类。四个适配器是同一份复制，所以四处一起漏。
   - 解决：四处统一改成 `except Exception`。`CancelledError` 与 `GeneratorExit` 属于 `BaseException`，取消和关闭仍能穿透；错误消息保留 `type(exc).__name__`，自家代码的 bug 也能从 `StreamError` 里看出来。每个适配器新增 `test_non_object_json_becomes_stream_error`（喂 `[]`），并在 `openai_compat` 新增 `test_cancellation_propagates`，证明放宽捕获后取消没有被吞。

13. **P-13 Anthropic 从未打缓存断点** — 2026-11-03
   - 现象：换成官方 Anthropic 端点后，`cache_read_input_tokens` 恒为 0（Kimi 兼容口录的样本里有 18 / 65），长 prompt 每一轮都按 1× 计费，首 token 延迟也没有改善。
   - 触发条件：官方 Messages API 必须显式在块上声明 `cache_control` 才写缓存；我们此前一处都没打。Kimi 的兼容口是服务端自动缓存，所以录制的真实样本看起来完全正常，把问题掩盖了——只测兼容口永远不会暴露。
   - 原因：`build_payload` 里 `system` 直接赋字符串，`tools` 与 `messages` 没有任何 `cache_control`。字符串形式的 system 根本带不了该字段；前缀按 `tools -> system -> messages` 累积，三处都不打就等于没有缓存。
   - 解决：`system` 改成 text 块数组并在末尾打点；最后一个工具打点；最后一条消息的最后一个可缓存块打点（`thinking` 不能显式标记，遇到往回找；`tool_result` / `tool_use` 官方支持）。三处都在段末，总数 3，官方上限是 4（D30）。离线断言覆盖形状、thinking 跳过与断点总数。**真实命中率待官方 key 验证**；Kimi 兼容口是否接受块数组与 `cache_control` 待冒烟，不认则按端点回退字符串 system。注意最小可缓存长度 1024（Sonnet 4.5）/ 2048（Sonnet 4.6、Haiku），短会话不命中属正常，不要拿命中率当验收指标。

14. **P-14 多轮对话无法复用连接** — 2026-11-03
   - 现象：同一个 provider 连续调用 `stream()`，每轮都新建并关闭一个 `httpx.AsyncClient`，TLS 握手与连接池每轮清零；`design.md` / D28 却写着「上层能共享一个长活 `AsyncClient` 的连接池」，文档与实现不一致。
   - 触发条件：agent 循环连续调用同一个 provider 时才会累积成延迟问题，单轮调用、单条测试都看不出来。
   - 原因：D10 把 `client` 注入做在 `post_sse` 这一层，而四个适配器的构造函数只收 `(spec, api_key)`，`stream()` 也没往下传——传输层留了口，适配层没开口。
   - 解决：`create_provider(spec, api_key="", client=None)`，四个适配器构造函数同样收可选 `client` 并透传给 `post_sse`。所有权写进工厂 docstring：注入的由调用方 `aclose()`，`None` 时保持原行为（自建并关闭）。新增 `test_provider_factory.py`，四种协议各注入一个 `MockTransport` client，连续两次 `stream()` 都命中它，结束后 `is_closed` 仍为 `False`（D29）。

验证：`uv run python -m gradus.ai.tests.adapter` → 126 passed（P-01~P-14 各配了对应用例）。
