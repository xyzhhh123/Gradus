# coding 层问题记录

本文件只收录**已经解决**的问题。未解决、待定、延后以及归属其它层的事项统一在 `coding_plan.md` 里程碑表的「未定 / 推迟」列中跟踪；解决后再归档到这里。

## 记录格式

### 问题标题

- **状态**：已解决
- **现象**：
- **触发**：
- **原因**：
- **解决**：
- **验证**：关联测试或命令
- **关联**：对应的 M 编号或模块文件

## 已解决问题

### 会话文件漏掉失败前已完成的轮次

- **状态**：已解决
- **现象**：一次运行在第 N 轮请求失败时，前 N-1 轮已经写回 `Agent` 历史，但会话文件里没有；恢复后上下文比失败前短。
- **触发**：`AgentError(kind="provider")` 且 `AgentError.new_messages` 非空。
- **原因**：`Session._run` 只在 `AgentDone` 时调 `SessionFile.append`，默认「失败就没有新消息」；实际 agent 会把失败前已完成的轮次放进 `AgentError.new_messages`。
- **解决**：`AgentError` 与 `AgentDone` 走同一个 `_append`，把 `new_messages` 追加到会话文件；失败的半轮仍由 agent 保证不入历史。
- **验证**：`tests/test_retry.py::SessionRetryTest::test_completed_turns_are_logged_on_error`。
- **关联**：M7、`session.py`。

### 项目 mcp.json 未经确认就执行命令

- **状态**：已解决
- **现象**：在工作区放一个 `.gradus/mcp.json`，启动 Gradus 就会按里面的 `command` 起子进程；换一个陌生仓库时等于执行仓库自带的命令。
- **触发**：默认配置路径下存在 `servers` 条目。
- **原因**：`Session.connect` 直接 `load_servers` + `connect`，没有把「项目配置可以执行代码」当成需要授权的动作；Pi 的同类入口有 project trust 拦截。
- **解决**：默认路径首次读取时通过 `confirm` 询问，确认后把项目绝对路径写入 `~/.gradus/trusted.json`；无确认回调（非交互）时跳过并给出告警；`--mcp` 显式指定路径视为已经同意。
- **验证**：`tests/test_session.py::McpTrustTest`（四种路径）与 `tests/test_mcp.py::TrustTest`（记忆与损坏恢复）。
- **关联**：M9、`mcp.py`、`session.py`。

### tool_search 的检索池只包含 MCP 工具

- **状态**：已解决
- **现象**：`tool_search` 只能搜到 MCP 的 deferred 工具；内置工具与其它来源的工具无法按需加载，系统提示也写死「MCP servers are connected」。
- **触发**：想让 MCP 之外的来源（扩展工具、脚本工具）复用同一套 deferred 加载。
- **原因**：`Exposure` 定义在 `mcp.py`，`Session.connect` 直接以 `ToolSearchTool(mcp_session.deferred)` 取池，装配层没有一层与来源无关的归类；`merge_tools` 只产出「全部声明」的单一元组。
- **解决**：`registry.py` 新增 `ToolCatalog`，`Exposure` / `EXPOSURES` 上移为公共类型；`Session.connect` 把内置工具与直接声明的 MCP 工具按 `direct` 入册、MCP 的 deferred 工具按 `deferred` 入册，`tool_search` 取 `catalog.deferred`；系统提示改为通用措辞，MCP server 清单降为可选附录。
- **验证**：`tests/test_registry.py::ToolCatalogTest`（5 个用例）；`uv run pytest -q` → 429 passed。
- **关联**：M11、`registry.py`、`session.py`、`mcp.py`。当前池中仍只有 MCP 工具，因为还没有第二种 deferred 来源；M13 / M15 接入后无需再改检索逻辑。

### 误判 MCP SDK 没有 OAuth 支持

- **状态**：已解决
- **现象**：调研 MCP OAuth 时得出的结论是「SDK 没有 OAuth，需要自己实现元数据发现、动态注册、本地回调页与 PKCE」。
- **触发**：用代码搜索工具在 `.venv/Lib/site-packages/mcp` 下搜 `oauth`，结果为空。
- **原因**：搜索工具默认遵循 `.gitignore`，`.venv` 被跳过，所以「搜不到」被误当成「不存在」；实际 `mcp/client/auth/oauth2.py` 里有完整的 `OAuthClientProvider`。
- **解决**：改用 `Glob` 列目录、直接 `Read` 确认；最终只实现 SDK 需要的三块宿主能力（token 存储、本地回调页、带 `auth` 的 HTTP 客户端），元数据发现 / 动态注册 / PKCE / 刷新交给 SDK。
- **验证**：`tests/test_oauth.py`（13 个用例，含回调页、端口复用、非交互拒绝）、`tests/test_mcp.py::OAuthConfigTest`（5 个用例）；`uv run pytest -q` → 497 passed。
- **关联**：M12、`oauth.py`、`mcp.py`。教训：在依赖目录里做「有没有」的判断，不能只看搜索工具的空结果。

### 把 429 限流误判成流式适配器问题

- **状态**：已解决
- **现象**：真实模型冒烟只看到 `<<StreamError>>`，既没有 `[error]` 也没有 `AgentDone`；第一轮工具跑完后缺最终回答，一度怀疑 `openai_compat` 的 SSE 解析或事件映射。
- **触发**：Kimi（Moonshot）同一 org 的限额为 `max RPM: 3`，多轮请求在 1~2 秒内打满。
- **原因**：事件打印只输出类型名，没打 `error_message`；而 `session._run` 在可重试时会把第一个 `AgentError` 吞掉去重跑，终端因此只看得到上游的 `StreamError`。
- **解决**：让脚本打印事件自带的 `error_message` / `message`，确认是 `HTTP 429 ... request reached organization max RPM: 3`。这不是适配器问题，也不是框架 bug；真正的缺陷是重试路径（见下一条）。
- **验证**：重跑后三次尝试的 `detail` 都是同一条 429，最后一次正确渲染成 `[error] provider: HTTP 429 ...`。
- **关联**：`render.py`、`session.py`、`retry.py`。教训：先打印事件携带的原始错误，再怀疑适配器。

### 运行级重试后新一轮被旧迭代器收尾打断

- **状态**：已解决
- **现象**：一次运行以 `StreamError` 结束后，`session._run` 用 `agent.continue_()` 重跑；第二轮只输出到 `StreamStart` 就断了——既没有 `AgentDone`，也没有用户可见的 `AgentError`，看起来像卡住或吞错。
- **触发**：`AgentError(kind="provider")` 且 `is_retryable` 为真（429 / 断流等），即任何一次运行级重试。
- **原因**：`Agent._consume` 在第一次 `anext` 时才读 `self._events` / `self._task`。`session._run` 重试时直接丢弃旧迭代器，旧生成器的 `finally` 要等引用计数归零才执行，而那时新一轮 `_start()` 已经把这两个字段换成新队列与新 task；`_finish_if_needed()` 于是把**新一轮**置回 `idle`、`_task=None`，并往**新队列**塞 `_END`，新一轮的 `_consume` 立刻读到结束标记返回。
- **解决**：`_start` 建好队列与 task 后显式传给 `_pump(queue, config)` / `_consume(queue, task)`，不再惰性读共享字段；`_finish_if_needed(queue)` 只在 `queue is self._events` 时收尾，旧迭代器只能清理自己那一次运行。
- **验证**：`tests/test_agent.py::AgentTest::test_retry_after_error_starts_a_clean_run`；真实模型冒烟复跑命中 429 时，三次尝试全部执行完再抛出 `[error] provider: HTTP 429 ...`。
- **关联**：M7、`agent.py`、`session.py`。

### Windows 上 bash 解析到 WSL 启动器

- **状态**：已解决
- **现象**：模型先调 `bash`，白等 4.2 秒拿到一段 `wsl: ...` 乱码错误，再改用 `powershell`；一次简单命令多花一轮。
- **触发**：`shutil.which("bash")` 命中 `C:\Windows\System32\bash.exe`，且本机 WSL 的默认发行版是 `docker-desktop`（没有 python）。
- **原因**：`create_shell_tools` 只判断「PATH 里有没有 bash」，把 WSL 的入口当成了可用的 bash。该入口需要已安装的发行版，进入的也不是当前项目目录。
- **解决**：Windows 上 `%SystemRoot%\System32\bash.exe` 一律跳过（`_usable_bash` / `_is_wsl_launcher`），只保留 Git Bash 之类的真实 bash；`powershell` 不受影响。
- **验证**：`tests/test_shell.py::CreateShellToolsTest::test_windows_skips_wsl_launcher`；实机 `create_shell_tools()` 只剩 `powershell`。
- **关联**：M10、`tools/shell.py`。

### 沙箱内命令与文件工具操作的不是同一份 workspace

- **状态**：已解决
- **现象**：`docker` / `appcontainer` 下 `write_file` 直接写宿主项目，`bash` / `powershell` 也直接改项目文件；一次会话的改动无法回滚，模型写坏就是真的写坏。
- **触发**：任何一次沙箱会话里的写操作。
- **原因**：`Environment` 只换了命令的执行位置，文件工具仍绑宿主根；workspace 是读写挂载 / 原地授权，缺少「先改副本、退出再合回」的中间层。
- **解决**：新增 `workspace.py`。沙箱后端为会话建副本：git 仓库根走 `git worktree add --detach <临时目录> HEAD`，其余整树复制并跳过 `.git` / `.gradus` / `node_modules` / `__pycache__` / `.venv` / `venv`。文件 / 搜索 / 命令 / `codemode` 工具与 `Policy` 的边界都换到副本，系统提示的 `cwd` 也用它；会话文件、规则文件、扩展与 MCP 配置仍读宿主。`Session.aclose()` 先 `sync()` 合回（worktree 在副本里 `git add -A` 后取 `diff --cached --binary` 再 `git apply`；复制模式按大小 + mtime（必要时比内容）合回新增与修改，删除只报告），再撤销 ACE / 销毁容器，最后删副本；合并失败保留副本并在 `workspace_sync` 里给出 `error` 与 `kept`。
- **验证**：`tests/test_workspace.py`（8 个用例）、`tests/test_sandbox.py::SessionSandboxTest::test_tools_follow_the_copy_and_merge_on_close`；`sandbox: appcontainer` 实机两种形态都验证（写入落在副本、关闭前宿主看不到新文件、关闭后 `applied=('b.txt','c.txt')`、worktree 解除注册且目录删除）。`uv run pytest -q` → 571 passed。
- **关联**：M14、`workspace.py`、`session.py`。仍存在的缺口写在 `coding_plan.md` M14 的待定列：复制模式删除不自动应用、worktree 里没有未跟踪文件、强杀后临时副本不清理。

### reload / MCP 刷新后工具退回宿主 workspace

- **状态**：已解决
- **现象**：沙箱后端下执行 `/reload` 或遇到 MCP `tools/list_changed` 触发重建后，文件工具又指向宿主目录：模型读到的与命令改的不是同一批文件。
- **触发**：`sandbox` 为 `docker` / `appcontainer` 且会话中重建过工具集。
- **原因**：`reload()` 与 `_refresh_mcp()` 里的 `_build_tools(self._workspace, ...)` 用的是宿主根，只有 `Session.connect` 用了副本根；根路径没有作为会话状态保存下来。
- **解决**：`Session.__init__` 记住 `_tool_root`（有副本时是副本路径，否则是宿主），两处重建都改用它。
- **验证**：`tests/test_sandbox.py::SessionSandboxTest::test_tools_follow_the_copy_and_merge_on_close`（`reload` 后写入仍落在副本，`aclose` 后合回宿主）。
- **关联**：M14、`session.py`。

### 复制模式下副本里的删除不会落到宿主

- **状态**：已解决
- **现象**：非 git 的 workspace 走整树复制时，模型在副本里删掉的文件，关闭会话后宿主上还在；`workspace_sync.deleted` 只报告不执行，等于「删了不算」。
- **触发**：沙箱后端的复制模式，且会话中删过文件。
- **原因**：`_sync_files` 只遍历副本里的现存文件做新增 / 修改比对，没有删除方向的信息；单纯「宿主有、副本没有就是删除」又会把会话期间用户在宿主上新建的文件一起删掉。
- **解决**：`create()` 复制完成后记一份基线（副本里的相对路径集合），`sync()` 用 `baseline - 当前副本` 得到真正的删除，逐个 `unlink` 宿主对应文件；基线之外的新增文件不参与判定，避免误删宿主上新建的文件。worktree 模式的已跟踪文件删除由补丁完成，带进来的未跟踪文件单独判定后删除。
- **验证**：`tests/test_workspace.py::CopyModeTest::test_deletions_are_applied`、`test_host_files_created_during_the_session_are_kept`、`WorktreeModeTest::test_deletions_are_applied`、`test_deleted_untracked_file_is_removed_on_host`；appcontainer 实机删文件后 `deleted=('a.txt',)`、宿主文件消失。
- **关联**：M14、`workspace.py`。注意 appcontainer 里只能用 `[IO.File]::Delete` 删文件，`Remove-Item` / `cmd del` 会被拒（见 `design.md` 的 backend 约束）。

### worktree 模式下模型看不到未跟踪文件

- **状态**：已解决
- **现象**：workspace 是 git 仓库时，副本走 `git worktree`；模型在副本里看不到 `.env`、本地脚本、被 `.gitignore` 忽略的文件，`read_file` 与命令都报路径不存在。
- **触发**：任何 git 仓库根作为 workspace 的沙箱会话。
- **原因**：`git worktree add --detach HEAD` 只检出已提交内容，未跟踪文件不在任何 commit 里；原实现把 worktree 当成完整副本用。
- **解决**：worktree 建好后用 `git ls-files --others -z` 列出宿主上未跟踪的文件（不带 `--exclude-standard`，所以被忽略的 `.env` 也在内），按 `COPY_SKIP` 过滤后复制进副本，并把这些路径记为 `extra`。合并时 `extra` 不走补丁（补丁会把它们当成新建，`git apply` 会因文件已存在而失败）：内容没变就跳过，改了就 `copy2` 回宿主，删了就在宿主上 `unlink`。
- **验证**：`tests/test_workspace.py::WorktreeModeTest::test_untracked_files_are_brought_into_the_copy`、`test_changed_untracked_file_is_copied_back`、`test_deleted_untracked_file_is_removed_on_host`；appcontainer 实机副本里能读到 `.env`，未改动时不出现在 `applied` 里。
- **关联**：M14、`workspace.py`。

### 强杀后临时副本留在磁盘上

- **状态**：已解决
- **现象**：进程被强杀（或 `os._exit`）时 `Session.aclose()` 不会执行，`%TEMP%` 下留下一份完整项目副本；git 仓库还会留下 worktree 注册，`git worktree list` 里出现死条目。
- **触发**：沙箱会话异常终止。
- **原因**：副本只由 `discard()` 清理，没有任何跨进程的残留标记；docker 的孤儿容器与 appcontainer 的孤儿 ACE 都有清理机制，副本没有。
- **解决**：副本统一放在 `%TEMP%\gradus-ws-<pid>-<id>`，旁边写 `<同名>.owner`（owner pid、宿主路径、是否 worktree）。启动时 `workspace.cleanup_orphans()` 扫这些标记：owner pid 还活着就跳过，否则先 `git worktree remove --force` + `prune` 再 `rmtree`，结果并进 `Session.sandbox_orphans`。副本必须直接放在 `%TEMP%` 下：实测放进 `%TEMP%\gradus\workspaces\...` 这种嵌套目录时，AppContainer 进程设不了工作目录，PowerShell 退回 `C:\`，相对路径全部失效。
- **验证**：`tests/test_workspace.py::OrphanCleanupTest`（只删 owner 已死的、`keep` 指定的不动、worktree 残留同时解除注册）；appcontainer 实机预先留一个死 pid 副本，连接时 `sandbox_orphans` 报出并删除。
- **关联**：M14、`workspace.py`、`session.py`。

### 副本放进嵌套目录后沙箱内的工作目录失效

- **状态**：已解决
- **现象**：把副本放到 `%TEMP%\gradus\workspaces\<pid>-<id>` 后，沙箱里的 `Get-Location` 变成 `C:\`，`Test-Path .` 报 `Access is denied`，`Remove-Item a.txt` 找不到文件；绝对路径操作却正常。看起来像「模型不会用相对路径」或「命令工具没传 cwd」，实际是副本位置的问题。
- **触发**：副本目录比临时目录深一层（`%TEMP%\<子目录>\<子目录>`）时必现；直接放 `%TEMP%\gradus-ws-*` 就正常。
- **原因**：`CreateProcessW` 的 `lpCurrentDirectory` 在 AppContainer 下对更深的路径设不成功，PowerShell 启动时退回 `C:\`；而绝对路径的访问检查只看文件自身 ACL，所以读写仍能成功，掩盖了问题。
- **解决**：副本目录固定为 `%TEMP%\gradus-ws-<pid>-<id>`，不再嵌套；`.owner` 标记做成同目录的兄弟文件（`<同名>.owner`），清理时按 `gradus-ws-*.owner` 匹配，不需要子目录。
- **验证**：`tests/test_sandbox_live.py::SandboxContractTest::test_backend_and_copy_location`（断言 `copy.parent == tempdir`）与 `test_cwd_and_relative_paths`（`Get-Location` 必须等于副本、相对路径写入必须落在副本）；实机 `Remove-Item a.txt` 从「找不到路径」恢复为「Access is denied（平台限制）」。
- **关联**：M14、`workspace.py`、`design.md` §5.5.1 第 1 条。

### 沙箱约束散落各处，每次改动都靠一次性脚本试错

- **状态**：已解决
- **现象**：改一趟沙箱要反复写临时探针（`gradus-acl-check.py`、`gradus-cwd-check.py`、各种 smoke），一条约束一次试错；探针用完就删，下一次改动又从零开始踩同样的坑（cwd、删除权限、未跟踪文件、残留副本）。
- **触发**：任何涉及 `sandbox.py` / `appcontainer.py` / `workspace.py` / 沙箱接线的改动。
- **原因**：平台约束只存在于对话里和 `design.md` 的散段落中，没有可执行的自检；完成定义也只要求离线 `pytest`，实机行为没人替你验。
- **解决**：三件事——`design.md` 新增 §5.5.1「沙箱使用契约」（10 条约束，每条给出原因、违反症状、对应检查）；新增 `tests/test_sandbox_live.py`（默认跳过，`GRADUS_SANDBOX_LIVE=1` 时一次跑完副本位置、cwd、相对路径、已跟踪 / 未跟踪文件、越界读写、删除、断网、超时、codemode、合回、孤儿清理）；`coding_plan.md` 完成定义加第 5 条，动沙箱必须跑实机契约。
- **验证**：`GRADUS_SANDBOX_LIVE=1 uv run python -m pytest gradus/coding/tests/test_sandbox_live.py -q -s` → 10 passed（appcontainer，约 34s）；离线 `uv run pytest -q` → 578 passed, 10 skipped。
- **关联**：M14、`design.md` §5.5.1、`tests/test_sandbox_live.py`、`coding_plan.md` 完成定义。注：`Remove-Item` 在 AppContainer 里被拒属于平台限制、未修复，按本文件规则留在 `coding_plan.md` M14 待定列，不归档到这里。

### 沙箱里模型没有可用的删文件手段

- **状态**：已解决
- **现象**：AppContainer 里 `Remove-Item` / `cmd del` 一律 `Access is denied`，模型要删文件只能碰运气写 `[System.IO.File]::Delete()`；多数模型第一条尝试就是 `Remove-Item`，失败后往往转去改内容，删除类任务整体做不成。
- **触发**：`sandbox: appcontainer` 下的任何删除需求。
- **原因**：平台限制（授 `M` / `F` / `DE,DC` 都绕不过），而文件工具里只有读、列、写、改，没有删除；把所有压力都压到 shell 上。
- **解决**：新增 `delete_file` 工具（`DELETE_SPEC` + `DeleteFileTool`），走宿主进程 `unlink` 副本里的文件，不经过 AppContainer；注册进 `create_file_tools`，权限策略按写工具处理（`WRITE_TOOLS`，一律询问）；目录与不存在的文件分别返回明确错误。
- **验证**：`tests/test_files.py::WriteToolsTest`（删除、目录、缺失文件 3 个用例）、`tests/test_permissions.py::PolicyTest::test_writes_ask`（`delete_file` 走询问）、`tests/test_sandbox_live.py::SandboxMergeTest::test_delete_file_tool_removes_on_host`（副本里删除、关闭后宿主消失且计入 `deleted`）。
- **关联**：M14、`tools/files.py`、`permissions.py`、`design.md` §5.5 / §5.5.1。

### AppContainer profile 从不回收

- **状态**：已解决
- **现象**：`DeleteAppContainerProfile` 已绑定但从未调用；profile 与解释器 ACE 永久留在系统里，怀疑授权错乱时没有干净的重来方式。
- **触发**：升级、换解释器、手工改过 workspace ACL 之后。
- **原因**：固定 profile 的设计目标是跨会话复用同一个 SID（崩溃后残留 ACE 下次还能清掉），所以正常关闭时刻意不删；缺少一个显式的「清掉重来」入口。
- **解决**：新增 `appcontainer.reset_state()` 与 CLI `gradus --reset-sandbox`：先撤销 `acl-grants.json` 里记录的 ACE、清空记录、删掉 `acl-*.done` 标记，最后 `DeleteAppContainerProfile`；若检测到别的 Gradus 进程还在用 appcontainer（记录里的 pid 仍存活）则拒绝执行，避免把在用的 SID 删掉。`DeleteAppContainerProfile` 在 profile 不存在时也返回 `S_OK`，所以删除前先看 `%LOCALAPPDATA%\Packages\<name>`，避免每次都说「已删除」。解释器 ACE 不单独撤销——SID 由固定名字派生，profile 重建后仍指向同一个 SID，ACE 会重新生效。
- **验证**：实机 `uv run gradus --reset-sandbox` → 撤销记录、删除 profile；随后跑 `GRADUS_SANDBOX_LIVE=1` 全套（11 passed），profile 自动重建、解释器授权重新计算。
- **关联**：M14、`appcontainer.py`、`cli.py`、`design.md` §5.5「同机多用户与并发实例」。

### 同机多用户时清理可能碰到别人的残留

- **状态**：已解决
- **现象**：副本 `.owner` 与 Docker 容器标签只记 pid；临时目录被重定向到共享位置、或目标机器上 Docker daemon 由多个用户共用时，一个用户启动 Gradus 会去清理另一个用户留下的副本 / 容器。
- **触发**：同机多用户，或 TEMP 指向共享目录。
- **原因**：清理判定只看「pid 是否存活」；pid 是机器级编号，跨用户没有归属信息。
- **解决**：三处一起改——AppContainer profile 名带用户名摘要（`gradus-sandbox-<hash>`，SID 只由名字派生，固定名字会让同机所有用户共用 SID）；副本 `.owner` 增加 `user` 字段、Docker 容器增加 `gradus.user` 标签；`workspace.cleanup_orphans()` 与 `sandbox.cleanup_orphans()` 在 pid 判定前先比用户名，不是当前用户的直接跳过。状态目录本身（`%TEMP%`、`%LOCALAPPDATA%\gradus`）按用户分开。
- **验证**：`tests/test_workspace.py::OrphanCleanupTest::test_skips_copies_owned_by_another_user`、`tests/test_sandbox.py::CleanupOrphansTest::test_skips_containers_owned_by_another_user`、`test_run_argv_hardens_the_container`（`gradus.user=` 标签）。
- **关联**：M14、`workspace.py`、`sandbox.py`、`design.md` §5.5「同机多用户与并发实例」。

### 沙箱会话里 MCP server 看到的是宿主 workspace

- **状态**：已解决（只覆盖 stdio 的缺省路径）
- **现象**：模型用命令或文件工具改了副本，MCP 工具读不到；MCP 改的文件沙箱里看不到，关闭合并时还可能被覆盖。两边都以为自己在改 workspace。
- **触发**：沙箱后端（`docker` / `appcontainer`）+ stdio MCP server，且 `mcp.json` 没写 `cwd`。
- **原因**：MCP SDK 的 `stdio_client` 在宿主直接起进程，不经过 `Environment.spawn`；`cwd` 缺省继承 Gradus 进程，指向宿主项目根，而不是副本。
- **解决**：`Session.connect` 在连接 MCP 前，把「`command` 非空且 `cwd` 为空」的 server 用不可变 `replace` 换成 `cwd=副本根`（`session._mcp_servers_in`）；原对象不动，刷新重连仍按原配置。显式写了 `cwd` 的保持原样。**没有做中途合并**：`sync()` 只在 `aclose()` 执行，worktree 模式对同一补丁重复 `git apply` 会失败，而且合并后副本继续跑会立刻与宿主分叉。
- **验证**：`tests/test_session.py::McpSandboxCwdTest`（只改缺省 cwd、显式 cwd 与 HTTP 不动、接入 `Session.connect` 后 server 拿到副本路径）。
- **关联**：M14、`session.py`、`mcp.py`、`design.md` §5.5。仍不覆盖：HTTP / SSE 没有 cwd，操作宿主；绝对路径参数仍指向宿主；server 进程本身仍在宿主、具备宿主权限和网络——「MCP server 进程入沙箱」留在 `coding_plan.md` M14 待定列。
