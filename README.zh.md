<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/banner-dark.svg">
  <img src="docs/banner-light.svg" alt="Gradus" width="440">
</picture>

[![CI](https://github.com/xyzhhh123/Gradus/actions/workflows/ci.yml/badge.svg)](https://github.com/xyzhhh123/Gradus/actions/workflows/ci.yml)
![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-3776ab?style=flat-square)
![License MIT](https://img.shields.io/badge/license-MIT-3da639?style=flat-square)
![Status pre-1.0](https://img.shields.io/badge/status-pre--1.0-fb923c?style=flat-square)

**分层的 Coding Agent 运行时。** Gradus 把模型的流式输出变成能调用工具的会话，并给每个会话一份私有的 workspace 副本，会话结束时再合回宿主。

[English](README.md) · [中文](README.zh.md) · [日本語](README.ja.md) · [한국어](README.ko.md)

<br>

<img src="docs/web-ui.gif" alt="Gradus Web UI：一个会话正在流式输出，并调用 bash 工具" width="820">

<sub>一个回合：流式回复 → <code>bash</code> 工具调用 → 结果。</sub>

</div>

需要 Python 3.13，以及一个模型（`.env` 里三行）。完整步骤见[快速开始](#-快速开始)。

```bash
git clone https://github.com/xyzhhh123/Gradus.git
cd Gradus && uv sync
uv run gradus
```

## 🎯 为什么是 Gradus

三条与常见 agent 外壳不同的主张。

1. 🧠 **历史属于会话，不属于厂商。** 历史以厂商中立的表示存储，每次请求再由当前 provider 翻译，所以一个会话可以在对话中途换到 Anthropic、OpenAI、Gemini 风格的端点。厂商私有字段（如 thinking block 的 `signature`）在转换中不会保留，其余都会。
2. 📦 **agent 不直接改你的 workspace。** 每个会话跑在副本上 —— 是 git 仓库就用 `git worktree`，否则用过滤过的目录树 —— 关闭时才合回。进程被杀，你没有任何损失。
3. 🧰 **它是运行时，不是应用。** 四个包，import 单向。`ai` 对其上层一无所知，所以你可以只取协议层；`agent` 给你循环与门面，不附带 CLI。

## ✨ 特性

- 🔌 **四种模型协议，一套内部形状** —— Anthropic Messages、OpenAI Chat Completions、OpenAI Responses、Gemini 统一成一套消息模型与 12 个流式事件。Gemini 按官方格式实现，仍是**测试中**（尚未用真实端点验证）。
- 🔁 **真正可控的工具循环** —— 单层异步循环、9 个 hook、8 个运行级事件、并行工具、`steer` / `follow_up`、取消与重试。
- 🛡️ **沙箱执行** —— Windows 免管理员即可用的 `restricted`，另有 `appcontainer`（已弃用）、`docker`、`local`。见[沙箱](#-沙箱)。
- 👥 **不止一个 agent** —— 多 agent 团队、后台任务、定时任务、历史召回，都以工具形式内置在 `coding` 层。
- 🧩 **MCP 与扩展** —— 项目级 MCP server（显式信任 + OAuth），以及从 `.gradus/` 加载的 Python 工具、斜杠命令与 `SKILL.md` 技能。
- 🐍 **Codemode** —— 模型写 Python 调用 `tools.*`，只有 `text()` / `return` 的结果进入上下文。
- 💾 **会话与上下文** —— JSONL 历史、断点恢复、token 预算、自动压缩、会话中途换模型。

## 🚀 快速开始

### 💻 运行 CLI

```bash
uv sync
```

不想保留仓库副本的话，也可以直接装成工具：

```bash
uv tool install git+https://github.com/xyzhhh123/Gradus
```

下面的示例把 `uv run gradus` 换成 `gradus` 即可。

在 `<workspace>/.env` 里写一段连接配置：

```ini
GRADUS_SERVICE=kimi-compat
GRADUS_MODEL=kimi-k2.7-code
GRADUS_API_KEY=sk-...
```

```bash
uv run gradus                      # workspace 就是当前目录
uv run gradus --workspace ../app   # 或者指到别处
uv run gradus --sandbox docker     # auto -> restricted -> appcontainer -> docker -> local
```

光有 key 无法确定 provider，所以 service、model、key 必须一起配。优先级：CLI 参数 > `.env` > `~/.gradus/config.json`。

REPL 里：`/reload` 重建工具与扩展，`/command` 与 `/skill:name` 展开已存提示词，`exit` 或 `Ctrl-D` 退出。

### 🌐 运行 Web UI

两个进程：Python host 与浏览器 bundle。先起 host。

```bash
uv sync --extra web
uv run gradus-serve --port 8765 --token dev
```

它会打印带 token 的地址：

```
Gradus web · kimi-k2.7-code · H:\your\workspace
open: http://127.0.0.1:8765/?token=dev
```

token 不是可选项：`/ws` 在没有正确 token 时会以 4401 关闭。去掉 `--token`，host 会随机生成一个并打印对应 URL。

然后二选一：

```bash
cd web && npm install && npm run build   # host 直接服务 web/dist —— 打开打印的地址
cd web && npm run dev                    # Vite 在 :5173，把 /ws 代理到 127.0.0.1:8765
```

用 dev server 时打开 `http://localhost:5173/?token=dev`：前端从页面 URL 读 `token`，所以必须与 `--token` 一致。加 `&mock=1` 可以完全不连 host，用脚本化的假连接跑 UI。

改了 `gradus/` 下的东西要重启 host；前端保存即热更，不需要重启。

### 🧪 运行测试

```bash
uv run pytest -q
```

离线跑 1002 个用例通过，约 95 秒。另有 15 个实机用例默认跳过：沙箱契约需要 `GRADUS_SANDBOX_LIVE=1`，容器测试需要 `GRADUS_DOCKER_LIVE=1`。

## 🔒 沙箱

| 后端 | 使用时机 | 边界 |
| --- | --- | --- |
| 🪟 `restricted` | Windows；`auto` 首选 | 受限令牌（合成 SID + logon SID + `Everyone`）：读跟随你的账户，写只落在副本与 scratch。**不隔离网络。** 免管理员，无需额外安装。 |
| 🪟 `appcontainer` | Windows；仅当 `restricted` 不可用 | AppContainer 令牌，无网络，Job Object 杀子进程。**已弃用**：保持可用，不再投入。 |
| 🐳 `docker` | 有 Docker engine | 容器，带 `--network none`、内存 / CPU / PID 限制。**测试中**：作为跨平台兜底保留，尚未用真实 engine 验证。 |
| 🏠 `local` | 兜底 | 无；命令以你的用户权限运行 |

沙箱会话从不直接改你的 workspace。它拿到一份私有副本 —— 是 git 仓库就用 `git worktree`，否则用过滤后的目录树 —— 文件工具与命令都作用在副本上。关闭时，新增与修改的文件写回，删除被执行；合并失败则保留副本并打印其路径。被强杀进程留下的副本，会在下次启动时清理。

有三条边界值得直说：

- ⚠️ **`restricted` 不拦网络。** 写边界是真的，网络边界不是。命令必须离线时用 `docker`。
- 🔒 **命令审批仍然开着。** 副本保护宿主免于误伤，但不等于让任意命令变安全。
- 🚫 **MCP server 进程不在沙箱内。** 它们以你的权限跑在宿主上。stdio server 的默认 `cwd` 会指到副本上，让相对路径对得上，但 HTTP server 与绝对路径仍然触达宿主。

## 🧱 架构

```
gradus/host      Web 层        host 进程、wire 协议、时钟
gradus/coding    产品层        配置、会话、工具、权限、沙箱、MCP、CLI
gradus/agent     控制层        循环、hook、并行工具、Agent 门面
gradus/ai        协议层        Provider、流式事件、传输
```

依赖只能向下。`ai` 不 import `agent` / `coding`；`agent` 不 import `coding`；`coding` 不 import `host`。

### 📐 设计原则

1. ⬇️ **依赖只能向下。** 上层可以调下层，下层永远看不到上层。
2. 📡 **事件即接口。** 12 个 `ai` 事件被 `agent` 原样转发，只追加运行级事件。
3. ⚡ **全链路异步。** `Provider.stream()`、`run_loop()`、`AgentTool.execute()` 都是 async；循环从不阻塞在 I/O 上。
4. 🔧 **工具属于上层。** `agent` 定义 `AgentTool` 协议；文件、搜索、shell 的实现活在 `coding`。

## 🐍 Python API

跑通一个完整循环，离线，无需网络与 API key：

```python
import asyncio

from gradus.agent.base import LoopConfig
from gradus.agent.loop import run_loop
from gradus.agent.types import AgentDone
from gradus.ai.base import ModelSpec
from gradus.ai.providers.fake import FakeProvider
from gradus.ai.types import UserMessage

spec = ModelSpec(
    provider="fake",
    model="fake",
    base_url="",
    context_window=8192,
    max_output_tokens=1024,
)
provider = FakeProvider(
    spec,
    scripts=[{
        "name": "answer",
        "steps": [
            {"kind": "text", "chunks": ["Hello, ", "I am Gradus"]},
            {"kind": "finish", "reason": "stop"},
        ],
    }],
)

async def main() -> None:
    async for event in run_loop(provider, [UserMessage("hi")], LoopConfig(system="")):
        if isinstance(event, AgentDone):
            print(event.new_messages[-1].content[0].text)

asyncio.run(main())
```

接真实模型，用有状态的 `Agent` 门面：

```python
from gradus.agent.agent import Agent
from gradus.agent.base import LoopConfig
from gradus.ai.base import ModelSpec

spec = ModelSpec(
    provider="anthropic",
    model="claude-sonnet-4-5",
    base_url="https://api.anthropic.com",
    context_window=200_000,
    max_output_tokens=8192,
)

agent = await Agent.connect(
    spec,
    LoopConfig(system="You are a helpful assistant"),
    api_key="sk-...",
)
try:
    async for event in agent.prompt("hi"):
        ...
finally:
    await agent.aclose()
```

`Agent.connect()` 创建一个 `httpx.AsyncClient` 并跨多次运行复用；`aclose()` 关闭它。把同一个 `client` 传给多个 `connect()` 可以共享连接池。

## 📖 参考

### 🚩 CLI 参数

| 参数 | 用途 |
| --- | --- |
| `--workspace` | 工具与命令的根目录（默认当前目录） |
| `--sandbox` | `auto` / `local` / `docker` / `appcontainer` / `restricted` |
| `--service` / `--provider` / `--model` / `--base-url` | 覆盖连接配置 |
| `--resume` / `--session-dir` | 恢复某个 JSONL 会话，或指定新会话存放位置 |
| `--mcp` / `--config` | 显式指定 MCP 与配置文件路径 |
| `--untrust [path]` | 撤销某项目 MCP 配置的信任，然后退出 |
| `--reset-sandbox` | 清理孤儿副本、AppContainer 授权与 profile |
| `--log` | 把工具与错误事件追加到 JSONL |

### 🧩 候选模型

Web 的模型选择器、设置面板与主题切换，见 **[docs/web-ui.md](docs/web-ui.md)**。

### 📁 目录

```
Gradus/
├── gradus/
│   ├── ai/          # Provider、内部协议、流式事件、传输
│   ├── agent/       # 循环、hook、工具运行时、有状态 Agent
│   ├── coding/      # 配置、会话、工具、权限、沙箱、MCP、CLI
│   └── host/        # Web host 进程、wire 协议、时钟
├── bench/           # 基准测试框架
├── web/             # 浏览器 bundle
├── pyproject.toml
└── uv.lock
```

Python 3.13 · 运行依赖：`httpx`、`httpx2`、`jsonschema`、`mcp` · 环境用 `uv` 管理。

## 📚 文档

| 层 | README | 设计 | 计划 |
| --- | --- | --- | --- |
| `ai` | [README](gradus/ai/README.md) | [设计](gradus/ai/docs/design.md) | [计划](gradus/ai/ai_plan.md) |
| `agent` | [README](gradus/agent/README.md) | [设计](gradus/agent/docs/design.md) | [计划](gradus/agent/agent_plan.md) |
| `coding` | [README](gradus/coding/README.md) | [设计](gradus/coding/docs/design.md) | [计划](gradus/coding/coding_plan.md) |

`gradus/coding/docs/` 下的延伸阅读：[沙箱](gradus/coding/docs/sandbox.md) · [上下文设计](gradus/coding/docs/context_design.md) · [多 agent](gradus/coding/docs/multi_agent.md) · [后台任务](gradus/coding/docs/background_tasks.md) · [定时任务](gradus/coding/docs/cron_tasks.md) · [历史召回](gradus/coding/docs/recall.md)。

## 🙏 致谢

Gradus 的分层、事件模型与工具契约参考了这些开源项目：

- **Pi**（`@earendil-works/pi-agent-core`）：agent 循环、hook 体系、steering / follow-up、工具协议的参照。
- **OpenCode**：API 与工具设计的参照。

## 📄 许可证

[MIT](LICENSE)
