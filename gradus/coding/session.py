"""会话装配与历史落盘。

负责：
- 建进程级 `httpx.AsyncClient`，所有会话共用；
- 用 `Agent.connect(..., client=pool)` 建每会话一个 `Agent`；
- 装配 `LoopConfig.system`（基础提示 + 环境信息）；
- 暴露 `prompt` / `abort` / `aclose`；
- 在轮次开始前与轮次收尾各检查一次上下文预算，超限时摘要旧消息并只装一个压缩状态
  （不替换历史、不重建 `Agent`）；
- 把历史写成 JSONL（线性链，格式预留树）。

不负责：工具实现、权限策略。

历史格式（对齐 Pi 的会话树）：
- 第一行是 `{"type": "session", "version": 1, ...}` 头；
- 之后每行 `{"type": "message", "id": "e1", "parentId": null, "message": ...}`；
- 压缩时追加一行 `{"type": "compaction", "summary", "firstKeptEntryId"}`；
- 当前每条新 entry 的 `parentId` 是上一条，叶子即最后一条；
- 恢复时从最后一条沿 `parentId` 回溯到根，再反转成消息序列；
  若链上有压缩 entry，只重放最后一次压缩的摘要与 `firstKeptEntryId` 之后的原文。

约束：
- 连接池按「谁创建谁关闭」由本模块持有，`Session.aclose()` 只关 Agent；
- 进程退出前由调用方调 `close_pool()`；
- 连接池只在单事件循环内使用，不做加锁。
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
from collections import deque
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import httpx

from ..agent.agent import Agent, AgentState
from ..agent.base import AgentTool, LoopConfig
from ..agent.hooks import AgentHooks, NextTurnUpdate
from ..agent.types import AgentDone, AgentError, AgentEvent, ToolResult, TurnEnd
from ..ai.base import expand_headers
from ..ai.models import fetch_limits
from ..ai.transport import DEFAULT_HEADERS
from ..ai.types import (    AssistantMessage,
    Message,
    StreamDone,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from . import appcontainer
from . import extensions
from . import restricted
from . import workspace
from .background import BackgroundTasks
from .config import Settings, with_choice
from .execenv import ExecEnvironment
from .sandbox import (
    DockerEnvironment,
    Environment,
    LocalEnvironment,
    cleanup_orphans,
    cleanup_temp,
    docker_problem,
)
from .context import (
    COMPACT_RETRY_SKIPS,
    CompactionError,
    CompactionState,
    ContextBudget,
    SummaryLayer,
    UsageAnchor,
    active_turn_start,
    compact,
    context_tokens,
    estimate_messages_tokens,
    estimate_text_tokens,
    keep_recent_work_products,
    make_summarizer,
    make_text_summarizer,
    merge_summaries,
    plan_trim,
    select_cut,
    MAX_SUMMARY_LAYERS,
    MAX_SUMMARY_TOKENS,
    project_view,
    should_compact,
    summary_message,
)
from .mcp import (
    McpServer,
    McpSession,
    connect as connect_mcp,
    default_config_path,
    default_user_config_path,
    is_trusted,
    load_servers,
    merge_servers,
    remember_trust,
)
from .permissions import (
    Confirm,
    Policy,
    make_after_tool_call,
    make_before_tool_call,
)
from .registry import ToolCatalog, ToolError
from .retry import RetryPolicy, is_retryable
from .multiagent import AgentTeam
from .schedule import Schedule, ScheduledJob
from .tasks import Task, TaskGraph
from .tools.background import create_check_background_tool
from .tools.codemode import create_codemode_tool
from .tools.files import FileTracker, create_file_tools
from .tools.mcp_resources import create_mcp_resource_tools
from .tools.multiagent import create_multiagent_tools
from .selector import select_ids
from .history import RecallError, read_ids, search, search_with_selector
from .tools.recall_history import RecallHooks, RecallHistoryTool
from .tools.skill import UseSkillTool
from .tools.schedule import create_manage_schedules_tool, create_schedule_task_tool
from .tools.search import create_search_tools
from .tools.shell import create_shell_tools
from .tools.tasks import create_task_tools
from .tools.tool_search import ToolSearchTool

#: 编排类里要常驻的两个：建任务与等结果。其余编排工具（任务图读写、子 agent、
#: 定时任务）默认进 `deferred`，由 `tool_search` 按需载入——普通改代码的会话
#: 一次都用不上，却要每个请求为它们的 schema 付钱。
RESIDENT_ORCHESTRATION = frozenset({"create_task", "wait"})
BASE_PROMPT = (
    "You are Gradus, a coding assistant.\n"
    "Be concise. Use tools when needed. Never invent file contents or command output.\n"
    "\n"
    "Tool use:\n"
    "- Locate before reading: grep/glob to narrow down, then read_file with offset/limit. "
    "Never read a large file whole.\n"
    "- write_file replaces the whole file; edit does exact replacements (each must match once). "
    "Read a file before editing it.\n"
    "- grep/glob return paths relative to the working directory; paths outside it are refused.\n"
    "- Shell output can be huge: pipe through head/tail/grep instead of dumping it, and do not "
    "use the shell to read whole files.\n"
    "- codemode runs a Python script that can call tools.* - use it when a task needs several "
    "tool calls in a row; it costs less context than calling them one by one.\n"
    "- recall_history fetches history that was compacted away: pass entry ids or a keyword query.\n"
    "- Task graph, sub-agent and schedule tools are NOT declared by default: call tool_search "
    "with what you need (e.g. \"spawn agent\", \"list tasks\"). A search that matches nothing "
    "returns the full index, and tool_search(load=[\"name\"]) loads one by exact name.\n"
    "- Read-only calls may run in parallel; writes and anything depending on an earlier result "
    "run in order.\n"
    "- Verify changes (run the tests or the command) before saying a task is done.\n"
)

SESSION_VERSION = 1

_pool: httpx.AsyncClient | None = None


def get_pool() -> httpx.AsyncClient:
    """取进程级连接池，第一次调用时创建。"""
    global _pool
    if _pool is None:
        _pool = httpx.AsyncClient(headers=DEFAULT_HEADERS)
    return _pool


async def close_pool() -> None:
    """关闭并丢弃进程级连接池；幂等。"""
    global _pool
    pool, _pool = _pool, None
    if pool is not None:
        await pool.aclose()


def build_system(cwd: str | Path | None = None) -> str:
    """装配系统提示词：基础提示 + 当前环境。

    `cwd` 缺省用进程的当前目录；沙箱后端传入副本路径，模型看到的才是它
    真正执行命令的地方。
    """
    return (
        f"{BASE_PROMPT}\n\n"
        "Environment:\n"
        f"- cwd: {cwd or os.getcwd()}\n"
        f"- os: {platform.system()}"
    )


class SessionError(ValueError):
    """会话文件无法读取或恢复。"""


@dataclass(frozen=True)
class SessionMeta:
    id: str
    model: str
    workspace: str
    created_at: str


def make_prepare_next_turn(search: ToolSearchTool):
    """把 `tool_search` 登记的工具加进下一轮请求；只在本次运行内有效。"""

    async def prepare(context: Any) -> NextTurnUpdate | None:
        pending = search.take_pending()
        if not pending:
            return None

        known = {tool.spec.name for tool in context.config.tools}
        extra = tuple(tool for tool in pending if tool.spec.name not in known)
        if not extra:
            return None

        return NextTurnUpdate(
            config=replace(context.config, tools=(*context.config.tools, *extra))
        )

    return prepare


def make_budget_reminder(limit: int):
    """轮数预算提醒：用到 75% 和 90% 时各注入一条用户消息。

    模型原本不知道还剩几轮，往往把预算耗在探索上。这两条提醒不改变历史前缀
    （只是追加），所以不会破坏 prompt cache。
    """

    sent: set[int] = set()

    async def prepare(context: Any) -> NextTurnUpdate | None:
        if limit < 8:
            return None
        turn = context.turn + 1
        for threshold in (0.75, 0.90):
            mark = max(1, int(limit * threshold))
            if threshold in sent or turn < mark:
                continue
            sent.add(threshold)
            left = limit - turn
            return NextTurnUpdate(
                messages=(
                    UserMessage(
                        f"预算提醒：已用 {turn}/{limit} 轮模型请求，还剩 {left} 轮。"
                        "请现在就把代码改完；留下 1-2 轮跑测试验证，不要再做无关探索。"
                    ),
                )
            )
        return None

    return prepare


def _compose(*hooks: Any) -> Any:
    """把多个 prepare_next_turn 合成一个：消息按顺序拼接，配置取第一个给出的。"""

    async def prepare(context: Any) -> NextTurnUpdate | None:
        messages: list[Any] = []
        config = None
        for hook in hooks:
            if hook is None:
                continue
            result = await hook(context)
            if result is None:
                continue
            messages.extend(result.messages or ())
            if config is None and result.config is not None:
                config = result.config
        if not messages and config is None:
            return None
        return NextTurnUpdate(messages=tuple(messages), config=config)

    return prepare


def _deferred_section(servers: tuple[tuple[str, str], ...]) -> str:
    """系统提示里的按需加载说明；`servers` 只是可选的来源清单。"""

    lines = [
        "Some tools are available but not declared.",
        "Call tool_search with a short query to load the tools you need.",
    ]
    if servers:
        lines.append("Groups:")
        for name, description in servers:
            lines.append(f"- {name}: {description}" if description else f"- {name}")
    return "\n".join(lines)


def _mcp_servers_in(
    root: Path, servers: Sequence[McpServer]
) -> tuple[McpServer, ...]:
    """把 stdio server 的缺省 cwd 指到 `root`（沙箱会话里是副本）。

    MCP server 由 SDK 在宿主进程里启动，看不到副本；不显式给 cwd 时把当前
    目录换成副本，让它的文件改动和其它工具一样在会话关闭时合回宿主。
    配置里写了 cwd 的保持原样——可能是用户特意指向宿主目录。
    """
    return tuple(
        replace(server, cwd=str(root))
        if server.command is not None and server.cwd is None
        else server
        for server in servers
    )


def _build_tools(
    root: Path,
    tracker: FileTracker,
    mcp_session: McpSession | None,
    policy: Policy,
    confirm: Confirm | None,
    ext_tools: Sequence[AgentTool] = (),
    env: Environment | None = None,
    background: BackgroundTasks | None = None,
    schedule: Schedule | None = None,
    tasks: TaskGraph | None = None,
    team: Callable[[], AgentTeam | None] | None = None,
    recall: RecallHistoryTool | None = None,
    skills: Sequence[extensions.Skill] = (),
    skill_usage: dict[str, int] | None = None,
    output_chars: int | None = None,
) -> tuple[
    tuple[AgentTool, ...], ToolSearchTool | None, tuple[tuple[str, str], ...], tuple[str, ...]
]:
    """装配工具集与可选的 `tool_search`。

    返回直接声明的工具、检索工具（没有 deferred 时为 `None`）、deferred server 清单
    与扩展工具的冲突告警。`env=None` 表示执行环境不可用：命令与 `codemode` 都不注册。
    `background` 为后台任务表；给出时才注册 `check_background` 与 `run_in_background`。
    `schedule` 为定时任务表；给出时才注册 `schedule_task` 与 `manage_schedules`。
    `tasks` 为任务图；给出时才注册六个任务工具。
    `team` 是团队访问器（团队在会话之后创建，所以用间接引用）；给出时才注册多 agent 工具。
    `output_chars` 是命令输出的落盘上限，`None` 用工具默认值。
    """
    catalog = ToolCatalog()
    file_guard = None
    if team is not None:
        resolved_root = Path(root).resolve()

        def file_guard(path: Path) -> str | None:
            current = team()
            if current is None:
                return None
            try:
                relative = Path(path).resolve().relative_to(resolved_root)
            except ValueError:
                return None
            return current.write_blocked(relative)

    catalog.add_all(create_file_tools(root, tracker, file_guard))
    catalog.add_all(create_search_tools(root))
    if env is not None:
        catalog.add_all(create_shell_tools(root, env, background, output_chars))
    if background is not None:
        catalog.add(create_check_background_tool(background))
    if schedule is not None:
        # 编排类默认按需加载：普通改代码的会话一次都用不上，却要每请求付 schema 钱。
        catalog.add(create_schedule_task_tool(schedule), "deferred")
        catalog.add(create_manage_schedules_tool(schedule), "deferred")
    if tasks is not None:
        catalog.add_all(create_task_tools(tasks), "deferred")
    if tasks is not None and team is not None:
        catalog.add_all(create_multiagent_tools(team), "deferred")
    if recall is not None:
        catalog.add(recall)
    if skills:
        catalog.add(
            UseSkillTool(lambda: skills, skill_usage if skill_usage is not None else {})
        )

    # 两步编排要常驻：建任务是「这件事要拆开做」的第一个动作，等结果（`wait`）
    # 是拆完之后的必然动作。其余编排工具由 `tool_search` 按需载入。
    for name in RESIDENT_ORCHESTRATION:
        for tool in catalog.all:
            if tool.spec.name == name:
                catalog.promote(tool)
                break

    warnings: list[str] = []
    for tool in ext_tools:
        try:
            catalog.add(tool)
        except ToolError as exc:
            warnings.append(f"extension tool {tool.spec.name}: {exc}")

    deferred_servers: tuple[tuple[str, str], ...] = ()
    if mcp_session is not None:
        if mcp_session.resource_handles:
            catalog.add_all(create_mcp_resource_tools(mcp_session.resource_handles))
        catalog.add_all(mcp_session.tools)
        catalog.add_all(mcp_session.deferred, "deferred")
        deferred_servers = mcp_session.deferred_servers

    if env is not None and env.name != "exec":
        # exec 模式：codemode 的 harness 脚本放在宿主 scratch 里，容器看不见
        # （map_path 会报 "outside the exec workspace"），先不注册，避免白耗轮次。
        catalog.add(
            create_codemode_tool(catalog.direct, policy, confirm, env, output_chars)
        )

    search_tool = ToolSearchTool(catalog.deferred) if catalog.deferred else None
    if search_tool is not None:
        catalog.add(search_tool)
    return catalog.direct, search_tool, deferred_servers, tuple(warnings)


async def _pick_backend() -> str:
    """`auto` 的顺序：受限令牌（Windows 目标后端）-> AppContainer -> Docker -> local。"""
    if restricted.problem() is None:
        return "restricted"
    if appcontainer.problem() is None:
        return "appcontainer"
    if await docker_problem() is None:
        return "docker"
    return "local"


async def _build_environment(
    settings: Settings,
    root: Path,
) -> tuple[
    Environment | None,
    str | None,
    tuple[str, ...],
    workspace.WorkspaceCopy | None,
]:
    """按配置建执行环境；沙箱不可用时不回落 local。

    返回环境、告警、被清理的残留（强杀后留下的孤儿容器 / 未撤销的授权），
    以及沙箱自有的 workspace 副本——`local` 与兜底时为 `None`，文件工具
    与命令都绑在副本上，会话关闭时再合回宿主。
    """
    backend = settings.sandbox
    if backend == "auto":
        backend = await _pick_backend()

    if backend == "local":
        if settings.sandbox == "auto":
            return (
                LocalEnvironment(root),
                "sandbox auto: no backend available; commands run locally",
                (),
                None,
            )
        return LocalEnvironment(root), None, (), None

    if backend == "exec":
        # attach 到外部给的容器：不建副本（容器本身一次性），文件工具直接读写宿主 workspace
        container = settings.sandbox_container.strip()
        if not container:
            return (
                None,
                "sandbox exec: no container configured; bash / powershell / codemode "
                "are disabled",
                (),
                None,
            )
        return (
            ExecEnvironment(
                container,
                workspace=root,
                container_workspace=settings.sandbox_container_workspace,
                python=settings.sandbox_container_python,
            ),
            None,
            (),
            None,
        )

    if backend in ("restricted", "appcontainer"):
        problem = (
            restricted.problem()
            if backend == "restricted"
            else appcontainer.problem()
        )
        if problem is not None:
            return (
                None,
                f"sandbox {backend} unavailable: {problem}; "
                "bash / powershell / codemode are disabled",
                (),
                None,
            )
    else:
        problem = await docker_problem()
        if problem is not None:
            return (
                None,
                f"sandbox docker unavailable: {problem}; "
                "bash / powershell / codemode are disabled",
                (),
                None,
            )

    notes: list[str] = []
    copy: workspace.WorkspaceCopy | None = None
    # 顺手回收临时目录里自己的残留（MCP 落盘结果、强杀留下的 scratch）。静默执行：
    # 失败不影响会话，也不并进 sandbox_orphans，避免每次启动都刷屏。
    await asyncio.to_thread(cleanup_temp)
    removed_workspaces = await workspace.cleanup_orphans()
    try:
        copy = await workspace.create(root)
    except OSError as exc:
        notes.append(f"sandbox workspace unavailable: {exc}; using the real workspace")

    tool_root = copy.path if copy is not None else root

    if backend == "restricted":
        removed = ()
        env: Environment = restricted.RestrictedEnvironment(tool_root)
    elif backend == "appcontainer":
        if settings.sandbox_network:
            notes.append(
                "sandbox appcontainer has no network; sandbox_network is ignored"
            )
        removed = await appcontainer.cleanup_orphans(tool_root)
        env: Environment = appcontainer.AppContainerEnvironment(tool_root)
    else:
        removed = tuple(await cleanup_orphans())
        env = DockerEnvironment(
            tool_root,
            image=settings.sandbox_image,
            network=settings.sandbox_network,
        )

    return (
        env,
        "; ".join(notes) if notes else None,
        (*removed_workspaces, *removed),
        copy,
    )


#: 项目规则文件名；每层目录按顺序取第一个存在的。
CONTEXT_FILENAMES = ("AGENTS.override.md", "AGENTS.md", "AGENTS.MD", "CLAUDE.md", "CLAUDE.MD")


def read_project_context(root: str | Path) -> tuple[tuple[Path, str], ...]:
    """从 workspace 向上逐层找规则文件，按「根 -> workspace」返回。

    每层只用第一个存在的文件，近的（workspace）排在后面，覆盖远的约定。
    读不了的文件直接跳过；不做大小限制，内容照原样进系统提示。
    """
    files: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    current = Path(root).expanduser().resolve()

    while True:
        found = _context_file_in(current)
        if found is not None and found[0] not in seen:
            files.append(found)
            seen.add(found[0])
        parent = current.parent
        if parent == current:
            break
        current = parent

    files.reverse()
    return tuple(files)


def _context_file_in(directory: Path) -> tuple[Path, str] | None:
    """返回目录里第一个可读的规则文件。"""
    for name in CONTEXT_FILENAMES:
        path = directory / name
        if not path.is_file():
            continue
        try:
            return path, path.read_text(encoding="utf-8-sig")
        except OSError:
            continue
    return None


def _context_section(files: Sequence[tuple[Path, str]]) -> str:
    """渲染 `<project_context>` 段；没有文件时返回空串。"""
    if not files:
        return ""

    body = "\n\n".join(
        f'<project_instructions path="{path}">\n{text.strip()}\n</project_instructions>'
        for path, text in files
    )
    return (
        "<project_context>\n"
        "Project-specific instructions and guidelines:\n\n"
        f"{body}\n</project_context>"
    )


def _compose_system(
    base: str,
    search_tool: ToolSearchTool | None,
    deferred_servers: tuple[tuple[str, str], ...],
    skills: Sequence[extensions.Skill],
    context: Sequence[tuple[Path, str]] = (),
) -> str:
    """基础提示 + 项目规则 + 技能清单 + deferred 说明。"""
    parts = [base]
    context_text = _context_section(context)
    if context_text:
        parts.append(context_text)
    skills_text = extensions.skills_section(skills)
    if skills_text:
        parts.append(skills_text)
    if search_tool is not None and deferred_servers:
        parts.append(_deferred_section(deferred_servers))
    return "\n\n".join(parts)


async def probe_limits(settings: Settings) -> Settings:
    """向端点要一次模型能力上限，拿得到就写回 `spec`（`probe_limits` 打开时用）。

    只补 `window_limit` / `output_limit`，不直接改预算口径：窗口按能力给（模型能装
    1M 就按 1M 管），输出上限仍取保守值（`max_output_tokens` 会原样进请求）。
    探测失败、超时、端点不报，都原样返回。
    """
    spec = settings.spec
    limits = await fetch_limits(
        spec.base_url,
        spec.model,
        api_key=settings.api_key,
        client=get_pool(),
        headers=settings.headers,
    )
    if limits is None:
        return settings

    window_limit = limits.window or spec.window_limit
    output_limit = limits.output or spec.output_limit
    context_window = spec.context_window
    max_output = spec.max_output_tokens
    # 之前只能按默认值，现在知道真实能力了：窗口跟着放大；显式压低过的不动。
    if window_limit is not None and spec.window_limit is None:
        context_window = window_limit
    if window_limit is not None and context_window > window_limit:
        context_window = window_limit
    if output_limit is not None:
        max_output = min(max_output, output_limit)

    return replace(
        settings,
        spec=replace(
            spec,
            context_window=context_window,
            max_output_tokens=max_output,
            window_limit=window_limit,
            output_limit=output_limit,
        ),
    )

def _budget_for(settings: Settings) -> ContextBudget:
    """按模型参数与配置覆盖推导上下文预算；配不出可用组合时抛 `ContextBudgetError`。"""
    spec = settings.spec
    overrides: dict[str, int] = {}
    if settings.context_reserve_tokens is not None:
        overrides["reserve_tokens"] = settings.context_reserve_tokens
    if settings.context_keep_tokens is not None:
        overrides["keep_tokens"] = settings.context_keep_tokens
    return ContextBudget.for_model(
        spec.context_window,
        spec.max_output_tokens,
        keep_preset=settings.keep_preset,
        **overrides,
    )


async def _connect_agent(
    settings: Settings,
    system: str,
    policy: Policy,
    confirm: Confirm | None,
    tools: tuple[AgentTool, ...],
    tool_search: ToolSearchTool | None,
    messages: list[Message] | None,
    trim_log: SessionFile | None = None,
    request_slot: list[int] | None = None,
    compaction_slot: list[CompactionState | None] | None = None,
) -> Agent:
    """按当前配置装配一个 `Agent`；压缩后重建也走这里，保证装配口径一致。"""

    # 预算按模型算一次：窗口、输出上限都不同，散着算容易各处用不同的数。
    budget = _budget_for(settings)

    async def trim(transform: Any) -> list[Message]:
        # 先投影（摘要 + 保留段），再对可见视图做兜底裁剪。
        # 投影是唯一视图入口：运行中与恢复后走同一段逻辑。
        state = compaction_slot[0] if compaction_slot is not None else None
        result = plan_trim(
            project_view(transform.messages, state), budget.window, budget.reserve
        )
        # 静默丢弃会让「它为什么忘事」无从回答：丢掉就记一笔。
        if trim_log is not None and result.dropped:
            trim_log.record_trim(result.dropped, result.dropped_tokens)
        # 记下本次请求实际发出的消息条数：用量锚点要按「请求」对齐，而不是按已提交的历史。
        # 注：发生硬裁时这里记的是裁剪后的条数，与完整历史的下标并不严格对齐；
        # 裁剪本身是异常状态，且下一轮会重新锚定，因此接受这个近似。
        if request_slot is not None:
            request_slot[0] = len(result.kept)
        return result.kept

    hooks = AgentHooks(
        transform_messages=trim,
        before_tool_call=make_before_tool_call(policy, confirm),
        after_tool_call=make_after_tool_call(),
    )
    hooks = replace(
        hooks,
        prepare_next_turn=_compose(
            make_prepare_next_turn(tool_search) if tool_search is not None else None,
            make_budget_reminder(settings.max_turns),
        ),
    )

    return await Agent.connect(
        settings.spec,
        LoopConfig(system=system, tools=tools, hooks=hooks, max_turns=settings.max_turns),
        api_key=settings.api_key,
        client=get_pool(),
        headers=settings.headers,
        messages=messages,
    )


class SessionFile:
    """一个 JSONL 会话文件：头 + 线性链 entry。"""

    def __init__(
        self,
        path: Path,
        meta: SessionMeta,
        messages: list[Message],
        entry_ids: list[str | None],
        last_id: str | None,
        next_index: int,
    ) -> None:
        self._path = path
        self._meta = meta
        self._messages = messages
        self._entry_ids = entry_ids
        self._last_id = last_id
        self._next_index = next_index
        #: 已记录的「请求前硬裁」条数，用来给反复出现的同一次裁剪去重。
        self._trim_marked = 0
        #: 已安装的压缩状态；历史本身不因压缩而改写。
        self._compaction: CompactionState | None = None
        #: 文件里出现过的所有条目 id（含被放弃分支），用于校验叶子切换目标。
        self._known_ids: set[str] = set()

    @classmethod
    def create(
        cls,
        directory: str | Path,
        settings: Settings,
        workspace: str | Path,
        *,
        name: str | None = None,
    ) -> SessionFile:
        """在新目录里建一个空会话文件；`name` 给出时用它当文件名。"""
        folder = Path(directory).expanduser().resolve()
        folder.mkdir(parents=True, exist_ok=True)

        now = datetime.now(timezone.utc)
        session_id = f"{now.strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:6]}"
        meta = SessionMeta(
            id=session_id,
            model=settings.spec.model,
            workspace=str(Path(workspace).expanduser().resolve()),
            created_at=now.isoformat(),
        )
        path = folder / f"{name or session_id}.jsonl"
        # 同名子 agent 会跨团队运行复用名字；不覆盖上一份对话。
        suffix = 2
        while name and path.exists():
            path = folder / f"{name}-{suffix}.jsonl"
            suffix += 1
        header = {
            "type": "session",
            "version": SESSION_VERSION,
            "id": meta.id,
            "model": meta.model,
            "workspace": meta.workspace,
            "created_at": meta.created_at,
        }
        with path.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(header, ensure_ascii=False) + "\n")

        return cls(path, meta, [], [], None, 0)

    @classmethod
    def load(cls, path: str | Path) -> SessionFile:
        """读一个会话文件；传目录时取其中最新的 `.jsonl`。"""
        resolved = Path(path).expanduser().resolve()
        if resolved.is_dir():
            candidates = sorted(resolved.glob("*.jsonl"))
            if not candidates:
                raise SessionError(f"no session files in {resolved}")
            resolved = candidates[-1]

        try:
            lines = resolved.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise SessionError(f"cannot read {resolved}: {exc}") from exc

        header: dict[str, Any] | None = None
        entries: list[dict[str, Any]] = []

        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SessionError(f"{resolved}:{number} is not JSON: {exc}") from exc

            kind = payload.get("type")
            if kind == "session":
                if header is not None:
                    raise SessionError(f"{resolved}: duplicate session header")
                header = payload
            elif kind == "usage":
                # 用量账本：不进消息树、不影响投影，读回来只要不报错即可（`total_usage` 直接读文件）。
                continue
            elif kind in {"message", "compaction", "merge", "trim", "leaf"}:
                entries.append(payload)
            else:
                raise SessionError(f"{resolved}:{number} has unknown type {kind!r}")

        if header is None:
            raise SessionError(f"{resolved}: missing session header")
        if header.get("version") != SESSION_VERSION:
            raise SessionError(
                f"{resolved}: unsupported version {header.get('version')!r}"
            )

        ordered = _ordered_entries(resolved, entries)
        message_entries = [entry for entry in ordered if entry["type"] == "message"]
        messages = [_message_from_dict(entry["message"]) for entry in message_entries]
        entry_ids: list[str | None] = [entry["id"] for entry in message_entries]

        # 历史保持完整：压缩只记录「从第几条开始可见」，投影交给 `project_view`。
        # 这样运行中与恢复后走同一段逻辑，不会各算一套。
        layers = _rebuild_layers(ordered)
        state: CompactionState | None = None
        compactions = [entry for entry in ordered if entry["type"] == "compaction"]
        if compactions:
            latest = compactions[-1]
            state = CompactionState(
                layers=layers,
                first_kept=_kept_index(resolved, latest, entry_ids),
                version=len(compactions),
            )

        if state is not None:
            _validate_pairs(resolved, messages[state.first_kept :])
        else:
            _validate_pairs(resolved, messages)

        meta = SessionMeta(
            id=str(header.get("id", "")),
            model=str(header.get("model", "")),
            workspace=str(header.get("workspace", "")),
            created_at=str(header.get("created_at", "")),
        )
        last_id = ordered[-1]["id"] if ordered else None
        loaded = cls(resolved, meta, messages, entry_ids, last_id, len(entries))
        loaded._compaction = state
        # 记下文件里出现过的所有 id（含当前分支之外的），切分支时要校验目标。
        loaded._known_ids = {str(entry["id"]) for entry in entries}
        return loaded

    @property
    def path(self) -> Path:
        return self._path

    @property
    def meta(self) -> SessionMeta:
        return self._meta

    @property
    def messages(self) -> list[Message]:
        return list(self._messages)

    @property
    def entry_ids(self) -> tuple[str | None, ...]:
        """每条消息对应的条目 id；压缩不改变它。"""
        return tuple(self._entry_ids)

    @property
    def leaf(self) -> str | None:
        """当前活跃叶子（最后一条消息的 entry id）；空会话为 `None`。"""
        return self._entry_ids[-1] if self._entry_ids else None

    def record_leaf(self, entry_id: str) -> bool:
        """把活跃叶子切到 `entry_id`，追加一条 `leaf` 事件。

        目标可以在**被放弃的分支**上——文件里那条链还在，切回去只是再追加一行。
        返回目标是否位于当前分支：`False` 表示内存里的历史不在这条链上，
        调用方需要重新加载会话才能看到那条分支（`Session.switch_branch` 会做）。
        """
        if entry_id not in self._known_ids and entry_id not in self._entry_ids:
            raise SessionError(
                f"{self._path}: unknown entry {entry_id!r}"
            )
        on_branch = entry_id in self._entry_ids

        self._next_index += 1
        leaf_id = f"e{self._next_index}"
        payload = {
            "type": "leaf",
            "id": leaf_id,
            "parentId": entry_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "leaf": entry_id,
        }
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._known_ids.add(leaf_id)

        # 后续消息挂到这条事件后面，链路仍然从目标叶子出发。
        self._last_id = leaf_id

        if on_branch:
            # 内存里同步截掉被放弃的尾巴，视图立刻体现回退。
            kept = self._entry_ids.index(entry_id) + 1
            del self._messages[kept:]
            del self._entry_ids[kept:]
            # 压缩状态与回退后的历史对不上时就撤掉，避免投影越界。
            if (
                self._compaction is not None
                and self._compaction.first_kept > len(self._messages)
            ):
                self._compaction = None
            self._trim_marked = 0
        return on_branch

    def append(self, messages: list[Message]) -> None:
        """把一段消息追加成链；整段一次写入，避免半行。"""
        if not messages:
            return

        lines: list[str] = []
        entry_ids: list[str] = []
        for message in messages:
            self._next_index += 1
            entry_id = f"e{self._next_index}"
            entry_ids.append(entry_id)
            lines.append(
                json.dumps(
                    {
                        "type": "message",
                        "id": entry_id,
                        "parentId": self._last_id,
                        "message": _message_to_dict(message),
                    },
                    ensure_ascii=False,
                )
            )
            self._last_id = entry_id

        with self._path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")

        self._messages.extend(messages)
        self._entry_ids.extend(entry_ids)
        self._known_ids.update(entry_ids)

    def record_trim(self, dropped: int, tokens: int) -> None:
        """记录一次「请求前硬裁」。

        `dropped` 是「从头丢掉了多少条」的累计值；同一个前缀会在每次请求里
        重复出现，所以按累计值去重——只有丢得更多时才写一条新记录。
        """
        if dropped <= self._trim_marked:
            return
        self._trim_marked = dropped
        self._next_index += 1
        entry_id = f"e{self._next_index}"
        payload = {
            "type": "trim",
            "id": entry_id,
            "parentId": self._last_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "dropped": dropped,
            "tokens": tokens,
        }
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

        self._last_id = entry_id

    def record_usage(self, usage: object, *, model: str = "") -> None:
        """记一轮的真实用量。

        只追加、不进消息树：它是「这次请求花了多少」的账本，不是对话内容。
        `cache_read` / `cache_write` 一起记，否则事后说不清缓存到底有没有吃到。
        """
        self._next_index += 1
        payload = {
            "type": "usage",
            "id": f"e{self._next_index}",
            "parentId": self._last_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "model": model,
            "input": getattr(usage, "input_tokens", 0),
            "output": getattr(usage, "output_tokens", 0),
            "cacheRead": getattr(usage, "cache_read_tokens", 0),
            "cacheWrite": getattr(usage, "cache_write_tokens", 0),
        }
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def total_usage(self) -> dict[str, int]:
        """把文件里所有 `usage` 记录加起来。坏行跳过，不让审计拖垮会话。"""
        totals = {"turns": 0, "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return totals
        for line in lines:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict) or payload.get("type") != "usage":
                continue
            totals["turns"] += 1
            for key in ("input", "output", "cacheRead", "cacheWrite"):
                value = payload.get(key)
                if isinstance(value, int):
                    totals[key] += value
        return totals

    def record_compaction(self, summary: str, first_kept: int) -> None:
        """追加一条压缩记录，并安装新的压缩状态。

        历史本身**一行不改**：`_messages` 保持完整，模型看到的视图由
        `project_view` 派生。这是「记录」与「视图」分开的关键一步。
        """
        if not 0 <= first_kept <= len(self._entry_ids):
            raise SessionError(f"{self._path}: no entry to keep at {first_kept}")
        if first_kept < len(self._entry_ids):
            first_kept_id = self._entry_ids[first_kept]
            if first_kept_id is None:
                raise SessionError(f"{self._path}: entry {first_kept} has no id")
        else:
            # 一条都不保留：仍要写一个合法的 parentId 指向当前叶子。
            first_kept_id = None

        self._next_index += 1
        entry_id = f"e{self._next_index}"
        payload = {
            "type": "compaction",
            "id": entry_id,
            "parentId": self._last_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "summary": summary,
            "firstKeptEntryId": first_kept_id,
        }
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

        self._last_id = entry_id
        existing = self._compaction.layers if self._compaction is not None else ()
        layers = (*existing, SummaryLayer(key=entry_id, text=summary))
        self._compaction = CompactionState(
            layers=layers,
            first_kept=first_kept,
            version=(self._compaction.version if self._compaction else 0) + 1,
        )
        # 压缩后历史不再被裁，之前记过的硬裁与新的求值基准无关了。
        self._trim_marked = 0

    def record_merge(self, text: str, supersedes: Sequence[str]) -> None:
        """追加一条合并记录：把它覆盖的那几份摘要换成一份。

        只追加、不改旧记录——旧摘要仍能被审计，只是不再参与视图。
        """
        self._next_index += 1
        entry_id = f"e{self._next_index}"
        payload = {
            "type": "merge",
            "id": entry_id,
            "parentId": self._last_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "text": text,
            "supersedes": list(supersedes),
        }
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

        self._last_id = entry_id
        if self._compaction is not None:
            dropped = set(supersedes)
            kept = [
                layer
                for layer in self._compaction.layers
                if layer.key not in dropped
            ]
            self._compaction = CompactionState(
                layers=(SummaryLayer(key=entry_id, text=text), *kept),
                first_kept=self._compaction.first_kept,
                version=self._compaction.version + 1,
            )

    @property
    def compaction(self) -> CompactionState | None:
        """已安装的压缩状态；没有压过就是 `None`。"""
        return self._compaction


class Session:
    """一次对话：一个 `Agent` 加一段历史，共用进程级连接池。"""

    def __init__(
        self,
        agent: Agent,
        settings: Settings,
        system: str,
        workspace: Path,
        policy: Policy,
        tracker: FileTracker,
        confirm: Confirm | None,
        tools: tuple[AgentTool, ...],
        tool_search: ToolSearchTool | None,
        mcp_session: McpSession | None,
        mcp_warnings: tuple[str, ...],
        log: SessionFile | None,
        base_system: str | None = None,
        loaded: extensions.Extensions | None = None,
        extension_tools: tuple[AgentTool, ...] = (),
        extension_warnings: tuple[str, ...] = (),
        trusted: bool = False,
        context_files: tuple[Path, ...] = (),
        context_enabled: bool = True,
        sandbox_warning: str | None = None,
        env: Environment | None = None,
        sandbox_orphans: tuple[str, ...] = (),
        workspace_copy: workspace.WorkspaceCopy | None = None,
        background: BackgroundTasks | None = None,
        schedule: Schedule | None = None,
        tasks: TaskGraph | None = None,
        session_dir: Path | None = None,
        owns_env: bool = True,
        team_cell: list[AgentTeam | None] | None = None,
        request_slot: list[int] | None = None,
        compaction_slot: list[CompactionState | None] | None = None,
        recall_tool: RecallHistoryTool | None = None,
        skill_usage: dict[str, int] | None = None,
    ) -> None:
        self._agent = agent
        self._settings = settings
        self._system = system
        self._base_system = system if base_system is None else base_system
        self._workspace = workspace
        self._policy = policy
        self._tracker = tracker
        self._confirm = confirm
        self._tools = tools
        self._tool_search = tool_search
        self._mcp = mcp_session
        self._mcp_warnings = mcp_warnings
        self._log = log
        self._extensions = loaded if loaded is not None else extensions.Extensions()
        self._extension_tools = extension_tools
        self._extension_warnings = extension_warnings
        self._trusted = trusted
        self._context_files = context_files
        self._context_enabled = context_enabled
        self._sandbox_warning = sandbox_warning
        self._env = env
        self._sandbox_orphans = sandbox_orphans
        self._workspace_copy = workspace_copy
        self._tool_root = workspace_copy.path if workspace_copy is not None else workspace
        self._workspace_sync: workspace.WorkspaceSync | None = None
        self._closed = False
        #: 上一次真实请求的用量锚点：`(输入 token, 该请求包含的消息条数)`。
        #: 压缩或裁剪之后清空，等下一次请求重新给出真实值。
        self._anchor: UsageAnchor | None = None
        #: 最近一次请求的消息条数，由 `transform_messages` 钩子写入。
        #: 用它而不是 `agent.messages` 的条数：一轮里可能有多次请求，而本轮内容
        #: 要等 `TurnEnd` 才提交，直接数会把整轮重复计一次。
        self._request_slot: list[int] = request_slot if request_slot is not None else [0]
        #: 已安装的压缩状态。历史保持完整，模型看到的是 `project_view` 的结果。
        #: 用「槽位」而不是字段，是为了让构造前就装配好的钩子也能读到最新值。
        self._compaction_slot: list[CompactionState | None] = (
            compaction_slot if compaction_slot is not None else [None]
        )
        #: `recall` 工具本体与��话侧的检索账本（每代压缩预算内可回读多少）。
        self._recall_tool = recall_tool
        #: 技能使用计数：`use_skill` 工具与 `/skill:` 展开都记在这里。
        self._skill_usage: dict[str, int] = skill_usage if skill_usage is not None else {}
        self._recall_spent = 0
        self._recall_version = -1
        #: 压缩失败的退避：连续失败几次就跳过几轮再试，避免每轮重付一次摘要费。
        self._compact_failures = 0
        self._compact_skips = 0
        #: 「本轮太大、压缩切不动」只提示一次，别每轮刷屏。
        self._turn_too_big = False
        #: 最近一次摘要请求的用量（含 cache_read），用于判断摘要有没有吃到前缀缓存。
        self.last_summary_usage = None
        #: `recall` 的关键词检索缓存：`(条目 id, 正文长度)` → 分词结果。已压缩的
        #: 消息不会再变，所以不需要失效；区段很长时省掉重复分词。
        self._recall_tokens: dict[tuple[str, int], list[str]] = {}
        #: 最近一次「小窗口选择器」调用的用量；只有零命中兜底时才会产生。
        self._selector_usage: Usage | None = None
        self._background = background
        self._schedule = schedule
        self._tasks = tasks
        #: 子 agent 的会话放在它下面的 `agents/` 里，不进会话列表。
        self._session_dir = session_dir
        #: 子 agent 共用主 agent 的执行环境，关闭时不能把它一起收掉。
        self._owns_env = owns_env
        #: 多 agent 团队的间接引用，由宿主在团队创建后回填。
        self._team_cell = team_cell
        if team_cell is None:
            self._team_getter: Callable[[], AgentTeam | None] | None = None
        else:
            cell = team_cell
            self._team_getter = lambda: cell[0]
        #: 运行结束才完成的后台通知；下一次运行开始时补送，合并成一条。
        self._notices: deque[str] = deque(maxlen=20)
        if background is not None:
            background.bind(self._notify)

    @classmethod
    async def connect(
        cls,
        settings: Settings,
        *,
        system: str | None = None,
        workspace: str | Path | None = None,
        confirm: Confirm | None = None,
        session_dir: str | Path | None = None,
        resume: str | Path | None = None,
        mcp_config: str | Path | None = None,
        interactive: bool | None = None,
        multiagent: bool = True,
    ) -> Session:
        """用启动配置装配一个会话。

        `system=None` 时用 `build_system()`；`workspace=None` 时用当前 cwd，
        恢复会话时优先用文件头里的 workspace。
        `session_dir` 给出时新建会话文件；`resume` 给出时从文件恢复并把后续
        消息追加回同一个文件。两者都为 `None` 时历史只在内存。
        `mcp_config=None` 时读 `<workspace>/.gradus/mcp.json`；文件不存在表示不启用 MCP。
        `interactive=None` 时按是否有 `confirm` 回调推断；为 `False` 时 MCP 的 OAuth
        不会打开浏览器，需要登录的 server 直接记成告警。
        连接池由本模块持有，不随会话关闭。
        """
        # 显式开了 `probe_limits` 就向端点要一次能力上限（失败静默、退回静态表）。
        # 探测放在预算校验之前：模型换个更大的窗口时，预算要按新的数才算得对。
        if settings.probe_limits:
            settings = await probe_limits(settings)
        # `{run}` 在这里固化：**一次会话一个 id**，主对话与摘要、合并都带同一个。
        # 不能留给每个 Provider 自己去展开——摘要会新建 Provider，那样就是另一个 id，
        # 网关按 id 分流，摘要会被送到没缓存的机器上（实测 cache_read 从 18,560 掉到 0）。
        settings = replace(settings, headers=expand_headers(settings.headers))
        # 保留量必须小于触发线：否则压完仍在线上，每轮都要重复付费。
        # 这是配置错误，应该在启动时暴露，而不是跑起来才发现。
        _budget_for(settings)

        resume_log = SessionFile.load(resume) if resume is not None else None

        if workspace is not None:
            root = Path(workspace).expanduser().resolve()
        elif resume_log is not None:
            root = Path(resume_log.meta.workspace).expanduser().resolve()
        else:
            root = Path.cwd().resolve()

        env, sandbox_warning, sandbox_orphans, workspace_copy = (
            await _build_environment(settings, root)
        )
        tool_root = workspace_copy.path if workspace_copy is not None else root

        prompt = build_system(tool_root) if system is None else system
        context = read_project_context(root) if system is None else ()
        context_enabled = system is None
        policy = Policy(workspace=tool_root)
        tracker = FileTracker()

        if resume_log is not None:
            log = resume_log
        elif session_dir is not None:
            log = SessionFile.create(session_dir, settings, root)
        else:
            log = None

        warnings: list[str] = []
        ext_warnings: list[str] = []
        schedule = Schedule(root / ".gradus" / "schedules.json")
        ext_warnings.extend(schedule.warnings)
        tasks = TaskGraph(root / ".gradus" / "tasks.json")
        ext_warnings.extend(tasks.warnings)
        trusted = is_trusted(root)
        if mcp_config is not None:
            servers = load_servers(Path(mcp_config).expanduser())
        else:
            project_path = default_config_path(root)
            project = load_servers(project_path)

            # 项目配置能执行任意命令：默认路径必须显式信任。
            # 用户级配置是用户自己写的，不需要项目信任。
            if project and not trusted:
                if confirm is None:
                    warnings.append(f"skipped untrusted mcp config: {project_path}")
                    project = []
                elif not await confirm(f"允许执行 {project_path} 中配置的 MCP server 命令？"):
                    warnings.append(f"mcp config declined: {project_path}")
                    project = []
                else:
                    remember_trust(root)
                    trusted = True

            servers = merge_servers(load_servers(default_user_config_path()), project)

        # 项目级扩展会执行任意 Python，必须走同一份信任记录。
        ext_project = extensions.default_project_dir(root)
        use_extensions = ext_project.is_dir()
        if use_extensions and not trusted:
            if confirm is None:
                ext_warnings.append(f"skipped untrusted extensions: {ext_project}")
                use_extensions = False
            elif not await confirm(f"允许执行 {ext_project} 中的扩展？"):
                ext_warnings.append(f"extensions declined: {ext_project}")
                use_extensions = False
            else:
                remember_trust(root)
                trusted = True

        # 扩展从**宿主**工作区加载，不是从副本 `tool_root`：`.gradus/` 被
        # `workspace.COPY_SKIP` 刻意排除在副本外（会话、信任、状态文件不该被复制），
        # 副本里根本没有 `extensions/`。技能是只读配置，路径指向宿主是自洽的——
        # 别"顺手"改成 `tool_root`，那会让沙箱会话一个技能都加载不到。
        loaded = extensions.load(root, project_enabled=use_extensions)
        #: 技能使用计数：`use_skill` 与 `/skill:` 都记在这里（不落盘，见 docs）。
        skill_usage: dict[str, int] = {}
        ext_warnings.extend(loaded.warnings)

        if workspace_copy is not None:
            servers = _mcp_servers_in(tool_root, servers)

        mcp_session = (
            await connect_mcp(
                servers,
                interactive=(confirm is not None if interactive is None else interactive),
            )
            if servers
            else None
        )
        if mcp_session is not None:
            warnings.extend(mcp_session.errors)

        # 后台任务表与会话同寿命；日志落在宿主的 .gradus/background 下，
        # 沙箱副本里看不到，但 `check_background` 会给出尾部输出。
        background = (
            BackgroundTasks(env, root / ".gradus" / "background")
            if env is not None
            else None
        )

        # 团队在会话之后创建（它需要本会话的 `spawn` 与 `notify`），
        # 所以工具先拿一个间接引用，宿主建好团队后再回填。
        # `multiagent=False`（bench / 无头调用）不建槽位，也就不注册多 agent 工具：
        # 注册了却没人回填团队，模型只会看到一组“一调就报错”的工具。
        team_cell: list[AgentTeam | None] | None = [None] if multiagent else None
        # recall 工具同理：会话构造完之后才能提供历史与预算。
        recall_hooks = RecallHooks()
        recall_tool = RecallHistoryTool(recall_hooks)

        try:
            base_prompt = prompt
            tools, search_tool, deferred_servers, tool_warnings = _build_tools(
                tool_root,
                tracker,
                mcp_session,
                policy,
                confirm,
                loaded.tools,
                env,
                background,
                schedule,
                tasks,
                (lambda: team_cell[0]) if team_cell is not None else None,
                recall_tool,
                skills=loaded.skills,
                skill_usage=skill_usage,
                output_chars=settings.tool_output_chars,
            )
            ext_warnings.extend(tool_warnings)
            prompt = _compose_system(
                prompt, search_tool, deferred_servers, loaded.skills, context
            )

            # 请求条数由 transform 钩子写入；构造 Session 前先建好槽位，前后共用同一个。
            request_slot: list[int] = [0]
            # 恢复出来的压缩状态也放进槽位：装配钩子时就能读到，不必等 Session 构造完。
            compaction_slot: list[CompactionState | None] = [
                resume_log.compaction if resume_log is not None else None
            ]
            agent = await _connect_agent(
                settings,
                prompt,
                policy,
                confirm,
                tools,
                search_tool,
                resume_log.messages if resume_log is not None else None,
                trim_log=log,
                request_slot=request_slot,
                compaction_slot=compaction_slot,
            )
        except BaseException:
            if mcp_session is not None:
                await mcp_session.aclose()
            if workspace_copy is not None:
                await workspace_copy.discard()
            raise

        session = cls(
            agent,
            settings,
            prompt,
            root,
            policy,
            tracker,
            confirm,
            tools,
            search_tool,
            mcp_session,
            tuple(warnings),
            log,
            base_prompt,
            loaded,
            loaded.tools,
            tuple(ext_warnings),
            trusted,
            tuple(path for path, _ in context),
            context_enabled,
            sandbox_warning,
            env,
            sandbox_orphans,
            workspace_copy,
            background=background,
            schedule=schedule,
            tasks=tasks,
            session_dir=Path(session_dir).expanduser() if session_dir is not None else None,
            team_cell=team_cell,
            request_slot=request_slot,
            compaction_slot=compaction_slot,
            recall_tool=recall_tool,
            skill_usage=skill_usage,
        )
        # 工具在会话之前装配好，所以这里回填它要调用的人。
        recall_hooks.runner = session.recall
        return session

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def system(self) -> str:
        return self._system

    @property
    def workspace(self) -> Path:
        return self._workspace

    @property
    def policy(self) -> Policy:
        return self._policy

    @property
    def session_path(self) -> Path | None:
        return self._log.path if self._log is not None else None

    @property
    def mcp_servers(self) -> tuple[str, ...]:
        """已连上的 MCP server 名字；未启用时为空。"""
        return self._mcp.servers if self._mcp is not None else ()

    @property
    def mcp_warnings(self) -> tuple[str, ...]:
        """MCP 相关告警：被跳过的未信任配置、连不上的 server。"""
        return self._mcp_warnings

    @property
    def commands(self) -> tuple[extensions.Command, ...]:
        """从扩展加载的斜杠命令。"""
        return self._extensions.commands

    @property
    def skills_used(self) -> dict[str, int]:
        """本次会话里每个技能被用了几次（`use_skill` 与 `/skill:` 都算）。"""
        return dict(self._skill_usage)

    @property
    def usage_totals(self) -> dict[str, int]:
        """本会话的用量合计（来自会话文件的 `usage` 记录）。"""
        if self._log is None:
            return {"turns": 0, "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
        return self._log.total_usage()

    @property
    def skills(self) -> tuple[extensions.Skill, ...]:
        """从扩展加载的技能。"""
        return self._extensions.skills

    @property
    def extension_warnings(self) -> tuple[str, ...]:
        """扩展相关告警：加载失败的单个文件、被跳过的同名工具、未信任目录。"""
        return self._extension_warnings

    @property
    def context_files(self) -> tuple[Path, ...]:
        """拼进系统提示的项目规则文件，按「根 -> workspace」排序。"""
        return self._context_files

    @property
    def sandbox_warning(self) -> str | None:
        """执行环境不可用时的原因；`None` 表示正常。"""
        return self._sandbox_warning

    @property
    def sandbox_backend(self) -> str:
        """实际用上的后端名（`auto` 解析后的结果）；环境不可用时返回 `none`。"""
        return self._env.name if self._env is not None else "none"

    @property
    def sandbox_orphans(self) -> tuple[str, ...]:
        """启动时清掉的残留：docker 是容器 ID，appcontainer 是路径。"""
        return self._sandbox_orphans

    @property
    def sandbox_workspace(self) -> Path | None:
        """沙箱自有的 workspace 副本；`local` 与兜底时为 `None`。"""
        return self._workspace_copy.path if self._workspace_copy is not None else None

    @property
    def workspace_sync(self) -> workspace.WorkspaceSync | None:
        """关闭时把副本合回宿主的结果；未关闭或 `local` 时为 `None`。"""
        return self._workspace_sync

    @property
    def tools(self) -> tuple[AgentTool, ...]:
        """当前声明给模型的工具。"""
        return self._tools

    @property
    def compaction(self) -> CompactionState | None:
        """已安装的压缩状态；历史本身不受影响。"""
        return self._compaction_slot[0]

    def _selector(self):
        """零命中兜底：一个独立的小窗口，只给它候选清单与查询。

        每次调用都覆盖 `_selector_usage`，所以「这次 recall 花没花额外的钱」事后可查。
        """

        async def pick(query: str, cands: list[tuple[str, str]]) -> Sequence[str]:
            result = await select_ids(
                self._settings, query, cands, client=get_pool()
            )
            self._selector_usage = result.usage
            return result.ids

        return pick

    async def recall(
        self,
        ids: Sequence[str] | None,
        query: str,
        limit: int,
    ) -> ToolResult:
        """从已压缩区段读回原文；每代压缩一份检索预算，超了整段拒绝。"""
        state = self._compaction_slot[0]
        if state is None:
            return ToolResult("当前没有已压缩的历史，直接读上下文即可", is_error=True)

        history = self._agent.messages
        entry_ids = self.message_ids
        covered = state.first_kept
        budget = self._settings.recall_tokens or max(
            2_000, self._settings.spec.context_window // 10
        )
        if self._recall_version != state.version:
            self._recall_version = state.version
            self._recall_spent = 0
        left = budget - self._recall_spent

        try:
            if ids:
                text = read_ids(history, entry_ids, covered, ids)
            else:
                # 关键词零命中时，再让模型在一个独立的小窗口里从候选清单里挑
                # （一次调用上限一发，护栏见 selector.py）。
                text, used_selector = await search_with_selector(
                    history,
                    entry_ids,
                    covered,
                    query,
                    limit,
                    cache=self._recall_tokens,
                    select=self._selector() if self._settings.selector else None,
                )
        except RecallError as exc:
            return ToolResult(str(exc), is_error=True)

        cost = estimate_text_tokens(text)
        if cost > left:
            return ToolResult(
                f"recall: 需要 {cost} token，本代只剩 {left}；请缩小范围",
                is_error=True,
            )
        self._recall_spent += cost
        return ToolResult(text)

    @property
    def history(self) -> tuple[Message, ...]:
        """完整历史（含被摘要掉的原文）。压缩不改变它。"""
        return self._agent.messages

    @property
    def message_ids(self) -> tuple[str | None, ...]:
        """每条消息的条目 id：有会话文件时用文件记录的，否则按位置合成。

        合成出来的 id 在「只追加」的会话里同样稳定；回退只截尾部，前面的 id 不变。
        """
        if self._log is not None:
            return self._log.entry_ids
        return tuple(f"e{index + 1}" for index in range(len(self._agent.messages)))

    @property
    def messages(self) -> tuple[Message, ...]:
        """模型可见的视图：摘要 + 保留段。由完整历史投影而来。"""
        return tuple(project_view(self._agent.messages, self._compaction_slot[0]))

    @property
    def state(self) -> AgentState:
        return self._agent.state

    @property
    def tasks(self) -> TaskGraph | None:
        """任务图；没有装配时为 `None`。"""
        return self._tasks

    @property
    def team(self) -> AgentTeam | None:
        """多 agent 团队；没有装配时为 `None`。"""
        return self._team_cell[0] if self._team_cell is not None else None

    def attach_team(self, team: AgentTeam | None) -> None:
        """由宿主在团队创建后回填；多 agent 工具持有的是这个间接引用。"""
        if self._team_cell is not None:
            self._team_cell[0] = team

    def prompt(self, text: str) -> AsyncIterator[AgentEvent]:
        """追加一条用户消息并启动一次运行；结束时把新消息落盘。

        以 `/` 开头的输入先按扩展命令 / 技能展开成提示词。
        """
        if self._closed:
            raise RuntimeError("session is closed")
        return self._run(self._expand(text))

    def take_due(self, moment: datetime | None = None) -> tuple[ScheduledJob, ...]:
        """取出这一分钟到点的定时任务，并标记为待交付。

        标记先于运行落盘：进程在「到点」与「跑完」之间退出时，重启后仍能看到
        这些任务处于待交付状态。没有任务表时返回空元组。
        """
        if self._schedule is None:
            return ()
        return self._schedule.due(moment)

    def pending_jobs(self) -> tuple[ScheduledJob, ...]:
        """待交付的定时任务，也就是「队列」里的内容。

        驱动层靠它决定下一轮跑哪条：`take_due` 只把到点的任务标进这里，
        真正点火由上层按序取。
        """
        if self._schedule is None:
            return ()
        return self._schedule.pending()

    async def spawn(self, name: str, task: Task) -> Session:
        """给子 agent 建一个会话：共享工作目录、环境与任务表，对话独立。

        与 `connect()` 的区别是不新建沙箱副本、不连 MCP、不读项目规则：
        子 agent 就在主 agent 的同一个目录里干活，只装配自己的工具与会话文件。
        会话文件落在 `<session_dir>/agents/<name>.jsonl`，不进会话列表。
        """
        if self._closed:
            raise RuntimeError("session is closed")

        settings = self._settings_for(task.model)
        root = self._workspace
        tool_root = self._tool_root
        # 共用主 agent 的策略：子 agent 的写操作仍然逐次询问，且沿用已记住的允许项。
        policy = self._policy
        tracker = FileTracker()
        # 后台任务的日志按子 agent 分目录：任务号会重号，落在一起会互相覆盖。
        background = (
            BackgroundTasks(self._env, tool_root / ".gradus" / "background" / name)
            if self._env is not None
            else None
        )
        # 子会话有自己的 recall：检索的是它自己的历史与预算。
        recall_hooks = RecallHooks()
        recall_tool = RecallHistoryTool(recall_hooks)
        tools, search_tool, _, _ = _build_tools(
            tool_root,
            tracker,
            None,
            policy,
            self._confirm,
            (),
            self._env,
            background,
            # 子 agent 只做自己那一个节点：不给任务图工具，也不给多 agent 工具，
            # 否则它既能改图、又能无限派生下一层。
            None,
            None,
            recall_tool,
            skills=self._extensions.skills,
            skill_usage=self._skill_usage,
            output_chars=settings.tool_output_chars,
        )
        prompt = self._base_system or build_system(tool_root)
        directory = (self._session_dir or root / ".gradus" / "sessions") / "agents"
        log = SessionFile.create(directory, settings, root, name=name)
        # 子会话自己的请求条数槽位：与父会话的锚点互不影响。
        request_slot: list[int] = [0]
        compaction_slot: list[CompactionState | None] = [None]
        agent = await _connect_agent(
            settings, prompt, policy, self._confirm, tools, search_tool, None,
            trim_log=log, request_slot=request_slot, compaction_slot=compaction_slot,
        )
        child = Session(
            agent,
            settings,
            prompt,
            root,
            policy,
            tracker,
            self._confirm,
            tools,
            search_tool,
            None,
            (),
            log,
            prompt,
            None,
            (),
            (),
            self._trusted,
            (),
            False,
            self._sandbox_warning,
            self._env,
            (),
            None,
            background,
            None,
            None,
            directory,
            False,
            request_slot=request_slot,
            compaction_slot=compaction_slot,
            recall_tool=recall_tool,
            # 子会话复用主会话的技能计数；原来这里引用了不存在的局部变量，一 spawn 就 NameError。
            skill_usage=self._skill_usage,
        )
        recall_hooks.runner = child.recall
        return child

    def _settings_for(self, model: str | None) -> Settings:
        """按节点的 `model` 提示挑一个候选；找不到就跟随主 agent。"""
        if not model:
            return self._settings
        for choice in self._settings.models:
            if model in (choice.name, choice.spec.model):
                return with_choice(self._settings, choice)
        return self._settings

    def prompt_scheduled(self, job: ScheduledJob) -> AsyncIterator[AgentEvent]:
        """跑一轮定时任务：模型产出第一轮后销账，整次运行失败记一次重试。

        与 `prompt` 的区别只有两处：提示词标成 `origin="harness"`，
        并把结果反馈给任务表。
        """
        if self._closed:
            raise RuntimeError("session is closed")
        return self._run(f"[Scheduled] {job.prompt}", origin="harness", job=job)

    def _settle(self, job: ScheduledJob, reason: str | None) -> None:
        """把一次定时运行的结果反馈给任务表。

        成功（模型产出了内容）销账；失败记一次重试，超过上限后放弃到下一次匹配。
        取消不在两条分支里：任务保持待交付，下一次 tick 会重新点火。
        """
        if self._schedule is None:
            return
        if reason is None:
            self._schedule.ack(job.id)
        else:
            self._schedule.fail(job.id, reason)

    def _expand(self, text: str) -> str:
        try:
            expanded = self._extensions.expand(text)
        except extensions.UnknownCommand as exc:
            raise SessionError(str(exc)) from exc
        # `/skill:name` 也算一次使用（与 `use_skill` 工具共用同一个计数）。
        if text.startswith("/skill:"):
            name = text[len("/skill:") :].partition(" ")[0].strip()
            if name:
                self._skill_usage[name] = self._skill_usage.get(name, 0) + 1
        return expanded

    async def _run(
        self,
        text: str | None,
        *,
        origin: Literal["user", "harness"] = "user",
        job: ScheduledJob | None = None,
    ) -> AsyncIterator[AgentEvent]:
        await self._refresh_mcp()
        await self._maybe_compact()

        policy = RetryPolicy()
        attempt = 0
        # `text` 为 `None` 时只跑一轮，不追加用户消息：外部事件已经在
        # `_flush_notices` 里变成插话，凭空造一条空的用户消息没有意义。
        events = (
            self._agent.continue_()
            if text is None
            else self._agent.prompt(text, origin=origin)
        )
        self._flush_notices()
        settled = False

        while True:
            retry = False

            async for event in events:
                # 模型已经产出一轮，就算「被接收」：销账后再失败也不重跑。
                if job is not None and not settled and isinstance(event, TurnEnd):
                    self._settle(job, None)
                    settled = True

                if isinstance(event, AgentError):
                    # 失败前已完成的轮次由 agent 写回历史；这里同步落盘，保证文件不落后。
                    # 取消或首轮就失败时 `new_messages` 为空，提问也视为没说过。
                    self._append(event.new_messages)
                    if attempt < policy.attempts and is_retryable(event):
                        attempt += 1
                        await asyncio.sleep(policy.delay(attempt))
                        # 一轮都没完成时提问还没进历史，重试要重新带上它。
                        events = self._agent.continue_(
                            UserMessage(text, origin=origin)
                            if text is not None and not event.new_messages
                            else None
                        )
                        retry = True
                        break
                    if job is not None and not settled:
                        # 取消不算失败：任务保持待交付，下一次 tick 重新点火。
                        if event.kind != "aborted":
                            self._settle(job, event.message)
                        settled = True
                    yield event
                    return

                if isinstance(event, StreamDone):
                    # 锚点绑在「请求」上而不是「消息」上：这条 usage 描述的是刚刚
                    # 发出的那次请求，之后的增量按这次请求的条数往后估算。
                    # 条数取钩子里记下的值，而不是 `agent.messages`——一轮里可能有
                    # 多次请求，而本轮内容要等 `TurnEnd` 才提交，直接数会把整轮重复算一次。
                    self._anchor = UsageAnchor(
                        tokens=event.usage.input_tokens,
                        messages=self._request_slot[0] or len(self._agent.messages),
                    )

                if isinstance(event, TurnEnd) and self._log is not None:
                    # 每轮记一条：成本、缓存命中、压缩收益都能从会话文件里复盘。
                    self._log.record_usage(
                        event.usage, model=self._settings.spec.model
                    )

                if isinstance(event, AgentDone):
                    self._append(event.new_messages)
                    if job is not None and not settled:
                        self._settle(job, None)
                        settled = True
                yield event

            if not retry:
                # 轮次收尾再看一次预算。只在轮次开始时检查的话，本轮新增的内容要等
                # 下一次请求才被算进去，而那时 `transform_messages` 里的兜底裁剪已经
                # 先把旧消息丢了——看起来「没事」，其实是静默丢历史而不是压缩。
                await self._maybe_compact()
                return

    def _append(self, messages: list[Message]) -> None:
        if self._log is not None:
            self._log.append(messages)

    def notify(self, text: str) -> bool:
        """把一条外部事件送进对话：子 agent 结果、后台任务完成。

        运行中作为插话在下一轮请求前取用；空闲时暂存，等下一次运行开头补送。
        返回是否**只是暂存**——调用方据此决定要不要再起一轮把它读掉。
        """
        if self._agent.state == "running":
            self._agent.notify(text)
            return False
        self._notices.append(text)
        return True

    def _notify(self, text: str) -> None:
        """`BackgroundTasks` 的完成回调；通知方不关心是插话还是暂存。

        动态读 `self._agent`：MCP 刷新、压缩、切模型都会重建 Agent，
        不能在构造时缓存方法引用。
        """
        self.notify(text)

    @property
    def has_notices(self) -> bool:
        """收件箱里是否还有没送进对话的外部事件。"""
        return bool(self._notices)

    def _flush_notices(self) -> None:
        """把运行外攒下的后台通知补送到本次运行开头。

        通知描述的是已发生的事实，不做「过期插话」式的丢弃；合并成一条，
        避免第一轮请求里连续出现多条用户来源的消息。
        """
        if not self._notices:
            return
        merged = "\n".join(self._notices)
        self._notices.clear()
        self._agent.notify(merged)

    def continue_(self) -> AsyncIterator[AgentEvent]:
        """在现有历史上再跑一轮，不追加用户消息。

        子 agent 跑完节点后是 idle，没人再 `prompt` 它，收件箱就永远读不到。
        这里起一轮：`_flush_notices` 在开头把攒下的消息补成插话，模型因此能
        读到「有人找它」，而不是空转一条空的用户发言。
        """
        if self._closed:
            raise RuntimeError("session is closed")
        return self._run(None, origin="harness")

    async def _refresh_mcp(self) -> None:
        """server 通知工具/资源列表变化后，重建工具集与 `Agent`。

        刷新失败只保留旧工具，不打断当前轮次。
        """
        if self._mcp is None or not self._mcp.changed:
            return

        try:
            refreshed = await self._mcp.refresh()
        except Exception:
            return

        tools, search_tool, deferred_servers, _ = _build_tools(
            self._tool_root,
            self._tracker,
            refreshed,
            self._policy,
            self._confirm,
            self._extension_tools,
            self._env,
            self._background,
            self._schedule,
            self._tasks,
            self._team_getter,
            recall=self._recall_tool,
            skills=loaded.skills,
            skill_usage=self._skill_usage,
            output_chars=self._settings.tool_output_chars,
        )
        context = read_project_context(self._workspace) if self._context_enabled else ()
        system = _compose_system(
            self._base_system,
            search_tool,
            deferred_servers,
            self._extensions.skills,
            context,
        )

        messages = list(self._agent.messages)
        await self._agent.aclose()
        self._agent = await _connect_agent(
            self._settings,
            system,
            self._policy,
            self._confirm,
            tools,
            search_tool,
            messages,
            trim_log=self._log,
            request_slot=self._request_slot,
            compaction_slot=self._compaction_slot,
        )
        self._tools = tools
        self._tool_search = search_tool
        self._system = system
        self._context_files = tuple(path for path, _ in context)
        self._mcp = refreshed

    async def _summarize(
        self,
        visible: list[Message],
        keep_recent: int,
    ) -> Compaction | None:
        """先走「重放主前缀」的摘要（吃缓存），装不下时退回拍文本的兜底。"""
        tools = [tool.spec for tool in self._tools]
        replay = make_summarizer(
            self._settings,
            client=get_pool(),
            system=self._system,
            tools=tools,
        )
        try:
            result = await compact(
                visible, summarizer=replay, keep_recent_tokens=keep_recent
            )
        except CompactionError:
            fallback = make_text_summarizer(self._settings, client=get_pool())
            result = await compact(
                visible, summarizer=fallback, keep_recent_tokens=keep_recent
            )
            self.last_summary_usage = getattr(fallback, "last_usage", None)
            return result
        # 记下这次摘要请求的真实用量：它是「摘要吃到前缀缓存」的唯一证据，
        # 真机脚本据此断言 `cache_read_tokens > 0`。
        self.last_summary_usage = getattr(replay, "last_usage", None)
        return result

    async def _maybe_compact(self) -> None:
        """超预算时摘要可见历史的前半段，安装新的压缩状态。

        历史本身不被替换，`Agent` 也不重建：下一次请求由 `transform_messages`
        投影成「摘要 + 保留段」。摘要失败只放弃本次压缩，不打断当前轮次。
        """
        history = list(self._agent.messages)
        budget = _budget_for(self._settings)
        state = self._compaction_slot[0]
        visible = project_view(history, state)
        if not should_compact(
            visible, budget.window, budget.reserve, anchor=self._anchor
        ):
            return

        # 上一轮失败了就先跳过（退避表 1/2/4/8 轮）：一次失败不该变成每轮重付，
        # 端点持续回错时更是如此。
        if self._compact_skips > 0:
            self._compact_skips -= 1
            return

        # 先自己算一次切点，只为分清「没东西可切」和「摘要失败」：
        # 前者是结构性上限（当前这一轮本身就超线），要说给用户听；后者静默重试。
        if (
            select_cut(
                visible, budget.keep_recent, active_turn_start(visible)
            )
            is None
        ):
            self._note_turn_too_big()
            return

        # 压前压后都用纯估算：`context_tokens` 会退回「最后一条带 usage 的
        # assistant」，那个数描述的是**压缩前**发出的那次请求——两边拿同一个数来比
        # 永远相等，于是每次都会「看起来没变小」而被拒。原因见 problem.md。
        before = estimate_messages_tokens(visible)
        try:
            result = await self._summarize(visible, budget.keep_recent)
        except CompactionError:
            self._note_compaction_failure()
            return
        if result is None:
            return

        # 切点相对「可见部分」计算，换算成完整历史的下标后再记住。
        base = state.first_kept if state is not None else 0
        first_kept = base + result.first_kept
        # 最近几条子 agent 结果是「工作产物」：别让它们一起进摘要。
        first_kept = keep_recent_work_products(history, first_kept, base)

        after = estimate_messages_tokens(
            [summary_message(result.summary), *history[first_kept:]]
        )
        if after >= before:
            return

        if self._log is not None:
            self._log.record_compaction(result.summary, first_kept)
            self._compaction_slot[0] = self._log.compaction
        else:
            version = (state.version if state is not None else 0) + 1
            layers = (
                *(state.layers if state is not None else ()),
                SummaryLayer(key=f"v{version}", text=result.summary),
            )
            self._compaction_slot[0] = CompactionState(
                layers=layers, first_kept=first_kept, version=version
            )

        # 分层有上限：超过就把最旧的两份合成一份。
        await self._merge_layers_if_needed()
        # 压缩换了视图，旧锚点描述的是已经不存在的上下文；下一次请求会重新锚定。
        self._anchor = None
        # 这一轮成功了：失败退避与「本轮太大」的提示都归零。
        self._compact_failures = 0
        self._compact_skips = 0
        self._turn_too_big = False

    def _note_compaction_failure(self) -> None:
        """摘要失败：记一次，并按退避表（1/2/4/8 轮）跳过下次自动尝试。

        不做退避的话，端点持续回错会变成「每轮付一次注定失败的摘要请求」——这正是
        设计里写明要避免的。
        """
        self._compact_failures += 1
        index = min(self._compact_failures, len(COMPACT_RETRY_SKIPS)) - 1
        self._compact_skips = COMPACT_RETRY_SKIPS[index]

    def _note_turn_too_big(self) -> None:
        """当前这一轮本身就超过保留预算：压缩切不动，只能说给用户听（只提示一次）。

        现象是「上下文满了却一直不压缩、只是忘事」；说清楚原因，用户才知道该拆任务
        或者少让工具输出一大片。
        """
        if self._turn_too_big:
            return
        self._turn_too_big = True
        self.notify(
            "上下文已到压缩线，但当前这一轮本身太大，没有可折的旧内容；"
            "请把任务拆小，或让单次工具输出更短。"
        )

    async def _merge_layers_if_needed(self) -> None:
        """摘要层数或总长度超过上限时，把最旧的两份合并成一份。

        合并失败就多留一层——超限只是多一点 token，不影响正确性，不值得中断。
        """
        state = self._compaction_slot[0]
        if state is None:
            return

        limit_layers = self._settings.summary_layers or MAX_SUMMARY_LAYERS
        limit_tokens = self._settings.summary_tokens or MAX_SUMMARY_TOKENS

        # 一次合并只减一层，但长度可能仍然超限（比如刚合并完又压进来一份很长的），
        # 所以循环到收敛；只剩一层时无从合并，接受超限。
        while len(state.layers) > 1:
            total = sum(estimate_text_tokens(layer.text) for layer in state.layers)
            if len(state.layers) <= limit_layers and total <= limit_tokens:
                return

            oldest = state.layers[:2]
            try:
                merged = await merge_summaries(
                    self._settings,
                    [layer.text for layer in oldest],
                    client=get_pool(),
                )
            except CompactionError:
                return
            if not merged:
                return

            if self._log is not None:
                self._log.record_merge(merged, [layer.key for layer in oldest])
                state = self._log.compaction
            else:
                state = CompactionState(
                    layers=(SummaryLayer(key="merged", text=merged), *state.layers[2:]),
                    first_kept=state.first_kept,
                    version=state.version + 1,
                )
            self._compaction_slot[0] = state

    async def reload(self) -> tuple[str, ...]:
        """重新读扩展目录并重建 `Agent`；历史保留，MCP 连接不动。

        返回扩展相关的告警。扩展目录在启动时未信任的话，这里不会重新询问，
        只提示重启。
        """
        if self._closed:
            raise RuntimeError("session is closed")

        project_dir = extensions.default_project_dir(self._workspace)
        if project_dir.is_dir() and not self._trusted:
            warnings = (
                "extensions are not trusted; restart Gradus to confirm them",
            )
            return warnings

        loaded = extensions.load(self._workspace, project_enabled=self._trusted)
        tools, search_tool, deferred_servers, tool_warnings = _build_tools(
            self._tool_root,
            self._tracker,
            self._mcp,
            self._policy,
            self._confirm,
            loaded.tools,
            self._env,
            self._background,
            self._schedule,
            self._tasks,
            self._team_getter,
            recall=self._recall_tool,
            output_chars=self._settings.tool_output_chars,
        )
        context = read_project_context(self._workspace) if self._context_enabled else ()
        system = _compose_system(
            self._base_system, search_tool, deferred_servers, loaded.skills, context
        )
        agent = await _connect_agent(
            self._settings,
            system,
            self._policy,
            self._confirm,
            tools,
            search_tool,
            list(self._agent.messages),
            trim_log=self._log,
            request_slot=self._request_slot,
            compaction_slot=self._compaction_slot,
        )

        previous, self._agent = self._agent, agent
        await previous.aclose()

        self._extensions = loaded
        self._extension_tools = loaded.tools
        self._extension_warnings = (*loaded.warnings, *tool_warnings)
        self._tools = tools
        self._tool_search = search_tool
        self._system = system
        self._context_files = tuple(path for path, _ in context)
        return self._extension_warnings

    async def switch_model(self, settings: Settings) -> None:
        """换模型：用同一份历史、工具与系统提示重建 `Agent`。

        历史是 provider 无关的中间表示，换协议时由新 provider 自己翻译；
        工具与系统提示不依赖模型，直接沿用。正在生成时拒绝切换。
        """
        if self._closed:
            raise RuntimeError("session is closed")
        if self.state == "running":
            raise SessionError("正在生成，先停止再切换模型")

        # 换模型等于换窗口、换输出上限：预算要按新模型重算，不满足就拒绝切换。
        _budget_for(settings)

        agent = await _connect_agent(
            settings,
            self._system,
            self._policy,
            self._confirm,
            self._tools,
            self._tool_search,
            list(self._agent.messages),
            trim_log=self._log,
            request_slot=self._request_slot,
            compaction_slot=self._compaction_slot,
        )
        previous, self._agent = self._agent, agent
        self._settings = settings
        # 不同模型的分词器不同，旧锚点的绝对值对新模型不再准确；让下一轮重新锚定。
        self._anchor = None
        await previous.aclose()

    async def switch_branch(self, entry_id: str) -> None:
        """把会话切到某条分支，可以是被放弃的那条。

        做法只有两步：**追加**一条 `leaf` 事件，再按文件重新加载。历史一个字节都不改，
        被放弃分支的消息仍在文件里，切回去时它们又回到回溯链上。

        代价：整条历史换掉，下一次请求从**分叉点之后**重新计算前缀（公共祖先照常命中）。
        """
        if self._closed:
            raise RuntimeError("session is closed")
        if self.state == "running":
            raise SessionError("正在生成，先停止再切分支")
        if self._log is None:
            raise SessionError("分支切换需要会话文件；本会话只存在内存里")

        self._log.record_leaf(entry_id)
        fresh = SessionFile.load(self._log.path)

        agent = await _connect_agent(
            self._settings,
            self._system,
            self._policy,
            self._confirm,
            self._tools,
            self._tool_search,
            fresh.messages,
            trim_log=fresh,
            request_slot=self._request_slot,
            compaction_slot=self._compaction_slot,
        )
        previous, self._agent = self._agent, agent
        self._log = fresh
        self._compaction_slot[0] = fresh.compaction
        # 视图换了，旧锚点描述的是另一条链；让下一轮重新锚定。
        self._anchor = None
        self._recall_version = -1
        await previous.aclose()

    async def apply_settings(self, settings: Settings) -> None:
        """只更新预算类配置，不重建 `Agent`；换模型请走 `switch_model`。

        预设、保留量、预留守只影响「下一次压缩」与请求前的兜底裁剪，改完立刻生效；
        协议、端点、key 都不动，所以历史、连接池、工具集都不用换。非法组合（例如
        保留量不小于触发线）在这里就抛，不等下一次压缩才发现。
        """
        merged = replace(
            self._settings,
            keep_preset=settings.keep_preset,
            context_keep_tokens=settings.context_keep_tokens,
            context_reserve_tokens=settings.context_reserve_tokens,
        )
        _budget_for(merged)
        self._settings = merged
        # 预算变了，旧锚点对应的触发线不再成立；下一轮重新锚定。
        self._anchor = None

    async def abort(self) -> None:
        """取消当前运行，已完成的轮次保留在历史里。"""
        await self._agent.abort()

    async def aclose(self) -> None:
        """关闭本会话的 Agent、后台任务、MCP 连接与执行环境；共享连接池不在这里关闭。

        后台命令先停：它们还在往沙箱副本里写，合回宿主时会丢产物。
        沙箱后端再把副本合回宿主，让环境撤销授权 / 销毁容器，最后删副本。
        合并失败时保留副本，路径记在 `workspace_sync`。幂等。
        """
        if self._closed:
            return
        self._closed = True
        if self._background is not None:
            await self._background.aclose()
        await self._agent.aclose()
        if self._mcp is not None:
            await self._mcp.aclose()

        copy = self._workspace_copy
        if copy is not None:
            self._workspace_sync = await copy.sync()
        if self._env is not None and self._owns_env:
            await self._env.aclose()
        if copy is not None and (
            self._workspace_sync is None or self._workspace_sync.error is None
        ):
            await copy.discard()


def _rebuild_layers(ordered: Sequence[dict[str, Any]]) -> tuple[SummaryLayer, ...]:
    """按顺序重放压缩/合并条目，得到当前的摘要分层。

    `compaction` 追加一层；`merge` 把它 `supersedes` 的那几层换成自己（放在最前面，
    因为它代表的是更老的内容）。
    """
    layers: list[SummaryLayer] = []
    for entry in ordered:
        kind = entry["type"]
        if kind == "compaction":
            layers.append(SummaryLayer(key=str(entry["id"]), text=str(entry.get("summary", ""))))
        elif kind == "merge":
            dropped = {str(item) for item in entry.get("supersedes") or []}
            layers = [layer for layer in layers if layer.key not in dropped]
            layers.insert(0, SummaryLayer(key=str(entry["id"]), text=str(entry.get("text", ""))))
    return tuple(layers)


def _ordered_entries(
    path: Path,
    entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """从最后一个 entry 沿 parentId 回溯，返回根到叶的顺序。"""
    if not entries:
        return []

    by_id: dict[str, dict[str, Any]] = {}
    for entry in entries:
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not entry_id:
            raise SessionError(f"{path}: message entry without id")
        if entry_id in by_id:
            raise SessionError(f"{path}: duplicate entry id {entry_id!r}")
        if entry["type"] == "message" and not isinstance(entry.get("message"), dict):
            raise SessionError(f"{path}: entry {entry_id!r} has no message")
        if entry["type"] == "compaction" and not isinstance(entry.get("summary"), str):
            raise SessionError(f"{path}: entry {entry_id!r} has no summary")
        by_id[entry_id] = entry

    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    current: dict[str, Any] | None = entries[-1]

    while current is not None:
        entry_id = current["id"]
        if entry_id in seen:
            raise SessionError(f"{path}: cycle at entry {entry_id!r}")
        seen.add(entry_id)
        ordered.append(current)

        parent_id = current.get("parentId")
        if parent_id is None:
            break
        parent = by_id.get(parent_id)
        if parent is None:
            raise SessionError(f"{path}: missing parent {parent_id!r}")
        current = parent

    ordered.reverse()
    return ordered


def _kept_index(
    path: Path,
    compaction: dict[str, Any],
    entry_ids: list[str | None],
) -> int:
    """压缩记录里 `firstKeptEntryId` 在消息序列中的位置。"""
    kept_id = compaction.get("firstKeptEntryId")
    if kept_id not in entry_ids:
        raise SessionError(f"{path}: compaction keeps unknown entry {kept_id!r}")
    return entry_ids.index(kept_id)


def _validate_pairs(path: Path, messages: list[Message]) -> None:
    """工具调用与结果必须一一对应。"""
    calls: list[str] = []
    results: list[str] = []

    for message in messages:
        if isinstance(message, AssistantMessage):
            calls.extend(
                block.id for block in message.content if isinstance(block, ToolCallBlock)
            )
        elif isinstance(message, ToolResultMessage):
            results.append(message.tool_call_id)

    missing = [call for call in calls if call not in results]
    extra = [result for result in results if result not in calls]
    if missing or extra:
        raise SessionError(
            f"{path}: tool calls and results do not match "
            f"(missing results: {missing}, orphan results: {extra})"
        )


def _message_to_dict(message: Message) -> dict[str, Any]:
    if isinstance(message, UserMessage):
        payload: dict[str, Any] = {"role": "user", "content": message.content}
        # 只给非用户来源写 origin，旧文件读出来全是人说的。
        if message.origin != "user":
            payload["origin"] = message.origin
        return payload
    if isinstance(message, AssistantMessage):
        return {
            "role": "assistant",
            "content": [_block_to_dict(block) for block in message.content],
            "stop_reason": message.stop_reason,
            "usage": _usage_to_dict(message.usage),
        }
    if isinstance(message, ToolResultMessage):
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "content": message.content,
            "is_error": message.is_error,
        }
    raise SessionError(f"cannot serialize {type(message).__name__}")


def _message_from_dict(payload: Any) -> Message:
    if not isinstance(payload, dict):
        raise SessionError("message payload must be an object")

    role = payload.get("role")
    if role == "user":
        content = payload.get("content")
        if not isinstance(content, str):
            raise SessionError("user content must be a string")
        origin = payload.get("origin", "user")
        if origin not in ("user", "harness"):
            raise SessionError(f"unknown user message origin {origin!r}")
        return UserMessage(content, origin=origin)

    if role == "assistant":
        blocks = payload.get("content")
        if not isinstance(blocks, list):
            raise SessionError("assistant content must be an array")
        return AssistantMessage(
            content=[_block_from_dict(block) for block in blocks],
            stop_reason=payload.get("stop_reason"),
            usage=_usage_from_dict(payload.get("usage")),
        )

    if role == "tool":
        tool_call_id = payload.get("tool_call_id")
        content = payload.get("content")
        if not isinstance(tool_call_id, str) or not isinstance(content, str):
            raise SessionError("tool message needs tool_call_id and content")
        return ToolResultMessage(
            tool_call_id=tool_call_id,
            content=content,
            is_error=bool(payload.get("is_error", False)),
        )

    raise SessionError(f"unknown message role {role!r}")


def _block_to_dict(block: Any) -> dict[str, Any]:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text, "refusal": block.refusal}
    if isinstance(block, ThinkingBlock):
        return {
            "type": "thinking",
            "thinking": block.thinking,
            "signature": block.signature,
        }
    if isinstance(block, ToolCallBlock):
        return {
            "type": "tool_call",
            "id": block.id,
            "name": block.name,
            "arguments": block.arguments,
            "raw": block.raw,
        }
    raise SessionError(f"cannot serialize {type(block).__name__}")


def _block_from_dict(payload: Any) -> Any:
    if not isinstance(payload, dict):
        raise SessionError("content block must be an object")

    kind = payload.get("type")
    if kind == "text":
        text = payload.get("text")
        if not isinstance(text, str):
            raise SessionError("text block needs text")
        return TextBlock(text=text, refusal=bool(payload.get("refusal", False)))

    if kind == "thinking":
        thinking = payload.get("thinking")
        if not isinstance(thinking, str):
            raise SessionError("thinking block needs thinking")
        signature = payload.get("signature")
        return ThinkingBlock(
            thinking=thinking,
            signature=signature if isinstance(signature, str) else None,
        )

    if kind == "tool_call":
        call_id = payload.get("id")
        name = payload.get("name")
        arguments = payload.get("arguments", {})
        if not isinstance(call_id, str) or not isinstance(name, str):
            raise SessionError("tool_call block needs id and name")
        if not isinstance(arguments, dict):
            raise SessionError("tool_call arguments must be an object")
        raw = payload.get("raw")
        return ToolCallBlock(
            id=call_id,
            name=name,
            arguments=arguments,
            raw=raw if isinstance(raw, str) else "",
        )

    raise SessionError(f"unknown block type {kind!r}")


def _usage_to_dict(usage: Usage | None) -> dict[str, int] | None:
    if usage is None:
        return None
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
    }


def _usage_from_dict(payload: Any) -> Usage | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise SessionError("usage must be an object")

    def number(key: str) -> int:
        value = payload.get(key, 0)
        if not isinstance(value, int):
            raise SessionError(f"usage.{key} must be an integer")
        return value

    return Usage(
        input_tokens=number("input_tokens"),
        output_tokens=number("output_tokens"),
        cache_read_tokens=number("cache_read_tokens"),
        cache_write_tokens=number("cache_write_tokens"),
    )
