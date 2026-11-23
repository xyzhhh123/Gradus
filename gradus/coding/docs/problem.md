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

### restricted 后端首次实机：进程起不来、写不进副本、关闭时崩溃

- **状态**：已解决
- **现象**：分三次出现——① 子进程立刻退出，`0xC0000142`（DLL 初始化失败），IDE 终端里还会被报成 `TRAE Sandbox Error: code=536870917`；② 进程能起来、能读，但写副本 `Access is denied`；③ 会话关闭时宿主进程 `0xC0000374`（堆损坏）。
- **触发**：`CreateRestrictedToken` 起进程的第一次实机验证。
- **原因**：三个独立问题——① 受限 SID 清单只有自己的合成 SID，系统 DLL 的读检查过不去，补上 logon SID 与 `Everyone`（对齐 Codex 的 workspace-write 形态）后正常；② 合成 SID 没在系统里注册，`icacls *<SID>` 报 1332（no mapping），授权根本没写进去；③ logon SID 是 `GetTokenInformation(TokenGroups)` 缓冲区里的指针，`aclose` 里对它 `LocalFree` 造成堆损坏。
- **解决**：受限 SID 清单改为「合成 SID + logon SID（`TokenGroups` 里 `SE_GROUP_LOGON_ID` 那条）+ `Everyone`」，且不放当前用户 SID（否则用户能写的地方全部放行）；ACE 改用 PowerShell `Get-Acl` / `Set-Acl` 配 `SecurityIdentifier` 对象，绕开 icacls 的名字解析；SID 分两类管——`ConvertStringSidToSidW` 分配的自己释放，logon SID 只保活它所在的缓冲区。
- **验证**：`GRADUS_SANDBOX_LIVE=1 uv run python -m pytest gradus/coding/tests/test_sandbox_live.py -q -s` → 11 passed / 1 skipped（`restricted`，网络那条按后端跳过）；实机确认副本可写、`C:\Users\Public` 写被拒、`C:\Windows\win.ini` 可读。离线：`tests/test_sandbox.py::RestrictedBackendTest`。
- **关联**：M16、`restricted.py`、`win32.py`、`design.md` §5.5 与 §5.5.1。另记一条环境限制：`%TEMP%` 被重定向到 `C:\Windows\Temp` 时受限进程写不进副本（父目录 traverse 只对登录用户开放），实机测试需把 `TEMP` 指到用户临时目录；`appcontainer` 反过来只认 `C:\Windows\Temp`。

### 设置页保存模型报 `[Errno 13] Permission denied: '...config.json.tmp'`

- **状态**：已解决（非代码缺陷，是启动环境的写限制）
- **现象**：Web 设置页保存候选模型，界面回 `[Errno 13] Permission denied: 'C:\Users\<user>\.gradus\config.json.tmp'`，磁盘上的 `config.json` 没有变化；把服务改成 `--config <workspace>\.gradus\test-config.json` 重启后，同样的操作一次就成功。
- **触发**：服务由**带受限写令牌的后台通道**启动（本项目 IDE 的后台命令通道即如此），而默认配置路径在用户主目录 `~/.gradus/config.json`。
- **原因**：`save_models` → `_write_payload` 的写法是「先写 `config.json.tmp`，校验后 `os.replace`」。受限令牌只放行工作区写入，第一步 `temp.write_text()` 就 EACCES。报错名字是 `.tmp` 而不是 `config.json`，正说明卡在**创建临时文件**、没走到替换。对照实验（同一用户、同一目录）：前台 PowerShell `Set-Content` 成功、前台 `uv run python` 写成功、后台长驻进程写同一路径被拒，而该进程写工作区内的会话文件 / 后台日志 / `tasks.json` 一直成功——所以是进程令牌，不是 ACL。
- **解决**：不改代码。要么起服务时用 `--config` 把配置放进工作区，要么从自己的终端启动（无写限制）。`save_models` 的原子写入保证失败时目标文件不动，所以配置不会被写坏。
- **验证**：`settings/save` 连续三次复现失败；改 `--config` 到工作区后连续三次成功；`Get-Acl` 显示当前用户对 `~/.gradus` 是 FullControl。
- **关联**：`config.py` 的 `save_models` / `_write_payload`、`host/serve.py` 的 `handle_settings_save`。

### 受限令牌读不到 `tempfile.mkdtemp` 建的目录

- **状态**：已解决
- **现象**：`restricted` 后端能起来之后，`codemode` 一律失败，只回一句 `script exited without a result`（子进程 stderr 被 `_drain` 吞掉）；契约测试「读 workspace 外的文件」也报 `Access to the path '<...>\secret.txt' is denied`。
- **触发**：环境 scratch 由 `tempfile.mkdtemp` 建；`codemode` 先把脚本写进 scratch，再让受限子进程去读它。
- **原因**：`mkdtemp` 在 Windows 上建的是**受保护 DACL** 目录（SDDL 带 `D:P`，只授 `OWNER RIGHTS` / `SYSTEM` / `Administrators`）。沙箱子进程是 LUA 过滤令牌，`OWNER RIGHTS` 在这个令牌下匹配不上（令牌里 Administrators 是 deny-only），于是它读不到目录里任何文件——`codemode` 的脚本正是这么读不到的。普通 `mkdir` 继承 `%TEMP%` 的 ACE（含用户 SID 与 `Users`），读就正常。
- **解决**：新增 `sandbox._private_dir()`——Windows 上在 `%TEMP%` 下用普通 `mkdir` + 随机名字建目录（POSIX 仍用 `mkdtemp` 的 0700 语义）；`create_scratch`、`appcontainer` 的 scratch 都改走它。契约测试的临时基座也从 `TemporaryDirectory` 换成普通目录，否则测的仍是受保护 DACL。
- **验证**：`GRADUS_SANDBOX_LIVE=1 uv run python -m pytest gradus/coding/tests/test_sandbox_live.py -q` → 11 passed / 1 skipped；实测受限会话里 `codemode` 的 `text('hi')` 与 `tools.read_file` 都能跑；`tests/test_sandbox.py::ScratchLayoutTest` 固定「Windows 上不调 `mkdtemp`」。
- **关联**：`sandbox.py` 的 `create_scratch` / `_private_dir`、`appcontainer.py`、`tests/test_sandbox_live.py::SandboxContractTest`。这一条同时修正上一条（`restricted 后端首次实机`）里「ACE 用 `Get-Acl` / `Set-Acl`」的说法，原因见下一条。

### `Set-Acl` 在受保护 DACL 上要求 `SeSecurityPrivilege`

- **状态**：已解决
- **现象**：`restricted` 后端起来前就报 `ACL AddAccessRule failed on <dir> (exit 1)`，stderr 被吞成一句空话；把 stderr 捞出来后是两种：`the module could not be loaded`（`Get-Acl`）与 `does not possess the 'SeSecurityPrivilege'`（`Set-Acl`）。
- **触发**：ACL 助手用 Windows PowerShell 5.1 跑 `Get-Acl` / `Set-Acl`，目标目录是 `mkdtemp` 建的受保护 DACL 目录。
- **原因**：① 子进程继承 pwsh 父进程的 `PSModulePath`（PS7 目录），5.1 解析到 PS7 的 `Microsoft.PowerShell.Security` 加载失败；② 非提权令牌修改受保护 DACL 需要 `SeSecurityPrivilege`，普通进程没有。
- **解决**：脚本整段不再调 cmdlet，改用 `[System.IO.DirectoryInfo]::GetAccessControl()` / `SetAccessControl()`（Win32 `SetSecurityInfo`，只要 `WRITE_DAC`）；`_acl` 仍给 5.1 子进程一份自己的 `PSModulePath` 作保险。
- **验证**：同一目录 before/after SDDL 对照（`D:P(...OW)(...SY)(...BA)` → 多出会话合成 SID 的 `(OI)(CI)M`）；`tests/test_sandbox.py` 的 `test_acl_script_avoids_the_security_cmdlets` 与 `test_acl_helper_gets_windows_powershell_module_paths`。
- **关联**：`restricted.py` 的 `_ACL_SCRIPT` / `_acl` / `_legacy_module_env`。

### 真机上压缩从不触发，历史被兜底裁剪悄悄裁短

- **状态**：已解决
- **现象**：真机连跑六轮，每轮 `input_tokens` 一直停在同一个数（18,653），`cache_read` 也不涨，`Session.compaction` 始终是 `None`；会话没报错，模型的回答也很正常。看起来「上下文管理在工作」，实际是每轮都在 `transform_messages` 里把旧消息丢掉。
- **触发**：真实模型、真实用量（每轮 `usage.input_tokens` 非零）下跑长会话；离线测试的假历史里 assistant 没有 `usage`，所以一直是绿的。
- **原因**：两个叠在一起。
  1. `_maybe_compact` 的接受条件用 `context_tokens(...)` 算压前压后，而它在没有锚点时**会退回「最后一条带 usage 的 assistant」**——那个数描述的是压缩**前**发出的那次请求。两边拿到同一个数，`after >= before` 恒成立，摘要算完也被丢掉。
  2. 预算只在轮次**开始前**检查。那时候本轮的新消息还没进历史，等它进历史时，`transform_messages` 的兜底裁剪已经先一步把旧消息删了——压缩永远晚一步。
- **解决**：接受条件两边都换成**纯估算**（`estimate_messages_tokens`），保证是同一个度量；`_run` 在轮次收尾再调一次 `_maybe_compact()`，那时本轮消息已全部落进历史，切点也终于可算（`select_cut` 不切当前轮）。
- **验证**：真机 `scripts/verify_context_live.py` 全绿——3 轮后压缩发生，`view=3 / history=6`，视图首条是摘要标记，摘要命中七栏、1,001 token，`recall` 能取回被压掉的原文、拒绝仍在上下文里的条目，重启后视图与运行时逐条一致；同一次运行里 `cache_read=11,136`，证明追加历史确实在复用前缀。离线 `test_compaction_is_not_refused_by_the_last_request_usage`（去掉修复即失败）、`test_budget_is_checked_at_turn_start_and_end`。
- **关联**：`session.py` 的 `_maybe_compact` / `_run`、`context.context_tokens` 的 usage 回退、`design.md` §7、`context_design.md` §5。

### 摘要请求吃不到前缀缓存（`{run}` 按 Provider 实例生成）

- **状态**：已解决
- **现象**：主对话轮次之间缓存命中正常（`cache_read=11,136`），但**摘要那一次请求**始终是 `cache_read=0`——输入 26,275 token 全价。
- **触发**：任何用 `opencode-go` 这类「要求带会话 id」的网关、并且配置里用 `{run}` 占位符的会话。
- **原因**：`{run}` 由 `ai.base.expand_headers` 在**构造 Provider 时**展开，而摘要会**新建一个 Provider**（`make_summarizer`），于是带着另一个 `x-opencode-session` 发出。网关按这个 id 把请求分流到别的机器，那台机器没有我们前面的前缀——前缀再逐字节一致也没用。两条错误猜测都排除过：① 「摘要把被裁掉的旧消息也重放了」——让它只重放上一发那几条，仍然是 0；② provider 不缓存这种形状——主对话明明命中了。
- **解决**：把 `{run}` 的展开时机从「每个 Provider」提到「每个会话」——`Session.connect` 里 `replace(settings, headers=expand_headers(settings.headers))`，主对话、摘要、合并共用同一个 id（`expand_headers` 幂等，重复展开不变）。
- **验证**：同一个上下文、同一段前缀，改前 `摘要 in=26,275 cache_read=0`，改后 `摘要 in=26,272 cache_read=18,560`（命中 70.7%）。真机脚本新增断言「摘要命中前缀缓存」；离线 `test_run_placeholder_is_frozen_per_session` / `test_summarizer_gets_the_frozen_header`。
- **关联**：`session.py` 的 `Session.connect`、`ai/base.py` 的 `expand_headers`、`ai/services.py` 的 `SERVICE_HEADERS`、`context_design.md` §5。

### 流式响应没关干净：`aclose(): asynchronous generator is already running`

- **状态**：已解决
- **现象**：一轮对话正常跑完、结果也对，但进程退出时 stderr 冒出
  `an error occurred during closing of asynchronous generator <AsyncClient.stream ...>` /
  `RuntimeError: aclose(): asynchronous generator is already running`。只出现在脚本里自己
  调 `asyncio.run` + `Session.aclose()` 的场景，官方的 `verify_context_live.py` 里没有。
- **触发**：模型走 SSE 流式（所有 provider），一轮结束后立即关闭会话。
- **原因**：`AsyncIterator` 的 `async for` 在 `break` / `GeneratorExit` 时**不会**替我们关闭
  迭代器。整条链是 `provider.stream → post_sse → _post_with_retry → _parse_sse_async →
  httpx(stream / aiter_lines / …)`；provider 在收到 `[DONE]` 时 `break`，后面几层就地被抛弃，
  要等事件循环关闭才被 GC 收尾。收尾时外层正在 `athrow`，内层又调 `aclose()`，于是报
  “already running”。实测在轮次结束后 `gc.get_objects()` 里能看到 5 个 Gradus 侧的
  `post_sse` / `_post_with_retry` / `_parse_sse_async` 仍活着。
- **解决**：逐层显式关闭，谁打开谁关：
  1. 四个 provider：`events = post_sse(...)` + `try: async for ... finally: await events.aclose()`；
  2. `post_sse`：同样的 `try/finally` 关闭 `_post_with_retry`；
  3. `_post_with_retry`：`finally` 关闭 `_parse_sse_async`；
  4. `_parse_sse_async`：`finally` 关闭上游的 `response.aiter_lines()`。
- **验证**：同一份复现脚本改前有噪声、改后干净；`gc.get_objects()` 里 Gradus 侧的异步生成器
  归零（只剩 httpx/httpcore 自己的内部迭代器，它们由 `async with client.stream` 的退出释放连接）。
  离线 `test_break_after_done_closes_post_sse`——用一个「既迭代又记录 aclose」的跟踪对象，
  去掉 provider 的 `await events.aclose()` 即失败（生成器会被引用计数碰巧回收，所以不能用生成器做断言对象）。
- **补第二刀（agent 层）**：`agent/loop.py` 用 `anext(stream)` 迭代 provider 流，三条提前返回路径
  （provider 抛异常、`StreamError`、流结束却没有 `StreamDone`）原本把生成器留给 GC；现在整段包在
  `try/finally` 里，`finally` 调 `aclose()`（用 `getattr` 兜住没有 `aclose` 的手写迭代器）。
  回归测试 `ProviderStreamCloseTest`：用一个「既迭代又记录 aclose」的对象，去掉 `finally` 即失败。
- **关联**：`ai/transport.py`（`post_sse` / `_post_with_retry` / `_parse_sse_async`）、
  `agent/loop.py`（`stream` 的 `try/finally`）、
  `ai/providers/*.py` 的 `stream()`、`ai/docs/design.md` 的 `transport.py` 章节。

### 多 agent 工具进了 deferred，能力等于不存在

- **状态**：已解决
- **现象**：会话明明配好了 `tasks` 与 `team`，模型整场一次 `tool_search` 都不调、一次 `spawn` 都不发，多 agent 能力全程未被使用。
- **触发**：`tasks` 与 `team` 都非空，`create_multiagent_tools(team)` 以 `deferred` 注册。
- **原因**：`deferred` 的工具 schema 不出现在请求里，模型只能靠主动 `tool_search` 发现；而 BASE_PROMPT 当时也没给出任何「你有派活能力」的线索。看不到，就想不起来要用。
- **解决**：
  1. `create_multiagent_tools(team)` 改为直接声明，不进 `deferred`；
  2. BASE_PROMPT 补一条：列到 sub-agent 工具时，先判断这次任务值不值得拆——值得就先 `plan_tasks` 再派活（不要自己把派出去的工作干了），不值得就说明一次并单干。
- **验证**：改前 bench 4 道题实测 0 次 `tool_search`、0 次 `spawn`。改后尚未重跑 bench 回归，需用 `python bench/run.py` 复测确认。
- **关联**：`session.py`（`_build_tools`、`BASE_PROMPT`）、`docs/multi_agent.md`。

### codemode 描述没写 import / open / print 不可用

- **状态**：已解决
- **现象**：模型在 codemode 里写 `import json`，运行即 `ValueError: import is not allowed`，一次工具调用白费。
- **触发**：任何想用脚本批量处理的任务。
- **原因**：`codemode_harness.py` 在子进程里用受限 builtins，AST 层面拒绝 `import` 与下划线属性，白名单里也没有 `open` / `print`；但 `CODEMODE_SPEC.description` 只说了 `tools.*`、`text()`、`return`，限制一个字没提。
- **解决**：描述补齐——`import` 不可用、下划线属性被拦、只拿得到 `tools` 命名空间与无副作用的 builtins（没有 `open`、没有 `print`），读数据要用 `tools.read_file(...)`。
- **验证**：`tests/test_codemode.py` 12 项通过；本改动只改描述文本，无行为变化。
- **关联**：`tools/codemode.py`（`CODEMODE_SPEC`）、`tools/codemode_harness.py`（`ALLOWED_BUILTINS`）。

### `<project_context>` 不被当作权威，模型重复勘察

- **状态**：已解决
- **现象**：一个 `project_context` 已能完整回答的架构问题（"`gradus/` 下有哪些包？依赖方向是什么？"）烧掉 16 个回合、96,075 输入 token、27 次工具调用，中途反复 grep 到 `(no matches)`。
- **触发**：工作区存在 `AGENTS.md`，且问题本身只涉及布局与依赖方向。
- **原因**：`BASE_PROMPT` 只写了「先用 grep/glob 定位再读文件」，全文没提 `<project_context>` 是权威来源。模型把项目地图当背景资料而非答案，仍然从零勘察。
- **解决**：BASE_PROMPT 增加一条：`<project_context>` 是项目自带的地图，已经能回答的问题（布局、约定、命令）直接从它回答，只验证将要改动的东西。
- **验证**：同一个问题三次实测——无 `AGENTS.md`、无引导 25 次调用；有 `AGENTS.md`、无引导 27 次调用 / 16 回合 / 96,075 输入 token；有 `AGENTS.md` + 本引导 **4 次调用 / 3 回合 / 11,937 输入 token（−88%）**。答案质量未降，反而更准（指出了 `test_boundary.py` 的 AST 扫描与可见面收窄）。
- **排查备注**：期间一度怀疑 `glob` 的 `mode=dirs` 失效——日志里 `glob {"pattern": "gradus/**/*", "mode": "dirs"}` 旁边就是 `(no matches)`。实测该组合正常返回 17 个目录；日志里那行 `(no matches)` 其实是同批并行调用中 `glob {"pattern": "gradus/*"}`（文件模式，`gradus/` 下确无直接文件）的结果，两行结果错位。**不要重复排查这个。**
- **关联**：`session.py`（`BASE_PROMPT`）、仓库根 `AGENTS.md`。
