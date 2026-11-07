"""MCP 工具接入。

负责：
- 读工作区的 `.gradus/mcp.json`；
- 按配置连 server（stdio 子进程或 Streamable HTTP）；
- 把 server 声明的工具转成 `AgentTool`，名字统一成 `<server>__<tool>`；
- 会话结束时关闭连接。

不负责：权限判断（工具名对 `permissions.py` 来说是未知工具，默认询问）、
会话装配（`session.py`）。

配置形状：

```json
{
  "servers": {
    "files": {"command": "npx", "args": ["-y", "server-filesystem", "."]},
    "remote": {"url": "https://example.com/mcp", "headers": {"Authorization": "Bearer ..."}}
  }
}
```

每个 server 必须且只能给 `command`（stdio）或 `url`（HTTP）之一。
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import re
import tempfile
from collections.abc import Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult

from ..agent.base import ExecutionMode, ToolUpdateCallback
from ..agent.types import ToolResult
from ..ai.base import ToolSpec
from . import oauth
from .registry import EXPOSURES, Exposure, ToolError, merge_tools

#: 工作区内的默认配置路径。
CONFIG_RELPATH = Path(".gradus") / "mcp.json"

#: 用户级配置路径（相对 home）；与项目配置按 server 名合并。
USER_CONFIG_RELPATH = Path(".gradus") / "mcp.json"

#: 连接失败时附带多少字符的 server stderr。
STDERR_TAIL_CHARS = 2000

#: 信任记录放在用户目录，按项目绝对路径记。
TRUST_RELPATH = Path(".gradus") / "trusted.json"

#: 暴露给模型的名字分隔符，同时避免不同 server 的同名工具冲突。
NAME_SEPARATOR = "__"

#: 与 MCP 生态一致的前缀，便于按前缀识别与批量过滤。
NAME_PREFIX = "mcp__"

#: 单个工具结果的字符上限；超出时去掉中间，全文落临时文件。
MAX_RESULT_CHARS = 20_000

#: 单次工具调用的默认超时秒数。
DEFAULT_TIMEOUT = 60.0

#: 默认暴露方式：不声明给模型，由 `tool_search` 按需加载。
DEFAULT_EXPOSURE = "deferred"

#: 只保留模型侧可以安全使用的字符。
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")

_SERVER_KEYS = frozenset(
    {
        "command",
        "args",
        "env",
        "cwd",
        "url",
        "headers",
        "enabled",
        "timeout",
        "exposure",
        "toolExposure",
        "description",
        "oauth",
    }
)


class McpConfigError(ValueError):
    """`.gradus/mcp.json` 读不出来或形状不对。"""


@dataclass(frozen=True)
class McpServer:
    """一个 server 的连接参数；`command` 与 `url` 二选一。"""

    name: str
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    oauth: oauth.OAuthSettings | None = None
    enabled: bool = True
    timeout: float = DEFAULT_TIMEOUT
    exposure: Exposure = DEFAULT_EXPOSURE
    tool_exposure: dict[str, Exposure] = field(default_factory=dict)
    description: str = ""

    @property
    def transport(self) -> str:
        return "stdio" if self.command is not None else "http"


@dataclass
class ChangeFlags:
    """server 主动通知的变化，由会话在下一轮前消费。"""

    tools: bool = False
    resources: bool = False


class McpServerHandle:
    """一个已连上的 server：持有连接、重连能力与变更标记。

    `server=None` 时只做一次性连接（测试用），调用失败不会尝试重连。
    """

    def __init__(
        self,
        name: str,
        timeout: float,
        client: Any,
        *,
        server: McpServer | None = None,
        stack: AsyncExitStack | None = None,
        flags: ChangeFlags | None = None,
        interactive: bool = True,
    ) -> None:
        self.name = name
        self.timeout = timeout
        self.client = client
        self.server = server
        self.flags = flags or ChangeFlags()
        self.interactive = interactive
        self._stack = stack if stack is not None else AsyncExitStack()
        self._lock = asyncio.Lock()
        self._epoch = 0

    @property
    def tools_changed(self) -> bool:
        return self.flags.tools

    @property
    def resources_changed(self) -> bool:
        return self.flags.resources

    def clear_changes(self) -> None:
        self.flags.tools = False
        self.flags.resources = False

    async def list_tools(self) -> Any:
        return await self._call(lambda client: client.list_tools())

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return await self._call(
            lambda client: client.call_tool(
                name, arguments, read_timeout_seconds=self.timeout
            )
        )

    async def list_resources(self, cursor: str | None) -> Any:
        return await self._call(lambda client: client.list_resources(cursor=cursor))

    async def list_resource_templates(self, cursor: str | None) -> Any:
        return await self._call(
            lambda client: client.list_resource_templates(cursor=cursor)
        )

    async def read_resource(self, uri: str) -> Any:
        return await self._call(lambda client: client.read_resource(uri))

    async def _call(self, factory: Any) -> Any:
        """执行一次请求；连接断了就重连一次再试。"""
        epoch = self._epoch
        try:
            return await asyncio.wait_for(factory(self.client), self.timeout)
        except Exception as exc:
            if self.server is None:
                raise
            if not await self._reconnect(epoch):
                raise exc
            return await asyncio.wait_for(factory(self.client), self.timeout)

    async def _reconnect(self, epoch: int) -> bool:
        async with self._lock:
            if self._epoch != epoch:
                return True  # 其它任务已经重连过
            await self._close_stack()
            stack = AsyncExitStack()
            try:
                client = await _open_client(
                    self.server, stack, interactive=self.interactive
                )
            except Exception:
                await _close_stack(stack)
                return False
            self._stack = stack
            self.client = client
            self._epoch += 1
            return True

    async def aclose(self) -> None:
        await self._close_stack()

    async def _close_stack(self) -> None:
        stack, self._stack = self._stack, AsyncExitStack()
        await _close_stack(stack)


@dataclass(frozen=True)
class McpSession:
    """一次会话持有的 MCP 连接与工具。

    `tools` 是直接声明的工具，`deferred` 是等 `tool_search` 加载的工具；
    `deferred_servers` 是出现 deferred 工具的 server（名字 + 描述），用于系统提示。
    `handles` 是连上的 server，资源工具用它按名字定位 client；
    `resource_handles` 只包含探测到支持 `resources/list` 的 server。
    `errors` 记录连不上的 server，格式是 `"<server>: <原因>"`。
    """

    tools: tuple[McpTool, ...]
    deferred: tuple[McpTool, ...]
    deferred_servers: tuple[tuple[str, str], ...]
    handles: tuple[McpServerHandle, ...]
    resource_handles: tuple[McpServerHandle, ...]
    errors: tuple[str, ...]

    @property
    def servers(self) -> tuple[str, ...]:
        return tuple(handle.name for handle in self.handles)

    @property
    def changed(self) -> bool:
        return any(
            handle.tools_changed or handle.resources_changed for handle in self.handles
        )

    async def aclose(self) -> None:
        for handle in self.handles:
            await handle.aclose()

    async def refresh(self) -> McpSession:
        """重新列出有变更的 server 的工具；失败时保留该 server 的旧工具。"""
        direct: list[McpTool] = []
        deferred: list[McpTool] = []
        deferred_servers: list[tuple[str, str]] = []
        resource_handles: list[McpServerHandle] = []

        for handle in self.handles:
            old_direct = [tool for tool in self.tools if tool.server == handle.name]
            old_deferred = [tool for tool in self.deferred if tool.server == handle.name]

            listing = None
            if handle.tools_changed:
                try:
                    listing = await handle.list_tools()
                except Exception:
                    listing = None

            if listing is None:
                direct.extend(old_direct)
                deferred.extend(old_deferred)
                if old_deferred:
                    deferred_servers.append(_server_label(handle))
            else:
                has_deferred = False
                for tool in listing.tools:
                    assert handle.server is not None
                    exposure = effective_exposure(handle.server, tool.name)
                    if exposure == "hidden":
                        continue
                    wrapped = _make_tool(handle, tool)
                    if exposure == "direct":
                        direct.append(wrapped)
                    else:
                        deferred.append(wrapped)
                        has_deferred = True
                if has_deferred:
                    deferred_servers.append(_server_label(handle))

            supported = (
                await supports_resources(handle.client, handle.timeout)
                if handle.resources_changed
                else handle in self.resource_handles
            )
            if supported:
                resource_handles.append(handle)
            handle.clear_changes()

        return McpSession(
            tools=tuple(direct),
            deferred=tuple(deferred),
            deferred_servers=tuple(deferred_servers),
            handles=self.handles,
            resource_handles=tuple(resource_handles),
            errors=self.errors,
        )


@dataclass(frozen=True)
class McpTool:
    """一个远端工具在本地的 `AgentTool` 投影。"""

    spec: ToolSpec
    execution_mode: ExecutionMode
    remote_name: str
    handle: McpServerHandle
    server: str = ""

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        try:
            result: CallToolResult = await self.handle.call_tool(
                self.remote_name, arguments
            )
        except Exception as exc:  # 连接断开、超时、server 返回错误都归到这里
            return ToolResult(f"mcp call failed: {type(exc).__name__}: {exc}", is_error=True)
        return ToolResult(clip_result(render_result(result)), is_error=bool(result.is_error))


def default_config_path(workspace: str | Path) -> Path:
    return Path(workspace).expanduser().resolve() / CONFIG_RELPATH


def forget_trust(workspace: str | Path) -> bool:
    """撤销项目的 MCP 信任；返回文件里是否真的有一条被删掉。

    文件不存在或损坏都当作"本来就没信任"，不报错也不重建。
    """
    path = trust_file()
    if not path.exists():
        return False

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False

    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, list):
        return False

    target = str(Path(workspace).expanduser().resolve())
    kept = [item for item in projects if item != target]
    if len(kept) == len(projects):
        return False

    path.write_text(
        json.dumps({"projects": kept}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return True


def default_user_config_path() -> Path:
    """用户级配置；所有项目共享，不需要项目信任。"""
    return Path.home() / USER_CONFIG_RELPATH


def trust_file() -> Path:
    return Path.home() / TRUST_RELPATH


def is_trusted(workspace: str | Path) -> bool:
    """项目是否已被允许执行它自己的 mcp.json。"""
    path = trust_file()
    if not path.exists():
        return False

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False

    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, list):
        return False
    return str(Path(workspace).expanduser().resolve()) in projects


def remember_trust(workspace: str | Path) -> None:
    """把项目绝对路径记进用户级信任文件；文件损坏时重建。"""
    path = trust_file()
    path.parent.mkdir(parents=True, exist_ok=True)

    projects: list[str] = []
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("projects"), list):
                projects = [item for item in data["projects"] if isinstance(item, str)]
        except (OSError, json.JSONDecodeError):
            projects = []

    target = str(Path(workspace).expanduser().resolve())
    if target not in projects:
        projects.append(target)
    path.write_text(
        json.dumps({"projects": projects}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_servers(path: str | Path) -> list[McpServer]:
    """读配置文件；文件不存在表示不启用 MCP。"""
    config_path = Path(path)
    if not config_path.exists():
        return []

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise McpConfigError(f"cannot read {config_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise McpConfigError(f"{config_path} is not JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise McpConfigError(f"{config_path}: top level must be an object")
    servers = data.get("servers")
    if servers is None:
        return []
    if not isinstance(servers, dict):
        raise McpConfigError(f"{config_path}: 'servers' must be an object")

    return [
        _parse_server(config_path, name, payload)
        for name, payload in servers.items()
    ]


def merge_servers(
    user: Sequence[McpServer], project: Sequence[McpServer]
) -> list[McpServer]:
    """按 server 名合并用户级与项目级配置；同名时项目配置整体覆盖。"""
    merged = {server.name: server for server in user}
    merged.update({server.name: server for server in project})
    return list(merged.values())


def make_tool_name(server: str, tool: str) -> str:
    """`mcp__<server>__<tool>`；非法字符替换成下划线。"""
    server_part = _UNSAFE.sub("_", server)
    tool_part = _UNSAFE.sub("_", tool)
    return f"{NAME_PREFIX}{server_part}{NAME_SEPARATOR}{tool_part}"


def clip_result(text: str) -> str:
    """超过上限时去掉中间，把全文写进临时文件并在结果里给出路径。"""
    if len(text) <= MAX_RESULT_CHARS:
        return text

    head_size = MAX_RESULT_CHARS // 2
    tail_size = MAX_RESULT_CHARS - head_size
    removed = len(text) - MAX_RESULT_CHARS
    path = _spill(text)
    return (
        f"{text[:head_size]}\n"
        f"…{removed} chars truncated…\n"
        f"{text[-tail_size:]}\n"
        f"[full output: {path}]"
    )


def _spill(text: str) -> str:
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".txt", prefix="gradus-mcp-", delete=False
    )
    with handle:
        handle.write(text)
    return handle.name


def render_result(result: CallToolResult) -> str:
    """把 MCP 的 content 列表拍成一段文本；非文本内容只留占位。"""
    parts: list[str] = []

    for block in result.content:
        kind = getattr(block, "type", "")
        if kind == "text":
            parts.append(block.text)
        elif kind == "resource_link":
            parts.append(f"[resource: {getattr(block, 'uri', '')}]")
        else:
            parts.append(f"[{kind or 'unknown'} content omitted]")

    if not parts and result.structured_content is not None:
        parts.append(json.dumps(result.structured_content, ensure_ascii=False))

    return "\n".join(parts) if parts else "(empty result)"


def stderr_tail(errlog: Any) -> str:
    """读 server stderr 的末尾；读不到就返回空串。"""
    if errlog is None:
        return ""
    try:
        errlog.flush()
        errlog.seek(0)
        text = errlog.read()
    except (OSError, ValueError):
        return ""
    return text.strip()[-STDERR_TAIL_CHARS:]


def _with_stderr(base: str, errlog: Any) -> str:
    tail = stderr_tail(errlog)
    return f"{base}\n[stderr]\n{tail}" if tail else base


def _stderr_sink() -> Any:
    return tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace")


def _close_sink(errlog: Any) -> None:
    if errlog is None:
        return
    try:
        errlog.close()
    except OSError:
        pass


async def connect(
    servers: Sequence[McpServer], *, interactive: bool = True
) -> McpSession:
    """连上启用的 server 并列出工具。

    单个 server 连不上只记进 `errors`，不影响其它 server；配置形状错误仍然
    在 `load_servers` 阶段直接失败。`interactive=False` 时 OAuth 不会打开浏览器，
    需要登录的 server 直接记成错误。
    """
    direct: list[McpTool] = []
    deferred: list[McpTool] = []
    deferred_servers: list[tuple[str, str]] = []
    handles: list[McpServerHandle] = []
    resource_handles: list[McpServerHandle] = []
    errors: list[str] = []

    for server in servers:
        if not server.enabled:
            continue
        stack = AsyncExitStack()
        errlog = _stderr_sink() if server.command is not None else None
        flags = ChangeFlags()
        try:
            client = await _open_client(
                server, stack, errlog, flags, interactive=interactive
            )
            listing = await client.list_tools()
        except Exception as exc:
            errors.append(
                _with_stderr(f"{server.name}: {type(exc).__name__}: {exc}", errlog)
            )
            _close_sink(errlog)
            await _close_stack(stack)
            continue

        handle = McpServerHandle(
            server.name,
            server.timeout,
            client,
            server=server,
            stack=stack,
            flags=flags,
            interactive=interactive,
        )
        handles.append(handle)
        if await supports_resources(client, server.timeout):
            resource_handles.append(handle)
        _close_sink(errlog)
        has_deferred = False
        for tool in listing.tools:
            exposure = effective_exposure(server, tool.name)
            if exposure == "hidden":
                continue
            wrapped = _make_tool(handle, tool)
            if exposure == "direct":
                direct.append(wrapped)
            else:
                deferred.append(wrapped)
                has_deferred = True

        if has_deferred:
            deferred_servers.append((server.name, server.description))

    try:
        direct_tools = merge_tools(direct)
        deferred_tools = merge_tools(deferred)
    except ToolError as exc:
        for handle in handles:
            await handle.aclose()
        raise McpConfigError(str(exc)) from exc

    return McpSession(
        tools=tuple(tool for tool in direct_tools if isinstance(tool, McpTool)),
        deferred=tuple(tool for tool in deferred_tools if isinstance(tool, McpTool)),
        deferred_servers=tuple(deferred_servers),
        handles=tuple(handles),
        resource_handles=tuple(resource_handles),
        errors=tuple(errors),
    )


def effective_exposure(server: McpServer, tool_name: str) -> Exposure:
    """单个工具的实际暴露方式：精确名 > 通配符（按配置顺序）> server 默认。"""
    if tool_name in server.tool_exposure:
        return server.tool_exposure[tool_name]
    for pattern, exposure in server.tool_exposure.items():
        if fnmatch.fnmatch(tool_name, pattern):
            return exposure
    return server.exposure


async def supports_resources(client: Any, timeout: float) -> bool:
    """探测 server 是否实现了 `resources/list`。

    不支持、超时或其它错误都当作「没有资源」：工具仍然可用，只是不注册资源工具。
    """
    try:
        await asyncio.wait_for(client.list_resources(cursor=None), timeout)
    except Exception:
        return False
    return True


TOOL_LIST_CHANGED = "notifications/tools/list_changed"
RESOURCE_LIST_CHANGED = "notifications/resources/list_changed"


def _notification_handler(flags: ChangeFlags) -> Any:
    """把 server 的 list_changed 通知记进标记；其它通知忽略。"""

    async def handler(message: Any) -> None:
        method = getattr(message, "method", "")
        if method == TOOL_LIST_CHANGED:
            flags.tools = True
        elif method == RESOURCE_LIST_CHANGED:
            flags.resources = True

    return handler


async def _close_stack(stack: AsyncExitStack) -> None:
    try:
        await stack.aclose()
    except Exception:
        pass


def _server_label(handle: McpServerHandle) -> tuple[str, str]:
    description = handle.server.description if handle.server is not None else ""
    return (handle.name, description)


async def _open_client(
    server: McpServer,
    stack: AsyncExitStack,
    errlog: Any = None,
    flags: ChangeFlags | None = None,
    interactive: bool = True,
) -> Any:
    handler = _notification_handler(flags) if flags is not None else None
    if server.command is not None:
        # env 由 MCP SDK 收窄：`get_default_environment()` 只继承 PATH / SYSTEMROOT /
        # APPDATA 等安全变量，再并上 mcp.json 的 env；宿主其余变量（API key）不传。
        params = StdioServerParameters(
            command=server.command,
            args=list(server.args),
            env=server.env or None,
            cwd=server.cwd,
        )
        transport = stdio_client(params, errlog=errlog) if errlog else params
        return await stack.enter_async_context(
            Client(transport, message_handler=handler)
        )

    assert server.url is not None
    if server.oauth is not None:
        http_client = await stack.enter_async_context(
            oauth.http_client(
                server.name, server.url, server.oauth, interactive=interactive
            )
        )
        transport = streamable_http_client(server.url, http_client=http_client)
        return await stack.enter_async_context(
            Client(transport, message_handler=handler)
        )
    if server.headers:
        http_client = await stack.enter_async_context(
            httpx2.AsyncClient(headers=server.headers)
        )
        transport = streamable_http_client(server.url, http_client=http_client)
        return await stack.enter_async_context(
            Client(transport, message_handler=handler)
        )
    return await stack.enter_async_context(Client(server.url, message_handler=handler))


def _make_tool(handle: McpServerHandle, tool: Any) -> McpTool:
    annotations = getattr(tool, "annotations", None)
    read_only = bool(getattr(annotations, "read_only_hint", False))
    description = getattr(tool, "description", None) or ""
    return McpTool(
        spec=ToolSpec(
            name=make_tool_name(handle.name, tool.name),
            description=f"[{handle.name}] {description}".strip(),
            parameters=tool.input_schema,
        ),
        execution_mode="parallel" if read_only else "serial",
        remote_name=tool.name,
        handle=handle,
        server=handle.name,
    )


def _parse_server(config_path: Path, name: str, payload: Any) -> McpServer:
    where = f"{config_path}: server {name!r}"
    if not isinstance(name, str) or not name.strip():
        raise McpConfigError(f"{config_path}: server name must be a non-empty string")
    if not isinstance(payload, dict):
        raise McpConfigError(f"{where} must be an object")

    unknown = set(payload) - _SERVER_KEYS
    if unknown:
        raise McpConfigError(f"{where} has unknown keys: {sorted(unknown)}")

    command = payload.get("command")
    url = payload.get("url")
    if (command is None) == (url is None):
        raise McpConfigError(f"{where} needs exactly one of 'command' or 'url'")
    if command is not None and (not isinstance(command, str) or not command.strip()):
        raise McpConfigError(f"{where}: 'command' must be a non-empty string")
    if url is not None and not _is_http_url(url):
        raise McpConfigError(f"{where}: 'url' must start with http:// or https://")

    args = payload.get("args", [])
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise McpConfigError(f"{where}: 'args' must be a list of strings")

    cwd = payload.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise McpConfigError(f"{where}: 'cwd' must be a string")

    enabled = payload.get("enabled", True)
    if not isinstance(enabled, bool):
        raise McpConfigError(f"{where}: 'enabled' must be a boolean")

    timeout = payload.get("timeout", DEFAULT_TIMEOUT)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or timeout <= 0
    ):
        raise McpConfigError(f"{where}: 'timeout' must be a positive number")

    exposure = payload.get("exposure", DEFAULT_EXPOSURE)
    if exposure not in EXPOSURES:
        raise McpConfigError(
            f"{where}: 'exposure' must be one of {sorted(EXPOSURES)}"
        )

    tool_exposure_raw = payload.get("toolExposure", {})
    if not isinstance(tool_exposure_raw, dict):
        raise McpConfigError(f"{where}: 'toolExposure' must be an object")
    tool_exposure: dict[str, Exposure] = {}
    for pattern, value in tool_exposure_raw.items():
        if value not in EXPOSURES:
            raise McpConfigError(
                f"{where}: 'toolExposure[{pattern}]' must be one of {sorted(EXPOSURES)}"
            )
        tool_exposure[str(pattern)] = value

    description = payload.get("description", "")
    if not isinstance(description, str):
        raise McpConfigError(f"{where}: 'description' must be a string")

    headers = _str_map(where, "headers", payload.get("headers"))

    return McpServer(
        name=name.strip(),
        command=command,
        args=tuple(args),
        env=_str_map(where, "env", payload.get("env")),
        cwd=cwd,
        url=url,
        headers=headers,
        oauth=_parse_oauth(where, payload.get("oauth"), url, headers),
        enabled=enabled,
        timeout=float(timeout),
        exposure=exposure,
        tool_exposure=tool_exposure,
        description=description,
    )


_OAUTH_KEYS = frozenset(
    {"clientId", "clientSecret", "scope", "clientName", "callbackPort", "timeout"}
)


def _parse_oauth(
    where: str,
    value: Any,
    url: str | None,
    headers: dict[str, str],
) -> oauth.OAuthSettings | None:
    if value is None:
        return None
    if url is None:
        raise McpConfigError(f"{where}: 'oauth' only applies to 'url' servers")
    if not isinstance(value, dict):
        raise McpConfigError(f"{where}: 'oauth' must be an object")

    unknown = set(value) - _OAUTH_KEYS
    if unknown:
        raise McpConfigError(f"{where}: 'oauth' has unknown keys: {sorted(unknown)}")

    if any(name.lower() == "authorization" for name in headers):
        raise McpConfigError(
            f"{where}: use either 'headers.Authorization' or 'oauth', not both"
        )

    strings: dict[str, str | None] = {}
    for key in ("clientId", "clientSecret", "scope", "clientName"):
        item = value.get(key)
        if item is not None and (not isinstance(item, str) or not item.strip()):
            raise McpConfigError(f"{where}: 'oauth.{key}' must be a non-empty string")
        strings[key] = item.strip() if isinstance(item, str) else None

    callback_port = value.get("callbackPort", 0)
    if isinstance(callback_port, bool) or not isinstance(callback_port, int):
        raise McpConfigError(f"{where}: 'oauth.callbackPort' must be an integer")
    if not 0 <= callback_port <= 65535:
        raise McpConfigError(f"{where}: 'oauth.callbackPort' must be 0-65535")

    oauth_timeout = value.get("timeout", oauth.DEFAULT_TIMEOUT)
    if (
        isinstance(oauth_timeout, bool)
        or not isinstance(oauth_timeout, (int, float))
        or oauth_timeout <= 0
    ):
        raise McpConfigError(f"{where}: 'oauth.timeout' must be a positive number")

    return oauth.OAuthSettings(
        client_id=strings["clientId"],
        client_secret=strings["clientSecret"],
        scope=strings["scope"],
        client_name=strings["clientName"] or "Gradus",
        callback_port=callback_port,
        timeout=float(oauth_timeout),
    )


def _str_map(where: str, key: str, value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(item, str) for item in value.values()
    ):
        raise McpConfigError(f"{where}: '{key}' must be an object of strings")
    return {str(k): v for k, v in value.items()}


def _is_http_url(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(("http://", "https://"))
