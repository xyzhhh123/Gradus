"""OAuth 支撑部件的离线测试（不联网、不开浏览器）。"""

from __future__ import annotations

import asyncio
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from mcp.client.auth.exceptions import OAuthFlowError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from gradus.coding import oauth


async def _get(url: str) -> bytes:
    """向回调页发一个最小 GET 请求，返回原始响应。"""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    reader, writer = await asyncio.open_connection(parts.hostname, parts.port)
    request = (
        f"GET {parts.path}?{parts.query} HTTP/1.1\r\n"
        f"Host: {parts.hostname}\r\nConnection: close\r\n\r\n"
    )
    writer.write(request.encode())
    await writer.drain()
    data = await reader.read()
    writer.close()
    return data


class FileTokenStorageTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "mcp-auth.json"
        self.storage = oauth.FileTokenStorage("demo|https://example.com/mcp", self.path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def test_round_trips_tokens_and_client(self):
        await self.storage.set_tokens(
            OAuthToken(access_token="a", refresh_token="r", expires_in=60)
        )
        await self.storage.set_client_info(
            OAuthClientInformationFull(client_id="cid", redirect_uris=["http://127.0.0.1:1/callback"])
        )

        tokens = await self.storage.get_tokens()
        info = await self.storage.get_client_info()

        self.assertEqual(tokens.access_token, "a")
        self.assertEqual(tokens.refresh_token, "r")
        self.assertEqual(info.client_id, "cid")

    async def test_corrupt_file_reads_as_empty(self):
        self.path.write_text("{not json", encoding="utf-8")

        self.assertIsNone(await self.storage.get_tokens())
        await self.storage.set_tokens(OAuthToken(access_token="a"))
        self.assertEqual((await self.storage.get_tokens()).access_token, "a")

    async def test_keys_are_isolated(self):
        other = oauth.FileTokenStorage("other|https://example.com/mcp", self.path)
        await self.storage.set_tokens(OAuthToken(access_token="one"))
        await other.set_tokens(OAuthToken(access_token="two"))

        self.assertEqual((await self.storage.get_tokens()).access_token, "one")
        self.assertEqual((await other.get_tokens()).access_token, "two")

    async def test_file_is_private(self):
        await self.storage.set_tokens(OAuthToken(access_token="a"))

        if sys.platform != "win32":
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    async def test_redirect_uri_round_trip(self):
        await self.storage.set_redirect_uri("http://127.0.0.1:4321/callback")

        self.assertEqual(self.storage.redirect_uri(), "http://127.0.0.1:4321/callback")

    async def test_reset_clears_client_and_tokens(self):
        await self.storage.set_tokens(OAuthToken(access_token="a"))
        await self.storage.set_client_info(
            OAuthClientInformationFull(client_id="cid", redirect_uris=["http://127.0.0.1:1/callback"])
        )

        await self.storage.reset_client()

        self.assertIsNone(await self.storage.get_tokens())
        self.assertIsNone(await self.storage.get_client_info())


class CallbackServerTest(unittest.IsolatedAsyncioTestCase):
    async def test_receives_code_and_state(self):
        server = oauth.CallbackServer(timeout=2)
        url = await server.start()
        try:
            data = await _get(f"{url}?code=abc&state=xyz")
            result = await server.wait()
        finally:
            await server.aclose()

        self.assertIn(b"200", data.split(b"\r\n")[0])
        self.assertIn("登录成功".encode(), data)
        self.assertEqual(result.code, "abc")
        self.assertEqual(result.state, "xyz")

    async def test_error_param_raises(self):
        server = oauth.CallbackServer(timeout=2)
        url = await server.start()
        try:
            data = await _get(f"{url}?error=access_denied&error_description=no")
            with self.assertRaises(OAuthFlowError):
                await server.wait()
        finally:
            await server.aclose()

        self.assertIn(b"400", data.split(b"\r\n")[0])

    async def test_unknown_path_is_404_and_keeps_waiting(self):
        server = oauth.CallbackServer(timeout=0.2)
        await server.start()
        try:
            data = await _get(f"http://127.0.0.1:{server.port}/other?code=abc")
            with self.assertRaises(OAuthFlowError):
                await server.wait()
        finally:
            await server.aclose()

        self.assertIn(b"404", data.split(b"\r\n")[0])

    async def test_timeout_raises(self):
        server = oauth.CallbackServer(timeout=0.1)
        await server.start()
        try:
            with self.assertRaises(OAuthFlowError):
                await server.wait()
        finally:
            await server.aclose()


class RedirectHandlerTest(unittest.IsolatedAsyncioTestCase):
    async def test_non_interactive_refuses_to_open_browser(self):
        callback = oauth.CallbackServer(timeout=0.1)
        await callback.start()
        try:
            handler = oauth._redirect_handler("demo", callback, False, None)
            with self.assertRaises(OAuthFlowError):
                await handler("https://auth.example.com/authorize")
        finally:
            await callback.aclose()


class HttpClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_builds_client_and_persists_redirect_uri(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mcp-auth.json"
            settings = oauth.OAuthSettings(client_id="cid", client_secret="secret")

            async with oauth.http_client(
                "demo", "https://example.com/mcp", settings, path=path
            ) as client:
                record = json.loads(path.read_text(encoding="utf-8"))["demo|https://example.com/mcp"]
                self.assertTrue(record["redirectUri"].startswith("http://127.0.0.1:"))
                self.assertEqual(record["client"]["client_id"], "cid")
                self.assertIsNotNone(client.auth)

            self.assertFalse(path.with_name(path.name + ".tmp").exists())

    async def test_reuses_stored_port_across_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mcp-auth.json"
            settings = oauth.OAuthSettings()

            async with oauth.http_client(
                "demo", "https://example.com/mcp", settings, path=path
            ):
                first = json.loads(path.read_text(encoding="utf-8"))[
                    "demo|https://example.com/mcp"
                ]["redirectUri"]

            async with oauth.http_client(
                "demo", "https://example.com/mcp", settings, path=path
            ):
                second = json.loads(path.read_text(encoding="utf-8"))[
                    "demo|https://example.com/mcp"
                ]["redirectUri"]

            self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
