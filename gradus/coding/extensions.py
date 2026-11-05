"""扩展与技能：从项目目录动态加载工具、命令与技能。

目录约定：`<workspace>/.gradus/extensions/`

- `tools/*.py`：模块里定义 `create_tool()`，返回一个 `AgentTool` 或它们的序列；
- `commands/*.md`：文件名即命令名，正文是提示词，`$ARGUMENTS` 替换成命令参数；
- `skills/*/SKILL.md`：开头用 `---` 包住 `name` / `description`，正文由模型按需读取。

约束：
- 扩展会执行任意 Python，必须先信任；信任记录与 `.gradus/mcp.json` 共用；
- 加载在装配时一次完成，不做热加载，改动后重启生效；
- 单个文件失败只记警告，不影响其它扩展；
- 同名时后加载的覆盖前面的（按文件名排序）；与内置工具重名由 `session._build_tools` 跳过并告警。

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
    """一个技能：系统提示里只放名字与描述，正文按需读取。"""

    name: str
    description: str
    path: Path

    def body(self) -> str:
        """读正文并去掉 frontmatter。"""
        text = self.path.read_text(encoding="utf-8")
        return _split_frontmatter(text)[1]


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


def load(
    workspace: str | Path,
    *,
    enabled: bool = True,
    project_dir: str | Path | None = None,
) -> Extensions:
    """扫描项目扩展目录；`enabled=False` 时直接返回空结果。"""
    if not enabled:
        return Extensions()

    folder = (
        Path(project_dir)
        if project_dir is not None
        else default_project_dir(workspace)
    )

    warnings: list[str] = []
    tools: dict[str, AgentTool] = {}
    commands: dict[str, Command] = {}
    skills: dict[str, Skill] = {}

    _load_tools(folder / TOOLS_DIR, tools, warnings)
    _load_commands(folder / COMMANDS_DIR, commands, warnings)
    _load_skills(folder / SKILLS_DIR, skills, warnings)

    return Extensions(
        tools=tuple(tools.values()),
        commands=tuple(commands.values()),
        skills=tuple(skills.values()),
        warnings=tuple(warnings),
    )


def skills_section(skills: Iterable[Skill]) -> str:
    """系统提示里的技能清单；没有技能时返回空串。"""
    items = list(skills)
    if not items:
        return ""

    lines = ["Skills (read the file when the description matches):"]
    lines.extend(f"- {skill.name}: {skill.description} ({skill.path})" for skill in items)
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
            name = str(meta.get("name", "")).strip() or child.name
            skills[name] = Skill(name, description, path)
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
