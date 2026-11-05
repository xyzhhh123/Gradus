"""codemode 子进程执行器。

独立脚本，不 import gradus；由 `codemode.py` 用 `python -I` 启动。

协议是一行一个 JSON：

- 脚本 -> 宿主：`{"type":"call","id":1,"name":"read_file","arguments":{...}}`
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
    """脚本侧的 `tools.<name>(...)` 代理；每次调用走一次宿主往返。"""

    def __init__(self):
        self._next = 0

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def call(**arguments):
            return self._call(name, arguments)

        return call

    def _call(self, name, arguments):
        self._next += 1
        _send(
            {
                "type": "call",
                "id": self._next,
                "name": name,
                "arguments": arguments,
            }
        )
        line = sys.__stdin__.readline()
        if not line:
            raise RuntimeError("host closed the bridge")
        reply = json.loads(line)
        if reply.get("is_error"):
            raise RuntimeError(reply.get("content") or "tool failed")
        return reply.get("content", "")


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

    safe = {name: getattr(builtins, name) for name in ALLOWED_BUILTINS}
    safe["__build_class__"] = builtins.__build_class__
    namespace = {
        "__builtins__": safe,
        "__name__": "__codemode__",
        "tools": Tools(),
        "text": text,
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
