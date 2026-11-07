# Gradus `coding` 层

[English](README.md)

Gradus 的产品层：把 `agent` 的循环装配成能直接用的助手。

## 依赖方向

```
coding/   产品层：配置、会话、工具、权限、入口
   |
agent/    循环、工具运行时、事件
   |
ai/       Provider、内部协议
```

规则：

- `coding` 可以 import `gradus.agent.*`、`gradus.ai.*` 与第三方库。
- 库代码里，`coding` 是唯一读环境变量、持有 API key、管理连接池生命周期的层；开发脚本 `ai/scripts/record.py` 例外，它也读 `.env` 取录制用的 key（解析在 `ai/envfile.py`）。
- `coding` 不修改 `agent` / `ai` 的协议；需要新能力时先在下层加口子，不在本层绕过去。

## 职责

负责：

- 读配置与 API key，构造 `ModelSpec` 与 Provider；
- 管理进程级 `httpx.AsyncClient` 与 `Agent` 的生命周期；
- 实现具体工具（文件、搜索、命令、MCP 资源、脚本执行），声明 `execution_mode` 与超时；
- 用 `before_tool_call` 做权限拦截，用事件流做日志与终端展示；
- 会话历史的内存保存与落盘；
- 上下文预算、压缩、重试与模型路由；
- 连接 `.gradus/mcp.json` 中列出的 MCP server（项目配置首次确认后记住），按 server / `toolExposure` 策略暴露工具：默认 `deferred`，由 `tool_search` 按需加载；配了 `oauth` 的 HTTP server 走浏览器授权，token 存在 `~/.gradus/mcp-auth.json`；
- 从 `<workspace>/.gradus/extensions` 动态加载扩展（需信任）——Python 工具、Markdown 斜杠命令、`SKILL.md` 技能；`/reload` 可不重启重新加载；
- 从 workspace 及其上级目录读 `AGENTS.override.md` / `AGENTS.md` / `CLAUDE.md`，按「根 -> workspace」拼进系统提示（不需要信任）；
- 命令与 `codemode` 走可替换的执行环境：`restricted`（Windows，免管理员：读跟随当前账户、写只在副本里；**不阻断网络**）、`appcontainer`（Windows 零安装，已弃用）、`local`（宿主）、`docker`（长驻容器、默认断网、限制内存 / CPU / 进程数、不继承宿主环境变量；**测试中**，保留为跨平台备用）；拿不到所选后端时不回落 local，直接禁用命令类工具并告警；
- 命令行入口。

不负责：

- 循环、轮次、工具调度、事件形状；
- Provider 协议与流式解析；
- 首字节前的网络重试（`ai` 负责）。

## 配置

优先级：命令行参数（`--service` / `--provider` / `--model` / `--base-url`）> 环境变量 > `~/.gradus/config.json`。某一级缺的键会回落到下一级。

`~/.gradus/config.json`（可用 `--config` 指定别的路径）：

```json
{
  "service": "deepseek",
  "model": "deepseek-chat",
  "api_key": "${DEEPSEEK_API_KEY}",
  "context_window": 200000,
  "max_output_tokens": 8192
}
```

`service` 是内置服务商快捷名（`deepseek` / `kimi-compat` / `qwen` / `anthropic` / `openai` 等，表在 `gradus/ai/services.py`）：写它就不用再填 `provider` 与 `base_url`。显式写的 `provider` / `base_url` 覆盖表里的值，但 `provider` 与 `service` 的协议不一致会报错。

`${NAME}` 会替换成同名环境变量。未知字段、JSON 格式错误、引用了未设置的变量都会报错；默认文件不存在只是表示“没有配置文件”。

CLI 启动时还会读 `<workspace>/.env`：文件里的值优先，会覆盖同名环境变量；空值视为未配置。解析在 `gradus/ai/envfile.py`，与 `ai/scripts/record.py` 共用。`.env` 按「一套连接」组织，把 `GRADUS_SERVICE`（带出协议与 `base_url`）、`GRADUS_MODEL`、`GRADUS_API_KEY` 写在一起，需要自定义端点时再加 `GRADUS_BASE_URL`；单靠 key 认不出服务商，所以端点和 key 必须成对出现，换服务商就整段替换。

项目被记进 MCP 信任记录后，可用 `gradus --untrust [路径]` 撤销（省略路径时用当前 workspace），执行完直接退出。

沙箱相关的三个字段：`sandbox`（`auto` / `local` / `docker` / `appcontainer` / `restricted`，缺省 `auto`）、`sandbox_image`（缺省 `python:3.13-slim`）、`sandbox_network`（缺省 `false`，即 `--network none`）。`auto` 按 `restricted` -> `appcontainer` -> `docker` -> `local` 的顺序挑第一个可用的，命令行可用 `--sandbox docker` / `--sandbox restricted` 显式指定。Docker 不可用时命令类工具会被禁用并打印 `sandbox warning:`。沙箱后端会给会话一份 workspace 副本（git 仓库根走 `git worktree` 并把未跟踪文件也带进去，其余整树复制），文件工具与命令都读写副本；退出时自动把改动合回宿主，新增、修改、删除都同步，合并失败会保留副本并打印路径，强杀留下的副本在下次启动时清理——因此命令与写工具仍然逐次询问。容器带 `gradus.owner=<pid>` 标签，进程被强杀后残留的容器会在下次以 docker 后端启动时清掉。`restricted` 是 Windows 目标后端：合成 SID + `CreateRestrictedToken`（`WRITE_RESTRICTED`）+ `CreateProcessAsUserW`，读跟随当前账户、写只落在副本与 scratch，**不阻断网络**。`appcontainer` 保留为已弃用的零安装兜底：AppContainer 隔离、默认断网、只注册 `powershell`（没有 Git Bash）。

## 目录

```
gradus/coding/
├── README.md
├── README.zh.md
├── coding_plan.md      # 里程碑、进度与未定项
├── config.py           # 配置与模型选择：环境变量 -> ModelSpec
├── session.py          # 会话装配与 JSONL 历史
├── context.py          # token 预算、压缩切点与摘要
├── retry.py            # 运行级重试策略（同模型）
├── registry.py         # 工具注册表与函数工具适配
├── mcp.py              # MCP server -> AgentTool
├── render.py           # 事件渲染：展示、日志
├── permissions.py      # 工具权限策略
├── sandbox.py          # 执行环境：协议、local、docker
├── restricted.py       # Windows 受限令牌后端（目标）
├── win32.py            # 两个 Windows 后端共用的 Win32 原语：管道、Job Object、ACL
├── appcontainer.py     # 旧的 AppContainer 后端（已弃用）
├── workspace.py        # 沙箱自有 workspace 副本与合回宿主
├── cli.py              # 命令行入口
├── tools/
│   ├── files.py        # read_file / write_file / edit / list_dir
│   ├── search.py       # grep / glob
│   └── shell.py        # bash / powershell
├── docs/
│   ├── design.md       # 设计说明
│   └── problem.md      # 已解决问题归档
└── tests/
```

没有 `__init__.py`，和 `gradus/ai`、`gradus/agent` 一样使用 namespace package。

## 现状

当前状态与未定项见 [coding_plan.md](coding_plan.md)。
