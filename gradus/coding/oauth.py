"""MCP 的 OAuth 登录：token 存储、本地回调页与 provider 装配。

SDK 的 `OAuthClientProvider` 已经负责元数据发现、动态注册、PKCE、刷新与 401 重试；
这里只补三样它需要的宿主能力：

- `FileTokenStorage`：把 token 与客户端注册信息写进 `~/.gradus/mcp-auth.json`（0600）；
- `CallbackServer`：在 `127.0.0.1` 上收授权回调，返回 `AuthorizationCodeResult`；
- `http_client()`：把上面两者接成一个带 `auth` 的 `httpx2.AsyncClient`。

约束：
- token 明文存放，只靠文件权限保护；不做跨进程锁（同一时刻只跑一个 Gradus）；
- 无交互环境（`interactive=False`）时不会打开浏览器，401 直接报「需要登录」；
- 回调端口优先复用上次注册的端口，避免授权服务器因 redirect_uri 变化拒绝。
"""

from __future__ import annotations

import asyncio
import json
import os
import webbrowser
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.auth.exceptions import OAuthFlowError
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)

AUTH_RELPATH = Path(".gradus") / "mcp-auth.json"
CALLBACK_PATH = "/callback"
DEFAULT_TIMEOUT = 300.0
HOST = "127.0.0.1"

SUCCESS_HTML = """<!doctype html><html lang="zh"><meta charset="utf-8">
<title>登录成功</title><body style="font-family:sans-serif;padding:3rem">
<h2>登录成功</h2><p>可以关闭这个页面，回到 Gradus。</p></body></html>"""

ERROR_HTML = """<!doctype html><html lang="zh"><meta charset="utf-8">
<title>登录失败</title><body style="font-family:sans-serif;padding:3rem">
<h2>登录失败</h2><p>{message}</p></body></html>"""


@dataclass(frozen=True)
class OAuthSettings:
    """`.gradus/mcp.json` 里 `oauth` 对象的形状。"""

    client_id: str | None = None
    client_secret: str | None = None
    scope: str | None = None
    client_name: str = "Gradus"
    callback_port: int = 0
    timeout: float = DEFAULT_TIMEOUT


def auth_file() -> Path:
    """token 文件位置；测试可以替换。"""
    return Path.home() / AUTH_RELPATH


class FileTokenStorage(TokenStorage):
    """按 `server|url` 存一份记录；文件权限 0600，目录 0700。"""

    def __init__(self, key: str, path: Path | None = None) -> None:
        self._key = key
        self._path = path if path is not None else auth_file()
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    async def get_tokens(self) -> OAuthToken | None:
        record = self._record()
        tokens = record.get("tokens")
        return OAuthToken.model_validate(tokens) if isinstance(tokens, dict) else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        await self._update({"tokens": tokens.model_dump(mode="json", exclude_none=True)})

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        record = self._record()
        info = record.get("client")
        if not isinstance(info, dict):
            return None
        try:
            return OAuthClientInformationFull.model_validate(info)
        except Exception:
            return None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        await self._update(
            {"client": client_info.model_dump(mode="json", exclude_none=True)}
        )

    def redirect_uri(self) -> str | None:
        record = self._record()
        value = record.get("redirectUri")
        return value if isinstance(value, str) else None

    async def set_redirect_uri(self, uri: str) -> None:
        await self._update({"redirectUri": uri})

    async def reset_client(self) -> None:
        """redirect_uri 变了：丢掉旧注册与旧 token，下次重新注册。"""
        await self._update({"client": None, "tokens": None})

    def _record(self) -> dict[str, Any]:
        data = _read(self._path)
        record = data.get(self._key)
        return record if isinstance(record, dict) else {}

    async def _update(self, changes: dict[str, Any]) -> None:
        async with self._lock:
            data = _read(self._path)
            record = data.get(self._key)
            if not isinstance(record, dict):
                record = {}
            record.update(changes)
            data[self._key] = record
            _write(self._path, data)


class CallbackServer:
    """一次性 HTTP 回调页：收 `?code=...&state=...`，返回给 SDK。"""

    def __init__(
        self,
        port: int = 0,
        *,
        path: str = CALLBACK_PATH,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._port = port
        self._path = path
        self._timeout = timeout
        self._server: asyncio.AbstractServer | None = None
        self._future: asyncio.Future[AuthorizationCodeResult] | None = None

    @property
    def redirect_url(self) -> str:
        assert self._server is not None
        return f"http://{HOST}:{self.port}{self._path}"

    @property
    def port(self) -> int:
        assert self._server is not None
        sockets = self._server.sockets or []
        return int(sockets[0].getsockname()[1]) if sockets else self._port

    @property
    def timeout(self) -> float:
        return self._timeout

    async def start(self) -> str:
        self._future = asyncio.get_running_loop().create_future()
        self._server = await asyncio.start_server(
            self._serve, HOST, self._port, reuse_address=True
        )
        return self.redirect_url

    async def wait(self) -> AuthorizationCodeResult:
        assert self._future is not None
        try:
            return await asyncio.wait_for(self._future, self._timeout)
        except TimeoutError as exc:
            raise OAuthFlowError(
                f"sign-in was not completed within {self._timeout:g}s"
            ) from exc

    async def aclose(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request = (await reader.readline()).decode("utf-8", errors="replace")
            await _drain_headers(reader)
            method, target = _split_request(request)
            parsed = urlsplit(target)

            if method != "GET" or parsed.path != self._path:
                await _respond(writer, 404, "未找到回调路径", error=True)
                return

            params = parse_qs(parsed.query)
            failure = _first(params, "error")
            if failure is not None:
                await _respond(writer, 400, _first(params, "error_description") or failure, error=True)
                self._fail(f"authorization failed: {failure}")
                return

            code = _first(params, "code")
            if not code:
                await _respond(writer, 400, "回调里没有 code", error=True)
                return

            await _respond(writer, 200, "登录成功")
            self._succeed(
                AuthorizationCodeResult(code=code, state=_first(params, "state"))
            )
        except Exception as exc:  # 回调页永远要给出响应，不把异常抛回事件循环
            await _respond(writer, 500, str(exc), error=True)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    def _succeed(self, result: AuthorizationCodeResult) -> None:
        if self._future is not None and not self._future.done():
            self._future.set_result(result)

    def _fail(self, message: str) -> None:
        if self._future is not None and not self._future.done():
            self._future.set_exception(OAuthFlowError(message))


@asynccontextmanager
async def http_client(
    name: str,
    url: str,
    settings: OAuthSettings,
    *,
    interactive: bool = True,
    opener: Callable[[str], Any] | None = None,
    path: Path | None = None,
) -> AsyncIterator[httpx2.AsyncClient]:
    """给一个 MCP HTTP server 配好 OAuth，产出 `httpx2.AsyncClient`。"""
    storage = FileTokenStorage(f"{name}|{url}", path)
    callback = await _open_callback(storage, settings)

    existing = await storage.get_client_info()
    if existing is not None and _redirect_of(existing) != callback.redirect_url:
        # 端口变了会与已注册的 redirect_uri 不符，重新注册一次。
        await storage.reset_client()
        existing = None

    if existing is None and settings.client_id:
        await storage.set_client_info(
            OAuthClientInformationFull(
                client_id=settings.client_id,
                client_secret=settings.client_secret,
                redirect_uris=[callback.redirect_url],
                token_endpoint_auth_method=(
                    "client_secret_post" if settings.client_secret else "none"
                ),
                scope=settings.scope,
            )
        )
    await storage.set_redirect_uri(callback.redirect_url)

    provider = OAuthClientProvider(
        server_url=url,
        client_metadata=OAuthClientMetadata(
            client_name=settings.client_name,
            redirect_uris=[callback.redirect_url],
            scope=settings.scope,
            token_endpoint_auth_method=(
                "client_secret_post" if settings.client_secret else "none"
            ),
        ),
        storage=storage,
        redirect_handler=_redirect_handler(name, callback, interactive, opener),
        callback_handler=callback.wait,
    )

    client = httpx2.AsyncClient(auth=provider)
    try:
        async with client:
            yield client
    finally:
        await callback.aclose()


async def _open_callback(
    storage: FileTokenStorage, settings: OAuthSettings
) -> CallbackServer:
    """优先用上次注册的端口；占用就退回随机端口。"""
    preferred = settings.callback_port or _port_of(storage.redirect_uri())
    callback = CallbackServer(port=preferred or 0, timeout=settings.timeout)
    try:
        await callback.start()
        return callback
    except OSError:
        if not preferred:
            raise
        fallback = CallbackServer(port=0, timeout=settings.timeout)
        await fallback.start()
        return fallback


def _redirect_handler(
    name: str,
    callback: CallbackServer,
    interactive: bool,
    opener: Callable[[str], Any] | None,
) -> Callable[[str], Awaitable[None]]:
    async def redirect(url: str) -> None:
        if not interactive:
            raise OAuthFlowError(
                f"{name} requires sign-in; run Gradus interactively to authorize"
            )
        open_url = opener or webbrowser.open
        print(
            f"\n[{name}] 需要授权，已打开浏览器；{callback.timeout:g} 秒内完成：\n{url}\n",
            flush=True,
        )
        try:
            await asyncio.to_thread(open_url, url)
        except Exception:
            pass

    return redirect


def _redirect_of(info: OAuthClientInformationFull) -> str | None:
    uris = info.redirect_uris or []
    return str(uris[0]) if uris else None


def _port_of(uri: str | None) -> int:
    if not uri:
        return 0
    try:
        return urlsplit(uri).port or 0
    except ValueError:
        return 0


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass

    temp = path.with_name(f"{path.name}.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass
    os.replace(temp, path)


async def _drain_headers(reader: asyncio.StreamReader) -> None:
    for _ in range(50):
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            return


def _split_request(request: str) -> tuple[str, str]:
    parts = request.split(" ")
    if len(parts) < 2:
        raise ValueError("malformed request line")
    return parts[0], parts[1]


def _first(params: dict[str, list[str]], key: str) -> str | None:
    values = params.get(key) or []
    return values[0] if values else None


async def _respond(
    writer: asyncio.StreamWriter, status: int, message: str, *, error: bool = False
) -> None:
    body = ERROR_HTML.format(message=message) if error else SUCCESS_HTML
    payload = body.encode("utf-8")
    head = (
        f"HTTP/1.1 {status} {'OK' if status == 200 else 'Error'}\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Connection: close\r\n\r\n"
    )
    writer.write(head.encode("utf-8") + payload)
    try:
        await writer.drain()
    except Exception:
        pass
