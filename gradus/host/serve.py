"""Web 后端入口：把 `coding.Session` 的事件流接到 WebSocket 上。

负责：鉴权、会话生命周期、命令分发、确认桥、静态文件。
不负责：装配细节（`coding/session.py`）、工具实现、权限判断。

依赖方向：`host -> coding -> agent -> ai`。本模块只调用 `Session` 的公开方法，
不改动下面三层。
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.responses import HTMLResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from ..ai.envfile import load_env_file
from ..coding.config import ConfigError, load
from ..coding.mcp import McpConfigError
from ..coding.session import Session, SessionError
from .wire import encode_event, snapshot_payload

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


class Connection:
    """一条 WebSocket 对应一个会话。第一版不做多会话切换。"""

    def __init__(self, websocket: WebSocket, settings: Any, args: argparse.Namespace) -> None:
        self.ws = websocket
        self.settings = settings
        self.args = args
        self.seq = 0
        self.ids = 0
        self.send_lock = asyncio.Lock()
        self.session: Session | None = None
        self.run_task: asyncio.Task[None] | None = None
        self.confirm = ConfirmBridge(self, args.confirm_timeout)

    def next_id(self) -> int:
        self.ids += 1
        return self.ids

    async def send(self, frame_type: str, payload: Any) -> None:
        async with self.send_lock:
            self.seq += 1
            await self.ws.send_json({"seq": self.seq, "type": frame_type, "payload": payload})

    async def run(self) -> None:
        try:
            self.session = await Session.connect(
                self.settings,
                workspace=self.args.workspace,
                confirm=self.confirm.ask,
                session_dir=self.session_dir(),
                mcp_config=self.args.mcp,
            )
        except (SessionError, McpConfigError, ConfigError) as exc:
            await self.send(
                "session/status",
                {"status": "error", "error": {"kind": "config", "message": str(exc)}},
            )
            await self.ws.close(code=1011)
            return

        try:
            await self.send("session/snapshot", snapshot_payload(self.session))
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
        base = Path(self.args.workspace).expanduser() if self.args.workspace else Path.cwd()
        return base / ".gradus" / "sessions"

    async def handle(self, command: Any) -> None:
        if not isinstance(command, dict):
            return
        kind = command.get("type")

        if kind == "session/prompt":
            text = str(command.get("text") or "")
            if not text.strip():
                return
            if self.run_task is not None and not self.run_task.done():
                return  # 一次只跑一轮；并发输入由前端禁用发送
            self.run_task = asyncio.create_task(self.run_prompt(text))
        elif kind == "session/cancel":
            if self.session is not None:
                await self.session.abort()
        elif kind == "interaction/answer":
            self.confirm.resolve(str(command.get("id") or ""), bool(command.get("ok")))

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
                if payload["type"] == "agent/error":
                    error = {"kind": payload["kind"], "message": payload["message"]}
                await self.send("agent/event", payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 未预期的异常也要让前端看到，而不是静默断开
            error = {"kind": "internal", "message": str(exc)}

        if error is None:
            await self.send("session/status", {"status": "idle"})
        else:
            await self.send("session/status", {"status": "error", "error": error})

    async def shutdown(self) -> None:
        session = self.session
        if session is None:
            return

        try:
            await session.abort()
        except Exception:
            pass

        if self.run_task is not None:
            try:
                await asyncio.wait_for(self.run_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                self.run_task.cancel()

        try:
            await session.aclose()
        except Exception:
            pass


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
