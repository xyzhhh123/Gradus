# web 层问题记录

本文件只收录**已经解决**的问题：`web/`（浏览器侧）与 `gradus/host/`（Python 侧）都算在内。
未解决、待定与已知限制在 [design.md](design.md) 里跟踪，解决后再归档到这里。

## 记录格式

### 问题标题

- **状态**：已解决
- **现象**：
- **触发**：
- **原因**：
- **解决**：
- **验证**：关联测试或实测步骤
- **关联**：模块文件

## 已解决问题

### 点击停止后显示「出错」并弹出 aborted run was cancelled

- **状态**：已解决
- **现象**：生成中点「停止」，请求确实中断了，但顶部状态变成「出错」，对话区出现红条 `aborted: aborted run was cancelled`。
- **触发**：`session/cancel` → `Session.abort()` → `AgentError(kind="aborted")`。
- **原因**：host 把**所有** `agent/error` 都记成失败，前端收到 `session/status {status:"error"}` 就显示横幅；但 `aborted` 是用户主动取消，不是运行故障。
- **解决**：`serve.py` 只在 `kind != "aborted"` 时记录错误，结束后统一发 `idle`；`reduce.ts` 收到 `agent/error` 且 `kind === "aborted"` 时回到 `idle` 并清空 `error`。
- **验证**：`web/src/model/reduce.test.ts`「中止回到就绪，未完成的工具标为已取消」；实机发送长回答并点停止，状态回到「就绪」、无红条。
- **关联**：`gradus/host/serve.py`、`web/src/model/reduce.ts`。

### 中止后审批面板一直挂在输入框上方

- **状态**：已解决
- **现象**：工具正在等授权时点「停止」，那一轮的「允许 / 拒绝」面板留在页面上；点拒绝只关掉面板，状态仍是「出错」。
- **触发**：`ConfirmBridge.ask()` 里挂起的 `asyncio.Future` 在运行被中止后没人收尾。
- **原因**：确认请求是「服务端发问、等前端回答」的长挂起调用；中止只取消了 agent 任务，没有清理 `pending` 里的 future，前端也就一直以为还有人等它回答。
- **解决**：新增 `ConfirmBridge.reject_all()`，在每轮结束（`finally`）与连接关闭时把所有等待中的确认按「拒绝」收尾并清空；前端 `session/status` 只要不是 `running` 就清掉 `pendingConfirm`。
- **验证**：`reduce.test.ts`「一轮结束后清掉残留审批」；实机取消后 DOM 中 `.approval` 数量为 0。
- **关联**：`gradus/host/serve.py`、`web/src/model/reduce.ts`。

### 发下一条消息时，上一轮的回复消失或错位到新消息下面

- **状态**：已解决
- **现象**：第一轮回答正常显示，发送第二条消息后，上一轮内容先是整体消失；中止场景下还会出现「旧回答渲染在新消息下面」的错位。
- **触发**：连续两轮对话，或第二轮在中止后马上发送。
- **原因**：实时内容只存在 `state.turns`，而 `agent/start` 会把它清空；`messages` 只来自快照，所以新一轮一开始旧内容就没了。顺序上，host 先发 `session/message`（回显用户输入）再发 `agent/start`，于是用户新消息先入 `messages`，旧轮次稍后才被清掉，视觉上就串位了。
- **解决**：新增 `commitTurns()`——收到 `session/message` 时**先**把上一轮的助手文本并入 `messages`，**再**追加这条用户消息；`agent/start` 只做 `turns: []` 的收尾。这样历史顺序恒为「旧回答 → 新提问」。
- **验证**：`reduce.test.ts`「新消息到达前，上一轮助手文本先并入历史」；实机连续两轮 + 中止后追加消息，顺序正确。
- **关联**：`web/src/model/reduce.ts`。

### 用户提问永远不会出现在对话里

- **状态**：已解决
- **现象**：mock 模式下发消息，页面上只有助手回复和工具卡，没有用户气泡；真实后端同样如此。
- **触发**：任意一次 `session/prompt`。
- **原因**：协议里缺少「用户消息已入库」的帧。前端只在快照里读历史，实时链路没有任何帧会告知它用户刚说了什么。
- **解决**：新增服务端帧 `session/message`。host 在开始运行前回显用户输入；前端按 `id` 去重后追加。
- **验证**：`reduce.test.ts`「session/message 按 id 去重」；浏览器实测 `.message-user` 右对齐气泡出现。
- **关联**：`gradus/host/serve.py`、`web/src/protocol/frames.ts`、`web/src/model/reduce.ts`。

### 工具卡片插件启动即失败：slot "tool.call.view" is not declared

- **状态**：已解决
- **现象**：页面空白或工具卡退化成原始 JSON，控制台报 `slot "tool.call.view" is not declared`。
- **触发**：`tool-bash` 与 `conversation` 同时注册，而声明该槽位的 `conversation` 还要等 `sessions`，起得更晚。
- **原因**：插件在 `apply` 里直接 `register`，此时声明方尚未执行；插件自身变成 FAILED，后续渲染全部落空。
- **解决**：改用 `ctx.slots.inject('tool.call.view', () => register(...))`——注册动作被推迟到槽位声明之后，和 Cordis 的 `inject` 是同一个「依赖没齐就等」的模式。
- **验证**：浏览器实测，工具卡正常渲染且启动状态表里没有 FAILED。
- **关联**：`web/src/plugins/tool-bash.tsx`、`web/src/slots/registry.ts`。

### /ws 握手返回 403，浏览器只看到 1006

- **状态**：已解决
- **现象**：前端一直显示「未连接」，浏览器 WebSocket 关闭码 1006；服务端访问日志里 `/ws` 是 403（或 404）。
- **触发**：`uvicorn` 启动后升级请求被当作普通 HTTP。
- **原因**：两个独立原因叠加：① `uvicorn` 自身不带 WebSocket 协议实现，缺 `websockets` 时拒绝升级；② `dist` 是加 token 逻辑之前的旧构建，握手 URL 没带 `?token=`，被服务端 4401 拒绝（浏览器统一表现为 403/1006）。
- **解决**：`web` 可选依赖加 `websockets>=13`；`resolveUrl()` 从页面 `location.search` 取 token 拼到 `/ws`；改完必须重新 `npm run build`，旧 bundle 不会自动更新。
- **验证**：`uv run --extra web python -c "...websockets.connect('ws://127.0.0.1:8765/ws?token=...')"` 能收到 `session/snapshot`；浏览器显示「已连接」。
- **关联**：`pyproject.toml`、`web/src/plugins/connection.ts`、`gradus/host/serve.py`。

### 取消的提问留在模型上下文里

- **状态**：已解决
- **现象**：点停止后再发新消息，模型有时会接着回答被取消的那条指令；界面上看不出区别，只有回复内容不对。
- **触发**：一轮都没完成就被取消（`AgentError(kind="aborted")` 且 `new_messages` 为空）。
- **原因**：`Agent.prompt()` 在运行开始前就把 `UserMessage` 追加进 `_messages`；中止时 `run_loop` 只回滚未完成的半轮（`new_messages=committed`），不会撤回这条提问。下一轮请求于是带着两条连续的 user 消息。
- **解决**：提问不再预先进入历史。`Agent.prompt()` 只把 `UserMessage` 交给 `run_loop`，后者拼进本次 transcript；终止事件的 `new_messages` 在最前面补上提问，一条产出都没有时返回空表（视为没说过）。因此取消时历史天然没有残留，也不需要事后 pop。
- **验证**：`tests/test_agent.py::test_abort_drops_unanswered_prompt`；实机取消后发「你好」，回复为纯问候，输入 token 与上一轮持平（2150 vs 2162），说明旧提问没有进入上下文。
- **关联**：`gradus/agent/agent.py`、`gradus/agent/loop.py`。
- **附带说明**：界面上的用户气泡仍然保留（host 已回显），只是模型侧丢弃。两者不一致是有意的——用户需要看到自己发过什么。

### 会话文件里只有回答，没有提问

- **状态**：已解决
- **现象**：`--resume` 恢复出的历史缺少用户消息；实测 `.gradus/sessions/*.jsonl` 里只有 `assistant` 和 `tool`。
- **触发**：任意一次成功运行并落盘。
- **原因**：`AgentDone.new_messages` 只累加助手与工具结果，用户消息在 `Agent.prompt()` 里直接进了内存历史；`Session._run` 只把 `new_messages` 交给 `_append`，于是提问从未写入文件。
- **解决**：把提问并入 `new_messages` 本身，而不是在写文件时补丁。`run_loop` 新增 `prompt` 参数：只拼进 transcript，终止事件的 `new_messages` 在最前面补上它；一条产出都没有（取消 / 首轮失败）时返回空表，提问视为没说过。`Agent.prompt()` 不再预写入，`_pump` 直接 `extend`；`Session._append` 恢复成只写 `new_messages`，重试时用 `continue_(prompt=...)` 在上一轮零产出时把提问带回。
- **验证**：`tests/test_retry.py` 的 `test_prompt_is_logged_with_the_answer`、`test_abort_without_turns_writes_nothing`、`test_final_failure_without_turns_writes_nothing`、`test_retry_after_empty_failure_writes_prompt_once`、`test_completed_turns_are_logged_on_error`；实机测试中会话文件的角色严格成对 `user,assistant`，被取消的那次没有留下任何条目。
- **关联**：`gradus/coding/session.py`、`gradus/agent/loop.py`。

### 空会话一直占着左侧栏

- **状态**：已解决
- **现象**：左侧栏会出现标题为空的条目；有些点进去一条对话都没有，却一直留在列表里；还有一批旧记录只有助手回复、没有用户提问，同样以空标题出现。
- **触发**：每次 WebSocket 连接都会新建一个会话文件；另外是早期「提问不落盘」bug 留下的残留文件。
- **原因**：`list_sessions()` 的清理分支挂在「`_session_meta()` 返回 `None`」上，但它只在 IO 出错时才返回 `None`，零消息文件返回的是 `("", workspace, 0)`。于是清理逻辑从未触发过——空文件只是被 `continue` 跳过，一直躺在磁盘上；只有助手消息的文件则带着空标题进入了列表。
- **解决**：`_session_meta()` 增加消息计数；`list_sessions()` 按三种情况分开处理——零消息：不展示且直接删除（正在使用的那个除外，删掉后追加写入会重建文件、丢掉头部）；有消息但没有用户提问：不展示但保留文件（内容未必没价值）；有用户提问或已改名：正常展示。
- **验证**：临时脚本用四种真实文件（空文件、只有头、只有助手消息、正常会话）跑 `list_sessions()`，确认前两种被删、第三种保留、第四种展示，且活动会话文件不会被删；`uv run pytest gradus/host/tests` 9 passed。
- **关联**：`gradus/host/serve.py`。

### 打开其它目录的会话后，工作区没跟着切

- **状态**：已解决
- **现象**：切换会话成功、历史也恢复了，但侧栏显示的工作区还是原来那个，右侧文件面板列的仍是旧目录的文件。
- **触发**：`session/open`。
- **原因**：`connect_session()` 无条件把 `self.workspace` 传给 `Session.connect(workspace=...)`。显式传入的工作区会覆盖会话文件头里的 `workspace`，恢复出来的会话于是跑在错误的目录里。
- **解决**：`resume` 非空时不再传 `workspace`，交给文件头决定；连接完成后把 `self.workspace` 同步成 `session.workspace`，快照里的 `workspace` 与 `fs/*` 的解析根一起跟上。
- **验证**：只做了代码走查（`resume` 分支不再传 `workspace`、连接后回写 `self.workspace`）；跨目录实机复测尚未做，见下方「未覆盖」。
- **关联**：`gradus/host/serve.py`。

### 侧栏/右侧栏缺会话管理、工作区浏览与宽度调整

- **状态**：已解决
- **现象**：① 会话只能打开，不能改名也不能删除；② 切换工作区只有一个手输绝对路径的输入框，没有目录浏览，最近路径也不持久；③ 右栏宽度写死 340px，不能拖；④ 右栏打开的文件关不掉，只能再点另一个文件替换；⑤ 侧栏宽度和断点是硬编码的固定值。
- **触发**：常规使用。
- **原因**：首版只做了「列出会话 + 输入框切目录 + 固定宽度面板」，交互细节留成了待办；会话文件的头部虽然允许扩展字段，但没有写入路径，所以标题只能靠「首条提问」推断。
- **解决**：① 会话行悬停露出重命名/删除：改名写回 JSONL 头部的 `title`（先写临时文件再 `os.replace`，中途失败不会留下半个头部），空标题表示回落到「取首条提问」；删除先内联确认，删当前会话时后端先切到新会话再删（旧文件可能已被当空会话清掉，`FileNotFoundError` 视为删成功）。② 工作区改为选择对话框：输入路径跳转 + 最近列表 + 从盘符/主目录起步的目录树，可逐层进入和 `..` 返回；新增 `workspace/browse` 与 `workspace/dirs|error` 帧，该接口不受当前工作区限制（只列目录名，不读文件）。③ 右栏可拖，下限 300px、上限为视口 70%，与 dsh 的 `RIGHTBAR_MIN` / `RIGHTBAR_MAX_RATIO` 对齐，宽度存 localStorage。④ 右栏头部区分两个关闭：`×` 只闭当前文件并回到目录树，箭头闭整个面板。⑤ 侧栏宽度 264–420、默认 280、收起 56、1024 自动收成图标条、768 以下隐藏，均取自 dsh 的 `columns.ts`，宽度可拖且持久。
- **验证**：`npm run build`（含 `tsc --noEmit`）通过；临时脚本覆盖改名（写回头部且不破坏后续行、空标题回落首条提问）、删除（文件消失、删不存在的会话返回错误而不崩）、`browse`（起点/子目录/父目录/隐藏目录/不存在路径）与 `handle()` 分发出的帧类型，全部 PASS。浏览器侧的重命名、删除、目录树、右栏拖拽与关闭文件只做了构建期检查，未逐项实机点选。
- **关联**：`gradus/host/serve.py`、`web/src/protocol/frames.ts`、`web/src/plugins/layout.tsx`、`web/src/plugins/explorer.tsx`、`web/src/styles.css`。

### 界面没有模型切换，会话内也换不了 API 形状

- **状态**：已解决
- **现象**：模型只能在启动时由 `.env` / 配置文件定死；想换到另一家或另一种协议，只能新开会话重配，换回来的那次选择也留不住。右栏一次只能看一个文件，开第二个就把第一个顶掉；换到大屏后正文显得很小。
- **触发**：常规使用。
- **原因**：① `ModelSpec` 在 `Agent` 构造期固定，`Session` 没有换模型入口，`Settings` 又是 frozen；② 候选模型没有承载处（环境变量是一维的，装不下多套 provider / key）；③ 右栏状态是单个 `file` 字段而不是列表；④ 字号写死 px，没有随窗口变化的基准变量。
- **解决**：
  - 配置新增 `models` 数组（`ModelChoice`：显示名 + `ModelSpec` + 可选独立 key），解析与顶层同口径；新增 `with_choice(settings, choice)`，候选没写 key 时沿用当前 key。
  - `Session.switch_model(settings)` 用同一份历史、工具与系统提示重建 `Agent`（Provider 构造期固定，只能重建），生成中拒绝。
  - host 新增 `model/state`（候选表 + 当前项）与 `model/select`；快照带上当前模型。前端在输入框左下加选择器（向上展开、按协议显示副标题、当前项打勾、生成中禁用）。
  - 新增设置面板（顶栏齿轮）：`settings/get|save` 读写配置文件的 `models` 候选表，写回走「临时文件 + 校验 + `os.replace`」，空 key 沿用原文件同位置的值；`services` 给内置服务商快捷名，`hasKey` 只报真假、不回显 key。这样加候选不需要手改 JSON。
  - 选择记到配置文件顶层 `active`：切换成功后 `save_active` 落盘，启动与重读时 `with_active` 按名字切回 `spec`，重启后仍停在同一个模型上；名字对不上就保持顶层配置的模型，保存候选表时顺手清掉失效的 `active`。
  - 右栏改为多文件标签：`files[] + activePath`，同路径只留一份，逐个关闭，关当前项后自动退到相邻标签或目录树。
  - 字号不再手动调：单一基准 `--content-font` 随视口取 `clamp(16px, 9px + 0.4167vw, 20px)`（1440→16 / 1920→17 / 2560 起封顶 20），`--code-font` / `--fs-*` / `--content-width` 全部由它派生；删掉步进器与 `web/src/model/fonts.ts`。
- **验证**：
  - 同一会话内真实切换三种协议（kimi 的 compat / anthropic / responses，同一 key、`local` 沙箱）：先让模型记住 41，切到 anthropic 答 `'41'`，再切到 responses 仍答 `'41'`，历史为 6 条交替的 user/assistant。
  - `gradus/coding/tests/test_config.py` 新增候选表解析、`with_choice` 沿用 key、缺地址报错、未知字段报错，以及 `active` 生效 / 失效回落 / `save_active` 往返 / 保存候选表清失效 `active` / `active` 类型校验共 9 个用例；`uv run pytest` 全量 626 passed。
  - 浏览器实测：设置页加一个 `kimi-k3` 候选 → 选择器切过去 → 发「只回复两个字：你好」→ 回复「你好」（输入 2688 / 输出 70），状态回就绪；右栏同时开 README.md 与 LICENSE 两个标签，点 × 只关掉其中一个。
- **关联**：`gradus/coding/config.py`、`gradus/coding/session.py`、`gradus/host/serve.py`、`gradus/host/wire.py`、`web/src/plugins/composer.tsx`、`web/src/plugins/settings.tsx`、`web/src/plugins/explorer.tsx`、`web/src/styles.css`。

### 界面只有深色，没有主题切换

- **状态**：已解决
- **现象**：整个界面固定深色，改不了，也没有「跟随系统」。
- **触发**：常规使用。
- **原因**：抄 dsh 视觉 token 时只搬了深色那一份。dsh 的 `design-platform.css` 里 `body {...}` 本来就是浅色语义 token（`--dsw-alias-bg-base: var(--dsw-static-neutral-bluish-00)` 等），深色只是 `body[data-ds-dark-theme]` 的覆盖；`THEME_PREFERENCES` 是 `light / dark / system`，默认 `system`。Gradus 首版把 `:root` 直接写成了深色值，还把颜色硬编码进各处规则里，等于把主题钉死了。
- **解决**：
  - `:root` 改成浅色整套 token，深色原值搬进 `:root[data-theme='dark']`，只覆盖颜色与阴影，字号/尺寸变量两边共用；`color-scheme` 也跟着切。
  - 把散在规则里的硬编码颜色抽成语义变量：滚动条、投影（`--shadow-soft` / `--shadow-strong`）、遮罩（`--backdrop`）、对比按钮（`--contrast-strong`）、红色/琥珀色半透明改用 `color-mix(in srgb, var(--danger|warn) N%, transparent)`，代码高亮六条抽成 `--hl-*`（浅色近 github light、深色近 github dark）。
  - 新增 `web/src/model/theme.ts`：三态偏好进 localStorage，解析结果写到 `<html data-theme>`；`system` 时订阅 `matchMedia('(prefers-color-scheme: dark)')`。**纯前端状态，不经 host**。
  - `index.html` 加一段内联脚本，在样式生效前先设好 `data-theme`，避免首帧闪一下；`theme.ts` 与它共用同一个存储键。
  - 设置页「外观」段加三个按钮（`ThemeRow` + 三个新图标），选中态跟随「已保存的偏好」而非解析后的颜色。
- **验证**：`npm run build`（含 `tsc --noEmit`）通过；`?mock` 开发服务器可正常加载、浏览器无报错。浅 / 深 / 跟随系统三态与刷新后保持只做了构建期与加载期检查，未逐项实机点选。
- **关联**：`web/src/styles.css`、`web/src/model/theme.ts`、`web/src/plugins/settings.tsx`、`web/src/icons.tsx`、`web/index.html`。

### 选择工作区像只能手输路径，而且到不了 C: 以外的盘符

- **状态**：已解决
- **现象**：工作区对话框像只能手打绝对路径，点不出资源管理器里的目录；而且从 `C:\` 出发到不了 D 盘。
- **触发**：点侧栏的工作区按钮。
- **原因**：① 输入框带 `autoFocus` 又排在目录列表上方，主次颠倒，看起来只能手输。② 起点（盘符列表）只在 `workspace/browse` 收到空路径时返回，而对话框打开时传的是当前工作区；从 `C:\` 点 `..` 会停在 `C:\`（`parent == current`），所以没有任何一条路能回到盘符列表。
- **解决**：目录列表提到最上，输入框降级成最下面的「或粘贴绝对路径」；顶部加一行常驻「此电脑」→ 空路径，盘符列表重新可达；「最近」单独成段。
- **验证**：`npm run build`（含 `tsc --noEmit`）通过，`?mock` 加载无报错；未在真实盘符之间逐档点选。
- **关联**：`web/src/plugins/layout.tsx`、`web/src/styles.css`、`gradus/host/serve.py`（`browse_roots`）。系统原生对话框见下一条。

### 工作区只能靠应用内目录树，弹不出系统「选择文件夹」

- **状态**：已解决
- **现象**：选工作区只有应用内那棵目录树，不能像 dsh 那样直接调出系统的文件夹选择框。
- **触发**：点侧栏的工作区按钮。
- **原因**：把「浏览器拿不到磁盘绝对路径」错误外推成了「整个应用都做不到」。浏览器确实不行（`webkitdirectory` 只给相对路径、`showDirectoryPicker()` 只给不透明句柄），但 dsh 的对话框根本不是浏览器弹的，是**宿主进程**自己弹的：`packages/host/directory-picker-*` 是一个 seam，`directory-picker-native` 在 macOS 用脚本化 AppKit 的 `NSOpenPanel`、Linux 走 Zenity（KDialog 兜底）、Windows 在子进程里用 koffi 驱动 COM 的 `IFileOpenDialog`；Electron 那条 `dialog.showOpenDialog` 只是 `apps/desktop` 额外的一层壳。Gradus 的宿主就是一个本地 Python 进程，同样弹得出。
- **解决**：
  - `host/serve.py` 新增 `pick_directory()`：`tkinter.filedialog.askdirectory()` 取绝对路径，整段经 `asyncio.to_thread` 跑在工作线程（tkinter 要求创建与使用同线程，同时不阻塞事件循环）；隐藏根窗口并置顶，免得开在浏览器后面。
  - 能力探测 `native_picker_available()` 在**启动时判定一次**（与 dsh 的 `directory-picker-auto` 同构）：Linux 且没有 `DISPLAY` / `WAYLAND_DISPLAY`（纯 SSH）直接判否，再试 `import tkinter`，失败即判否。判否就把 `workspace/pick` 从快照的 `capabilities` 里去掉——**服务不了就明确拒绝，不做近似**。
  - 新增客户端命令 `workspace/pick`，结果复用现成的 `workspace/dirs`（前端就当用户点了一次目录跳转，零新状态）；取消不发帧，不可用或 Tk 起不来回 `workspace/error`。
  - 前端把快照的 `capabilities` 存进 `SessionState`，`WorkspaceDialog` 头部按 `CAPABILITY_PICK` 决定要不要渲染「浏览…」按钮。
- **验证**：`uv run pytest gradus/host/tests` 9 passed；`npm run build`（含 `tsc --noEmit`）通过。本机探测 `NATIVE_PICKER == True`、`CAPABILITIES == ('session/message', 'workspace/pick')`，工作线程里建 `Tk()` 正常；桩测 `handle_pick` 四条分支（选中真实目录 → `workspace/dirs` 且路径一致、取消 → 0 帧、选不存在的目录 → `workspace/error 不是目录`、探测判否 → `workspace/error 当前环境没有可用的系统目录对话框`）与 `handle()` 的分发全部符合预期。**真正弹出对话框并点选未实机验证**（需要人手点一下）。
- **关联**：`gradus/host/serve.py`、`gradus/host/wire.py`、`web/src/protocol/frames.ts`、`web/src/model/state.ts`、`web/src/model/reduce.ts`、`web/src/plugins/layout.tsx`、`web/src/styles.css`、`web/docs/design.md`（§5.2）。

## 实测覆盖清单

三条链路已在真实后端上跑过（`gradus-serve` + 真实模型 key + `restricted` 沙箱）：

| 链路 | 结果 |
|---|---|
| 流式文本 + 工具调用（`list_dir` / `read_file`） | 通过，卡片显示真实文件列表与文件内容 |
| 授权确认（`powershell`）→ 点允许 → 执行 | 通过，输出 `hi` |
| 生成中点停止 → 状态回就绪、无残留 | 通过 |
| 取消后追问 → 旧提问不进入模型上下文 | 通过 |
| 落盘检查 → 会话文件含 `user` / `assistant` | 通过 |
| 同一会话切换 `openai_compat` → `anthropic` → `openai_responses` | 通过，换协议后仍记得上一轮内容 |
| 模型选择器切换候选 | 通过，触发器与选中标记同步 |
| 设置页新增一个候选 → 切换 → 发消息 | 通过，回复正常且状态回就绪 |
| 右栏同时打开两个文件、单独关闭 | 通过 |
| 浅色 / 深色 / 跟随系统三态与刷新后保持 | 未逐项实机点选，只过了构建与加载期检查 |

## 未覆盖

- **工具卡「已取消」徽标**：只在 `reduce.test.ts` 单测里覆盖。取消时模型恰好没有调用工具，实机未出现该徽标。
- **多标签页 / 多客户端**：一条连接一个会话，同一会话开两个页面时状态会互相覆盖。
- **断线重连**：每次重连都会新建一个会话文件，旧历史不会续上——恢复要等事件级日志（见 design.md §5.5）。
- **界面与上下文的不一致**：取消的提问气泡仍留在页面上，但已从模型历史中移除。刷新后它会随新会话一起消失。
- **并发写同一个会话文件**：`SessionFile` 没有文件锁，两个进程同时 resume 同一会话会写出分叉的 `parentId` 链。当前 UI 只有一个连接，碰不到；多端同时打开前必须先加锁或改成单写者。
- **工作区对话框不能新建目录**：只能浏览、进入与切换，要新建目录得先自己开终端。
- **右栏没有文件搜索**：只能逐层点开，文件多时全靠滚动。
- **改名会整体重写会话文件**：`rename_session` 先读全文、换头部再 `os.replace`，长会话有额外 IO；期间若有别的进程在追加（同上面的并发写问题），那段内容会丢。
- **跨目录打开会话的工作区修复**：只做了代码走查，未实机复测（见上文同名条目）。
- **会话改名 / 删除 / 工作区目录树 / 右栏拖拽**：后端逻辑有临时脚本覆盖，浏览器侧只过了 `tsc` 与打包；本文中已逐项点选的是模型选择器、设置页加候选、右栏多文件标签。
- **字号随视口自动缩放**：只核对了 `clamp(16px, 9px + 0.4167vw, 20px)` 的取值与派生变量，未在不同窗口宽度下逐档实机测量。
- **记住的模型在重启后生效**：配置层（`with_active` / `save_active` / 失效回落）有单测；`handle_model_select` 落盘与重启复现只做了代码走查，未实机复测。
- **主题三态与防闪烁**：`theme.ts` 的读写与 `system` 订阅、`index.html` 内联脚本只过了构建期检查与 `?mock` 加载；浅 / 深切换、系统深浅切换、刷新后保持均未实机点选。
- **模型候选的可用性**：`models` 只校验字段与地址格式，不探测端点；切到不可用的候选时错误落在会话状态里，不会在启动时提示。
- **跨协议的思考块**：切换协议时 `ThinkingBlock` / `signature` 不做转换，chat 与 responses 形状不回传思考块，同一段历史换家后上下文不完全等价。
- **切换模型是整段重建**：`switch_model` 会新建 `Agent` 并关闭旧的，工具与历史复用，但正在跑的一轮必须等结束（host 直接拒绝），不会自动排队。
