"""`use_skill`：把某个技能的正文取给模型。

技能清单只放名字与描述（`extensions.skills_section`），正文由这个工具按需提供——
比让模型拼路径去 `read_file` 更可控：路径不会失效、用了哪个技能可记录、超长正文能分段读。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from ...agent.base import AgentTool, ToolUpdateCallback
from ...agent.types import ToolCapability, ToolResult
from ...ai.base import ToolSpec

TOOL_NAME = "use_skill"
SKILL_CAPABILITY = ToolCapability(effects=("read",), result_kind="evidence")


def use_skill_spec(limit: int) -> ToolSpec:
    return ToolSpec(
        name=TOOL_NAME,
        description=(
            "Read a skill's full instructions. The skill list in the system prompt shows "
            "names and one-line descriptions; call this when a description matches what "
            "you are about to do. Long skills come back in chunks: pass `offset` to "
            "continue."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Skill name exactly as listed in the system prompt",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": f"Character offset to continue from (default 0, chunk {limit})",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    )


class UseSkillTool:
    """按名字取技能正文；每次调用都记一次使用。"""

    execution_mode = "serial"
    capability = SKILL_CAPABILITY

    def __init__(
        self,
        skills: Callable[[], Sequence[Any]],
        usage: dict[str, int],
    ) -> None:
        from ..extensions import BODY_LIMIT

        self._skills = skills
        self._usage = usage
        self._limit = BODY_LIMIT
        self.spec = use_skill_spec(BODY_LIMIT)

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        name = str(arguments.get("name") or "").strip()
        if not name:
            return ToolResult("name is required", is_error=True)

        items = list(self._skills())
        skill = next((item for item in items if item.name == name), None)
        if skill is None:
            available = ", ".join(item.name for item in items) or "(none)"
            return ToolResult(f"unknown skill: {name}; available: {available}", is_error=True)
        if not getattr(skill, "model_invocable", True):
            # 这类技能只能由用户 `/skill:name` 触发：模型不该自己把它翻出来。
            return ToolResult(
                f"skill {name} can only be started by the user via /skill:{name}",
                is_error=True,
            )

        offset = arguments.get("offset") or 0
        try:
            offset = max(0, int(offset))
        except (TypeError, ValueError):
            return ToolResult("offset must be an integer", is_error=True)

        try:
            chunk, total = skill.read(offset)
        except OSError as exc:
            return ToolResult(f"cannot read skill {name}: {exc}", is_error=True)

        self._usage[name] = self._usage.get(name, 0) + 1

        header = f'<skill name="{name}">'
        footer = "</skill>"
        if offset + len(chunk) < total:
            end = offset + len(chunk)
            footer = (
                f"</skill>\n[skill {name}: {end}/{total} chars; "
                f'continue with use_skill(name="{name}", offset={end})]'
            )
        return ToolResult(f"{header}\n{chunk}\n{footer}")
