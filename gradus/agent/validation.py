"""工具参数的 JSON Schema 校验。

只做一件事：把模型给出的 `arguments` 与工具声明的 `ToolSpec.parameters`
对照，返回可读的错误消息。循环拿到消息后生成错误工具结果，不进入工具。

依赖方向：本模块 -> jsonschema + 标准库。
"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError


def validate_arguments(
    tool_name: str,
    parameters: dict[str, Any],
    arguments: dict[str, Any],
) -> str | None:
    """校验一个工具调用的参数。

    返回 `None` 表示通过；否则返回以工具名开头的错误消息。
    schema 本身非法时不阻塞运行，但会把问题写进错误消息。
    """
    if not parameters:
        return None

    try:
        Draft202012Validator.check_schema(parameters)
    except SchemaError as exc:
        return f"invalid parameters schema for {tool_name}: {exc.message}"

    validator = Draft202012Validator(parameters)
    errors = sorted(
        validator.iter_errors(arguments),
        key=lambda error: tuple(
            str(part) for part in error.absolute_path
        ),
    )
    if not errors:
        return None

    details = "; ".join(_format_error(error) for error in errors)
    return f"invalid arguments for {tool_name}: {details}"


def _format_error(error: ValidationError) -> str:
    path = ".".join(str(part) for part in error.absolute_path)
    if not path:
        return error.message
    return f"{path}: {error.message}"
