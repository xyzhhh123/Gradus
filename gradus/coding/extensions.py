"""扩展与技能：从用户目录与项目目录动态加载工具、命令与技能。

两个根目录：

- 用户级 `~/.gradus/extensions/`：总是加载，不需要信任；只认 `skills/` 与 `commands/`；
- 项目级 `<workspace>/.gradus/extensions/`：需要信任，可放 `tools/` 执行代码。

形态：

- `tools/*.py`：模块里定义 `create_tool()`，返回一个 `AgentTool` 或它们的序列；
- `commands/*.md`：文件名即命令名，正文是提示词，`$ARGUMENTS` 替换成命令参数；
- `skills/*/SKILL.md`：开头用 `---` 包住 `name` / `description`；正文由 `use_skill` 工具按需提供。

约束：
- 用户级扩展是"用户自己写的"：不执行工具代码，只放文本类扩展；
- 项目级扩展会执行任意 Python，必须先信任；信任记录与 `.gradus/mcp.json` 共用；
- **同名时用户级优先**：项目里的技能不能覆盖你自己的技能，冲突会给警告；
- 加载在装配时一次完成，不做热加载，改动后 `/reload` 或重启生效；
- 单个文件失败只记警告，不影响其它扩展；与内置工具重名由 `session._build_tools` 跳过并告警。

不负责：工具权限（`permissions.py`）、系统提示拼装（`session.py`）。
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..agent.base import AgentTool

#: 扩展目录，相对 workspace。
EXTENSIONS_RELPATH = Path(".gradus") / "extensions"

#: 技能描述在 frontmatter 里的长度上限；索引里再压到 `INDEX_DESCRIPTION_LIMIT`。
DESCRIPTION_LIMIT = 1024
#: 技能清单里每行描述的字符上限：名字必须完整，描述可以短。
INDEX_DESCRIPTION_LIMIT = 200
#: `use_skill` 单次返回的正文上限；超出用 `offset` 续读。
BODY_LIMIT = 8_000

COMMANDS_DIR = "commands"
SKILLS_DIR = "skills"
TOOLS_DIR = "tools"
SKILL_FILE = "SKILL.md"


class ExtensionError(ValueError):
    """单个扩展无法加载。"""


class UnknownCommand(ValueError):
    """输入以 `/` 开头，但不是已加载的命令或技能。"""


@dataclass(frozen=True)
class Command:
    """一条斜杠命令：正文就是替换后的提示词。"""

    name: str
    body: str
    path: Path


@dataclass(frozen=True)
class Skill:
    """一个技能：系统提示里只放名字与描述，正文由 `use_skill` 按需取。"""

    name: str
    description: str
    path: Path
    #: 为 False 时：不进模型索引，`use_skill` 拒绝对它调用；`/skill:` 仍可用。
    model_invocable: bool = True

    @property
    def short_description(self) -> str:
        """索引里用的描述：超长截断，避免一条技能占掉半屏上下文。"""
        if len(self.description) <= INDEX_DESCRIPTION_LIMIT:
            return self.description
        return self.description[:INDEX_DESCRIPTION_LIMIT] + "…"

    def body(self) -> str:
        """读正文并去掉 frontmatter。"""
        text = self.path.read_text(encoding="utf-8")
        body = _split_frontmatter(text)[1].strip()
        # Imported Claude/Codex skills use this placeholder for bundled assets.
        # Gradus has no plugin-root environment variable, so expand it only after
        # the skill has been explicitly loaded.
        root = self.path.parent.resolve().as_posix()
        return body.replace("${CLAUDE_PLUGIN_ROOT}", root).replace(
            "${CLAUDE_SKILL_DIR}", root
        )

    def read(self, offset: int = 0) -> tuple[str, int]:
        """读正文的一段，返回 `(片段, 总长度)`；`offset` 用于续读。"""
        text = self.body()
        return text[offset : offset + BODY_LIMIT], len(text)


@dataclass(frozen=True)
class Extensions:
    """一次加载的结果。"""

    tools: tuple[AgentTool, ...] = ()
    commands: tuple[Command, ...] = ()
    skills: tuple[Skill, ...] = ()
    warnings: tuple[str, ...] = ()

    def command(self, name: str) -> Command | None:
        return next((item for item in self.commands if item.name == name), None)

    def skill(self, name: str) -> Skill | None:
        return next((item for item in self.skills if item.name == name), None)

    def expand(self, text: str) -> str:
        """把 `/命令 参数` 换成提示词；不以 `/` 开头时原样返回。"""
        if not text.startswith("/"):
            return text

        head, _, args = text.partition(" ")
        args = args.strip()
        name = head[1:]

        if name.startswith("skill:"):
            skill = self.skill(name[len("skill:") :])
            if skill is None:
                raise UnknownCommand(f"unknown skill: /{name}")
            body = skill.body()
        else:
            command = self.command(name)
            if command is None:
                raise UnknownCommand(f"unknown command: /{name}")
            body = command.body

        if "$ARGUMENTS" in body:
            return body.replace("$ARGUMENTS", args)
        return f"{body}\n\n{args}" if args else body


def default_project_dir(workspace: str | Path) -> Path:
    """项目级扩展目录；使用前必须确认项目已被信任。"""
    return Path(workspace).expanduser().resolve() / EXTENSIONS_RELPATH


def default_user_dir() -> Path:
    """用户级扩展目录：跨项目可用，不需要信任。"""
    from .config import gradus_home

    return gradus_home() / "extensions"


def load(
    workspace: str | Path,
    *,
    enabled: bool = True,
    project_enabled: bool = True,
    project_dir: str | Path | None = None,
    user_dir: str | Path | None = None,
) -> Extensions:
    """扫描用户级与项目级扩展目录。

    `enabled=False` 表示整体关闭（返回空）。`project_enabled=False` 表示项目未信任：
    项目级整个跳过，**用户级仍然加载**——用户自己写的东西不该因为项目没信任而消失。

    同名冲突时**用户级优先**：项目内容来自别人，不能覆盖你自己定义的技能/命令。
    """

    if not enabled:
        return Extensions()

    project = (
        Path(project_dir)
        if project_dir is not None
        else default_project_dir(workspace)
    )
    user = Path(user_dir) if user_dir is not None else default_user_dir()

    warnings: list[str] = []
    tools: dict[str, AgentTool] = {}
    commands: dict[str, Command] = {}
    skills: dict[str, Skill] = {}

    # 用户级：只认 skills / commands，不执行工具代码。
    _load_commands(user / COMMANDS_DIR, commands, warnings)
    _load_skills(user / SKILLS_DIR, skills, warnings)

    if project_enabled:
        project_skills: dict[str, Skill] = {}
        project_commands: dict[str, Command] = {}
        _load_tools(project / TOOLS_DIR, tools, warnings)
        _load_commands(project / COMMANDS_DIR, project_commands, warnings)
        _load_skills(project / SKILLS_DIR, project_skills, warnings)

        for name, skill in project_skills.items():
            if name in skills:
                warnings.append(
                    f"project skill {name} is shadowed by the user-level one "
                    f"({skills[name].path}); rename it to use both"
                )
                continue
            skills[name] = skill
        for name, command in project_commands.items():
            if name in commands:
                warnings.append(
                    f"project command {name} is shadowed by the user-level one"
                )
                continue
            commands[name] = command

    return Extensions(
        tools=tuple(tools.values()),
        commands=tuple(commands.values()),
        skills=tuple(skills.values()),
        warnings=tuple(warnings),
    )


def skills_section(skills: Iterable[Skill]) -> str:
    """系统提示里的技能清单；没有技能时返回空串。

    只放名字与（截断过的）描述：路径留给 `use_skill`，索引里带路径会诱导模型绕过工具。
    """
    # 只有允许模型调用的进索引：列出来却调不动，只会让模型白试一次。
    items = [skill for skill in skills if skill.model_invocable]
    if not items:
        return ""

    lines = ["Skills (call use_skill to read one):"]
    lines.extend(f"- {skill.name}: {skill.short_description}" for skill in items)
    return "\n".join(lines)


def _load_tools(folder: Path, tools: dict[str, AgentTool], warnings: list[str]) -> None:
    for path in _files(folder, "*.py"):
        try:
            for tool in _load_tool(path):
                tools[tool.spec.name] = tool
        except Exception as exc:
            warnings.append(f"extension tool {path}: {exc}")


def _load_tool(path: Path) -> list[AgentTool]:
    module = _import(path)
    factory = getattr(module, "create_tool", None)
    if not callable(factory):
        raise ExtensionError("missing create_tool()")

    value = factory()
    items = value if isinstance(value, (list, tuple)) else [value]
    if not items:
        raise ExtensionError("create_tool() returned nothing")

    tools: list[AgentTool] = []
    for item in items:
        spec = getattr(item, "spec", None)
        name = getattr(spec, "name", None)
        if not isinstance(name, str) or not name:
            raise ExtensionError("create_tool() must return AgentTool")
        if not callable(getattr(item, "execute", None)):
            raise ExtensionError(f"{name}: missing execute()")
        tools.append(item)
    return tools


def _import(path: Path) -> Any:
    """按文件路径 import，同名文件互不干扰。"""
    module_name = f"_gradus_ext_{path.stem}_{abs(hash(path.as_posix())):x}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ExtensionError("cannot import module")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def _load_commands(
    folder: Path, commands: dict[str, Command], warnings: list[str]
) -> None:
    for path in _files(folder, "*.md"):
        try:
            body = path.read_text(encoding="utf-8").strip()
            if not body:
                raise ExtensionError("empty command")
            commands[path.stem] = Command(path.stem, body, path)
        except Exception as exc:
            warnings.append(f"extension command {path}: {exc}")


def _flag_value(value: object) -> bool:
    """把 frontmatter 里的布尔写成宽容解析：true/yes/on/1 都算真。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "on", "1"}
    return False


def _load_skills(folder: Path, skills: dict[str, Skill], warnings: list[str]) -> None:
    if not folder.is_dir():
        return

    for child in sorted(folder.iterdir()):
        path = child / SKILL_FILE
        if not child.is_dir() or not path.is_file():
            continue
        try:
            meta, _ = _split_frontmatter(path.read_text(encoding="utf-8"))
            description = str(meta.get("description", "")).strip()
            if not description:
                raise ExtensionError("missing description")
            if len(description) > DESCRIPTION_LIMIT:
                raise ExtensionError(
                    f"description is longer than {DESCRIPTION_LIMIT} characters"
                )
            name = str(meta.get("name", "")).strip() or child.name
            disabled = _flag_value(meta.get("disable-model-invocation"))
            skills[name] = Skill(
                name, description, path, model_invocable=not disabled
            )
        except Exception as exc:
            warnings.append(f"extension skill {path}: {exc}")


def _files(folder: Path, pattern: str) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(path for path in folder.glob(pattern) if path.is_file())


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """拆出 `---` 包住的 `key: value` 与正文；没有 frontmatter 时正文原样返回。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text.strip()

    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            meta: dict[str, str] = {}
            for row in lines[1:index]:
                key, sep, value = row.partition(":")
                if sep and key.strip():
                    meta[key.strip()] = value.strip()
            return meta, "\n".join(lines[index + 1 :]).strip()

    return {}, text.strip()
