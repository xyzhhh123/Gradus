"""MCP 接入的离线测试：配置解析、工具投影、stdio 真连。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp.types import CallToolResult, TextContent

from gradus.coding import mcp


def _write(tmp: str, payload: object) -> Path:
    path = Path(tmp) / "mcp.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class LoadServersTest(unittest.TestCase):
    def test_missing_file_means_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(mcp.load_servers(Path(tmp) / "none.json"), [])

    def test_stdio_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(
                tmp,
                {
                    "servers": {
                        "files": {
                            "command": "npx",
                            "args": ["-y", "server-filesystem", "."],
                            "env": {"TOKEN": "x"},
                            "cwd": ".",
                        }
                    }
                },
            )

            servers = mcp.load_servers(path)

        server = servers[0]
        self.assertEqual(server.name, "files")
        self.assertEqual(server.transport, "stdio")
        self.assertEqual(server.args, ("-y", "server-filesystem", "."))
        self.assertEqual(server.env, {"TOKEN": "x"})
        self.assertEqual(server.cwd, ".")

    def test_http_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(
                tmp,
                {
                    "servers": {
                        "remote": {
                            "url": "https://example.com/mcp",
                            "headers": {"Authorization": "Bearer x"},
                        }
                    }
                },
            )

            server = mcp.load_servers(path)[0]

        self.assertEqual(server.transport, "http")
        self.assertEqual(server.headers["Authorization"], "Bearer x")

    def test_empty_servers(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(mcp.load_servers(_write(tmp, {"servers": {}})), [])

    def test_top_level_must_be_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(mcp.McpConfigError):
                mcp.load_servers(_write(tmp, []))

    def test_invalid_json_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mcp.json"
            path.write_text("{not json", encoding="utf-8")

            with self.assertRaises(mcp.McpConfigError):
                mcp.load_servers(path)

    def test_requires_exactly_one_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            both = _write(
                tmp,
                {"servers": {"s": {"command": "x", "url": "https://a/mcp"}}},
            )
            with self.assertRaises(mcp.McpConfigError):
                mcp.load_servers(both)

            neither = _write(tmp, {"servers": {"s": {}}})
            with self.assertRaises(mcp.McpConfigError):
                mcp.load_servers(neither)

    def test_unknown_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, {"servers": {"s": {"command": "x", "retries": 1}}})

            with self.assertRaises(mcp.McpConfigError):
                mcp.load_servers(path)

    def test_bad_args_and_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad_args = _write(tmp, {"servers": {"s": {"command": "x", "args": "y"}}})
            with self.assertRaises(mcp.McpConfigError):
                mcp.load_servers(bad_args)

            bad_env = _write(tmp, {"servers": {"s": {"command": "x", "env": {"A": 1}}}})
            with self.assertRaises(mcp.McpConfigError):
                mcp.load_servers(bad_env)

    def test_url_scheme_is_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, {"servers": {"s": {"url": "ftp://a"}}})

            with self.assertRaises(mcp.McpConfigError):
                mcp.load_servers(path)

    def test_default_path_is_under_workspace(self):
        path = mcp.default_config_path(Path("work"))

        self.assertEqual(path, Path("work").resolve() / ".gradus" / "mcp.json")

    def test_enabled_and_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(
                tmp,
                {
                    "servers": {
                        "on": {"command": "x"},
                        "off": {"command": "y", "enabled": False, "timeout": 5},
                    }
                },
            )

            servers = {server.name: server for server in mcp.load_servers(path)}

        self.assertTrue(servers["on"].enabled)
        self.assertEqual(servers["on"].timeout, mcp.DEFAULT_TIMEOUT)
        self.assertFalse(servers["off"].enabled)
        self.assertEqual(servers["off"].timeout, 5.0)

    def test_exposure_defaults_to_deferred(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, {"servers": {"s": {"command": "x"}}})

            self.assertEqual(mcp.load_servers(path)[0].exposure, "deferred")

    def test_exposure_and_tool_exposure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(
                tmp,
                {
                    "servers": {
                        "s": {
                            "command": "x",
                            "exposure": "direct",
                            "toolExposure": {"delete_*": "hidden", "get_*": "deferred"},
                            "description": "Internal tools",
                        }
                    }
                },
            )

            server = mcp.load_servers(path)[0]

        self.assertEqual(server.exposure, "direct")
        self.assertEqual(server.tool_exposure["delete_*"], "hidden")
        self.assertEqual(server.description, "Internal tools")

    def test_bad_exposure_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad_server = _write(
                tmp, {"servers": {"s": {"command": "x", "exposure": "codemode"}}}
            )
            with self.assertRaises(mcp.McpConfigError):
                mcp.load_servers(bad_server)

            bad_tool = _write(
                tmp,
                {
                    "servers": {
                        "s": {"command": "x", "toolExposure": {"a": "nope"}}
                    }
                },
            )
            with self.assertRaises(mcp.McpConfigError):
                mcp.load_servers(bad_tool)

    def test_bad_enabled_and_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad_enabled = _write(
                tmp, {"servers": {"s": {"command": "x", "enabled": "no"}}}
            )
            with self.assertRaises(mcp.McpConfigError):
                mcp.load_servers(bad_enabled)

            bad_timeout = _write(
                tmp, {"servers": {"s": {"command": "x", "timeout": 0}}}
            )
            with self.assertRaises(mcp.McpConfigError):
                mcp.load_servers(bad_timeout)


class OAuthConfigTest(unittest.TestCase):
    def test_parses_oauth_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(
                tmp,
                {
                    "servers": {
                        "sentry": {
                            "url": "https://mcp.example.com/mcp",
                            "oauth": {
                                "clientId": "cid",
                                "clientSecret": "secret",
                                "scope": "read write",
                                "clientName": "Gradus CLI",
                                "callbackPort": 8765,
                                "timeout": 30,
                            },
                        }
                    }
                },
            )

            settings = mcp.load_servers(path)[0].oauth

        self.assertEqual(settings.client_id, "cid")
        self.assertEqual(settings.client_secret, "secret")
        self.assertEqual(settings.scope, "read write")
        self.assertEqual(settings.client_name, "Gradus CLI")
        self.assertEqual(settings.callback_port, 8765)
        self.assertEqual(settings.timeout, 30.0)

    def test_empty_object_uses_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(
                tmp,
                {"servers": {"s": {"url": "https://example.com/mcp", "oauth": {}}}},
            )

            settings = mcp.load_servers(path)[0].oauth

        self.assertIsNone(settings.client_id)
        self.assertEqual(settings.client_name, "Gradus")
        self.assertEqual(settings.callback_port, 0)

    def test_oauth_rejects_stdio(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, {"servers": {"s": {"command": "x", "oauth": {}}}})

            with self.assertRaises(mcp.McpConfigError):
                mcp.load_servers(path)

    def test_oauth_and_authorization_header_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(
                tmp,
                {
                    "servers": {
                        "s": {
                            "url": "https://example.com/mcp",
                            "headers": {"Authorization": "Bearer x"},
                            "oauth": {},
                        }
                    }
                },
            )

            with self.assertRaises(mcp.McpConfigError):
                mcp.load_servers(path)

    def test_oauth_rejects_unknown_key_and_bad_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            unknown = _write(
                tmp,
                {"servers": {"s": {"url": "https://example.com/mcp", "oauth": {"x": 1}}}},
            )
            with self.assertRaises(mcp.McpConfigError):
                mcp.load_servers(unknown)

            bad_port = _write(
                tmp,
                {
                    "servers": {
                        "s": {
                            "url": "https://example.com/mcp",
                            "oauth": {"callbackPort": 70000},
                        }
                    }
                },
            )
            with self.assertRaises(mcp.McpConfigError):
                mcp.load_servers(bad_port)


class ToolNameTest(unittest.TestCase):
    def test_names(self):
        self.assertEqual(mcp.make_tool_name("files", "read"), "mcp__files__read")
        self.assertEqual(mcp.make_tool_name("my server", "do it"), "mcp__my_server__do_it")


class RenderResultTest(unittest.TestCase):
    def test_text_blocks_are_joined(self):
        result = CallToolResult(
            content=[
                TextContent(type="text", text="a"),
                TextContent(type="text", text="b"),
            ]
        )

        self.assertEqual(mcp.render_result(result), "a\nb")

    def test_structured_content_when_no_text(self):
        result = CallToolResult(content=[], structured_content={"a": 1})

        self.assertEqual(mcp.render_result(result), '{"a": 1}')

    def test_empty_result(self):
        self.assertEqual(mcp.render_result(CallToolResult(content=[])), "(empty result)")


class EffectiveExposureTest(unittest.TestCase):
    def test_server_default(self):
        server = mcp.McpServer(name="s", command="x", exposure="hidden")

        self.assertEqual(mcp.effective_exposure(server, "anything"), "hidden")

    def test_exact_name_wins_over_pattern(self):
        server = mcp.McpServer(
            name="s",
            command="x",
            exposure="deferred",
            tool_exposure={"get_*": "direct", "get_one": "hidden"},
        )

        self.assertEqual(mcp.effective_exposure(server, "get_one"), "hidden")
        self.assertEqual(mcp.effective_exposure(server, "get_two"), "direct")

    def test_first_matching_pattern_wins(self):
        server = mcp.McpServer(
            name="s",
            command="x",
            tool_exposure={"get_*": "direct", "*": "hidden"},
        )

        self.assertEqual(mcp.effective_exposure(server, "get_x"), "direct")
        self.assertEqual(mcp.effective_exposure(server, "delete_x"), "hidden")


class _FakeClient:
    def __init__(self, *, result=None, error=None):
        self.result = result
        self.error = error
        self.called: tuple[str, dict] | None = None
        self.timeout: float | None = None

    async def call_tool(self, name, arguments, read_timeout_seconds=None):
        self.called = (name, arguments)
        self.timeout = read_timeout_seconds
        if self.error is not None:
            raise self.error
        return self.result


class McpToolTest(unittest.IsolatedAsyncioTestCase):
    def _tool(self, client, timeout: float = 60.0):
        handle = mcp.McpServerHandle("srv", timeout, client)
        return mcp.McpTool(
            spec=mcp.ToolSpec(name="srv__t", description="d", parameters={}),
            execution_mode="serial",
            remote_name="t",
            handle=handle,
            server="srv",
        )

    async def test_forwards_remote_name_and_arguments(self):
        client = _FakeClient(
            result=CallToolResult(content=[TextContent(type="text", text="ok")])
        )

        result = await self._tool(client, timeout=12.0).execute("c1", {"x": 1})

        self.assertEqual(client.called, ("t", {"x": 1}))
        self.assertEqual(client.timeout, 12.0)
        self.assertEqual(result.content, "ok")
        self.assertFalse(result.is_error)

    async def test_error_result_is_marked(self):
        client = _FakeClient(
            result=CallToolResult(
                content=[TextContent(type="text", text="bad input")], is_error=True
            )
        )

        result = await self._tool(client).execute("c1", {})

        self.assertTrue(result.is_error)
        self.assertEqual(result.content, "bad input")

    async def test_client_exception_becomes_error_result(self):
        client = _FakeClient(error=RuntimeError("disconnected"))

        result = await self._tool(client).execute("c1", {})

        self.assertTrue(result.is_error)
        self.assertIn("disconnected", result.content)


class ClipResultTest(unittest.TestCase):
    def test_short_text_is_unchanged(self):
        self.assertEqual(mcp.clip_result("hello"), "hello")

    def test_long_text_keeps_head_and_tail(self):
        text = "a" * 15_000 + "MIDDLE" + "b" * 15_000

        clipped = mcp.clip_result(text)

        self.assertIn("chars truncated", clipped)
        self.assertTrue(clipped.startswith("a" * 100))
        self.assertTrue(clipped.rstrip().endswith("]"))
        self.assertNotIn("MIDDLE", clipped)

    def test_full_text_is_written_to_a_file(self):
        text = "a" * 25_000

        clipped = mcp.clip_result(text)
        path = clipped.rsplit("[full output: ", 1)[1].rstrip("]")

        self.assertEqual(Path(path).read_text(encoding="utf-8"), text)
        Path(path).unlink()


class TrustTest(unittest.TestCase):
    def test_unknown_project_is_untrusted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "trusted.json"
            with patch.object(mcp, "trust_file", lambda: store):
                self.assertFalse(mcp.is_trusted(tmp))
                mcp.remember_trust(tmp)
                self.assertTrue(mcp.is_trusted(tmp))
                self.assertTrue(mcp.is_trusted(Path(tmp) / "."))

    def test_corrupt_store_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "trusted.json"
            store.write_text("{oops", encoding="utf-8")
            with patch.object(mcp, "trust_file", lambda: store):
                self.assertFalse(mcp.is_trusted(tmp))
                mcp.remember_trust(tmp)
                self.assertTrue(mcp.is_trusted(tmp))

    def test_forget_trust_removes_only_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "trusted.json"
            kept = Path(tmp) / "kept"
            kept.mkdir()
            with patch.object(mcp, "trust_file", lambda: store):
                mcp.remember_trust(tmp)
                mcp.remember_trust(kept)

                self.assertTrue(mcp.forget_trust(tmp))
                self.assertFalse(mcp.is_trusted(tmp))
                self.assertTrue(mcp.is_trusted(kept))

    def test_forget_trust_without_record_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "trusted.json"
            with patch.object(mcp, "trust_file", lambda: store):
                self.assertFalse(mcp.forget_trust(tmp))
                self.assertFalse(store.exists())


class ConnectTest(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_server_is_not_connected(self):
        session = await mcp.connect(
            [mcp.McpServer(name="off", command="nope", enabled=False)]
        )
        try:
            self.assertEqual(session.servers, ())
            self.assertEqual(session.errors, ())
            self.assertEqual(session.tools, ())
        finally:
            await session.aclose()

    async def test_bad_server_does_not_block_others(self):
        session = await mcp.connect(
            [mcp.McpServer(name="bad", command="definitely-not-a-real-command-xyz")]
        )
        try:
            self.assertEqual(session.servers, ())
            self.assertEqual(session.tools, ())
            self.assertEqual(len(session.errors), 1)
            self.assertIn("bad:", session.errors[0])
        finally:
            await session.aclose()


class MergeServersTest(unittest.TestCase):
    def test_merge_rules(self):
        user = [
            mcp.McpServer(name="shared", command="user"),
            mcp.McpServer(name="only_user", command="x"),
        ]

        with self.subTest("project overrides the same name"):
            merged = mcp.merge_servers(
                user, [mcp.McpServer(name="shared", command="project")]
            )
            self.assertEqual(len(merged), 2)
            self.assertEqual(merged[0].command, "project")

        with self.subTest("project-only servers are appended"):
            merged = mcp.merge_servers(user, [mcp.McpServer(name="only_project", command="y")])
            self.assertEqual(
                [server.name for server in merged],
                ["shared", "only_user", "only_project"],
            )

        with self.subTest("project can disable a user server"):
            merged = mcp.merge_servers(
                user, [mcp.McpServer(name="shared", command="user", enabled=False)]
            )
            self.assertFalse(merged[0].enabled)


class StderrTest(unittest.TestCase):
    def test_tail(self):
        self.assertEqual(mcp.stderr_tail(None), "")

        with tempfile.TemporaryFile("w+", encoding="utf-8") as sink:
            sink.write("first\nsecond\n")
            self.assertEqual(mcp.stderr_tail(sink), "first\nsecond")


class NotificationHandlerTest(unittest.IsolatedAsyncioTestCase):
    async def test_flags(self):
        flags = mcp.ChangeFlags()
        handler = mcp._notification_handler(flags)

        await handler(SimpleNamespace(method="notifications/tools/list_changed"))
        self.assertTrue(flags.tools)
        self.assertFalse(flags.resources)

        await handler(SimpleNamespace(method="notifications/resources/list_changed"))
        self.assertTrue(flags.resources)

        other = mcp.ChangeFlags()
        await mcp._notification_handler(other)(SimpleNamespace(method="notifications/message"))
        self.assertFalse(other.tools)
        self.assertFalse(other.resources)


class ReconnectTest(unittest.IsolatedAsyncioTestCase):
    async def test_reconnects_once_and_retries(self):
        first = _FakeClient(error=RuntimeError("disconnected"))
        second = _FakeClient(
            result=CallToolResult(content=[TextContent(type="text", text="ok")])
        )
        handle = mcp.McpServerHandle(
            "srv", 5.0, first, server=mcp.McpServer(name="srv", command="x")
        )

        async def opener(server, stack, errlog=None, flags=None, interactive=True):
            return second

        with patch.object(mcp, "_open_client", opener):
            result = await handle.call_tool("t", {})

        self.assertEqual(result.content[0].text, "ok")
        self.assertIs(handle.client, second)

    async def test_failed_reconnect_keeps_original_error(self):
        first = _FakeClient(error=RuntimeError("disconnected"))
        handle = mcp.McpServerHandle(
            "srv", 5.0, first, server=mcp.McpServer(name="srv", command="x")
        )

        async def opener(server, stack, errlog=None, flags=None):
            raise OSError("cannot start")

        with patch.object(mcp, "_open_client", opener):
            with self.assertRaises(RuntimeError) as caught:
                await handle.call_tool("t", {})

        self.assertIn("disconnected", str(caught.exception))


class _ListClient:
    def __init__(self, tools=None, error=None):
        self.tools = tools or []
        self.error = error
        self.calls = 0

    async def list_tools(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return SimpleNamespace(tools=self.tools)


def _remote_tool(name: str) -> SimpleNamespace:
    return SimpleNamespace(
        name=name, description="d", input_schema={}, annotations=None
    )


def _handle(name: str, client, *, flags=None, exposure="direct", description=""):
    return mcp.McpServerHandle(
        name,
        5.0,
        client,
        server=mcp.McpServer(
            name=name, command="x", exposure=exposure, description=description
        ),
        flags=flags,
    )


def _wrapped(handle, remote_name: str) -> mcp.McpTool:
    return mcp.McpTool(
        spec=mcp.ToolSpec(name=handle.name + "__" + remote_name, description="d", parameters={}),
        execution_mode="parallel",
        remote_name=remote_name,
        handle=handle,
        server=handle.name,
    )


class RefreshTest(unittest.IsolatedAsyncioTestCase):
    async def test_changed_server_is_relisted(self):
        flags = mcp.ChangeFlags(tools=True)
        handle = _handle("srv", _ListClient([_remote_tool("new")]), flags=flags)
        old = _wrapped(handle, "old")
        session = mcp.McpSession(
            tools=(old,),
            deferred=(),
            deferred_servers=(),
            handles=(handle,),
            resource_handles=(),
            errors=(),
        )

        refreshed = await session.refresh()

        self.assertEqual([tool.remote_name for tool in refreshed.tools], ["new"])
        self.assertFalse(flags.tools)

    async def test_failure_keeps_old_tools(self):
        flags = mcp.ChangeFlags(tools=True)
        handle = _handle("srv", _ListClient(error=RuntimeError("boom")), flags=flags)
        old = _wrapped(handle, "old")
        session = mcp.McpSession(
            tools=(old,),
            deferred=(),
            deferred_servers=(),
            handles=(handle,),
            resource_handles=(),
            errors=(),
        )

        refreshed = await session.refresh()

        self.assertEqual(refreshed.tools, (old,))
        self.assertFalse(flags.tools)

    async def test_deferred_server_is_reported(self):
        flags = mcp.ChangeFlags(tools=True)
        handle = _handle(
            "srv",
            _ListClient([_remote_tool("d1")]),
            flags=flags,
            exposure="deferred",
            description="Demo",
        )
        session = mcp.McpSession(
            tools=(),
            deferred=(),
            deferred_servers=(),
            handles=(handle,),
            resource_handles=(),
            errors=(),
        )

        refreshed = await session.refresh()

        self.assertEqual(refreshed.tools, ())
        self.assertEqual([tool.remote_name for tool in refreshed.deferred], ["d1"])
        self.assertEqual(refreshed.deferred_servers, (("srv", "Demo"),))


class SupportsResourcesTest(unittest.IsolatedAsyncioTestCase):
    async def test_probe(self):
        class Working:
            async def list_resources(self, cursor=None):
                return SimpleNamespace(resources=[], next_cursor=None)

        class Failing:
            async def list_resources(self, cursor=None):
                raise RuntimeError("method not found")

        class Hanging:
            async def list_resources(self, cursor=None):
                await asyncio.sleep(1)

        with self.subTest("listing works"):
            self.assertTrue(await mcp.supports_resources(Working(), 1.0))

        with self.subTest("server errors"):
            self.assertFalse(await mcp.supports_resources(Failing(), 1.0))

        with self.subTest("timeout"):
            self.assertFalse(await mcp.supports_resources(Hanging(), 0.01))


SERVER_SOURCE = '''
from mcp.server.mcpserver import MCPServer

server = MCPServer("demo", log_level="ERROR")


@server.tool()
def add(a: int, b: int) -> str:
    """Add two numbers."""
    return str(a + b)


if __name__ == "__main__":
    server.run("stdio")
'''


class StdioConnectionTest(unittest.IsolatedAsyncioTestCase):
    async def test_lists_and_calls_tool_over_stdio(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "server.py"
            script.write_text(SERVER_SOURCE, encoding="utf-8")
            servers = [
                mcp.McpServer(
                    name="demo",
                    command=sys.executable,
                    args=(str(script),),
                    cwd=tmp,
                    exposure="direct",
                )
            ]

            async with asyncio.timeout(60):
                session = await mcp.connect(servers)
            try:
                self.assertEqual(session.servers, ("demo",))
                self.assertEqual(
                    [tool.spec.name for tool in session.tools], ["mcp__demo__add"]
                )
                self.assertEqual(session.deferred, ())

                result = await session.tools[0].execute("c1", {"a": 2, "b": 3})

                self.assertFalse(result.is_error)
                self.assertEqual(result.content, "5")
            finally:
                await session.aclose()

    async def test_deferred_tools_are_not_declared(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "server.py"
            script.write_text(SERVER_SOURCE, encoding="utf-8")
            servers = [
                mcp.McpServer(
                    name="demo",
                    command=sys.executable,
                    args=(str(script),),
                    cwd=tmp,
                    description="Demo tools",
                )
            ]

            async with asyncio.timeout(60):
                session = await mcp.connect(servers)
            try:
                self.assertEqual(session.tools, ())
                self.assertEqual(
                    [tool.spec.name for tool in session.deferred], ["mcp__demo__add"]
                )
                self.assertEqual(session.deferred_servers, (("demo", "Demo tools"),))
            finally:
                await session.aclose()


FAIL_SOURCE = '''
import sys

print("boom from server", file=sys.stderr)
sys.stderr.flush()
sys.exit(1)
'''


ENV_SERVER_SOURCE = '''
import os

from mcp.server.mcpserver import MCPServer

server = MCPServer("env", log_level="ERROR")


@server.tool()
def read_env(name: str) -> str:
    """Read an environment variable."""
    return os.environ.get(name, "<missing>")


if __name__ == "__main__":
    server.run("stdio")
'''


class StdioEnvironmentTest(unittest.IsolatedAsyncioTestCase):
    """stdio server 只拿到白名单 env，宿主凭证不外传。"""

    async def test_only_the_allowlist_and_configured_env_reach_the_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "server.py"
            script.write_text(ENV_SERVER_SOURCE, encoding="utf-8")
            servers = [
                mcp.McpServer(
                    name="env",
                    command=sys.executable,
                    args=(str(script),),
                    cwd=tmp,
                    exposure="direct",
                    env={"GRADUS_MCP_TEST_EXTRA": "configured"},
                )
            ]

            with patch.dict(
                os.environ, {"GRADUS_MCP_TEST_SECRET": "must-not-leak"}, clear=False
            ):
                async with asyncio.timeout(60):
                    session = await mcp.connect(servers)
            try:
                tool = session.tools[0]

                async def read(name: str) -> str:
                    result = await tool.execute("call", {"name": name})
                    self.assertFalse(result.is_error, result.content)
                    return result.content

                self.assertEqual(await read("GRADUS_MCP_TEST_SECRET"), "<missing>")
                self.assertNotEqual(await read("PATH"), "<missing>")
                self.assertEqual(await read("GRADUS_MCP_TEST_EXTRA"), "configured")
            finally:
                await session.aclose()


FAIL_SOURCE = '''
import sys

print("boom from server", file=sys.stderr)
sys.stderr.flush()
sys.exit(1)
'''


class StderrConnectionTest(unittest.IsolatedAsyncioTestCase):
    async def test_connect_error_includes_server_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "bad.py"
            script.write_text(FAIL_SOURCE, encoding="utf-8")
            servers = [
                mcp.McpServer(
                    name="bad",
                    command=sys.executable,
                    args=(str(script),),
                    cwd=tmp,
                )
            ]

            async with asyncio.timeout(60):
                session = await mcp.connect(servers)
            try:
                self.assertEqual(len(session.errors), 1)
                self.assertIn("[stderr]", session.errors[0])
                self.assertIn("boom from server", session.errors[0])
            finally:
                await session.aclose()


if __name__ == "__main__":
    unittest.main()
