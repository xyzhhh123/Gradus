"""codemode 子进程执行器。

独立脚本，不 import gradus；由 `codemode.py` 用 `python -I` 启动。

协议是一行一个 JSON：

- 脚本 -> 宿主：`{"type":"call","id":1,"name":"read_file","arguments":{...}}`
- 脚本 -> 宿主：`{"type":"search","id":2,"query":"issues","limit":8}`
- 脚本 -> 宿主：`{"type":"describe","id":3,"name":"mcp__github__list_issues"}`
- 宿主 -> 脚本：`{"type":"result","id":1,"content":"...","is_error":false}`
- 脚本结束：`{"type":"done","outputs":["..."],"returned":"..."}`
- 脚本失败：`{"type":"error","message":"..."}`

只做 API 隔离：Python 进程仍是同一个 OS 用户，能直接读写文件的话拦不住。
"""

import ast
import builtins
import contextlib
import io
import json
import sys

MAX_TEXT_CHARS = 200_000


class ToolValue(str):
    """A string-compatible nested tool result with structured fields attached."""

    def __new__(cls, content, structured_content=None, details=None):
        value = str.__new__(cls, content)
        value.structured_content = structured_content
        value.details = details
        return value

BLOCKED_NAMES = frozenset(
    {
        "__import__",
        "breakpoint",
        "compile",
        "delattr",
        "dir",
        "eval",
        "exec",
        "exit",
        "getattr",
        "globals",
        "help",
        "input",
        "locals",
        "open",
        "quit",
        "setattr",
        "vars",
    }
)

ALLOWED_BUILTINS = (
    "ArithmeticError",
    "AssertionError",
    "Exception",
    "IndexError",
    "KeyError",
    "NotImplementedError",
    "RuntimeError",
    "StopIteration",
    "TypeError",
    "ValueError",
    "ZeroDivisionError",
    "abs",
    "all",
    "any",
    "bool",
    "bytes",
    "dict",
    "enumerate",
    "filter",
    "float",
    "format",
    "frozenset",
    "int",
    "isinstance",
    "issubclass",
    "iter",
    "len",
    "list",
    "map",
    "max",
    "min",
    "next",
    "object",
    "pow",
    "range",
    "repr",
    "reversed",
    "round",
    "set",
    "slice",
    "sorted",
    "str",
    "sum",
    "tuple",
    "type",
    "zip",
)


def _send(payload):
    sys.__stdout__.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.__stdout__.flush()


class Tools:
    """脚本侧的 `tools.<name>(...)` / `tools[name](...)` 代理。"""

    def __init__(self):
        self._next = 0

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def call(**arguments):
            return self._call(name, arguments)

        return call

    def __getitem__(self, name):
        if not isinstance(name, str) or not name or name.startswith("_"):
            raise KeyError(name)

        def call(**arguments):
            return self._call(name, arguments)

        return call

    def _request(self, kind, payload):
        self._next += 1
        message = {"type": kind, "id": self._next}
        message.update(payload)
        _send(message)
        line = sys.__stdin__.readline()
        if not line:
            raise RuntimeError("host closed the bridge")
        reply = json.loads(line)
        if reply.get("is_error"):
            raise RuntimeError(reply.get("content") or "tool failed")
        content = reply.get("content", "")
        if kind == "call":
            return ToolValue(
                content,
                reply.get("structured_content"),
                reply.get("details"),
            )
        return content

    def _call(self, name, arguments):
        return self._request("call", {"name": name, "arguments": arguments})

    def search(self, query, limit):
        return self._request("search", {"query": query, "limit": limit})

    def describe(self, name):
        return self._request("describe", {"name": name})


def check(tree):
    """拒绝 import、危险内建名与下划线开头的属性访问。"""
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise ValueError("import is not allowed")
        if isinstance(node, ast.Name) and node.id in BLOCKED_NAMES:
            raise ValueError(f"{node.id} is not allowed")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ValueError(f"attribute {node.attr} is not allowed")


def format_value(value):
    if value is None:
        return None
    if isinstance(value, ToolValue) and value.structured_content is not None:
        value = value.structured_content
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except TypeError:
        return repr(value)


def main():
    source_path = sys.argv[1]
    with open(source_path, encoding="utf-8") as handle:
        source = handle.read()

    body = "\n".join("    " + line for line in source.splitlines()) or "    pass"
    module = "def __script__():\n" + body
    try:
        tree = ast.parse(module)
        check(tree)
    except (SyntaxError, ValueError) as exc:
        _send({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        return 1

    outputs = []
    written = 0

    def text(value):
        nonlocal written
        item = str(value)
        if written >= MAX_TEXT_CHARS:
            return
        item = item[: MAX_TEXT_CHARS - written]
        written += len(item)
        outputs.append(item)

    bridge = Tools()

    def search_tools(query, limit=8):
        if not isinstance(query, str) or not query.strip():
            raise ValueError("search_tools query must not be empty")
        return bridge.search(query, limit)

    def describe_tool(name):
        if not isinstance(name, str) or not name:
            raise ValueError("describe_tool name must not be empty")
        return bridge.describe(name)

    safe = {name: getattr(builtins, name) for name in ALLOWED_BUILTINS}
    safe["__build_class__"] = builtins.__build_class__
    namespace = {
        "__builtins__": safe,
        "__name__": "__codemode__",
        "tools": bridge,
        "text": text,
        "search_tools": search_tools,
        "describe_tool": describe_tool,
    }

    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            exec(compile(tree, "<codemode>", "exec"), namespace)
            returned = namespace["__script__"]()
    except Exception as exc:
        _send({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        return 1

    _send(
        {
            "type": "done",
            "outputs": outputs,
            "returned": format_value(returned),
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
