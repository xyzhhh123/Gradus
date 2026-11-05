"""MCP 资源工具的离线测试。"""

from __future__ import annotations

import base64
import json
import os
import unittest
from types import SimpleNamespace

from gradus.coding.mcp import McpServerHandle
from gradus.coding.tools.mcp_resources import create_mcp_resource_tools


def _resource(uri: str, name: str = "r", **kwargs) -> SimpleNamespace:
    data = {
        "uri": uri,
        "name": name,
        "title": None,
        "description": None,
        "mime_type": None,
        "size": None,
    }
    data.update(kwargs)
    return SimpleNamespace(**data)


def _template(uri_template: str, name: str = "t", **kwargs) -> SimpleNamespace:
    data = {
        "uri_template": uri_template,
        "name": name,
        "title": None,
        "description": None,
        "mime_type": None,
    }
    data.update(kwargs)
    return SimpleNamespace(**data)


class FakeClient:
    def __init__(self, resources=None, templates=None, contents=None, pages=None, fail=False):
        self.resources = resources or []
        self.templates = templates or []
        self.contents = contents or []
        self.pages = pages
        self.fail = fail
        self.calls: list[tuple[str, object]] = []

    async def list_resources(self, cursor=None):
        self.calls.append(("resources", cursor))
        if self.fail:
            raise RuntimeError("boom")
        if self.pages is None:
            return SimpleNamespace(resources=self.resources, next_cursor=None)
        index = 0 if cursor is None else int(cursor)
        items, next_cursor = self.pages[index]
        return SimpleNamespace(resources=items, next_cursor=next_cursor)

    async def list_resource_templates(self, cursor=None):
        self.calls.append(("templates", cursor))
        if self.fail:
            raise RuntimeError("boom")
        return SimpleNamespace(resource_templates=self.templates, next_cursor=None)

    async def read_resource(self, uri):
        self.calls.append(("read", uri))
        if self.fail:
            raise RuntimeError("nope")
        return SimpleNamespace(contents=self.contents)


def _tools(*clients: tuple[str, FakeClient]) -> dict[str, object]:
    handles = tuple(McpServerHandle(name, 5.0, client) for name, client in clients)
    return {tool.spec.name: tool for tool in create_mcp_resource_tools(handles)}


class ListResourcesTest(unittest.IsolatedAsyncioTestCase):
    async def test_lists_all_servers_with_pagination(self):
        a = FakeClient(
            pages=[
                ([_resource("file://a1")], "1"),
                ([_resource("file://a2")], None),
            ]
        )
        b = FakeClient(resources=[_resource("file://b")])
        tools = _tools(("b", b), ("a", a))

        result = await tools["list_mcp_resources"].execute("c1", {})

        payload = json.loads(result.content)
        self.assertEqual(
            [(item["server"], item["uri"]) for item in payload["resources"]],
            [("a", "file://a1"), ("a", "file://a2"), ("b", "file://b")],
        )
        self.assertNotIn("errors", payload)

    async def test_server_filter_returns_one_page(self):
        a = FakeClient(
            pages=[
                ([_resource("file://a1")], "1"),
                ([_resource("file://a2")], None),
            ]
        )
        tool = _tools(("a", a))["list_mcp_resources"]

        first = json.loads((await tool.execute("c1", {"server": "a"})).content)
        self.assertEqual(first["server"], "a")
        self.assertEqual(first["nextCursor"], "1")
        self.assertEqual([item["uri"] for item in first["resources"]], ["file://a1"])

        second = json.loads(
            (await tool.execute("c2", {"server": "a", "cursor": "1"})).content
        )
        self.assertNotIn("nextCursor", second)
        self.assertEqual([item["uri"] for item in second["resources"]], ["file://a2"])

    async def test_unknown_server_is_rejected(self):
        tool = _tools(("a", FakeClient()))["list_mcp_resources"]

        with self.assertRaises(ValueError) as caught:
            await tool.execute("c1", {"server": "nope"})

        self.assertIn("available: a", str(caught.exception))

    async def test_cursor_without_server_is_rejected(self):
        tool = _tools(("a", FakeClient()))["list_mcp_resources"]

        with self.assertRaises(ValueError):
            await tool.execute("c1", {"cursor": "1"})

    async def test_failing_server_is_collected(self):
        good = FakeClient(resources=[_resource("file://ok")])
        bad = FakeClient(fail=True)
        tool = _tools(("good", good), ("bad", bad))["list_mcp_resources"]

        payload = json.loads((await tool.execute("c1", {})).content)

        self.assertEqual([item["uri"] for item in payload["resources"]], ["file://ok"])
        self.assertEqual(payload["errors"][0]["server"], "bad")
        self.assertIn("RuntimeError", payload["errors"][0]["error"])

    async def test_ui_resources_are_skipped(self):
        client = FakeClient(
            resources=[
                _resource("ui://widget"),
                _resource("file://plain"),
                _resource("file://app", mime_type="text/html; profile=mcp-app"),
            ]
        )
        tool = _tools(("a", client))["list_mcp_resources"]

        payload = json.loads((await tool.execute("c1", {})).content)

        self.assertEqual([item["uri"] for item in payload["resources"]], ["file://plain"])

    async def test_templates_use_uri_template_key(self):
        client = FakeClient(templates=[_template("file://{path}", description="files")])
        tool = _tools(("a", client))["list_mcp_resource_templates"]

        payload = json.loads((await tool.execute("c1", {})).content)

        self.assertEqual(payload["resourceTemplates"][0]["uriTemplate"], "file://{path}")
        self.assertEqual(payload["resourceTemplates"][0]["server"], "a")


class ReadResourceTest(unittest.IsolatedAsyncioTestCase):
    async def test_reads_text(self):
        client = FakeClient(
            contents=[SimpleNamespace(uri="file://a", text="hello", mime_type="text/plain")]
        )
        tool = _tools(("a", client))["read_mcp_resource"]

        result = await tool.execute("c1", {"server": "a", "uri": "file://a"})

        self.assertEqual(result.content, "hello")
        self.assertFalse(result.is_error)

    async def test_multiple_contents_are_labelled(self):
        client = FakeClient(
            contents=[
                SimpleNamespace(uri="file://a", text="one", mime_type=None),
                SimpleNamespace(uri="file://b", text="two", mime_type=None),
            ]
        )
        tool = _tools(("a", client))["read_mcp_resource"]

        result = await tool.execute("c1", {"server": "a", "uri": "file://dir"})

        self.assertEqual(result.content, "file://a: one\nfile://b: two")

    async def test_binary_is_spilled_to_file(self):
        data = b"\x00\x01\x02"
        client = FakeClient(
            contents=[
                SimpleNamespace(
                    uri="file://blob", blob=base64.b64encode(data).decode(), mime_type="application/pdf"
                )
            ]
        )
        tool = _tools(("a", client))["read_mcp_resource"]

        result = await tool.execute("c1", {"server": "a", "uri": "file://blob"})

        path = result.content.rsplit(": ", 1)[-1].rstrip("]")
        try:
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), data)
            self.assertTrue(path.endswith(".pdf"))
        finally:
            os.unlink(path)

    async def test_server_and_uri_are_required(self):
        tool = _tools(("a", FakeClient()))["read_mcp_resource"]

        with self.assertRaises(ValueError):
            await tool.execute("c1", {"uri": "file://a"})
        with self.assertRaises(ValueError):
            await tool.execute("c2", {"server": "a"})


class ToolShapeTest(unittest.TestCase):
    def test_tools_are_parallel_and_named(self):
        tools = _tools(("a", FakeClient()))

        self.assertEqual(
            sorted(tools),
            ["list_mcp_resource_templates", "list_mcp_resources", "read_mcp_resource"],
        )
        for tool in tools.values():
            self.assertEqual(tool.execution_mode, "parallel")
