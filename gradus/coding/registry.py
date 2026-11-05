"""工具注册表与自定义工具适配。

负责：
- 把内置工具、MCP 工具、自定义工具放进同一个序列，按注册顺序交给 `LoopConfig.tools`；
- 在装配阶段做重名校验，启动即报错，不用等到第一次运行；
- 按暴露方式分组：`direct` 直接声明、`deferred` 交给 `tool_search`、`hidden` 不暴露；
- 把普通 Python 函数包成 `AgentTool`，让自定义工具不用手写一个类。

不负责：权限判断（`permissions.py`）、MCP 连接（`mcp.py`）。
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from ..agent.base import AgentTool, ExecutionMode, ToolUpdateCallback
from ..agent.types import ToolResult
from ..ai.base import ToolSpec

ToolHandler = Callable[[dict[str, Any]], str | Awaitable[str]]

#: 工具的暴露方式：直接声明、按需加载、完全不暴露。
Exposure = Literal["direct", "deferred", "hidden"]

#: 暴露方式的合法取值，供配置校验复用。
EXPOSURES = frozenset({"direct", "deferred", "hidden"})


class ToolError(ValueError):
    """工具装配失败，例如重名或名字为空。"""


class ToolRegistry:
    """按名字去重的工具集合，保持注册顺序。"""

    def __init__(self, tools: Iterable[AgentTool] = ()) -> None:
        self._tools: list[AgentTool] = []
        self._names: set[str] = set()
        for tool in tools:
            self.add(tool)

    def add(self, tool: AgentTool) -> None:
        name = tool.spec.name
        if not name:
            raise ToolError("tool name is empty")
        if name in self._names:
            raise ToolError(f"duplicate tool name: {name}")
        self._tools.append(tool)
        self._names.add(name)

    @property
    def tools(self) -> tuple[AgentTool, ...]:
        return tuple(self._tools)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._names)

    def __len__(self) -> int:
        return len(self._tools)


class ToolCatalog:
    """按暴露方式分组的工具集合，保持注册顺序。

    与 `ToolRegistry` 的区别：注册表只管去重与顺序，目录还决定工具是否声明给模型。
    名字在全部分组之间唯一，同一个工具不能既 `direct` 又 `deferred`。
    """

    def __init__(self, tools: Iterable[tuple[AgentTool, Exposure]] = ()) -> None:
        self._direct: list[AgentTool] = []
        self._deferred: list[AgentTool] = []
        self._hidden: list[AgentTool] = []
        self._names: set[str] = set()
        for tool, exposure in tools:
            self.add(tool, exposure)

    def add(self, tool: AgentTool, exposure: Exposure = "direct") -> None:
        name = tool.spec.name
        if not name:
            raise ToolError("tool name is empty")
        if name in self._names:
            raise ToolError(f"duplicate tool name: {name}")
        if exposure == "direct":
            self._direct.append(tool)
        elif exposure == "deferred":
            self._deferred.append(tool)
        elif exposure == "hidden":
            self._hidden.append(tool)
        else:
            raise ToolError(f"unknown exposure: {exposure}")
        self._names.add(name)

    def add_all(self, tools: Iterable[AgentTool], exposure: Exposure = "direct") -> None:
        for tool in tools:
            self.add(tool, exposure)

    @property
    def direct(self) -> tuple[AgentTool, ...]:
        return tuple(self._direct)

    @property
    def deferred(self) -> tuple[AgentTool, ...]:
        return tuple(self._deferred)

    @property
    def hidden(self) -> tuple[AgentTool, ...]:
        return tuple(self._hidden)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._names)

    def __len__(self) -> int:
        return len(self._names)


@dataclass(frozen=True)
class FunctionTool:
    """把普通函数适配成 `AgentTool`。

    处理函数收到已解析的参数字典，返回字符串；返回 awaitable 时会被 await。
    抛异常交给循环转成错误结果，这里不吞。
    """

    spec: ToolSpec
    handler: ToolHandler
    execution_mode: ExecutionMode = "serial"

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        result = self.handler(arguments)
        if inspect.isawaitable(result):
            result = await result
        return ToolResult(str(result))


def merge_tools(*groups: Sequence[AgentTool]) -> tuple[AgentTool, ...]:
    """按顺序合并多组工具；重名直接抛 `ToolError`。"""
    registry = ToolRegistry()
    for group in groups:
        for tool in group:
            registry.add(tool)
    return registry.tools
