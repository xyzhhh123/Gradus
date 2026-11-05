# coding 层实施计划

## 目标与边界

**目标**：把 `ai` 的一次补全和 `agent` 的循环装成一个能直接跑的产品：读配置、建会话、调工具、受权限约束、能落盘、能压缩上下文。

**边界**：只改 `gradus/coding/`。`agent` / `ai` 的协议不改；缺能力时先在下层加口子，并在本文件记录。

**依赖方向**（不可逆）：

```
coding  ->  agent  ->  ai
```

库代码里，`coding` 是唯一允许读环境变量、持有凭证、管理连接池生命周期的层；开发脚本 `ai/scripts/record.py` 例外（`.env` 解析在 `ai/envfile.py`，见偏差 D31）。

文档分工：本文件是唯一的进度与待办来源；`docs/design.md` 只记已定契约；`docs/problem.md` 只记已解决；README 只做入口与职责。计划外取舍不单独记账，历史看 git。

## 里程碑

| 编号 | 内容 | 状态 | 未定 / 推迟（回补条件） |
| --- | --- | --- | --- |
| M0 | 配置、提示词与会话：`config.py` 读配置文件 / 环境变量产出 `ModelSpec`；`session.py` 装配 `LoopConfig.system`（含项目规则文件）与 `AgentHooks`、进程级连接池、每会话一个 `Agent`（`Agent.connect`）；从 workspace 向上读 `AGENTS.override.md` / `AGENTS.md` / `CLAUDE.md` 拼进 `<project_context>` | 完成 | 模型注册表与项目级配置（需要按模型自动带窗口 / 输出上限时） |
| M1 | 只读工具：`read_file`（offset/limit、2000 行 / 50KB、无行号）、`list_dir`（500 项）；相对 cwd 解析，不设沙箱 | 完成 | — |
| M2 | CLI 闭环：`prompt` -> 模型 -> 工具 -> 回答；流式渲染事件；退出时调 `close_pool()`；`overrides` 参数；JSON 配置文件（默认 `~/.gradus/config.json`，`--config` 可覆盖，支持 `${ENV}` 展开）；`service` 内置服务商表（`ai/services.py`，自动带出 `provider` 与 `base_url`）；启动时读 `<workspace>/.env`（文件优先） | 完成 | 多 key / 轮换（未定） |
| M3 | 权限与观测：`before_tool_call` 按工具名 + 参数规则放行 / 拒绝 / 询问；内置敏感路径与危险命令黑名单；`--log` 写 JSONL | 完成 | 缓存命中率展示、稳定 session id（可随时接）；usage 汇总与成本（需要展示统计时） |
| M4 | 写工具与结果约束：`write_file`、`edit`（精确替换、唯一匹配、先读后写）；工具结果截断与脱敏 | 完成 | — |
| M5 | 会话落盘与恢复：JSONL 线性链（每行带 `id` / `parentId`，格式预留树），保证工具调用与结果成对 | 完成 | 用户实体与跨会话偏好（需要跨会话记忆或多用户时）；会话分支 / fork / 导出（需要多分支会话时） |
| M6 | 上下文预算与压缩：`transform_messages` 裁剪、摘要旧消息、保留近期原文；必要时重建 Agent | 完成 | 运行中压缩（需要 agent 提供运行中重建的口子）；压缩提示展示（需要 agent 增加事件时）；摘要带上「读过/改过哪些文件」（压缩后重复读文件成为问题时） |
| M7 | 重试与模型路由：同模型重试（`AgentError(provider)`、指数退避）；不做自动降级或按任务选模型，换模型由启动配置决定，运行中换用重建 Agent | 完成 | 自动降级链 / 按任务选模型（需要时按用户配置或另开里程碑）；并发上限（待 agent 的 AG-18） |
| M8 | 工具注册：内置工具之外的注册表，MCP / 自定义统一转成 `AgentTool`，重名校验 | 完成 | 多模态工具结果进历史（待 ai 的 AG-19）；MCP 进度上报（需要 server 支持 progress） |
| M9 | 安全边界：项目 `.gradus/mcp.json` 默认不执行、需显式信任；`enabled` / `timeout` / 单 server 失败隔离；工具名加 `mcp__` 前缀；结果 20KB 中间截断并把全文落临时文件；`--untrust [路径]` 撤销信任 | 完成 | — |
| M10 | 工具补齐：`grep`、`glob`、`bash` / `powershell`；权限规则覆盖命令前缀与搜索范围 | 完成 | 无（Windows 上命中 `System32\bash.exe` 时已跳过，见 `problem.md`） |
| M11 | MCP 暴露策略：`exposure`（`direct` / `deferred` / `hidden`）、`toolExposure` 按工具名覆盖、`tool_search`（用 `prepare_next_turn` 换 `config.tools`）、`description` 进系统提示 | 完成 | 内置工具能否配 `deferred`（当前只有 MCP server 级配置，`codemode` 固定 `direct`） |
| M12 | MCP 资源与生命周期：`list_mcp_resources` / `list_mcp_resource_templates` / `read_mcp_resource`、断线重连、`tools/list_changed` / `resources/list_changed`、stderr 诊断、用户级与项目级配置合并、OAuth 登录（PKCE + 本地回调页 + token 落盘） | 完成 | — |
| M13 | 扩展与技能：目录约定动态加载——`<workspace>/.gradus/extensions` 下的 `tools/*.py`（`create_tool()`）、`commands/*.md`、`skills/*/SKILL.md`；目录走信任记录；单个文件失败只告警；`/命令` 与 `/skill:名字` 展开成提示词；`/reload` 重新扫描并重建 Agent | 完成 | 扩展注册 provider（已定不做：需要放宽 `ai` 层的 provider 名与工厂）；扩展工具的只读声明（已定不做：声明约束不了实际行为，扩展工具一律按未知工具询问）；扩展文件变化不自动监听 |
| M14 | 执行环境与命令沙箱：`sandbox.py` 提供 `local` / `docker` 两种后端，`bash` / `powershell` / `codemode` 统一走 `Environment`；缺省 `sandbox: auto`，按 `appcontainer` -> `docker` -> `local` 挑第一个可用的；Docker 后端长驻容器、workspace 挂 `/workspace`、默认断网、限内存 / CPU / 进程数、不继承宿主环境变量；拿不到 Docker 时禁用命令类工具并告警；容器带 `gradus.owner=<pid>` / `gradus.user=<用户名>` 标签，启动时清理强杀残留（别人的容器不碰）；`appcontainer.py` 提供 Windows 零安装后端（AppContainer + Job Object，默认断网，只注册 `powershell`；授权记录 `路径 -> pid`，启动时撤销 owner 已死的 workspace ACE）；沙箱自有 workspace：`workspace.py` 在沙箱后端下建副本（git 仓库根走 `git worktree` 并把未跟踪文件带进去，其余整树复制），文件工具与命令都绑副本，关闭时自动合回宿主（含删除），强杀留下的副本按 owner pid + 用户名清理，别人的残留不碰；`delete_file` 走宿主进程删除，绕开 AppContainer 的 `Remove-Item` 限制；AppContainer profile 名带用户摘要（SID 只由名字派生，同机不同用户必须不同名）；`gradus --reset-sandbox` 显式回收 `acl-grants.json` / `acl-*.done` / AppContainer profile（含旧固定名）；沙箱会话里 stdio MCP server 的缺省 `cwd` 指到副本（显式配置的保留），它的相对路径读写和其它工具走同一条合并路径 | 完成（沙箱自有 workspace + Windows AppContainer） | 域名白名单 / egress 代理；modal / ssh 等多后端；恢复「每条命令一个容器」的临时模式；服务化多租户（同机多用户已隔离，但同一用户的多会话仍共享 profile 与 ACL 记录）；MCP server 进程本身入沙箱（stdio transport 改走 `Environment.spawn`、docker 内起进程；HTTP / SSE server 与默认断网冲突，需要白名单） |
| M15 | codemode（Python）：`codemode` 工具接收 Python 源码；脚本在独立子进程运行，通过 JSON-RPC 调用 `tools.*`，只有 `text()` / `return` 的输出进模型上下文；脚本进程用受限 builtins 与 AST 过滤；脚本级一次确认（只读直接放行，写与命令集中确认一次） | 完成 | 与 M14 的关系：受限 builtins 是 API 过滤，不是安全边界；`sandbox: docker` 时脚本进程也进容器。沙箱自有 workspace 已随 M14 落地；「写工具免确认」按用户选择不做 |

未定与推迟只写在最后一列，不单列清单；已定的契约写进 `docs/design.md`。

顺序理由：先有会话装配和只读工具，才能在真实模型上验证；CLI 闭环之后再补权限与落盘，避免一开始就把交互形态定死。写工具排在权限之后，保证 `write_file` / `edit` 第一次出现时已经有拦截策略。M9 起是对照 Pi 盘点后的补齐项，按风险排序：先堵安全边界与可用性缺口，再处理 MCP 的上下文成本。命令沙箱（M14）延缓，先做不依赖 OS 边界的 M11 / M12。M15（codemode）依赖 M11 的暴露档与 M14 的隔离：可以先做子进程桥与脚本级确认，但在 M14 完成前，脚本进程只算 API 隔离，不算安全边界。

## 完成定义

每个里程碑结束时必须同时满足：

1. `uv run python -c "import gradus.coding.<模块>"` 通过；
2. 新增能力有离线测试，不依赖真实 API key；
3. `uv run pytest -q` 全绿，且 `agent` / `ai` 的用例数不变；
4. 本文档的「进度检查」已更新；
5. 动了沙箱（`sandbox.py` / `appcontainer.py` / `workspace.py` / 沙箱接线）时，`GRADUS_SANDBOX_LIVE=1 uv run python -m pytest gradus/coding/tests/test_sandbox_live.py -q -s` 必须全过——按 `design.md` §5.5.1 的契约表核对，不再用一次性脚本试错。

## 进度检查

**已完成**

- 目录骨架、README、设计文档、本计划。
- `sandbox.py`：`Environment` 协议（`scratch_dir` / `map_path` / `python` / `spawn` / `recycle` / `aclose`）；`LocalEnvironment` 就是原来的 `create_subprocess_exec`；`DockerEnvironment` 长驻容器 + `docker exec`，workspace 挂 `/workspace`、临时目录挂 `/gradus-tmp`、harness 只读挂 `/opt/gradus/codemode_harness.py`，默认 `--network none`、`--memory 2g --cpus 2 --pids-limit 512`、不继承宿主环境变量；超时 `docker rm -f` 后懒重建；容器带 `gradus.sandbox=1` / `gradus.owner=<pid>` 标签，`cleanup_orphans()` 在下次以 docker 后端启动时删掉 owner 进程已不存在的残留（`_pid_alive()` 在 Windows 走 `OpenProcess` + `GetExitCodeProcess`，POSIX 走 `os.kill(pid, 0)`）；`docker_problem()` 探测没装或 daemon 未启动。
- `tools/shell.py` / `tools/codemode.py` 接线：命令与脚本都走 `env.spawn()`，超时统一调 `env.recycle()`；docker 后端只注册 `bash`（容器里没有宿主 `bash` 路径，也没有 `powershell`）；codemode 的脚本写进 `env.scratch_dir()`，harness 与脚本路径都过 `env.map_path()`。
- `config.py` / `cli.py` 沙箱配置：`sandbox`（`local` / `docker`）、`sandbox_image`、`sandbox_network`（默认 false），CLI 加 `--sandbox`；Docker 不可用时不回落 local，`bash` / `powershell` / `codemode` 都不注册，`Session.sandbox_warning` 与 CLI 的 `sandbox warning:` 给出原因。
- `envfile.py` 与 `services.py` 都在 `ai/` 下（`coding` 与 `ai/scripts` 共用）：`load_env_file(path)` 读 `KEY=VALUE` / `export` / 注释与引号，**文件优先**（覆盖同名环境变量），空值跳过；`SERVICES` 是服务商名 -> `(协议, base_url)` 的表，`record.py` 用 `base_url + 协议路径` 拼完整端点。
- `config.py`：`Settings` / `ConfigError` / `load(env=None, overrides=None, config=None)`；优先级 `overrides` > `env` > `os.environ` > 配置文件；默认 `~/.gradus/config.json`（用户级），`--config` 可覆盖；字段 `service` / `provider` / `model` / `base_url` / `api_key` / `context_window` / `max_output_tokens` / `sandbox` / `sandbox_image` / `sandbox_network`，未知字段报错，字符串里的 `${NAME}` 展开成环境变量（未设置报错）；`service` 自动带出 `provider` 与 `base_url`，显式 `base_url` 覆盖表里的地址，`provider` 与 `service` 协议冲突、服务商名未知都报错；读 6 个环境变量（含窗口与输出上限），`fake` 被拒绝，`api_key` 去掉首尾空白且不进 `repr`，`base_url` 校验 http(s) scheme。
- `session.py`：进程级 `get_pool()` / `close_pool()`；`Session.connect(settings, system=None, workspace=None, confirm=None, session_dir=None, resume=None)` 装配每会话一个 `Agent`，注入文件工具、共享 `FileTracker` 与 `AgentHooks`；`workspace` 缺省为 cwd（恢复时取会话头）；暴露 `prompt` / `abort` / `aclose` / `messages` / `state` / `system` / `workspace` / `policy` / `session_path` / `context_files`。
- 项目规则文件（`session.read_project_context`）：每层目录按 `AGENTS.override.md` / `AGENTS.md` / `AGENTS.MD` / `CLAUDE.md` / `CLAUDE.MD` 取第一个，从 workspace 走到根，按「根 -> workspace」渲染成 `<project_context>` / `<project_instructions path="...">` 拼在基础提示之后、技能清单之前；`system=` 显式传入时不拼；`/reload` 与 MCP 工具刷新重读；不监听变化、不需要信任、不截断。
- `SessionFile`（同在 `session.py`）：`create` / `load` / `append`；JSONL 头 + 线性链 entry，`parentId` 串成链；恢复时从叶子回溯、校验 `id` 唯一与工具调用 / 结果配对；`Session.prompt` 在 `AgentDone` 时把 `new_messages` 追加落盘。
- `tools/files.py`：`read_file` / `list_dir` / `write_file` / `edit` / `delete_file` 与 `create_file_tools` / `create_read_only_tools`；`read_file` 支持 `offset` / `limit`、无行号、2000 行或 50KB 截断；`list_dir` 500 项、目录带 `/`；`write_file` 新建或覆盖并建父目录；`edit` 用 `edits[]` 精确替换，`old_text` 必须唯一且互不重叠，未读过文件拒绝；`delete_file` 只删单个文件、拒绝目录，走宿主进程不依赖 shell（AppContainer 里 `Remove-Item` 被拒）；路径相对 cwd，不设沙箱。
- `tools/search.py`：`grep`（`pattern` / `path` / `glob` / `ignoreCase` / `literal` / `context` / `limit`，默认 100 条，输出 `path:line: text`，单行 300 字符）与 `glob`（`pattern` / `path` / `limit`，默认 1000 条，输出相对路径）；跳过 `.git`、`node_modules`、虚拟环境等目录，扫描走 `asyncio.to_thread`。
- `tools/shell.py`：`bash` 与 `powershell` 分开注册；只注册 PATH 里真实存在的解释器，`powershell` 仅 Windows；以 workspace 为 cwd 执行（仅起点，不是隔离），合并 stdout / stderr，非零退出码标记错误，默认超时 120s（上限 600s），输出上限 30000 字符。
- `permissions.py`：`Policy` / `Decision` / `make_before_tool_call`；只读（`read_file` / `list_dir` / `grep` / `glob` / `tool_search` / 三个 MCP 资源工具）放行，写（`write_file` / `edit` / `delete_file`）与命令（`bash` / `powershell`）询问；敏感路径（`.env*`、`*.pem`、`id_rsa*`、`.git/*`）与危险命令（`rm -rf`、`sudo`、`git push --force`）拒绝；写越界询问；无确认回调时询问类调用直接拒绝；`sanitize_result` / `make_after_tool_call` 截断 50k 字符并脱敏 `sk-`、`Bearer`、`api_key=`。
- `render.py`：`render(event)` 纯函数，输出文本增量、工具开始 / 结束、`AgentDone`、`AgentError`；`render_log(event)` 输出 `ToolExecutionEnd` / `AgentError` 的 JSONL 行。
- `context.py`：`estimate_text_tokens` / `estimate_message_tokens` / `context_tokens`（宽字符 1 字 1 token，其余 3.5 字符 1 token；最近一条成功 assistant 的 `input_tokens + output_tokens` 做锚点，只估算其后消息）、`should_compact`（窗口 - 16384）、`select_cut`（只切在 user 消息处，保留 20000）、`trim_messages`（请求前兜底裁剪）、`serialize_messages` / `make_summarizer` / `compact` / `summary_message`。
- `session.py` 压缩接线：`prompt` 前检查预算，摘要旧消息后用 `Agent.connect(messages=...)` 重建，工具 / `FileTracker` / 权限策略沿用原实例；`transform_messages` 做请求前裁剪；`SessionFile` 支持 `compaction` entry，恢复时重放最后一次压缩；摘要失败只放弃本次压缩，不打断轮次。
- `retry.py`：`RetryPolicy`（额外重试 2 次，0.5s 起翻倍、上限 30s）与 `is_retryable`；只重试 `provider`，能从消息里解析出 HTTP 状态时只认可重试集合；`aborted` / `max_turns` / `config` 不重试。`session._run` 用 `agent.continue_()` 重跑，不重复追加用户消息；`AgentError.new_messages` 也落盘。
- `registry.py`：`ToolRegistry` / `merge_tools` 按名字去重、保持注册顺序；`FunctionTool` 把普通函数（同步或异步）适配成 `AgentTool`；`ToolCatalog` 按 `direct` / `deferred` / `hidden` 分组（跨组同名拒绝），`Exposure` / `EXPOSURES` 供 MCP 配置校验复用。
- `mcp.py`：读工作区 `.gradus/mcp.json`（`--mcp` 可覆盖），支持 stdio（`command` / `args` / `env` / `cwd`）与 Streamable HTTP（`url` / `headers`）；工具名 `mcp__<server>__<tool>`，`read_only_hint` 决定 parallel / serial；`enabled` / `timeout`（默认 60s）；单 server 失败只记录告警、不影响其它 server；结果超 20KB 保留首尾、全文落临时文件；无文本时退回 `structured_content`。
- `mcp.py` 信任：默认配置路径首次读取时走 `confirm` 询问，确认后把项目绝对路径写进 `~/.gradus/trusted.json`；非交互（无 `confirm`）直接跳过并记录告警；`--mcp` 显式路径视为已经同意。
- `mcp.py` 暴露策略：server 级 `exposure`（默认 `deferred`）与 `toolExposure` 覆盖表（精确名 > 首个命中的通配符 > server 默认），`connect` 按 `direct` / `deferred` / `hidden` 三分桶；`description` 只用于系统提示；新增 `effective_exposure`。
- `mcp.py` server 句柄与资源探测：连上的 server 以 `McpServerHandle`（名字 / timeout / client）记录在 `McpSession.handles`，`servers` 改为由句柄派生的属性；连接后用 `supports_resources` 探测 `resources/list`（失败 / 超时视为不支持），通过的记入 `resource_handles`。
- `mcp.py` stderr 诊断：stdio server 的 stderr 写进临时文件；连接失败时 `stderr_tail` 取最后 2000 字符，以 `[stderr]` 段附进 `errors`；连接成功后关闭临时文件。
- `mcp.py` 配置合并：用户级 `~/.gradus/mcp.json` 与项目级配置按 server 名合并，同名时项目整体覆盖（可用 `enabled: false` 关掉用户级条目）；`--mcp` 只读指定文件，不合并。`Session.connect` 只对项目级配置做信任询问。
- `mcp.py` 生命周期：每个 server 一个 `AsyncExitStack` 与 `McpServerHandle`；请求失败（超时 / 断线）先重连一次再重试，重连失败返回原始错误，句柄内的锁保证并发只重连一次；`McpSession.aclose` 逐个关闭。
- `mcp.py` 变更通知：`Client(message_handler=...)` 监听 `tools/list_changed` / `resources/list_changed`，只置 `ChangeFlags`；`McpSession.refresh` 重新列出有变更的 server、按 `exposure` 重建工具，失败的 server 保留旧工具并清标记。
- `oauth.py`：`OAuthSettings`（`clientId` / `clientSecret` / `scope` / `clientName` / `callbackPort` / `timeout`）；`FileTokenStorage` 按 `server|url` 把 token 与注册信息写进 `~/.gradus/mcp-auth.json`（目录 0700、文件 0600、临时文件原子替换）；`CallbackServer` 在 `127.0.0.1` 收 `/callback`，成功 / 失败页，默认 300s 超时；`http_client()` 组装 `OAuthClientProvider` 并产出带 `auth` 的 `httpx2.AsyncClient`，优先复用记录的 `redirectUri` 端口，端口变化时清掉旧注册让 SDK 重新注册；非交互时授权链接只报错不弹浏览器。
- `mcp.py` OAuth 接线：`McpServer.oauth` 解析（stdio 不支持、与 `headers.Authorization` 互斥、未知键与端口越界报错）；`_open_client` 对带 `oauth` 的 HTTP server 走 `oauth.http_client`；`connect(..., interactive=)` 与 `McpServerHandle.interactive` 让首次连接与重连保持同一交互口径。
- `session.py` / `cli.py` 热加载：`Session.reload()` 重新扫描扩展目录、重建工具集 / 系统提示 / `Agent`，历史与 MCP 连接保留；启动时未信任的扩展目录不重新询问，只返回「重启确认」告警；CLI 的 `/reload` 调用它并打印命令 / 技能 / 工具数量与告警。
- `session.py` 刷新接线：`Session.prompt` 开头发现 `McpSession.changed` 时刷新并重建工具集与 `Agent`（历史不变，系统提示里的 deferred 段重算）；装配逻辑抽成 `_build_tools`，连接与刷新共用。
- `tools/mcp_resources.py`：`list_mcp_resources` / `list_mcp_resource_templates`（`server` / `cursor`）与 `read_mcp_resource`（`server` + `uri`）；无 `server` 时遍历全部 server 并自动翻页、按名字排序、单 server 失败进 `errors`；跳过 `ui://` 与 `mcp-app` 资源；文本直出、多段加 `uri:` 前缀、二进制落临时文件；输出 JSON（camelCase）；按 server 的 `timeout` 收口。
- `session.py` 资源接线：存在 `resource_handles`（支持 resources 的 server）时把三个资源工具按 `direct` 注册进 catalog；`permissions.py` 把它们放进 `READ_TOOLS`，只读放行、`parallel` 执行。
- `tools/tool_search.py`：`tool_search(query, limit=8)` 在 deferred 池里按「名字命中 +3、描述命中 +1」排序，返回工具说明并登记待加载；`take_pending` 取走并清空，避免重复注入；空 query 报错、无命中返回占位。
- `session.py` deferred 接线：装配统一走 `ToolCatalog`（内置 / 直接声明的 MCP 工具入 `direct`，MCP 的 deferred 工具入 `deferred`），`tool_search` 池子取 `catalog.deferred`、不绑定 MCP；`make_prepare_next_turn` 把登记的工具追加进下一轮 `config.tools`（不写会话文件、不改消息）；`catalog.deferred` 非空时在系统提示追加通用说明与 server 清单；加载只在本次运行内有效，下一次 `prompt` 回到原始工具集。
- `tools/codemode.py`：`codemode(code, timeout?)` 把脚本写进临时文件、以 `python -I` 起子进程，走一行一个 JSON 的往返（脚本 `call` / 宿主 `result` / `done` / `error`）；脚本内 `tools.<name>(**kwargs)` 返回内容字符串，工具报错抛 `RuntimeError`；只有 `text()` 与 `return` 进上下文，`print` 丢弃；输出上限 30000 字符，默认超时 120s（上限 600s），超时 / 异常后杀进程并删临时文件；`codemode` 与 `tool_search` 不暴露给脚本；`execution_mode = serial`。
- `tools/codemode_harness.py`：子进程侧执行器，不 import `gradus`；把脚本包进函数后 `exec`，白名单 builtins + AST 拒绝 `import`、危险内建名（`eval` / `exec` / `open` / `getattr` 等）与下划线属性；`text()` 累计上限 200000 字符。
- `session.py` / `permissions.py` codemode 接线：`_build_tools` 在 `tool_search` 之前把 codemode 按 `direct` 入册，工具池取 `catalog.direct`；`codemode` 进 `READ_TOOLS`（脚本本身只是运行器），脚本内的写与命令由 `CodemodeTool` 按同一 `Policy` 逐次判断，整段脚本只确认一次。
- `extensions.py`：只扫 `<workspace>/.gradus/extensions`；`tools/*.py` 必须有 `create_tool()`（返回一个 `AgentTool` 或序列），按路径 `importlib` 导入；`commands/*.md` 文件名即命令名、`$ARGUMENTS` 替换或参数追加；`skills/*/SKILL.md` 读 frontmatter 的 `name` / `description`，正文剥掉 frontmatter 按需读；同目录同名按文件名顺序后者覆盖；单个文件失败只记一条告警。
- `session.py` 扩展接线：扩展目录与 `.gradus/mcp.json` 共用信任检查（未信任且无确认回调、或拒绝信任都跳过并告警）；`_build_tools` 把扩展工具在校验重名后按 `direct` 入册、冲突只告警；系统提示追加技能清单（名字 + 描述 + 路径）；`prompt` 先展开 `/命令` 与 `/skill:名字`，未知命令抛 `SessionError` 不发给模型；暴露 `commands` / `skills` / `extension_warnings`；MCP 刷新重建时保留扩展工具与技能清单。
- `cli.py` 扩展接线：启动时打印已加载的扩展命令，扩展告警以 `extension warning:` 输出；交互循环把未知命令的 `SessionError` 打印成 `error:`。
- `session.py` 工具接线：`create_file_tools` / `create_search_tools` / 命令工具与 MCP 工具经 `ToolCatalog` 入册后按 `direct` 注入（执行环境由 `_build_environment` 建，`env=None` 时不注册命令与 codemode）；`aclose()` 同时关 Agent、MCP 连接与执行环境；`mcp_servers` / `mcp_warnings` / `sandbox_warning` 暴露已连 server、告警与环境问题。
- `cli.py`：`argparse` 支持 `--service` / `--provider` / `--model` / `--base-url` / `--sandbox` / `--workspace` / `--log` / `--session-dir` / `--resume` / `--mcp` / `--config` / `--untrust`；启动时先读 `<workspace>/.env`（文件优先）；`--untrust [路径]` 撤销项目信任后直接退出（不读模型配置）；交互循环复用 `run_once()`；`exit` / `quit` / EOF 退出，运行中 Ctrl-C 调 `abort()`；默认把会话写到 `<workspace>/.gradus/sessions`；启动时打印已连的 MCP server、`mcp_warnings`、实际使用的配置文件与 `context: ...`；`McpConfigError` 按会话错误退出；退出时 `aclose()` + `close_pool()`。
- `pyproject.toml`：`[project.scripts] gradus = "gradus.coding.cli:cli"` + setuptools 构建（`namespaces = true`，三层都是 namespace package）；`uv sync` 后 `uv run gradus` / `.venv\Scripts\gradus.exe` 在任何目录都能用，README 的快速开始改成实际可跑的命令。
- `tests/test_config.py`：37 个用例（含缺字段 3 个子测试、非法整数 8 个子测试、overrides 优先级；配置文件全字段、环境变量覆盖文件、文件补缺、默认文件缺失、显式路径缺失、坏 JSON、未知字段、非法值类型、`${ENV}` 展开与缺失变量、文件值仍走同一套校验、配置文件里的 `service`；`envfile.load_env_file` 的文件值覆盖同名环境变量、空值跳过、忽略注释 / 空行 / 无等号行 / `export`、去引号、文件不存在无副作用；`service` 带出 provider 与 base_url、显式 base_url 覆盖、协议冲突报错、未知名字列出可选值；沙箱默认值、环境变量覆盖、未知后端、非法布尔、配置文件字段）。
- `tests/test_session.py`：33 个用例（池共享、`aclose` 不关池、`close_pool` 重建、关闭后拒绝 `prompt`、`system` 内容、自定义 `system`、workspace 缺省与覆盖；会话头、链式 `parentId`、消息往返、按目录取最新、未配对调用 / 孤儿结果 / 缺头拒绝、创建会话文件、恢复消息与 workspace；压缩记录、恢复重放、压缩后继续追加、未知 entry 拒绝、超预算重建与摘要失败保留历史；未信任配置跳过、拒绝信任、确认后记住、显式路径免询问；规则文件的祖先顺序 / 同目录 override 优先 / 无文件时为空 / 注入系统提示 + `context_files` / `/reload` 重读 / 显式 `system` 不合并）。
- `tests/test_files.py`：25 个用例（读取、分页、行/字节截断、超长首行、缺文件、目录、越界 offset、绝对路径、列目录排序/空目录/limit/缺路径/非目录；写文件的新建/覆盖/建父目录/写目录；编辑的先读后写/单块/多块/重复/未找到/重叠/空数组/写后标记已读）。
- `tests/test_permissions.py`：18 个用例（只读放行、写内/写外询问合并为一条、敏感路径、危险命令、普通命令、`powershell` 询问与拒绝、未知工具、hook 的放行 / 拒绝 / 确认通过 / 确认拒绝 / 无回调；脱敏的 sk- / Bearer / 普通文本 / 截断 / after hook 两种路径）。
- `tests/test_cli.py`：17 个用例（文本、工具开始 / 结束、错误、`AgentDone`、`AgentError`、静默事件、参数映射（含 `--service` / `--sandbox`）、日志 JSON；`--untrust` 删除记录、省略路径用 workspace、未信任时无副作用）。
- `tests/test_sandbox.py`：25 个用例（本地环境路径与执行；docker 的路径映射 / 越界拒绝 / run 参数加固（含 owner 标签）/ 网络开关 / exec 工作目录；Docker 探测失败；孤儿容器只删 owner 已死的 / 无容器时无操作 / pid 存活判断；appcontainer 的残留授权只撤 owner 已死的、当前 workspace 与已删除路径不动；auto 依次落到 appcontainer / docker / local 且 appcontainer 可用时不探测 docker；会话在 docker 不可用时去掉命令与 codemode 并给出告警、可用时保留、启动清理孤儿并按结果暴露、local 不探测 docker；appcontainer 不可用时去掉命令类工具、可用时只注册 `powershell` 并提示忽略 `sandbox_network`、启动清理残留授权；沙箱副本下 `reload` 后工具仍绑副本、写进副本的文件在 `aclose` 时合回宿主且副本被删）。
- `tests/test_context.py`：29 个用例（ASCII / CJK / 混合估算、消息估算、usage 锚点与错误 / 中止 / 零用量排除、触发阈值、切点只在 user、请求前裁剪不拆工具对、序列化与截断、压缩前缀与空摘要）。
- `tests/test_retry.py`：11 个用例（退避翻倍与上限、可重试 / 不可重试状态、非 provider 不重试、重试成功且不重复追加用户消息、401 不重试、aborted 不重试、次数用尽、失败前已完成轮次落盘）。
- `tests/test_registry.py`：13 个用例（注册顺序、重名与空名拒绝、跨组合并、`FunctionTool` 同步 / 异步 / 异常透传；`ToolCatalog` 按 exposure 分组、`add_all` 保序、跨组同名 / 空名 / 未知 exposure 拒绝）。
- `tests/test_mcp.py`：52 个用例（缺文件、stdio / HTTP 配置、空 servers、顶层与 JSON 形状、互斥传输、未知键、args / env / url / enabled / timeout / exposure 校验、`oauth` 字段解析与校验（stdio 拒绝、与 `Authorization` 互斥、未知键、端口越界）、默认路径、`mcp__` 命名、结果渲染与 20KB 中间截断 + 全文落盘、信任记录读写与损坏恢复、`forget_trust` 只删目标 / 无记录时无副作用、`McpTool` 转发 / 超时 / 错误、禁用 server 不连接、坏 server 不影响其它、真实 stdio 子进程的列出与调用、默认 deferred 与三分桶、`toolExposure` 覆盖与非法值、`effective_exposure` 的精确名 / 通配符优先级、`supports_resources` 的成功 / 报错 / 超时、配置合并三条规则、`stderr_tail` 与真实子进程 stderr 透传、通知标记、重连成功 / 失败、`refresh` 的重列 / 失败保留 / deferred 清单）。
- `tests/test_search.py`：13 个用例（命中标注与忽略目录、literal、ignoreCase、glob 过滤、上下文行、limit 提示、非法正则、缺路径、无命中、`**` 匹配、glob limit、非目录、工厂命名）。
- `tests/test_shell.py`：9 个用例（输出捕获、cwd、非零退出码、超时终止、空命令与坏解释器合并、长输出截断；按 PATH 与平台选择工具）。
- `tests/test_tool_search.py`：8 个用例（名字优先于描述、limit 截断、无命中、空 query、pending 去重与取走后清空；`prepare_next_turn` 把新工具加进下一轮、无 pending 返回 `None`、已声明工具跳过）。
- `tests/test_mcp_resources.py`：12 个用例（跨 server 汇总与自动翻页、按名字排序、单 server 单页与 `cursor`、未知 server、无 server 时的 `cursor` 拒绝、单 server 失败进 `errors`、跳过 `ui://` / `mcp-app`、模板输出 `uriTemplate`；读文本、多段加前缀、二进制落临时文件、缺参拒绝；三个工具名与 `parallel`）。
- `tests/test_codemode.py`：11 个用例（`text()` 与 `return`；脚本调工具、工具报错抛 `RuntimeError`、未知工具报错；写工具每段脚本只确认一次、拒绝确认、无确认回调拒绝；`import` 与 `__class__` 被拒；超时杀进程；空 `code` 拒绝）。
- `tests/test_extensions.py`：20 个用例（空目录、`enabled=False` 返回空、工具 / 命令 / 技能加载、坏文件与缺 `create_tool()` / 返回非工具只告警、同目录同名后者覆盖、`$ARGUMENTS` 替换与追加、普通文本原样、未知命令、`/skill:` 展开、缺 `description` 告警、正文剥 frontmatter、技能清单文本、扩展工具与内置重名被跳过；会话暴露命令 / 技能 / 扩展工具、未知命令抛 `SessionError`、未信任目录被跳过、`reload` 拾取新文件、未信任时只告警）。
- `tests/test_oauth.py`：13 个用例（token / 注册信息往返、按 `server|url` 隔离、损坏文件当空、文件权限 0600、`redirectUri` 往返与 `reset_client`；回调页收 `code` / `state`、`error` 参数报错、未知路径 404 且继续等待、超时报错；非交互拒绝打开浏览器；`http_client` 生成带 `auth` 的客户端并持久化端口、二次调用复用同一端口）。
- `workspace.py`：`WorkspaceCopy` / `WorkspaceSync` / `create` / `cleanup_orphans`；沙箱后端下建副本，git 仓库根用 `git worktree add --detach` 并把 `git ls-files --others -z` 列出的未跟踪文件（含被忽略的 `.env`）带进去，其余 `copytree` 并跳过 `.git` / `.gradus` / `node_modules` / `__pycache__` / `.venv` / `venv` / `.mypy_cache` / `.pytest_cache` / `.ruff_cache` / `dist` / `build` / `target`；`sync()` 在 worktree 模式走 `git add -A` + `diff --cached --binary` + `git apply`，带进来的未跟踪文件直接复制、删除直接删宿主，复制模式按大小与 mtime（必要时比内容）合回新增 / 修改，并按建副本时的基线删除；副本位于 `%TEMP%\gradus-ws-<pid>-<id>`，`.owner` 记录 owner pid，`cleanup_orphans` 按 pid 清理强杀残留；`discard()` 解除 worktree 注册并删目录。
- `session.py` 沙箱副本接线：`_build_environment` 返回副本，文件工具、搜索工具、命令工具、`codemode` 与 `Policy` 的 workspace 边界都换到副本；系统提示的 `cwd` 用副本路径；会话文件、规则文件、扩展与 MCP 配置仍读宿主；`aclose()` 先 `sync()` 再撤销授权 / 销毁容器，最后删副本；新增 `sandbox_workspace` / `workspace_sync`；CLI 退出时打印合并结果，失败时给出保留路径。
- `tests/test_workspace.py`：15 个用例（复制模式跳过重目录、合回新增与修改、无改动为空、删除落到宿主、会话期间宿主新建的文件不误删、`discard` 删目录；worktree 模式合并补丁、应用已跟踪文件的删除、未跟踪文件带进副本、改动未跟踪文件合回、删除未跟踪文件落到宿主、`discard` 解除注册；孤儿清理只删 owner 已死的、`keep` 指定的不动、worktree 残留同时解除注册——用到 Git 的用例在装 Git 的机器上跑）。
- `tests/test_sandbox_live.py`：10 条实机契约检查，默认跳过，`GRADUS_SANDBOX_LIVE=1` 才跑（副本位置与工作目录、相对路径读写、已跟踪 / 未跟踪文件可见、越界读写被拒、删除（`.NET` 可用、`Remove-Item` 只记录）、断网、超时、codemode、关闭时合回新增 / 修改 / 删除、孤儿副本清理）；与 `design.md` §5.5.1 的约束表一一对应。

**验证结果**

- `uv run python -c "import gradus.ai.envfile, gradus.ai.services, gradus.coding.cli, gradus.coding.render, gradus.coding.permissions, gradus.coding.context, gradus.coding.retry, gradus.coding.registry, gradus.coding.mcp, gradus.coding.oauth, gradus.coding.extensions, gradus.coding.tools.search, gradus.coding.tools.shell, gradus.coding.tools.tool_search, gradus.coding.tools.mcp_resources, gradus.coding.tools.codemode, gradus.coding.tools.codemode_harness"` 通过。
- `uv run pytest gradus/coding/tests -q` → 362 passed, 10 skipped（实机契约默认跳过）。
- `uv run pytest -q` → 582 passed, 10 skipped（ai 130 + agent 90 + coding 362）。
- 实机契约 `GRADUS_SANDBOX_LIVE=1 uv run python -m pytest gradus/coding/tests/test_sandbox_live.py -q -s` → 10 passed（appcontainer，约 34s）；`Remove-Item` 记为 denied，`.NET` 删除通过。
- `--sandbox appcontainer` 实机冒烟（Windows 11，非管理员）：AppContainer 内 `cmd echo`、读 workspace 文件、`python -c` 均正常，联网被拒；`_prepare` 首次约 10s（解释器 ACE），标记命中后 0.06s。
- `--sandbox appcontainer` 端到端（走真实工具，共享 workspace 版）：`powershell` 读写 workspace 正常、宿主能看见写入（现在是副本，关闭时才合回）；读 workspace 之外的文件与写 workspace 之外的位置都被拒；命令超时 3s 触发并杀掉进程；`codemode` 调 `read_file` 与纯脚本都返回结果。
- `--sandbox appcontainer` 残留清理：模拟强杀（`os._exit`，跳过 `aclose`）后，下次启动的 `cleanup_orphans` 撤销了两条残留 ACE 并清空记录；正常 `aclose` 同样撤销 ACE 并清记录。
- 缺省 `auto` 实机解析：Windows 上 `session._pick_backend()` → `appcontainer`（不探测 docker）。
- `--sandbox docker` 的真机集成验证**暂缓**：本机 Docker Desktop 已安装（`%LOCALAPPDATA%\Programs\DockerDesktop\resources\bin`）但引擎未运行，`docker_problem()` 会按「不可用」处理（禁用命令类工具 + 告警，不回落 local）；起容器 / workspace 挂载 / 断网 / 退出清理 / 孤儿清理这几项只覆盖了离线测试。
- 真实模型端到端冒烟（Kimi / Moonshot `kimi-k2.7-code`，`sandbox: local`）：一轮内完成「读 `a.txt` → 写 `b.txt` → 跑命令 `6*7` → 中文总结」，3 turns / in 8396 / out 400 tokens，`b.txt` 落盘内容与工具结果一致。
- 冒烟中暴露并修复：`session._run` 重试时直接丢弃旧事件迭代器，旧 `_consume` 的 `finally` 会晚于新一轮 `_start` 执行，把新一轮置回 idle 并往新队列塞 `_END`，表现为「第二次尝试只输出到 `StreamStart` 就没有终止事件」。`Agent._start` 现在把队列与 task 显式传给 `_pump` / `_consume`，`_finish_if_needed(queue)` 只收尾它自己那一次运行；回归用例 `test_retry_after_error_starts_a_clean_run`。复跑命中同一 429 时，3 次尝试全部执行后正确抛出 `[error] provider: HTTP 429 ...`。
- 沙箱自有 workspace 实机验证（`sandbox: appcontainer`，直接调工具，不走模型）：副本落在临时目录，`write_file` 与 `powershell` 都写进副本；关闭前宿主看不到 `b.txt`；`aclose()` 后 `workspace_sync` 为 `applied=('b.txt', 'c.txt')`，宿主两个文件内容正确，副本目录已删除。
- 沙箱副本三项补齐的实机验证（`appcontainer` + git worktree）：预先留一个 owner pid 已死的副本，连接时 `sandbox_orphans` 报出并删除；副本里能看到并读出未跟踪的 `.env`；用 `[IO.File]::Delete` 删掉副本里的 `a.txt` 后关闭会话，`workspace_sync` 为 `deleted=('a.txt',)`，宿主 `a.txt` 消失、`.env` 与 `keep.txt` 保留，worktree 解除注册、副本目录删除。
- 副本位置实机结论：副本若放在 `%TEMP%\gradus\workspaces\...` 这类嵌套目录下，AppContainer 进程设不了工作目录，PowerShell 退回 `C:\`，相对路径全部失效（`Test-Path .` 直接 Access denied）；改成直接放 `%TEMP%\gradus-ws-*` 后恢复正常。
- appcontainer 删除限制的实测：目录授 `M`、`F`、`M+(DE,DC)` 三种都没用，`Remove-Item` / `cmd del` 一律 `Access is denied`，`[System.IO.File]::Delete` 正常；已写进 `design.md` 的 backend 约束，未改授权粒度。
- 边界说明：AppContainer 内读 `C:\Windows\win.ini` 成功属于默认的系统目录读权限（`ALL APPLICATION PACKAGES` 可读 `%SystemRoot%`），不是回退到 local；宿主用户目录在副本之外仍被拒（之前已验证）。

