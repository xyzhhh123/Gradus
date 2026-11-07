# coding 层设计

## 1. 定位

`coding` 是 Gradus 的产品层。它不定义循环与协议，只决定「用什么模型、拿什么工具、在什么权限下、把结果给谁看」。

## 2. 依赖方向

```
coding  ->  agent  ->  ai
```

- `coding` 可以使用下层的全部公开接口，包括 `Agent.connect` / `Agent.aclose`、`AgentHooks`、`AgentTool`、`create_provider`。
- `coding` 不修改下层协议。需要新的 hook 点或事件字段时，先改 `agent`，再回来用。
- `coding` 独占：环境变量、API key、连接池生命周期、文件系统与子进程访问。

## 3. 装配关系

一次会话的对象图：

```text
config.load()            -> ModelSpec
session.Session
  ├── httpx.AsyncClient          # 进程级，所有会话共用
  ├── create_provider(spec, key, client=pool)
  └── Agent.connect(..., client=pool)   # 每会话一个，历史隔离
cli
  └── agent.prompt() -> AsyncIterator[AgentEvent] -> render.render()
```

连接池的创建、首次连接与关闭时机见 `agent/docs/design.md` §7.1；`coding` 只负责在进程退出或会话销毁时调用 `aclose()`。

配置来源与优先级：`overrides`（CLI 参数）> `env`（注入的环境变量）> `os.environ` > 配置文件。配置文件默认 `~/.gradus/config.json`（用户级，避免把 key 提交进仓库），可用 `--config` 指定；字段为 `service` / `provider` / `model` / `base_url` / `api_key` / `context_window` / `max_output_tokens`，未知字段直接报错。字符串里的 `${NAME}` 展开成同名环境变量，未设置时报错，便于文件里只写 `${ANTHROPIC_API_KEY}`。默认文件不存在视为没有配置；`--config` 指向的文件不存在则报错。

`service` 是内置服务商快捷名（表在 `gradus/ai/services.py`，与 `ai/scripts/record.py` 共用）：写 `"service": "deepseek"` 就带出 `provider` 与 `base_url`，不必手填。显式写的 `provider` / `base_url` 仍然生效——`base_url` 覆盖表里的地址，`provider` 与 `service` 的协议不一致时报错；服务商名未知时报错并列出可选值。

CLI 启动时先把 `<workspace>/.env` 里的键填入环境变量（`KEY=VALUE` / `export` / `#` 注释 / 值两侧引号；**文件优先**，同名环境变量会被文件里的值覆盖；空值视为未配置，跳过），再按上面的优先级取值。解析在 `gradus/ai/envfile.py`（`ai/scripts/record.py` 也要用，依赖方向只允许放在 `ai` 侧），与 `ai/scripts/record.py` 共用同一份规则。

`.env` 按「一套连接」组织：`GRADUS_SERVICE`（带出协议与 `base_url`）、`GRADUS_MODEL`、`GRADUS_API_KEY` 写在同一个块里，需要自定义端点时再写 `GRADUS_BASE_URL`。单靠 key 认不出服务商——同一协议下有多家（DeepSeek / Kimi / Qwen / GLM 都是 `openai_compat`），同一家又可能提供多种协议（Kimi 三种都有），key 也没有稳定可辨的前缀——所以端点与 key 必须成对出现；换服务商就整段替换，不做「key 反查服务商」的推断。`ai/scripts/record.py` 的 key 解析是「按协议的变量（`OPENAI_COMPAT_API_KEY` 等）优先，其次通用 `GRADUS_API_KEY`」，录制同一家的多种协议时只写一行通用 key 即可。

## 4. 模块职责

| 文件 | 职责 |
| --- | --- |
| `config.py` | 从 JSON 配置文件、环境变量与 CLI overrides 读 key、`base_url`、模型名，产出 `Settings`（`ModelSpec` + key）；不做网络 |
| `session.py` | 建池、建 Provider、建 `Agent`；装配 `LoopConfig.system`（含项目规则文件）与 `AgentHooks`；会话文件的创建、恢复与链式落盘；超预算时触发压缩并重建 `Agent` |
| `context.py` | token 估算（CJK 加权、真实 usage 做锚点）、压缩切点、旧消息摘要；不读写会话文件 |
| `retry.py` | 运行级重试策略：同模型重试、退避、判断哪些 `AgentError` 值得重跑；不换模型 |
| `registry.py` | 工具装配：`ToolRegistry` 按名字去重、保持顺序；`ToolCatalog` 按 `direct` / `deferred` / `hidden` 分组；`FunctionTool` 把普通函数适配成 `AgentTool` |
| `mcp.py` | 读 `.gradus/mcp.json`（含项目信任）、连 MCP server（stdio / Streamable HTTP）、把远端工具投影成 `AgentTool`、单 server 失败隔离 |
| `oauth.py` | MCP 的 OAuth 支撑：token 文件存储、本地回调页、带 `auth` 的 HTTP 客户端 |
| `sandbox.py` | 执行环境：`local` / `docker`（**测试中**）/ Windows 后端；命令与 codemode 都通过它起进程 |
| `restricted.py` | Windows 目标后端：合成 SID + 受限令牌（`WRITE_RESTRICTED`），读跟随用户、写只在副本 |
| `win32.py` | 两个 Windows 后端共用的原语：管道、Job Object、最小环境块、命令行拼接、ACL |
| `appcontainer.py` | Windows AppContainer 后端（**已弃用**，保留可用、不再演进）：零安装的进程级边界，`powershell` 与 codemode 走它 |
| `workspace.py` | 沙箱自有 workspace：git worktree 优先、复制兜底；关闭时把改动合回宿主并清理副本 |
| `extensions.py` | 扫 `<workspace>/.gradus/extensions`，动态加载工具、斜杠命令与技能；单个文件失败只告警 |
| `tools/mcp_resources.py` | `list_mcp_resources` / `list_mcp_resource_templates` / `read_mcp_resource`；跨 server 汇总、分页、二进制落临时文件 |
| `render.py` | 把 `AgentEvent` 渲染成终端文本；不定义 hook、不做权限判断 |
| `permissions.py` | 按工具名 + 路径 / 命令前缀判断放行、拒绝还是询问；工具结果截断与脱敏 |
| `tools/files.py` | `read_file` / `list_dir` / `write_file` / `edit` |
| `tools/search.py` | `grep`（内容）/ `glob`（路径）；跳过常见忽略目录，扫描在线程里跑 |
| `tools/shell.py` | `bash` / `powershell`；只在解释器存在时注册，命令在 workspace 内执行 |
| `tools/tool_search.py` | `tool_search`：在任意来源的 `deferred` 工具里按名字 / 描述检索，登记给 `prepare_next_turn`；不执行选中的工具 |
| `tools/codemode.py` | `codemode`：起子进程跑模型写的 Python，通过 JSON 桥调用已声明工具；脚本级一次确认 |
| `tools/codemode_harness.py` | 子进程执行器：受限 builtins、AST 过滤、`tools.*` 代理；不 import gradus |
| `cli.py` | 参数解析、交互循环、把用户输入接到 `session` |

## 5. 工具契约

沿用 `agent.base.AgentTool`：

- `spec: ToolSpec`：名字、描述、JSON Schema；
- `execution_mode`：只读工具可声明 `parallel`，写操作与命令保持 `serial`；
- 工具自身超时用内部 `asyncio.timeout`，`LoopConfig.tool_timeout` 只作兜底；
- `async execute(call_id, arguments, on_update=None) -> ToolResult`；
- 长输出通过 `on_update` 上报进度；`details` 等富字段只给 UI，不进入模型上下文。

工具集通过 `LoopConfig.tools` 注入，新增工具不改契约。

路径：相对路径基于会话 cwd 解析；工具内不做沙箱，绝对路径与 `..` 允许，越界拦截归 M3 的 `before_tool_call`（对齐 Pi）。`read_file` 上限 2000 行或 50KB，`list_dir` 上限 500 项，输出都不带行号。

写：`write_file` 新建或覆盖，自动创建父目录；`edit` 用 `edits[]` 做精确替换，每个 `old_text` 必须在原文中唯一且互不重叠，未读过文件时拒绝（`FileTracker` 记录 `read_file` / `write_file` 成功过的路径）。

搜索：`grep` 支持 `pattern` / `path` / `glob` / `ignoreCase` / `literal` / `context` / `limit`（默认 100 条），输出 `path:line: text`，单行截断到 300 字符；`glob` 支持 `pattern` / `path` / `limit`（默认 1000 条），输出相对路径。两者都跳过 `.git`、`node_modules`、虚拟环境等目录，不读 `.gitignore`，扫描走 `asyncio.to_thread`。

命令：`bash` 与 `powershell` 分开注册，模型按环境选；只注册 PATH 里真实存在的解释器，`powershell` 仅 Windows。`cwd` 固定为 workspace，stdout / stderr 合并，CRLF 统一成 LF；非零退出码标记 `is_error` 并附上退出码；默认超时 120s（上限 600s），输出上限 30000 字符。

命令没有沙箱：`cwd` 只是起点，`cd ..` 或绝对路径可以离开；危险命令黑名单是字符串前缀匹配，可被绕过；实际闸门是交互确认（无确认回调时命令一律拒绝）。需要真正隔离时用容器或虚拟机运行 Gradus；内置隔离是延缓项，见 [coding_plan.md](../coding_plan.md) 的 M14。

结果治理：`after_tool_call` 把所有工具结果截断到 50k 字符，并把 `sk-...`、`Bearer ...`、`api_key=...` 替换为 `***`；未发生变化时返回 `None`，不复制结果。

内置工具与注册工具走同一个契约：全都实现 `AgentTool`，由 `registry.merge_tools` 按顺序合并，重名直接抛 `ToolError`（启动即失败，不覆盖内置工具）。自定义 Python 工具用 `FunctionTool` 包一层；MCP 工具按下面的规则投影。

### 5.1 MCP

配置读两处：

- 用户级：`~/.gradus/mcp.json`，所有项目共享，不需要项目信任；
- 项目级：`<workspace>/.gradus/mcp.json`，默认路径首次读取需要信任（见下）。

按 server 名合并：同名时项目配置整体覆盖用户配置（包括用 `enabled: false` 关掉用户级条目）；不同名时两边都保留。`--mcp` 指定路径时只读该文件，不合并用户级配置。文件不存在表示不启用。

```json
{
  "servers": {
    "files": {"command": "npx", "args": ["-y", "server-filesystem", "."]},
    "remote": {"url": "https://example.com/mcp", "headers": {"Authorization": "Bearer ..."}}
  }
}
```

- 每个 server 必须且只能给 `command`（stdio 子进程）或 `url`（Streamable HTTP）之一；其余字段只允许 `args` / `env` / `cwd` / `headers` / `oauth` / `enabled` / `timeout` / `exposure` / `toolExposure` / `description`，未知键报错；`timeout` 是单次调用秒数，默认 60；`enabled: false` 保留条目但不连接；
- 信任：默认路径 `.gradus/mcp.json` 首次读取时在终端确认，确认后把项目绝对路径记进 `~/.gradus/trusted.json`；无确认回调（非交互）时跳过并给出告警；`--mcp` 显式指定路径视为已经同意；`gradus --untrust [路径]` 撤销信任（省略路径时用当前 workspace），执行完直接退出，不读模型配置；
- 工具名统一为 `mcp__<server>__<tool>`，非法字符替换成 `_`，避免不同 server 的同名工具冲突；
- 工具的 JSON Schema 直接取 MCP 的 `input_schema`；描述前加 `[server]` 前缀；
- `annotations.read_only_hint` 为真时 `execution_mode="parallel"`，否则 `serial`；
- 结果把文本块拼成一段；超过 20000 字符时保留首尾、去掉中间，并把全文写入临时文件，路径随结果返回；图片 / 音频 / 内嵌资源只留占位（多模态结果进历史待 ai 的 AG-19）；没有文本时退回 `structured_content` 的 JSON；
- 权限：MCP 工具名不在 `Policy.read_tools` / `write_tools` 里，按未知工具处理，默认询问；`read_only_hint` 不自动放行；
- 生命周期：`Session.connect` 建连并列出工具；单个 server 连不上只记进告警（`<server>: <原因>`），其余 server 与内置工具照常可用；配置形状错误仍然直接失败；`Session.aclose` 关闭全部连接；
- 重连：每次请求失败（超时、连接断开）先重连一次再重试；重连失败则返回原始错误；并发调用由句柄内的锁保证只重连一次；资源工具走同一套逻辑；
- 变更通知：`notifications/tools/list_changed` 与 `notifications/resources/list_changed` 只置标记；下一次 `Session.prompt` 开头重新列出有变更的 server，重建工具集与 `Agent`（历史不变）；列不出工具的 server 保留旧工具；
- 诊断：stdio server 的 stderr 写入临时文件；连接失败时把最后 2000 字符附在告警的 `[stderr]` 段里，连接成功后关闭该文件；
- 依赖：`mcp` / `httpx2` 是硬依赖，不做可选安装（已定）；未配置 server 时只多付安装体积，不建连接、不占运行时资源；
- 加载时机：工具在 `Session.connect` 时从 server 拉取并投影成元组，不监听**配置文件**变化、不热重载、不按名字动态导入代码；增删 server 或改 `exposure` 需要重启会话。server 主动发出的 `list_changed` 通知是例外，按上面的刷新规则处理。

暴露策略（M11）：

- `exposure` 取 `direct` / `deferred` / `hidden`，server 级默认 `deferred`；`direct` 直接进 `LoopConfig.tools`，`hidden` 完全不投影，`deferred` 连上但不声明；
- `toolExposure` 是 `工具名 -> exposure` 的覆盖表：先按精确名匹配，再按配置顺序取第一个命中的通配符（`fnmatch`），最后回落到 server 的 `exposure`；
- `description` 是给模型看的 server 说明，只用于系统提示里的 deferred 列表；
- 存在 deferred 工具时，`Session.connect` 在系统提示末尾追加一段「有工具可用但未声明」的通用说明，并列出 MCP server 的名字 + 描述（将来其它来源的 deferred 工具复用同一段说明）；
- 暴露策略由 `registry.ToolCatalog` 统一持有：内置工具与直接声明的 MCP 工具按 `direct` 入册，MCP 的 `deferred` 工具按 `deferred` 入册，`hidden` 不经过目录；工具来源不限于 MCP，将来扩展工具或脚本工具可以按同一套方式入册；
- `tool_search(query, limit=8)`：在 `catalog.deferred` 里按「名字命中 +3、描述命中 +1」排序，返回命中的工具说明，并把它们登记为待加载；本身只读、`parallel`、不受权限询问；
- 加载通过 `prepare_next_turn` 生效：只把新工具追加进下一轮 `LoopConfig.tools`，不写会话文件、不改历史消息；加载结果只在本次运行内有效，下一次 `prompt` 从原始工具集重新开始；
- 已声明的工具不会重复追加；`tool_search` 只在 `catalog.deferred` 非空时注册。

MCP 资源（M12 第一部分）：

- 三个工具名与参数对齐 Codex / Pi：`list_mcp_resources`、`list_mcp_resource_templates` 接收 `server` / `cursor`，`read_mcp_resource` 必须同时给 `server` 与 `uri`；
- 不传 `server` 时列出全部已连 server、自动翻页、按 server 名排序；单个 server 失败只进 `errors`，不影响其它 server。传 `server` 时只列一页，`nextCursor` 续页；`cursor` 不能脱离 `server` 使用；
- 列表输出 JSON，字段用 camelCase（`uriTemplate` / `mimeType` / `nextCursor`），值为 `None` 的字段省略；`ui://` 与 `mimeType` 含 `mcp-app` 的界面资源跳过；
- `read_mcp_resource` 的文本内容直接返回；多段内容每段加 `uri:` 前缀；二进制内容 base64 解码后写临时文件，结果里给路径；
- 三个工具都是只读：`permissions.py` 直接放行、`execution_mode="parallel"`，总输出走 20KB 中间截断；
- 连接时探测 `resources/list`：成功的 server 记进 `McpSession.resource_handles`，失败或超时视为「不支持 resources」，工具仍然可用；只有存在 `resource_handles` 时才注册这三个工具；
- 每次调用按该 server 的 `timeout` 收口。

OAuth 登录（M12 续）：

- 启用：HTTP server 加 `"oauth": {}`；可选字段 `clientId` / `clientSecret` / `scope` / `clientName` / `callbackPort` / `timeout`（默认 300 秒）；stdio server 不支持，`headers.Authorization` 与 `oauth` 互斥，都在配置阶段报错；
- 实现直接用 `mcp.client.auth.OAuthClientProvider`：元数据发现、动态注册、PKCE、刷新与 401 重试由 SDK 负责。本层只补三样：`FileTokenStorage`（`~/.gradus/mcp-auth.json`，权限 0600，按 `server|url` 分记录）、`CallbackServer`（`127.0.0.1` 的 `/callback`，成功 / 失败页）、带 `auth` 的 `httpx2.AsyncClient`；
- 触发：连接时遇到 401 就打印授权链接并打开浏览器；非交互（`confirm` 为空或 `interactive=False`）不打开浏览器，直接记成「需要登录」告警；
- 端口复用：优先用记录里的 `redirectUri` 端口，被占用则退回随机端口，并清掉旧注册让 SDK 重新注册；配置里写了 `clientId` / `clientSecret` 时不走动态注册，直接写入存储；
- token 明文存放，只靠文件权限；不加跨进程锁；
- 刷新由 SDK 自动完成；刷新失败会重新走授权，非交互环境表现为连接告警。

上面是当前实现的契约；进度与取舍以 [coding_plan.md](../coding_plan.md) 为准。

### 5.2 codemode

`codemode(code, timeout?)` 让模型写一段 Python，在独立子进程里跑，通过桥调用已声明的工具。

- 脚本可见的工具是 `catalog.direct` 里的内置工具、直接声明的 MCP 工具与资源工具；不含 `codemode` 自己与 `tool_search`，deferred 工具也不暴露给脚本；
- 调用形状 `tools.<name>(**kwargs)`，返回工具内容字符串；工具返回 `is_error` 时脚本里抛 `RuntimeError`，脚本可以自己捕获；
- 只有 `text(...)` 与 `return` 的值进模型上下文，`print` 的输出丢弃；结果上限 30000 字符；
- 权限：`codemode` 本身按只读放行；脚本内每次调用都走 `Policy.decide`，`ask` 类（写、命令、未知工具）在同一次脚本里只确认一次，`deny` 直接拒绝；没有确认回调时按拒绝处理；
- 隔离：子进程用 `python -I`，只注入白名单 builtins，AST 拒绝 `import`、危险内建名（`eval` / `exec` / `open` 等）与下划线开头的属性访问；
- 超时默认 120 秒、上限 600 秒；超时或父进程退出时子进程被 kill；临时脚本文件在结束后删除；
- **这不是沙箱**：进程仍是同一 OS 用户，绕过桥直接读写文件是可能的；真隔离（进程树、网络、文件系统、资源限制）归 M14。

### 5.3 扩展与技能

扩展不加代码、不改契约，只放文件。目录只有一个：`<workspace>/.gradus/extensions`，跟着仓库走。

- `tools/*.py`：模块定义 `create_tool()`，返回一个 `AgentTool` 或它们的序列；按文件路径 `importlib` 导入，模块名带路径哈希，同名文件互不干扰；
- `commands/*.md`：文件名即命令名，正文是提示词；`$ARGUMENTS` 替换成命令参数，没有占位符时参数追加在正文后；
- `skills/*/SKILL.md`：frontmatter 里必须有 `description`，`name` 缺省用目录名；系统提示只列「名字 + 描述 + 路径」，正文由模型用 `read_file` 按需读取，也可以用 `/skill:<名字>` 强制展开。

冲突：同一目录内同名按文件名顺序，后加载的覆盖前面的；扩展工具与内置工具重名时跳过并告警（不覆盖内置）；单个文件加载失败只记一条告警，其余照常。

信任：目录里是会执行的 Python，与 `.gradus/mcp.json` 共用同一份信任记录（`~/.gradus/trusted.json`，按项目路径记）。未信任且没有确认回调时跳过并告警；拒绝信任同样跳过。

命令展开发生在 `Session.prompt`：以 `/` 开头但不是已加载命令时抛 `SessionError`，不发给模型；`Session.commands` / `skills` / `extension_warnings` 供 CLI 展示。

热加载：CLI 的 `/reload` 重新扫描目录，重建工具集、系统提示与 `Agent`；历史保留，MCP 连接不动。启动时未信任的扩展目录不会重新询问，只提示重启。仍然不监听文件变化。

### 5.4 项目规则文件

启动时从 workspace 向上逐层找规则文件，行为对齐 Pi：

- 每层按 `AGENTS.override.md` / `AGENTS.md` / `AGENTS.MD` / `CLAUDE.md` / `CLAUDE.MD` 取第一个存在的；
- 一路走到文件系统根，按「根 -> workspace」拼进系统提示，近的（workspace）排在后面，覆盖远的约定；
- 渲染成 `<project_context>`，每个文件一段 `<project_instructions path="...">`；
- 不需要信任：只是文本，不产生额外能力；不做大小限制，内容计入上下文预算；
- `Session.connect(system=...)` 显式给系统提示时不拼规则文件（调用方全权接管）；
- `Session.connect` 读一次，`/reload` 与 MCP 工具刷新会重读；不监听文件变化；
- `Session.context_files` 暴露实际用到的路径，CLI 启动时打印 `context: ...`。

### 5.5 执行环境（沙箱）

命令与 `codemode` 不直接起进程，而是拿一个 `Environment`（`sandbox.py`）：

- 缺省值是 `auto`：按 `restricted` -> `appcontainer` -> `docker` -> `local` 的顺序挑第一个可用的；一个都没有时回落 `local`，并用 `sandbox_warning` 说明原因；
- `local`：现状，宿主用户权限；
- `restricted`：Windows 目标后端（`restricted.py`）。每个会话生成一个合成 SID（`S-1-5-21-<4 个随机数>`），只对副本与 scratch 授 `(OI)(CI)M`；以当前进程主令牌为 base 调 `CreateRestrictedToken(DISABLE_MAX_PRIVILEGE | LUA_TOKEN | WRITE_RESTRICTED)`，受限 SID 清单是「合成 SID + logon SID + `Everyone`」，再用 `CreateProcessAsUserW` 起进程。**读按当前用户，写只落在圈内**；不建账户、不要管理员。传给 `CreateProcessAsUserW` 的是自身主令牌的受限版本，这种情形免 `SeAssignPrimaryTokenPrivilege`；
- `docker`：长驻容器 + `docker exec`；workspace 读写挂到 `/workspace`，临时目录挂 `/gradus-tmp`，codemode harness 只读挂 `/opt/gradus/codemode_harness.py`。**测试中**：保留为跨平台备用，尚未在真实引擎上跑完 §5.5.1 的实机契约；断网场景用 `docker`，`restricted` 做不到；
- `appcontainer`：Windows 零安装后端（`appcontainer.py`），用系统自带的 AppContainer 建进程级边界，不需要额外安装。**已弃用**：模块与类用 `warnings.deprecated`（PEP 702）标记，保留可用、不再演进；文件本身冻结——除一次性机械搬移（共用原语抽到 `win32.py`）外不再改动，新能力只进 `restricted.py` / `win32.py`；
- 默认 `--network none`（`sandbox_network: true` 才放行）、`--memory 2g --cpus 2 --pids-limit 512`，不继承宿主环境变量；
- 超时直接丢弃容器，下次调用重建；会话关闭时 `docker rm -f`；
- 强杀残留：容器带 `gradus.owner=<pid>` 与 `gradus.user=<用户名>` 标签，下次以 docker 后端启动时先清理「owner 进程已不存在」的容器；pid 被系统复用时最多留下一个旧容器，不会误删正在用的；别的用户的容器不碰（Docker daemon 是机器级共享资源）；
- 拿不到 Docker 时**不回落 local**：`bash` / `powershell` / `codemode` 都不注册，`Session.sandbox_warning` 给出原因，CLI 以 `sandbox warning:` 打印；
- 边界：workspace 是沙箱私有的副本（见下），沙箱内仍能改坏副本；`restricted` 挡住的是副本之外的写，`appcontainer` / `docker` 还挡读与网络。**`restricted` 不阻断网络**——unelevated 没有防火墙规则可用，需要断网就用 `docker`。因此命令审批不放开；
- **MCP server 进程不在沙箱边界内，但 stdio server 的缺省 cwd 指向副本**：stdio server 由 MCP SDK 在宿主直接起进程（`stdio_client`，不经过 `Environment.spawn`），HTTP / SSE server 从宿主直连。沙箱会话里，配置没写 `cwd` 的 stdio server 会把 cwd 换成副本（`session._mcp_servers_in`），它读写的相对路径于是和文件工具 / 命令一致，改动随副本在关闭时合回；显式写了 `cwd` 的保持原样。限制仍在：server 以宿主用户权限运行，能读写 workspace 之外的文件、默认联网，绝对路径参数仍指向宿主；HTTP / SSE 没有 cwd，操作的是宿主；参数级权限只能落回「未知工具 → 询问」。这条只保证路径一致，不提供隔离。stdio server 的环境变量由 MCP SDK 收窄成白名单（Windows 上 12 个安全变量：`PATH` / `SYSTEMROOT` / `APPDATA` / `TEMP` 等）再并上 `mcp.json` 的 `env`，宿主里的 `GRADUS_API_KEY` 之类不会传过去；`tests/test_mcp.py::StdioEnvironmentTest` 用真实子进程验证了这一点；
- 文件工具与命令绑在同一份副本上：`read_file` / `write_file` / `edit` / `delete_file` / `grep` / `glob` 看到的就是命令看到的目录，权限策略的 workspace 边界也换到副本。

沙箱自有 workspace（`workspace.py`）：

- 只在沙箱后端（`restricted` / `appcontainer` / `docker`）下建副本；`local` 直接在宿主上操作；
- 形态优先 `git worktree`：仅当 workspace 本身是 git 仓库根时 `git worktree add --detach <临时目录> HEAD`；worktree 只有已提交内容，因此建好后会用 `git ls-files --others -z` 把**未跟踪文件（含被 `.gitignore` 忽略的 `.env`、本地脚本）**复制进去（跳过 `.venv` / `node_modules` / 构建产物等目录）。不复制的话模型在副本里根本看不到它们；
- 兜底是整树复制，跳过 `.git` / `.gradus` / `node_modules` / `__pycache__` / `.venv` / `venv` / `.mypy_cache` / `.pytest_cache` / `.ruff_cache` / `dist` / `build` / `target`；
- 关闭会话时自动合回宿主：worktree 模式在副本里 `git add -A` 后取 `diff --cached --binary`（路径限定到该打的文件），用 `git apply` 打到宿主；带进来的未跟踪文件不走补丁（补丁会当成新建），改动直接 `copy2`、内容没变则跳过，被删掉的在宿主上 `unlink`；复制模式按大小 + mtime（必要时比内容）找出新增 / 修改并 `copy2` 回去；
- 删除也会同步：worktree 里已跟踪文件的删除由补丁完成，未跟踪文件与复制模式的删除按「建副本时存在、之后在副本里消失」判定后删宿主文件；复制模式用建副本时的文件清单做基线，会话期间宿主**新建**的文件不会被误删；
- 合并失败不抛异常：保留副本，`Session.workspace_sync` 给出 `error` 与 `kept` 路径，CLI 以 `workspace: merge failed:` 提示；合并成功后删副本，worktree 同时 `git worktree remove` + `prune`；
- 副本放在 `%TEMP%\gradus-ws-<pid>-<id>`，旁边一个 `.owner` 记录 owner pid、用户名与宿主路径；启动时按 pid 清理强杀留下的副本（worktree 先解除注册），别的用户的副本不碰，结果并进 `Session.sandbox_orphans`；
- 副本必须直接放在 `%TEMP%` 下、不能嵌套子目录：AppContainer 进程在更深的目录里设不了工作目录，PowerShell 会退回 `C:\`，相对路径全部失效（`restricted` 不受这条限制，副本位置统一处理）；
- 顺序：先 `sync()` 合回，再让环境撤销 ACE / 销毁容器，最后删副本；
- 系统提示里的 `cwd` 用副本路径，模型执行 `pwd` / `Get-Location` 看到的就是它；
- 会话文件、项目规则文件、扩展与 MCP 配置仍读宿主 workspace，不读副本。

临时目录回收（`sandbox.cleanup_temp`）：

- 每次 `Session.connect` 建副本前静默跑一次，只碰固定前缀下自己的东西：`gradus-mcp-*`（MCP 结果落盘）、`gradus-tmp-*` / `gradus-rs-*`（会话 scratch）、`gradus-ac-*`（AppContainer scratch）；
- scratch 目录带 `.owner`（创建者 pid）：owner 已死立刻删，owner 还活着就保留；AppContainer 已冻结、没有标记，按 7 天窗口兜底；
- `gradus-mcp-*` 是一次性产物，超过 1 天就删；
- 删除失败只跳过，不影响会话；结果不进 `sandbox_orphans`，避免每次启动刷屏；
- 副本 `gradus-ws-*` 不走这里，仍按 `.owner` + pid 由 `workspace.cleanup_orphans` 处理（它能区分用户，比时间窗口准）。

`restricted` 后端的已知约束：

- 只在 Windows 可用；`problem()` 给出原因时同样按「禁用命令类工具 + 告警」处理；
- **不阻断网络**，也没有 `sandbox_network` 开关；CLI 横幅固定写 `network NOT isolated`；
- 受限 SID 清单只放合成 SID、logon SID 与 `Everyone`，**不能放当前用户 SID**——那会把用户能写的位置全部放行。清单里只有合成 SID 时进程起不来（`0xC0000142`，DLL 初始化失败）；补上 logon SID 与 `Everyone` 后正常（没有单独拆验是哪一条必需）；
- 合成 SID 没在系统里注册，`icacls *<SID>` 报 1332（no mapping）；授 / 撤 ACE 走 PowerShell 的 `Get-Acl` / `Set-Acl` 配 `SecurityIdentifier` 对象；
- logon SID 指向 `GetTokenInformation(TokenGroups)` 缓冲区内部，不能 `LocalFree`，否则进程堆损坏（`0xC0000374`）；
- ACE 只落在副本与 scratch 上，两者随会话删除，所以没有 profile、没有 `acl-grants.json`，`--reset-sandbox` 不需要处理它；`aclose` 仍先撤 ACE，避免合并失败保留副本时留下能写的目录；
- 副本所在位置必须让受限 SID 可写：`%TEMP%` 被重定向到 `C:\Windows\Temp` 这类目录时，父目录的 traverse 只对登录用户开放，写会被拒（IDE 终端里就是这样，实机测试要先把 `TEMP` 指到用户临时目录）；
- 最小环境块把 `TEMP` / `TMP` / `USERPROFILE` / `APPDATA` 都指到 scratch；工具往这些地方写会落到 scratch；
- 进程放进 Job Object（`KILL_ON_JOB_CLOSE`），父进程一死子进程连带退出。

`appcontainer`（已弃用）的已知约束：

- 只在 Windows 可用；`problem()` 给出原因时同样按「禁用类命令工具 + 告警」处理；
- 副本目录必须让 AppContainer 能 traverse：`%TEMP%` 落在用户 profile 下（`C:\Users\<user>\AppData\Local\Temp`）时，AppContainer 设不了工作目录，PowerShell 退回 `C:\`、相对路径全失效；实机验证要在 `TEMP=C:\Windows\Temp` 下跑。`restricted` 不受这条限制；
- 没有网络能力开关：AppContainer 不给 capability 时默认断网，`sandbox_network` 被忽略并给出 warning；
- 容器里没有 Git Bash（安装目录不在授权范围），因此只注册 `powershell`；
- 命令里删文件要用 .NET：`Remove-Item` / `cmd del` 在 AppContainer 里一律 `Access is denied`（目录已授 `M` 甚至 `F` 也一样），`[System.IO.File]::Delete()` / `[System.IO.Directory]::Delete()` 正常；改目录权限、`DE,DC` 都不能绕过这一条。文件工具不受影响：`delete_file` 走宿主进程删副本里的文件，不经过 AppContainer；
- 文件授权：workspace 与临时目录递归授「修改」，会话结束撤销；解释器只授 `Lib`（跳过 `site-packages`）、`DLLs`、`Library\bin` 等必要子树，ACE 不撤销，并用 `%LOCALAPPDATA%\gradus\acl-*.done` 标记跳过重算——Windows 修改目录 DACL 会遍历整棵子树，对 conda 这种几十万文件的根目录，重复授权会卡几分钟；
- 强杀残留：授权时把「路径 -> 进程 pid」记进 `%LOCALAPPDATA%\gradus\acl-grants.json`，下次以 appcontainer 后端启动时撤销 owner 进程已不存在的路径（当前的 workspace 例外），结果走 `Session.sandbox_orphans`，与 docker 的孤儿容器同一套展示；
- 子进程用最小环境块（不继承宿主 key），其中 `LOCALAPPDATA` 不能省：AppContainer 靠它定位 `Packages\<SID>`，缺了 `CreateProcess` 直接报 `ERROR_ENVVAR_NOT_FOUND`；
- 进程放进 Job Object（`KILL_ON_JOB_CLOSE`），父进程一死子进程连带退出；
- codemode 的 harness 会复制一份进临时目录再执行：沙箱里只有 workspace、临时目录与解释器目录可读，包安装目录不在其中。

#### 同机多用户与并发实例

同一台机器上多个 OS 用户各跑一个 Gradus 时，隔离靠「每用户私有目录 + 只清理自己的残留」：

- 临时目录与 `%LOCALAPPDATA%\gradus` 按用户分开（Windows 默认 `C:\Users\<user>\AppData\Local\...`），副本、scratch、ACL 记录、解释器标记都落在里面；
- AppContainer profile 名带用户摘要（`gradus-sandbox-<hash>`）：SID 只由名字派生、不含用户，固定名字会让同机所有用户共用同一个 SID，共享 workspace 上的 ACE 就会互相生效；现在两个用户各拿各的 SID；
- 清理只碰自己的：副本 `.owner` 记录 pid + 用户名，Docker 容器带 `gradus.user` 标签；pid 存活、或用户名不匹配都跳过；
- `gradus --reset-sandbox`：显式清掉强杀残留、`acl-grants.json`、`acl-*.done` 标记与 AppContainer profile；检测到别的 Gradus 进程还在用 appcontainer 时拒绝执行（删 profile 会让它的 SID 失效）。`restricted` 没有持久状态，不受这个命令影响；
- 这不等于服务化多租户：同一用户的两个会话仍共享一个 profile 与一份 ACL 记录，文件权限跟随 OS 用户，不按会话再切一层。要做「一个服务进程接待多个互不可信的租户」，需要每租户独立进程 / 容器与独立凭证，属于另一层架构。

### 5.5.1 沙箱使用契约

改沙箱相关代码（`sandbox.py` / `restricted.py` / `win32.py` / `appcontainer.py` / `workspace.py` / `session.py` 的沙箱接线）时按下表核对，不要靠手工试。**每次改动前后各跑一次**：

```powershell
uv run pytest -q                                         # 离线契约
$env:GRADUS_SANDBOX_LIVE="1"
uv run python -m pytest gradus/coding/tests/test_sandbox_live.py -q -s   # 实机契约（缺省 restricted）
$env:GRADUS_SANDBOX_BACKEND="appcontainer"               # 换后端再跑一遍
uv run gradus --reset-sandbox                            # 怀疑授权 / profile 坏了时先清一次
```

实机检查覆盖并逐条打印：副本位置与工作目录、相对路径读写、已跟踪 / 未跟踪文件可见、越界读写、删除、断网、超时、codemode、关闭时合回（新增 / 修改 / 删除）、孤儿副本清理。读与断网两条按后端不同：`restricted` 允许读副本之外、不阻断网络，`appcontainer` 两者都拦。它跑不过就是契约被破坏，不需要再写一次性探针脚本。

| 约束 | 原因 | 违反后的症状 | 实机检查 |
| --- | --- | --- | --- |
| 受限 SID 清单必须含 logon SID 与 `Everyone`，且不能含当前用户 SID | 只有合成 SID 时 DLL 无法初始化；含用户 SID 则用户能写的位置全部放行 | 进程立刻退出（`0xC0000142`）或写边界失效 | `test_cwd_and_relative_paths`、`test_reads_and_writes_outside_are_denied` |
| logon SID 不能 `LocalFree` | 它指向 `GetTokenInformation` 缓冲区内部 | 进程堆损坏（`0xC0000374`），`aclose` 时崩溃 | `test_sandbox.py` + 实机关闭会话 |
| 合成 SID 的 ACE 用 `Set-Acl`，不要用 `icacls *SID` | 未注册的 SID 让 icacls 报 1332 | 授权失败，副本不可写 | 实机 `test_cwd_and_relative_paths` |
| 副本必须直接位于临时目录下一层 | AppContainer 进程在更深目录里设不了工作目录 | PowerShell 退回 `C:\`，`Test-Path .` / 相对路径全部失败 | `test_backend_and_copy_location`、`test_cwd_and_relative_paths` |
| 命令里删文件只能用 `[IO.File]::Delete`；文件删除用 `delete_file` | `Remove-Item` / `cmd del` 在 AppContainer 里被平台拒绝，授 `M` / `F` / `DE,DC` 都不行；`delete_file` 走宿主进程 | 模型 `rm` 失败，删除类操作整体不可用 | `test_delete_works_through_dotnet`、`test_delete_file_tool_removes_on_host` |
| worktree 必须另外带进未跟踪文件 | `git worktree add` 只检出 HEAD，`.env`、本地脚本不在任何 commit 里 | 模型看不到这些文件，命令报路径不存在 | `test_existing_and_untracked_files_are_visible` |
| 带进来的未跟踪文件不能进补丁 | 补丁会把它们当成新建，宿主已有同名文件 → `git apply` 失败 | 整个合并失败、副本被迫保留 | `SandboxMergeTest` |
| 关闭顺序必须是 `sync()` → `env.aclose()` → `discard()` | ACE 撤销与容器销毁都需要目录还存在 | 撤销失败、留下 ACE 或容器 | `SandboxMergeTest`、`test_sandbox.py` |
| 解释器 ACE 只授必要子树并写 `acl-*.done` 标记 | 改目录 DACL 会遍历整棵子树，conda 几十万文件要卡几分钟 | 首次启动 26s 以上 | 启动耗时（`_prepare` 命中标记 0.06s） |
| `SECURITY_CAPABILITIES` 必须活到 `CreateProcess` 之后 | 属性列表存的是它的地址，被 GC 后访问违例 | 进程起不来、`0x2` 访问违例 | 代码注释 + `test_sandbox.py` |
| 最小环境块必须含 `LOCALAPPDATA` / `APPDATA` | AppContainer 靠它定位 `Packages\<SID>` | `CreateProcess` 报 `ERROR_ENVVAR_NOT_FOUND` | `test_sandbox.py` |
| 副本带 `.owner`（pid / 用户名 / 宿主 / worktree）并按 pid 清理，别人的副本不碰 | 强杀时没有跨进程信号可用；临时目录可能被重定向到共享位置 | `%TEMP%` 堆积副本、`git worktree list` 留死条目、误删他人的副本 | `test_dead_copy_is_removed_on_start`、`test_skips_copies_owned_by_another_user` |
| 复制模式用建副本时的基线判定删除 | 「宿主有、副本没有」无法区分删除与宿主新建 | 误删会话期间用户新建的文件 | `test_host_files_created_during_the_session_are_kept` |

使用方式（不要在契约之外自己拼）：

```python
one = await session.Session.connect(settings, workspace=root)  # sandbox=auto -> restricted -> appcontainer -> docker -> local
one.sandbox_workspace    # 副本路径；local 或兜底时为 None
one.sandbox_orphans      # 启动时清掉的残留（容器 / ACE / 副本）
await one.aclose()       # 先合回宿主，再撤销授权 / 销毁容器，最后删副本
one.workspace_sync       # 合回结果：applied / deleted / error / kept
```

约定之外的事不要做：不要手工 `icacls` 授整棵树、不要自己找 `%TEMP%` 下的副本、不要在关闭后访问副本、不要把 `restricted` 当成会断网的沙箱、不要把文件工具指回宿主根、不要在别的会话还在跑时删 AppContainer profile、不要假设 MCP server 在沙箱里执行（只有 stdio server 的缺省 cwd 跟着副本走）。

## 6. 权限

`before_tool_call` 返回 `BeforeToolCallResult(block=True, reason=...)` 时，agent 不会执行工具，但会生成一条 `is_error` 结果交给模型。

已定：`before_tool_call` 是 async hook，需要确认时直接 `await` 一个注入的确认回调即可，不阻塞事件循环。

默认策略：只读工具（`read_file` / `list_dir` / `grep` / `glob` / `tool_search` / `list_mcp_resources` / `list_mcp_resource_templates` / `read_mcp_resource` / `codemode`）放行；写工具（`write_file` / `edit` / `delete_file`）与命令（`bash` / `powershell`）询问；敏感路径（`.env*`、`*.pem`、`id_rsa*`、`.git/*`）和危险命令（`rm -rf`、`sudo`、`git push --force`）直接拒绝；写越界走询问。白名单先按工具名，参数级规则覆盖路径与命令前缀。`codemode` 只放行脚本运行器本身，脚本内的写与命令由 `CodemodeTool` 按同一策略逐次判断、整段脚本只确认一次。扩展工具默认落在「未知工具」分支，按询问处理。

## 7. 会话与历史

历史落盘为 JSONL，格式对齐 Pi 的会话树：

- 第一行是 `{"type":"session","version":1,"id","model","workspace","created_at"}`；
- 之后每行 `{"type":"message","id":"e1","parentId":null,"message":...}`；当前只写线性链，叶子是最后一条；
- 恢复时从叶子沿 `parentId` 回溯到根；文件里若已有分叉，以最后一行为叶子；
- 压缩时追加 `{"type":"compaction","id","parentId","summary","firstKeptEntryId"}`；原消息不删，只在恢复时按最后一次压缩重放「摘要 + `firstKeptEntryId` 之后的原文」。

`Session.prompt` 在 `AgentDone` 时把 `new_messages` 追加到文件。恢复时校验 entry `id` 唯一、`parentId` 无缺失、压缩引用的 entry 存在，并要求 `ToolCallBlock` 与 `ToolResultMessage` 一一对应；不满足直接拒绝启动。

上下文预算在每轮 `prompt` 开始前检查，不在运行中改历史：

- 估算：宽字符（CJK / 假名 / 谚文）按 1 字 1 token，其余按 3.5 字符 1 token；最近一条成功 assistant 的 `usage`（`input_tokens + output_tokens`）做锚点，只估算它之后的消息；估算口径不追求精确，准确性由锚点与溢出错误兜底；
- 触发：`tokens > context_window - 16384`；
- 切点：从末尾往前累加，取最早一个仍在 20000 token 内的 `user` 消息；只切在 user 处，工具调用与结果不会被拆开；
- 摘要：用同一模型、同一连接池发一次独立请求，不写入会话历史；失败只放弃本次压缩，不打断当前轮次；
- 重建：压缩后用 `Agent.connect(messages=[摘要, *保留原文])` 重建 `Agent`，工具、`FileTracker`、权限策略沿用原实例。

换模型：启动时由 `config` 选定；运行中需要换时，用 `Agent.connect(messages=...)` 重建 Agent，因为 Provider 的模型名在构造期固定。

### 7.1 运行级重试

`ai` 的传输重试只覆盖「尚未吐出任何事件」的失败（408 / 409 / 425 / 429 / 5xx、连接失败）。运行级重试补的是剩下的情况：中途断流、空流、厂商违约——它们都以 `AgentError(kind="provider")` 结束，且失败的半轮不会进入历史。

- 位置：`session._run` 捕获 `AgentError` 后用 `agent.continue_()` 重跑，不重复追加用户消息；
- 次数：额外重试 2 次，退避 0.5s 起、逐次翻倍、单次上限 30s；
- 范围：只重试 `provider`；`aborted` / `max_turns` / `config` 不重试；消息里能解析出 HTTP 状态时，只重试 `RETRYABLE_STATUS` 中的状态（400 / 401 / 403 / 404 / 413 / 422 等不重试）；
- 换模型：不做自动降级链，也不按任务自动选模型；模型由启动配置决定，要换就重建 `Agent`；
- 落盘：`AgentError.new_messages` 是失败前已完成的轮次，与 `AgentDone` 一样追加到会话文件，避免文件落后于内存历史。

## 8. 观测

`coding` 订阅 `AgentEvent`，不改事件。终端渲染集中在 `render.render()`，CLI 只负责读写终端：

- `ToolExecutionStart` / `End`：展示工具名、耗时、首行结果；
- `ToolExecutionUpdate`：实时输出；
- `AgentError`：打印 `kind` 与消息；
- `StreamDone.usage`：累计 token 与缓存命中；
- `--log PATH`：把 `ToolExecutionEnd` / `AgentError` 以 JSONL 追加写入，默认不写文件。

## 9. 未定项

本层未定、推迟与回补条件统一维护在 [coding_plan.md](../coding_plan.md) 里程碑表的「未定 / 推迟」列。
本文件只记录已定的契约，不维护第二份清单。
