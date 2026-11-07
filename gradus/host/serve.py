"""Web 后端入口：把 `coding.Session` 的事件流接到 WebSocket 上。

负责：鉴权、会话生命周期、命令分发、确认桥、静态文件。
不负责：装配细节（`coding/session.py`）、工具实现、权限判断。

依赖方向：`host -> coding -> agent -> ai`。本模块只调用 `Session` 的公开方法，
不改动下面三层。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import string
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.responses import HTMLResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from ..ai.envfile import load_env_file
from ..ai.services import SERVICES
from ..coding.config import (
    ConfigError,
    default_config_path,
    load,
    save_active,
    save_models,
    with_active,
    with_choice,
)
from ..coding.mcp import McpConfigError
from ..coding.session import Session, SessionError
from .wire import (
    CAPABILITIES as BASE_CAPABILITIES,
    PICK_CAPABILITY,
    encode_event,
    model_summary,
    snapshot_payload,
)

DIST = Path(__file__).resolve().parents[2] / "web" / "dist"

NO_BUILD_PAGE = """<!doctype html>
<meta charset="utf-8">
<title>Gradus</title>
<body style="font:14px system-ui;background:#151517;color:#d7dbe3;padding:40px">
<h1 style="font-size:16px">前端还没有构建</h1>
<p>在 <code>Gradus/web</code> 下执行 <code>npm install &amp;&amp; npm run build</code>，然后刷新本页。</p>
<p>开发时可以改用 <code>npm run dev</code>，它会代理 <code>/ws</code> 到 127.0.0.1:8765。</p>
</body>
"""


def native_picker_available() -> bool:
    """宿主能否弹系统「选择文件夹」对话框；启动时判定一次。

    和 dsh 的 `directory-picker-auto` 同构：服务不了就明确拒绝，不做近似。
    没有显示会话时（纯 SSH、无 X/Wayland）即使弹了也没人看得见。
    """
    if sys.platform.startswith("linux") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        return False
    try:
        import tkinter  # noqa: F401
    except ImportError:
        return False
    return True


def pick_directory(initial: str = "") -> str | None:
    """弹系统对话框选一个文件夹，返回绝对路径；用户取消返回 `None`。

    tkinter 要求「创建与使用在同一个线程」，所以整段跑在 `asyncio.to_thread`
    的工作线程里：既不阻塞事件循环，也不跨线程碰 Tk 对象。
    """
    from tkinter import Tk, filedialog

    root = Tk()
    root.withdraw()  # 只要对话框，不要那个空的根窗口
    root.attributes("-topmost", True)  # 否则容易开在浏览器后面
    try:
        options: dict[str, Any] = {"parent": root, "title": "选择工作区"}
        if initial:
            options["initialdir"] = initial
        chosen = filedialog.askdirectory(**options)
    finally:
        root.destroy()
    return str(Path(chosen)) if chosen else None


#: 原生目录选择是可选能力：探测不过时不报给前端，前端继续用应用内目录树。
NATIVE_PICKER = native_picker_available()

#: 快照里报出的能力表；`workspace/pick` 缺席时前端不显示「浏览…」。
CAPABILITIES = BASE_CAPABILITIES + ((PICK_CAPABILITY,) if NATIVE_PICKER else ())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="gradus-serve", description="Gradus web host")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workspace", help="默认当前目录")
    parser.add_argument("--config", help="配置文件，默认 ~/.gradus/config.json")
    parser.add_argument("--session-dir", help="会话文件目录，默认 <workspace>/.gradus/sessions")
    parser.add_argument("--mcp", help="mcp.json 路径")
    parser.add_argument("--token", help="访问令牌，默认随机生成")
    parser.add_argument("--confirm-timeout", type=float, default=120.0, help="确认超时秒数")
    return parser.parse_args(argv)


class ConfirmBridge:
    """把 `confirm` 回调转成一次请求 / 响应。

    超时或连接断开一律返回 False：不能在 UI 缺席时默认放行。
    """

    def __init__(self, connection: "Connection", timeout: float) -> None:
        self.connection = connection
        self.timeout = timeout
        self.pending: dict[str, asyncio.Future[bool]] = {}

    async def ask(self, question: str) -> bool:
        confirm_id = f"c-{self.connection.next_id()}"
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.pending[confirm_id] = future
        try:
            await self.connection.send(
                "interaction/confirm", {"id": confirm_id, "question": question}
            )
            return await asyncio.wait_for(future, timeout=self.timeout)
        except asyncio.TimeoutError:
            return False
        finally:
            self.pending.pop(confirm_id, None)

    def resolve(self, confirm_id: str, ok: bool) -> None:
        future = self.pending.get(confirm_id)
        if future is not None and not future.done():
            future.set_result(ok)

    def reject_all(self) -> None:
        """运行结束或连接断开时，把所有等待中的确认按拒绝收尾。

        否则中止的那一轮会在前端留下永远等不到回答的审批面板。
        """
        for future in self.pending.values():
            if not future.done():
                future.set_result(False)
        self.pending.clear()


class Connection:
    """一条 WebSocket 对应一个活动会话，可以新建和切换。"""

    #: 目录树里跳过的名字：体积大或与对话无关。
    SKIP_DIRS = frozenset(
        {".git", "node_modules", ".venv", "__pycache__", ".gradus", "dist", "build"}
    )
    #: 预览文件的读取上限，超出截断。
    MAX_FILE_BYTES = 256 * 1024

    def __init__(self, websocket: WebSocket, settings: Any, args: argparse.Namespace) -> None:
        self.ws = websocket
        self.settings = settings
        self.args = args
        self.seq = 0
        self.ids = 0
        self.send_lock = asyncio.Lock()
        self.session: Session | None = None
        self.run_task: asyncio.Task[None] | None = None
        self.switch_task: asyncio.Task[None] | None = None
        self.workspace = Path(args.workspace).expanduser() if args.workspace else Path.cwd()
        self.confirm = ConfirmBridge(self, args.confirm_timeout)

    def next_id(self) -> int:
        self.ids += 1
        return self.ids

    def reload_settings(self) -> None:
        """按配置文件重读一次，拿到别的连接（或设置页）刚存下的候选表。

        上次在界面上选中的候选（`active`）在这里生效，重启后仍停在同一个模型上。
        读不了就沿用启动时那份：配置坏了不该把已有连接挡在外面。
        """
        try:
            self.settings = with_active(load(overrides={}, config=self.args.config))
        except (ConfigError, OSError):
            pass

    async def send(self, frame_type: str, payload: Any) -> None:
        async with self.send_lock:
            self.seq += 1
            await self.ws.send_json({"seq": self.seq, "type": frame_type, "payload": payload})

    async def run(self) -> None:
        self.reload_settings()
        try:
            await self.connect_session()
        except (SessionError, McpConfigError, ConfigError, OSError) as exc:
            await self.send(
                "session/status",
                {"status": "error", "error": {"kind": "config", "message": str(exc)}},
            )
            await self.ws.close(code=1011)
            return

        try:
            await self.send("session/snapshot", snapshot_payload(self.session, CAPABILITIES))
            await self.send_sessions()
            await self.send_model_state()
            while True:
                command = await self.ws.receive_json()
                await self.handle(command)
        except WebSocketDisconnect:
            pass
        finally:
            await self.shutdown()

    def session_dir(self) -> Path:
        if self.args.session_dir:
            return Path(self.args.session_dir).expanduser()
        return self.workspace / ".gradus" / "sessions"

    @staticmethod
    def discard_empty(session: Session) -> None:
        """空会话不留文件：每次连接都会建一个，否则列表全是噪声。"""
        path = session.session_path
        if path is None or session.messages:
            return
        try:
            path.unlink()
        except OSError:
            pass

    async def connect_session(
        self, workspace: Path | None = None, resume: Path | None = None
    ) -> None:
        """关掉旧会话，连一个新的；`resume` 给出时从会话文件恢复。"""
        if self.session is not None:
            await self.shutdown()
            self.session = None
        if workspace is not None:
            self.workspace = workspace
        # 恢复会话时把工作区交给文件头决定，否则打开别的目录的会话会跑在当前目录。
        self.session = await Session.connect(
            self.settings,
            workspace=None if resume is not None else self.workspace,
            confirm=self.confirm.ask,
            session_dir=self.session_dir(),
            mcp_config=self.args.mcp,
            resume=resume,
        )
        self.workspace = self.session.workspace

    def list_sessions(self) -> list[dict[str, Any]]:
        """扫会话目录，只读头部和第一条用户消息，按修改时间倒序。"""
        directory = self.session_dir()
        if not directory.is_dir():
            return []

        paths = sorted(
            directory.glob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True
        )
        active = self.session.session_path if self.session is not None else None
        sessions: list[dict[str, Any]] = []
        for path in paths[:50]:
            meta = self._session_meta(path)
            if meta is None or meta[2] == 0:
                # 一条消息都没有：不展示，顺手清掉（正在用的那个除外，
                # 删了之后追加写入会重建文件而丢掉头部）。
                if path != active:
                    self._discard_file(path)
                continue
            title, workspace, _messages = meta
            if not title:
                # 只有助手消息、没有用户提问：早期落盘 bug 的产物，不展示，但保留文件。
                continue
            sessions.append(
                {
                    "id": path.stem,
                    "title": title,
                    "updated": int(path.stat().st_mtime * 1000),
                    "workspace": workspace or str(self.workspace),
                }
            )
        return sessions

    @staticmethod
    def _discard_file(path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass

    @staticmethod
    def _session_meta(path: Path) -> tuple[str, str, int] | None:
        """返回 `(标题, 工作区, 消息数)`；读不了时返回 None。

        标题优先取头里的 `title`（用户改过名），否则取第一条用户提问。
        """
        title = ""
        workspace = ""
        messages = 0
        try:
            with path.open(encoding="utf-8") as handle:
                for index, line in enumerate(handle):
                    if index > 400 or (title and workspace and messages):
                        break
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        break
                    kind = payload.get("type")
                    if kind == "session":
                        workspace = str(payload.get("workspace") or "")
                        title = str(payload.get("title") or "").strip()[:60]
                        continue
                    if kind != "message":
                        continue
                    messages += 1
                    message = payload.get("message")
                    if title or not isinstance(message, dict) or message.get("role") != "user":
                        continue
                    content = message.get("content")
                    if isinstance(content, str) and content.strip():
                        title = content.strip().splitlines()[0][:60]
        except OSError:
            return None
        return title, workspace, messages

    def rename_session(self, session_id: str, title: str) -> None:
        """改会话头里的 `title`；空标题表示恢复成「取首条提问」。"""
        path = self.session_dir() / f"{session_id}.jsonl"
        if not path.is_file():
            raise SessionError(f"会话不存在：{session_id}")

        lines = path.read_text(encoding="utf-8").splitlines()
        header = json.loads(lines[0]) if lines else None
        if not isinstance(header, dict) or header.get("type") != "session":
            raise SessionError(f"会话文件缺少头部：{session_id}")

        cleaned = title.strip()
        if cleaned:
            header["title"] = cleaned[:60]
        else:
            header.pop("title", None)
        lines[0] = json.dumps(header, ensure_ascii=False)

        # 先写临时文件再替换：中途失败不会留下半个头部。
        temp = path.with_name(path.name + ".tmp")
        temp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(temp, path)

    async def delete_session(self, session_id: str) -> None:
        """删会话文件；删的是当前会话时先换到一个新会话再删。"""
        path = self.session_dir() / f"{session_id}.jsonl"
        if not path.is_file():
            raise SessionError(f"会话不存在：{session_id}")
        if self.session is not None and self.session.session_path == path.resolve():
            await self.switch(None, None)
        try:
            path.unlink()
        except FileNotFoundError:
            # 切走时旧会话已经被当空会话清掉了，等于删成功。
            pass
        except OSError as exc:
            raise SessionError(f"删除失败：{exc}") from exc

    @staticmethod
    def browse_roots() -> list[dict[str, str]]:
        """可选工作区的起点：Windows 是各盘符，其它系统是主目录与 `/`。"""
        if os.name == "nt":
            return [
                {"name": f"{letter}:\\", "path": f"{letter}:\\"}
                for letter in string.ascii_uppercase
                if Path(f"{letter}:\\").exists()
            ]
        return [
            {"name": "主目录", "path": str(Path.home())},
            {"name": "/", "path": "/"},
        ]

    def browse(self, raw: str) -> dict[str, Any]:
        """列出 `raw` 下的子目录；空路径返回起点。不受当前工作区限制。"""
        text = raw.strip()
        if not text:
            return {"path": "", "parent": None, "dirs": self.browse_roots()}

        current = Path(text).expanduser().resolve()
        if not current.is_dir():
            raise SessionError(f"不是目录：{text}")

        dirs: list[dict[str, str]] = []
        for child in sorted(current.iterdir(), key=lambda item: item.name.lower()):
            if child.name.startswith("."):
                continue
            try:
                if not child.is_dir():
                    continue
            except OSError:
                continue
            dirs.append({"name": child.name, "path": str(child)})
            if len(dirs) >= 500:
                break

        parent = current.parent
        return {
            "path": str(current),
            "parent": None if parent == current else str(parent),
            "dirs": dirs,
        }

    async def send_sessions(self) -> None:
        path = self.session.session_path if self.session is not None else None
        await self.send(
            "session/list",
            {
                "sessions": self.list_sessions(),
                "activeId": path.stem if path is not None else None,
            },
        )

    def config_path(self) -> Path:
        return Path(self.args.config).expanduser() if self.args.config else default_config_path()

    def model_state(self) -> dict[str, Any]:
        """可选模型 + 当前选中项；`models` 为空时只剩当前模型一条。"""
        if self.session is None:
            return {"activeId": None, "models": [], "active": None}

        settings = self.settings
        spec = settings.spec
        models: list[dict[str, Any]] = []
        active_id = ""
        for index, choice in enumerate(settings.models):
            item = {
                "id": f"m{index}",
                "name": choice.name,
                "provider": choice.spec.provider,
                "model": choice.spec.model,
            }
            models.append(item)
            if choice.spec == spec:
                active_id = item["id"]

        summary = model_summary(settings)
        if not active_id:
            # 当前模型不在候选表里（来自环境变量）：补一条占位，界面才有东西可显示。
            models.insert(0, {"id": "current", **summary})
            active_id = "current"

        return {"activeId": active_id, "models": models, "active": summary}

    def settings_state(self) -> dict[str, Any]:
        """设置页的输入：候选表（不含 key）、可选服务商与配置文件路径。"""
        settings = self.settings
        models = [
            {
                "id": f"m{index}",
                "name": choice.name,
                "provider": choice.spec.provider,
                "model": choice.spec.model,
                "baseUrl": choice.spec.base_url,
                "hasKey": bool(choice.api_key),
            }
            for index, choice in enumerate(settings.models)
        ]
        return {
            "path": str(self.config_path()),
            "services": [
                {"name": name, "provider": provider, "baseUrl": url}
                for name, (provider, url) in sorted(SERVICES.items())
            ],
            "models": models,
            "current": model_summary(settings),
        }

    async def send_model_state(self) -> None:
        await self.send("model/state", self.model_state())

    async def handle_model_select(self, model_id: str) -> None:
        session = self.session
        if session is None or not model_id:
            return

        settings = self.settings
        if model_id != "current":
            try:
                choice = settings.models[int(model_id.removeprefix("m"))]
            except (ValueError, IndexError):
                await self.send(
                    "session/status",
                    {
                        "status": "error",
                        "error": {"kind": "config", "message": f"未知模型：{model_id}"},
                    },
                )
                return
            target = with_choice(settings, choice)
        else:
            return  # 已经在当前模型上

        if self.run_task is not None and not self.run_task.done():
            await self.send(
                "session/status",
                {
                    "status": "error",
                    "error": {"kind": "config", "message": "正在生成，先停止再切换模型"},
                },
            )
            return

        try:
            await session.switch_model(target)
        except (SessionError, RuntimeError, ConfigError, OSError) as exc:
            await self.send(
                "session/status",
                {"status": "error", "error": {"kind": "config", "message": str(exc)}},
            )
            return

        self.settings = target
        try:
            # 记到配置文件，下次启动（或新连接）仍停在这个候选上。
            await asyncio.to_thread(save_active, self.args.config, choice.name)
        except (ConfigError, OSError):
            pass  # 记不住不影响这次切换
        await self.send_model_state()
        await self.send("session/status", {"status": "idle"})

    async def handle_settings_get(self) -> None:
        await self.send("settings/state", self.settings_state())

    async def handle_settings_save(self, entries: Any) -> None:
        """保存候选表：写回配置文件，重读后广播新的候选与配置路径。"""
        if not isinstance(entries, list):
            await self.send("settings/error", {"message": "models 必须是数组"})
            return
        if self.run_task is not None and not self.run_task.done():
            await self.send("settings/error", {"message": "正在生成，先停止再修改配置"})
            return

        try:
            await asyncio.to_thread(save_models, self.args.config, entries)
            fresh = load(overrides={}, config=self.args.config)
        except (ConfigError, OSError) as exc:
            await self.send("settings/error", {"message": str(exc)})
            return

        # 保存候选表不该把当前选中的模型顶掉：仍按本连接记着的名字切回去，
        # 该候选被删掉时 `with_active` 会保持顶层配置的模型。
        self.settings = with_active(replace(fresh, active=self.settings.active))
        await self.send("settings/state", self.settings_state())
        await self.send_model_state()

    async def switch(self, workspace: Path | None, session_id: str | None) -> None:
        # 切换前先把在跑的这一轮收干净，免得事件落到新会话上。
        if self.run_task is not None and not self.run_task.done():
            if self.session is not None:
                await self.session.abort()
            try:
                await asyncio.wait_for(self.run_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                self.run_task.cancel()
        self.run_task = None

        try:
            if session_id:
                path = self.session_dir() / f"{session_id}.jsonl"
                if not path.is_file():
                    raise SessionError(f"会话不存在：{session_id}")
                await self.connect_session(resume=path)
            else:
                if workspace is not None and not workspace.is_dir():
                    raise SessionError(f"工作区不存在：{workspace}")
                await self.connect_session(workspace=workspace)
        except (SessionError, McpConfigError, ConfigError, OSError) as exc:
            await self.send(
                "session/status",
                {"status": "error", "error": {"kind": "config", "message": str(exc)}},
            )
            return

        await self.send("session/snapshot", snapshot_payload(self.session, CAPABILITIES))
        await self.send_sessions()
        await self.send_model_state()
        await self.send("session/status", {"status": "idle"})

    def resolve_path(self, relative: str) -> Path:
        """把相对路径解析到工作区内；越界一律拒绝。"""
        root = (self.session.workspace if self.session is not None else self.workspace).resolve()
        target = (root / relative).resolve()
        if target != root and root not in target.parents:
            raise SessionError(f"路径越界：{relative}")
        return target

    def list_entries(self, relative: str) -> list[dict[str, Any]]:
        root = (self.session.workspace if self.session is not None else self.workspace).resolve()
        target = self.resolve_path(relative)
        if not target.is_dir():
            raise SessionError(f"不是目录：{relative}")

        entries: list[dict[str, Any]] = []
        for child in sorted(target.iterdir(), key=lambda item: (item.is_file(), item.name.lower())):
            if child.name in self.SKIP_DIRS or child.name.startswith("."):
                continue
            entries.append(
                {
                    "name": child.name,
                    "path": child.relative_to(root).as_posix(),
                    "dir": child.is_dir(),
                }
            )
            if len(entries) >= 500:
                break
        return entries

    def read_entry(self, relative: str) -> dict[str, Any]:
        target = self.resolve_path(relative)
        if not target.is_file():
            raise SessionError(f"不是文件：{relative}")

        size = target.stat().st_size
        data = target.read_bytes()[: self.MAX_FILE_BYTES]
        binary = b"\x00" in data
        return {
            "path": relative,
            "content": "" if binary else data.decode("utf-8", errors="replace"),
            "binary": binary,
            "truncated": size > self.MAX_FILE_BYTES,
        }

    async def handle(self, command: Any) -> None:
        if not isinstance(command, dict):
            return
        kind = command.get("type")

        if kind == "session/prompt":
            text = str(command.get("text") or "")
            if not text.strip():
                return
            if self.switch_task is not None and not self.switch_task.done():
                return  # 正在切换会话，输入先丢弃
            if self.run_task is not None and not self.run_task.done():
                return  # 一次只跑一轮；并发输入由前端禁用发送
            self.run_task = asyncio.create_task(self.run_prompt(text))
        elif kind == "session/cancel":
            if self.session is not None:
                await self.session.abort()
        elif kind == "session/create":
            workspace = str(command.get("workspace") or "").strip()
            self.start_switch(Path(workspace).expanduser() if workspace else None, None)
        elif kind == "session/open":
            self.start_switch(None, str(command.get("id") or ""))
        elif kind == "session/rename":
            await self.handle_rename(
                str(command.get("id") or ""), str(command.get("title") or "")
            )
        elif kind == "session/delete":
            await self.handle_delete(str(command.get("id") or ""))
        elif kind == "model/select":
            await self.handle_model_select(str(command.get("id") or ""))
        elif kind == "settings/get":
            await self.handle_settings_get()
        elif kind == "settings/save":
            await self.handle_settings_save(command.get("models"))
        elif kind == "fs/list":
            await self.handle_list(str(command.get("path") or ""))
        elif kind == "fs/read":
            await self.handle_read(str(command.get("path") or ""))
        elif kind == "workspace/browse":
            await self.handle_browse(str(command.get("path") or ""))
        elif kind == "workspace/pick":
            await self.handle_pick(str(command.get("path") or ""))
        elif kind == "interaction/answer":
            self.confirm.resolve(str(command.get("id") or ""), bool(command.get("ok")))

    def start_switch(self, workspace: Path | None, session_id: str | None) -> None:
        if self.switch_task is not None and not self.switch_task.done():
            return
        self.switch_task = asyncio.create_task(self.switch(workspace, session_id))

    async def handle_list(self, relative: str) -> None:
        try:
            entries = await asyncio.to_thread(self.list_entries, relative)
        except (SessionError, OSError) as exc:
            await self.send("fs/error", {"message": str(exc)})
            return
        await self.send("fs/entries", {"path": relative, "entries": entries})

    async def handle_read(self, relative: str) -> None:
        try:
            payload = await asyncio.to_thread(self.read_entry, relative)
        except (SessionError, OSError) as exc:
            await self.send("fs/error", {"message": str(exc)})
            return
        await self.send("fs/file", payload)

    async def handle_rename(self, session_id: str, title: str) -> None:
        try:
            if not session_id:
                raise SessionError("缺少会话 id")
            await asyncio.to_thread(self.rename_session, session_id, title)
        except (SessionError, OSError, json.JSONDecodeError) as exc:
            await self.send(
                "session/status",
                {"status": "error", "error": {"kind": "config", "message": str(exc)}},
            )
            return
        await self.send_sessions()

    async def handle_delete(self, session_id: str) -> None:
        try:
            if not session_id:
                raise SessionError("缺少会话 id")
            await self.delete_session(session_id)
        except (SessionError, OSError, json.JSONDecodeError) as exc:
            await self.send(
                "session/status",
                {"status": "error", "error": {"kind": "config", "message": str(exc)}},
            )
            return
        await self.send_sessions()

    async def handle_browse(self, path: str) -> None:
        try:
            payload = await asyncio.to_thread(self.browse, path)
        except (SessionError, OSError) as exc:
            await self.send("workspace/error", {"message": str(exc)})
            return
        await self.send("workspace/dirs", payload)

    async def handle_pick(self, path: str) -> None:
        """原生「选择文件夹」；结果仍走 `workspace/dirs`，前端当成一次普通跳转。"""
        if not NATIVE_PICKER:
            await self.send(
                "workspace/error", {"message": "当前环境没有可用的系统目录对话框"}
            )
            return
        try:
            chosen = await asyncio.to_thread(pick_directory, path)
        except Exception as exc:  # Tk 起不来的方式很多，报给前端而不是静默失败
            await self.send("workspace/error", {"message": f"打开目录选择器失败：{exc}"})
            return
        if chosen is None:
            return  # 用户取消：不发帧，对话框保持原样
        await self.handle_browse(chosen)

    async def run_prompt(self, text: str) -> None:
        session = self.session
        if session is None:
            return

        await self.send(
            "session/message",
            {"id": f"u-{self.next_id()}", "role": "user", "text": text},
        )
        await self.send("session/status", {"status": "running"})

        error: dict[str, Any] | None = None
        try:
            async for event in session.prompt(text):
                payload = encode_event(event)
                if payload is None:
                    continue
                if payload["type"] == "agent/error" and payload["kind"] != "aborted":
                    error = {"kind": payload["kind"], "message": payload["message"]}
                await self.send("agent/event", payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 未预期的异常也要让前端看到，而不是静默断开
            error = {"kind": "internal", "message": str(exc)}
        finally:
            # 中止时可能还有工具在等确认；不清理会让前端一直显示审批面板。
            self.confirm.reject_all()

        if error is None:
            await self.send("session/status", {"status": "idle"})
        else:
            await self.send("session/status", {"status": "error", "error": error})
        # 标题取首条提问，跑完刷新一次，侧栏才能看到新标题。
        await self.send_sessions()

    async def shutdown(self) -> None:
        session = self.session
        if session is None:
            return

        try:
            await session.abort()
        except Exception:
            pass
        self.confirm.reject_all()

        if self.run_task is not None:
            try:
                await asyncio.wait_for(self.run_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                self.run_task.cancel()

        if (
            self.switch_task is not None
            and not self.switch_task.done()
            and self.switch_task is not asyncio.current_task()
        ):
            self.switch_task.cancel()

        try:
            await session.aclose()
        except Exception:
            pass

        self.discard_empty(session)


def build_app(settings: Any, args: argparse.Namespace, token: str) -> Starlette:
    async def ws_endpoint(websocket: WebSocket) -> None:
        if websocket.query_params.get("token") != token:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        await Connection(websocket, settings, args).run()

    def index(_request: Any) -> HTMLResponse:
        return HTMLResponse(NO_BUILD_PAGE, status_code=200)

    routes: list[Any] = [WebSocketRoute("/ws", ws_endpoint)]
    if DIST.is_dir():
        routes.append(Mount("/", app=StaticFiles(directory=str(DIST), html=True), name="web"))
    else:
        routes.append(Route("/", index))

    return Starlette(routes=routes)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    base = Path(args.workspace).expanduser() if args.workspace else Path.cwd()
    if args.workspace and not base.is_dir():
        print(f"workspace not found: {base}", file=sys.stderr)
        return 2

    load_env_file(base / ".env")
    try:
        settings = load(overrides={}, config=args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    token = args.token or secrets.token_urlsafe(16)
    app = build_app(settings, args, token)

    host = "127.0.0.1" if args.host in {"0.0.0.0", "::"} else args.host
    print(f"Gradus web · {settings.spec.model} · {base.resolve()}")
    print(f"open: http://{host}:{args.port}/?token={token}")
    if not DIST.is_dir():
        print("web/dist 不存在：先执行 npm run build，或用 npm run dev 走代理", file=sys.stderr)

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def cli() -> None:
    raise SystemExit(main())
