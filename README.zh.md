# Gradus

[English](README.md)

**分层的 Coding Agent 运行时。** Gradus 把模型的流式输出变成能调用工具的会话，并给每个会话一份私有 workspace 副本，会话结束时再合回宿主。

## 特性

- **四种模型协议，一套内部形状** —— Anthropic Messages、OpenAI Chat Completions、OpenAI Responses、Gemini 统一成一套消息模型与 12 个流式事件。Gemini 按官方格式实现，仍是**测试中**（尚未用真实端点验证）。
- **真正可控的工具循环** —— 单层异步循环、9 个 hook、8 个运行级事件、并行工具、`steer` / `follow_up`、取消与重试。
- **沙箱执行** —— `restricted`（Windows，免管理员：读照常，写只在副本里）/ `appcontainer`（已弃用）/ `docker` / `local`；每个会话操作私有副本，新增、修改、删除在关闭时合回宿主。
- **MCP 与扩展** —— 项目级 MCP server（显式信任 + OAuth），以及从 `.gradus/` 加载的 Python 工具、斜杠命令与 `SKILL.md` 技能。
- **Codemode** —— 模型写 Python 调用 `tools.*`，只有 `text()` / `return` 的结果进入上下文。
- **会话与上下文** —— JSONL 历史、断点恢复、token 预算与自动压缩。

## 架构

```
gradus/coding    产品层   配置、会话、工具、权限、沙箱、CLI
      |
gradus/agent     控制层   循环、hook、并行工具、Agent 门面
      |
gradus/ai        协议层   Provider、流式事件、传输
```

依赖只能向下。`ai` 不 import `agent` / `coding`，`agent` 不 import `coding`。

## 当前状态

| 层 | 内容 | 用例数 |
| --- | --- | --- |
| `ai` | 4 个厂商适配器 + fake，全异步 | 130 |
| `agent` | 循环、hook、并行工具、消息队列、`Agent` 门面 | 90 |
| `coding` | 配置、会话、工具、权限、MCP、扩展、codemode、沙箱、CLI | 380 |

共收集 600 个用例：离线 589 个通过；11 个沙箱实机契约默认跳过，需要 `GRADUS_SANDBOX_LIVE=1`。

Python 3.13 · 运行依赖：`httpx`、`httpx2`、`jsonschema`、`mcp` · 环境用 `uv` 管理。

## 快速开始

```bash
git clone https://github.com/xyzhhh123/Gradus.git
cd Gradus
uv sync
uv run pytest -q
```

### 跑 CLI

在 `<workspace>/.env` 里写一套连接：

```ini
GRADUS_SERVICE=kimi-compat
GRADUS_MODEL=kimi-k2.7-code
GRADUS_API_KEY=sk-...
```

```bash
uv run gradus                      # workspace 为当前目录
uv run gradus --workspace ../app   # 或指定其它目录
uv run gradus --sandbox docker     # auto -> restricted -> appcontainer -> docker -> local
```

单靠一个 key 认不出服务商，所以服务、模型、key 要放在一起配。优先级：命令行参数 > `.env` > `~/.gradus/config.json`。

| 参数 | 用途 |
| --- | --- |
| `--workspace` | 工具与命令的根目录（缺省为当前目录） |
| `--sandbox` | `auto` / `local` / `docker` / `appcontainer` / `restricted` |
| `--service` / `--provider` / `--model` / `--base-url` | 覆盖连接配置 |
| `--resume` / `--session-dir` | 恢复 JSONL 会话，或指定新会话目录 |
| `--mcp` / `--config` | 指定 MCP 与配置文件路径 |
| `--untrust [路径]` | 撤销项目的 MCP 信任后退出 |
| `--reset-sandbox` | 清理孤儿副本、AppContainer 授权与 profile |
| `--log` | 把工具与错误事件追加写入 JSONL |

交互模式里：`/reload` 重载工具与扩展，`/命令` 与 `/skill:名字` 展开保存的提示词，`exit` / `Ctrl-D` 退出。

### 多模型切换（Web）

`~/.gradus/config.json`（或 `--config` 指向的文件）可以额外列一张候选表，Web 界面的模型选择器直接读它：

```json
{
  "models": [
    { "name": "Kimi · Chat", "service": "kimi-compat", "model": "kimi-k2.7-code" },
    { "name": "Kimi · Messages", "service": "kimi-anthropic", "model": "kimi-k2.7-code" },
    { "name": "Kimi · Responses", "service": "kimi-responses", "model": "kimi-k2.7-code" }
  ]
}
```

每项支持的连接字段与顶层相同（`service` / `provider` / `model` / `base_url` / `api_key` / `context_window` / `max_output_tokens`），`name` 是显示名；不写 `api_key` 就沿用当前 key。候选表只从文件读，环境变量仍只描述当前模型。

不必手改 JSON：Web 顶栏的齿轮打开设置面板，可以增删改候选模型（不回显 key）。最后一次选中的模型会写进顶层 `"active"`，下次启动自动切回去。设置面板的「外观」段还能在浅色 / 深色 / 跟随系统之间切换（存在浏览器 localStorage，不写配置文件）。

切换不新建会话：历史是协议无关的中间表示，由新 provider 自己翻译，所以同一个会话可以在 `openai_compat` / `anthropic` / `openai_responses` 之间来回切。厂商私有字段（如思考块的 `signature`）不会被转换，换家后可能被丢弃或原样透传。

## 沙箱

| 后端 | 何时使用 | 边界 |
| --- | --- | --- |
| `restricted` | Windows，`auto` 优先选它 | 受限令牌（合成 SID + logon SID + `Everyone`）：读跟随当前账户，写只落在副本与 scratch。**不阻断网络。** 免管理员、免安装。 |
| `appcontainer` | Windows，仅在 `restricted` 不可用时 | AppContainer 令牌、默认断网、Job Object 连带杀子进程。**已弃用**：保留可用、不再演进。 |
| `docker` | Docker 引擎可用 | 容器内执行，`--network none`，限制内存 / CPU / 进程数。**测试中**：作为跨平台备用保留，尚未在真实引擎上验证。 |
| `local` | 兜底 | 无；命令以当前用户权限直接执行 |

沙箱会话不会直接改你的 workspace：它先拿到一份私有副本——git 仓库根走 `git worktree`，其余走过滤后的整树复制——文件工具和命令都在这份副本上工作。关闭时写回新增与修改、执行删除；合并失败会保留副本并打印路径。强杀留下的副本会在下次启动时清理。

三条边界需要说清楚：

- **`restricted` 不阻断网络。** 写边界是真的，网络不是。命令必须离线时用 `docker`。
- **命令审批不会关掉。** 副本防的是误伤宿主，不等于任意命令都安全。
- **MCP server 进程不在沙箱里。** 它以宿主用户权限运行；stdio server 的缺省 `cwd` 会指到副本，让相对路径对齐，但 HTTP server 与绝对路径仍然直接落到宿主。

## Python API

离线跑通一次完整循环——不联网、不需要 key：

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
            {"kind": "text", "chunks": ["你好，", "我是 Gradus"]},
            {"kind": "finish", "reason": "stop"},
        ],
    }],
)

async def main() -> None:
    async for event in run_loop(provider, [UserMessage("你好")], LoopConfig(system="")):
        if isinstance(event, AgentDone):
            print(event.new_messages[-1].content[0].text)

asyncio.run(main())
```

接真实模型时用有状态的 `Agent` 门面：

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
    LoopConfig(system="你是一个助手"),
    api_key="sk-...",
)
try:
    async for event in agent.prompt("你好"):
        ...
finally:
    await agent.aclose()
```

`Agent.connect()` 建一个 `httpx.AsyncClient` 并在多次运行间复用，`aclose()` 负责关闭；想多个 Agent 共用连接池，把同一个 `client` 传给每个 `connect()`。

## 目录

```
Gradus/
├── gradus/
│   ├── ai/          # Provider、内部协议、流式事件、传输
│   ├── agent/       # 循环、hook、工具运行时、有状态 Agent
│   └── coding/      # 配置、会话、工具、权限、沙箱、MCP、CLI
├── pyproject.toml
└── uv.lock
```

## 文档

| 层 | README | 设计 | 计划 |
| --- | --- | --- | --- |
| `ai` | [README](gradus/ai/README.zh.md) | [设计](gradus/ai/docs/design.md) | [计划](gradus/ai/ai_plan.md) |
| `agent` | [README](gradus/agent/README.zh.md) | [设计](gradus/agent/docs/design.md) | [计划](gradus/agent/agent_plan.md) |
| `coding` | [README](gradus/coding/README.zh.md) | [设计](gradus/coding/docs/design.md) | [计划](gradus/coding/coding_plan.md) |

## 设计原则

1. **单向依赖**：上层可以调用下层，下层对上层完全不可见。`ai` 不 import `agent` / `coding`，`agent` 不 import `coding`。
2. **事件就是接口**：`ai` 的 12 个事件被 `agent` 原样透传，`agent` 只补充运行级事件。
3. **全链路异步**：`Provider.stream()`、`run_loop()`、`AgentTool.execute()` 都是异步的，循环内不做阻塞 I/O。
4. **工具由上层实现**：`agent` 只定义 `AgentTool` 协议，文件、搜索、命令的实现都在 `coding`。

## 致谢

Gradus 的分层方式、事件模型与工具契约参考了以下开源项目：

- **Pi**（`@earendil-works/pi-agent-core`）：agent loop、hook 体系、steering / follow-up 与工具协议的参考。
- **OpenCode**：API 与工具体系设计的参考。

## 许可

[MIT](LICENSE)
