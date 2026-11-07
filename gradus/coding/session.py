"""会话装配与历史落盘。

负责：
- 建进程级 `httpx.AsyncClient`，所有会话共用；
- 用 `Agent.connect(..., client=pool)` 建每会话一个 `Agent`；
- 装配 `LoopConfig.system`（基础提示 + 环境信息）；
- 暴露 `prompt` / `abort` / `aclose`；
- 每轮开始前检查上下文预算，超限时摘要旧消息并重建 `Agent`；
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
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from ..agent.agent import Agent, AgentState
from ..agent.base import AgentTool, LoopConfig
from ..agent.hooks import AgentHooks, NextTurnUpdate
from ..agent.types import AgentDone, AgentError, AgentEvent
from ..ai.types import (
    AssistantMessage,
    Message,
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
from .config import Settings
from .sandbox import (
    DockerEnvironment,
    Environment,
    LocalEnvironment,
    cleanup_orphans,
    cleanup_temp,
    docker_problem,
)
from .context import (
    CompactionError,
    compact,
    make_summarizer,
    should_compact,
    summary_message,
    trim_messages,
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
from .tools.codemode import create_codemode_tool
from .tools.files import FileTracker, create_file_tools
from .tools.mcp_resources import create_mcp_resource_tools
from .tools.search import create_search_tools
from .tools.shell import create_shell_tools
from .tools.tool_search import ToolSearchTool

BASE_PROMPT = (
    "You are Gradus, a coding assistant.\n"
    "Be concise. Use tools when needed. Never invent file contents or command output."
)

SESSION_VERSION = 1

_pool: httpx.AsyncClient | None = None


def get_pool() -> httpx.AsyncClient:
    """取进程级连接池，第一次调用时创建。"""
    global _pool
    if _pool is None:
        _pool = httpx.AsyncClient()
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
) -> tuple[
    tuple[AgentTool, ...], ToolSearchTool | None, tuple[tuple[str, str], ...], tuple[str, ...]
]:
    """装配工具集与可选的 `tool_search`。

    返回直接声明的工具、检索工具（没有 deferred 时为 `None`）、deferred server 清单
    与扩展工具的冲突告警。`env=None` 表示执行环境不可用：命令与 `codemode` 都不注册。
    """
    catalog = ToolCatalog()
    catalog.add_all(create_file_tools(root, tracker))
    catalog.add_all(create_search_tools(root))
    if env is not None:
        catalog.add_all(create_shell_tools(root, env))

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

    if env is not None:
        catalog.add(create_codemode_tool(catalog.direct, policy, confirm, env))

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


async def _connect_agent(
    settings: Settings,
    system: str,
    policy: Policy,
    confirm: Confirm | None,
    tools: tuple[AgentTool, ...],
    tool_search: ToolSearchTool | None,
    messages: list[Message] | None,
) -> Agent:
    """按当前配置装配一个 `Agent`；压缩后重建也走这里，保证装配口径一致。"""

    async def trim(transform: Any) -> list[Message]:
        return trim_messages(transform.messages, settings.spec.context_window)

    hooks = AgentHooks(
        transform_messages=trim,
        before_tool_call=make_before_tool_call(policy, confirm),
        after_tool_call=make_after_tool_call(),
    )
    if tool_search is not None:
        hooks = replace(hooks, prepare_next_turn=make_prepare_next_turn(tool_search))

    return await Agent.connect(
        settings.spec,
        LoopConfig(system=system, tools=tools, hooks=hooks),
        api_key=settings.api_key,
        client=get_pool(),
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

    @classmethod
    def create(
        cls,
        directory: str | Path,
        settings: Settings,
        workspace: str | Path,
    ) -> SessionFile:
        """在新目录里建一个空会话文件。"""
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
        path = folder / f"{session_id}.jsonl"
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
            elif kind in {"message", "compaction"}:
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

        compactions = [entry for entry in ordered if entry["type"] == "compaction"]
        if compactions:
            latest = compactions[-1]
            kept = _kept_index(resolved, latest, entry_ids)
            messages = [summary_message(str(latest["summary"])), *messages[kept:]]
            entry_ids = [None, *entry_ids[kept:]]

        _validate_pairs(resolved, messages)

        meta = SessionMeta(
            id=str(header.get("id", "")),
            model=str(header.get("model", "")),
            workspace=str(header.get("workspace", "")),
            created_at=str(header.get("created_at", "")),
        )
        last_id = ordered[-1]["id"] if ordered else None
        return cls(resolved, meta, messages, entry_ids, last_id, len(entries))

    @property
    def path(self) -> Path:
        return self._path

    @property
    def meta(self) -> SessionMeta:
        return self._meta

    @property
    def messages(self) -> list[Message]:
        return list(self._messages)

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

    def record_compaction(self, summary: str, first_kept: int) -> None:
        """追加一条压缩记录，并把内存里的历史换成「摘要 + 保留原文」。"""
        if not 0 <= first_kept < len(self._entry_ids):
            raise SessionError(f"{self._path}: no entry to keep at {first_kept}")
        first_kept_id = self._entry_ids[first_kept]
        if first_kept_id is None:
            raise SessionError(f"{self._path}: entry {first_kept} has no id")

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
        self._messages = [summary_message(summary), *self._messages[first_kept:]]
        self._entry_ids = [None, *self._entry_ids[first_kept:]]


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

        loaded = extensions.load(root, enabled=use_extensions)
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

        try:
            base_prompt = prompt
            tools, search_tool, deferred_servers, tool_warnings = _build_tools(
                tool_root, tracker, mcp_session, policy, confirm, loaded.tools, env
            )
            ext_warnings.extend(tool_warnings)
            prompt = _compose_system(
                prompt, search_tool, deferred_servers, loaded.skills, context
            )

            agent = await _connect_agent(
                settings,
                prompt,
                policy,
                confirm,
                tools,
                search_tool,
                resume_log.messages if resume_log is not None else None,
            )
        except BaseException:
            if mcp_session is not None:
                await mcp_session.aclose()
            if workspace_copy is not None:
                await workspace_copy.discard()
            raise

        return cls(
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
        )

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
    def messages(self) -> tuple[Message, ...]:
        return self._agent.messages

    @property
    def state(self) -> AgentState:
        return self._agent.state

    def prompt(self, text: str) -> AsyncIterator[AgentEvent]:
        """追加一条用户消息并启动一次运行；结束时把新消息落盘。

        以 `/` 开头的输入先按扩展命令 / 技能展开成提示词。
        """
        if self._closed:
            raise RuntimeError("session is closed")
        return self._run(self._expand(text))

    def _expand(self, text: str) -> str:
        try:
            return self._extensions.expand(text)
        except extensions.UnknownCommand as exc:
            raise SessionError(str(exc)) from exc

    async def _run(self, text: str) -> AsyncIterator[AgentEvent]:
        await self._refresh_mcp()
        await self._maybe_compact()

        policy = RetryPolicy()
        attempt = 0
        events = self._agent.prompt(text)

        while True:
            retry = False

            async for event in events:
                if isinstance(event, AgentError):
                    # 失败前已完成的轮次由 agent 写回历史；这里同步落盘，保证文件不落后。
                    self._append(event.new_messages)
                    if attempt < policy.attempts and is_retryable(event):
                        attempt += 1
                        await asyncio.sleep(policy.delay(attempt))
                        events = self._agent.continue_()
                        retry = True
                        break
                    yield event
                    return

                if isinstance(event, AgentDone):
                    self._append(event.new_messages)
                yield event

            if not retry:
                return

    def _append(self, messages: list[Message]) -> None:
        if self._log is not None:
            self._log.append(messages)

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
        )
        context = read_project_context(self._workspace) if self._context_enabled else ()
        system = _compose_system(
            self._base_system,
            search_tool,
            deferred_servers,
            self._extensions.skills,
            context,
        )

        messages = list(self.messages)
        await self._agent.aclose()
        self._agent = await _connect_agent(
            self._settings,
            system,
            self._policy,
            self._confirm,
            tools,
            search_tool,
            messages,
        )
        self._tools = tools
        self._tool_search = search_tool
        self._system = system
        self._context_files = tuple(path for path, _ in context)
        self._mcp = refreshed

    async def _maybe_compact(self) -> None:
        """超预算时摘要旧消息、重建 `Agent`，并把压缩记录写进会话文件。

        摘要失败只放弃本次压缩，不打断当前轮次。
        """
        messages = list(self._agent.messages)
        if not should_compact(messages, self._settings.spec.context_window):
            return

        summarizer = make_summarizer(self._settings, client=get_pool())
        try:
            result = await compact(messages, summarizer=summarizer)
        except CompactionError:
            return
        if result is None:
            return

        if self._log is not None:
            self._log.record_compaction(result.summary, result.first_kept)

        rebuilt = [summary_message(result.summary), *messages[result.first_kept :]]
        await self._agent.aclose()
        self._agent = await _connect_agent(
            self._settings,
            self._system,
            self._policy,
            self._confirm,
            self._tools,
            self._tool_search,
            rebuilt,
        )

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

        loaded = extensions.load(self._workspace)
        tools, search_tool, deferred_servers, tool_warnings = _build_tools(
            self._tool_root,
            self._tracker,
            self._mcp,
            self._policy,
            self._confirm,
            loaded.tools,
            self._env,
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
            list(self.messages),
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

    async def abort(self) -> None:
        """取消当前运行，已完成的轮次保留在历史里。"""
        await self._agent.abort()

    async def aclose(self) -> None:
        """关闭本会话的 Agent、MCP 连接与执行环境；共享连接池不在这里关闭。

        沙箱后端先把副本合回宿主，再让环境撤销授权 / 销毁容器，最后删副本。
        合并失败时保留副本，路径记在 `workspace_sync`。幂等。
        """
        if self._closed:
            return
        self._closed = True
        await self._agent.aclose()
        if self._mcp is not None:
            await self._mcp.aclose()

        copy = self._workspace_copy
        if copy is not None:
            self._workspace_sync = await copy.sync()
        if self._env is not None:
            await self._env.aclose()
        if copy is not None and (
            self._workspace_sync is None or self._workspace_sync.error is None
        ):
            await copy.discard()


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
        return {"role": "user", "content": message.content}
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
        return UserMessage(content)

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
