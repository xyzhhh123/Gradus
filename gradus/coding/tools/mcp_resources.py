"""MCP 资源工具：`list_mcp_resources` / `list_mcp_resource_templates` / `read_mcp_resource`。

名字与参数对齐 Codex / Pi，模型不用重新学：

- 不传 `server` 时列出所有已连 server 的资源，并汇总各 server 的错误；
- 传 `server` 时只列一页，`cursor` 续下一页；
- 读取必须同时给 `server` 与 `uri`；文本直接返回，二进制落临时文件并返回路径。

只负责资源读取，不负责连接（`mcp.py`）、权限（`permissions.py`）。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import tempfile
from collections.abc import Sequence
from typing import Any

from ...agent.base import AgentTool
from ...ai.base import ToolSpec
from ..mcp import McpServerHandle, clip_result
from ..registry import FunctionTool

LIST_PARAMETERS = {
    "type": "object",
    "properties": {
        "server": {
            "type": "string",
            "description": "MCP server name. Omit to list every connected server.",
        },
        "cursor": {
            "type": "string",
            "description": "Opaque cursor from a previous call with the same server.",
        },
    },
    "additionalProperties": False,
}

READ_PARAMETERS = {
    "type": "object",
    "properties": {
        "server": {
            "type": "string",
            "description": "MCP server name exactly as configured.",
        },
        "uri": {
            "type": "string",
            "description": "Resource URI, as returned by list_mcp_resources.",
        },
    },
    "required": ["server", "uri"],
    "additionalProperties": False,
}

LIST_RESOURCES_SPEC = ToolSpec(
    name="list_mcp_resources",
    description=(
        "List resources provided by MCP servers. Resources are files, schemas, or other "
        "data a server exposes for context. Prefer them over guessing or web search."
    ),
    parameters=LIST_PARAMETERS,
)

LIST_TEMPLATES_SPEC = ToolSpec(
    name="list_mcp_resource_templates",
    description=(
        "List parameterized resource templates provided by MCP servers. Fill the "
        "template to get a URI, then call read_mcp_resource."
    ),
    parameters=LIST_PARAMETERS,
)

READ_RESOURCE_SPEC = ToolSpec(
    name="read_mcp_resource",
    description="Read one resource from an MCP server by server name and resource URI.",
    parameters=READ_PARAMETERS,
)


def create_mcp_resource_tools(
    handles: Sequence[McpServerHandle],
) -> tuple[AgentTool, ...]:
    """按已连上的 server 生成三个只读资源工具。"""

    async def list_resources(arguments: dict[str, Any]) -> str:
        payload = await _list(handles, arguments, "resources")
        return clip_result(json.dumps(payload, ensure_ascii=False, indent=2))

    async def list_templates(arguments: dict[str, Any]) -> str:
        payload = await _list(handles, arguments, "resourceTemplates")
        return clip_result(json.dumps(payload, ensure_ascii=False, indent=2))

    async def read_resource(arguments: dict[str, Any]) -> str:
        server_name = _string_arg(arguments, "server")
        uri = _string_arg(arguments, "uri")
        if not server_name:
            raise ValueError("server is required")
        if not uri:
            raise ValueError("uri is required")

        handle = _find(handles, server_name)
        result = await handle.read_resource(uri)
        return clip_result(_render_contents(result.contents))

    return (
        FunctionTool(LIST_RESOURCES_SPEC, list_resources, execution_mode="parallel"),
        FunctionTool(LIST_TEMPLATES_SPEC, list_templates, execution_mode="parallel"),
        FunctionTool(READ_RESOURCE_SPEC, read_resource, execution_mode="parallel"),
    )


async def _list(
    handles: Sequence[McpServerHandle], arguments: dict[str, Any], kind: str
) -> dict[str, Any]:
    server_name = _string_arg(arguments, "server")
    cursor = _string_arg(arguments, "cursor")

    if server_name:
        handle = _find(handles, server_name)
        result = await _page(handle, kind, cursor)
        payload: dict[str, Any] = {
            "server": handle.name,
            kind: [_listed(handle.name, item, kind) for item in _items(result, kind) if _visible(item)],
        }
        next_cursor = getattr(result, "next_cursor", None)
        if next_cursor:
            payload["nextCursor"] = next_cursor
        return payload

    if cursor:
        raise ValueError("cursor can only be used when a server is specified")

    entries: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for handle in sorted(handles, key=lambda item: item.name):
        page_cursor: str | None = None
        try:
            while True:
                result = await _page(handle, kind, page_cursor)
                entries.extend(
                    _listed(handle.name, item, kind)
                    for item in _items(result, kind)
                    if _visible(item)
                )
                page_cursor = getattr(result, "next_cursor", None)
                if not page_cursor:
                    break
        except Exception as exc:  # 单个 server 不支持或出错，不影响其它 server
            errors.append({"server": handle.name, "error": f"{type(exc).__name__}: {exc}"})

    payload = {kind: entries}
    if errors:
        payload["errors"] = errors
    return payload


async def _page(handle: McpServerHandle, kind: str, cursor: str | None) -> Any:
    if kind == "resources":
        return await handle.list_resources(cursor)
    return await handle.list_resource_templates(cursor)


def _items(result: Any, kind: str) -> list[Any]:
    if kind == "resources":
        return list(result.resources)
    return list(result.resource_templates)


def _listed(server: str, item: Any, kind: str) -> dict[str, Any]:
    data: dict[str, Any] = {"server": server, "name": item.name}
    if kind == "resources":
        data["uri"] = item.uri
    else:
        data["uriTemplate"] = item.uri_template

    for key, attr in (
        ("title", "title"),
        ("description", "description"),
        ("mimeType", "mime_type"),
        ("size", "size"),
    ):
        value = getattr(item, attr, None)
        if value is not None:
            data[key] = value
    return data


def _visible(item: Any) -> bool:
    """MCP App 的界面资源（`ui://` / `mcp-app`）只有宿主能渲染，跳过。"""
    uri = getattr(item, "uri", None) or getattr(item, "uri_template", None) or ""
    mime = getattr(item, "mime_type", None) or ""
    return not (uri.startswith("ui://") or "mcp-app" in mime.lower())


def _render_contents(contents: Sequence[Any]) -> str:
    if not contents:
        return "(empty resource)"

    labelled = len(contents) > 1
    parts: list[str] = []
    for item in contents:
        prefix = f"{item.uri}: " if labelled else ""
        text = getattr(item, "text", None)
        if text is not None:
            parts.append(f"{prefix}{text}")
            continue
        data = base64.b64decode(getattr(item, "blob", ""))
        mime = getattr(item, "mime_type", None)
        path = _spill_bytes(data, mime)
        parts.append(f"{prefix}[binary {mime or 'unknown'}: {path}]")
    return "\n".join(parts)


def _spill_bytes(data: bytes, mime: str | None) -> str:
    suffix = mimetypes.guess_extension(mime) if mime else None
    handle = tempfile.NamedTemporaryFile(
        "wb", suffix=suffix or "", prefix="gradus-mcp-", delete=False
    )
    with handle:
        handle.write(data)
    return handle.name


def _find(handles: Sequence[McpServerHandle], name: str) -> McpServerHandle:
    for handle in handles:
        if handle.name == name:
            return handle
    available = ", ".join(handle.name for handle in handles)
    detail = f"; available: {available}" if available else ""
    raise ValueError(f'unknown MCP server "{name}"{detail}')


def _string_arg(arguments: dict[str, Any], key: str) -> str | None:
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value.strip() or None
