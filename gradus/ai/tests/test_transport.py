"""传输层测试：SSE 解析是纯函数，`post_sse` 用 httpx.MockTransport 离线跑。

`parse_sse` 同步、纯函数，用普通 `TestCase` 直接断言；`post_sse` 是异步迭代器，
用 `IsolatedAsyncioTestCase` 的 `async def test_*` 边收边断言。

运行（在 Gradus 目录下）：
    uv run pytest
"""

from __future__ import annotations

import unittest

import httpx

from gradus.ai.transport import TransportFailure, parse_sse, post_sse

HEADERS = {"Content-Type": "application/json"}
URL = "https://example.test/v1/stream"


def make_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestParseSse(unittest.TestCase):
    def test_single_event(self):
        events = list(parse_sse(["event: message", 'data: {"a":1}', ""]))

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event, "message")
        self.assertEqual(events[0].data, '{"a":1}')

    def test_event_name_defaults_to_none(self):
        events = list(parse_sse(['data: hello', ""]))

        self.assertIsNone(events[0].event)

    def test_multiline_data_joined_with_newline(self):
        events = list(parse_sse(["data: line1", "data: line2", ""]))

        self.assertEqual(events[0].data, "line1\nline2")

    def test_comments_and_unknown_fields_ignored(self):
        events = list(parse_sse([": keep-alive", "id: 42", "retry: 100", "data: x", ""]))

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].data, "x")

    def test_crlf_and_missing_trailing_blank_line(self):
        events = list(parse_sse(["data: a\r\n", "\r\n", "data: b\r\n"]))

        self.assertEqual([e.data for e in events], ["a", "b"])

    def test_empty_data_is_delivered(self):
        events = list(parse_sse(["data:", ""]))

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].data, "")

    def test_record_without_data_is_skipped(self):
        events = list(parse_sse(["event: ping", "", ""]))

        self.assertEqual(events, [])

    def test_done_sentinel_preserved_verbatim(self):
        events = list(parse_sse(["data: [DONE]", ""]))

        self.assertEqual(events[0].data, "[DONE]")

    def test_space_after_colon_stripped_once(self):
        events = list(parse_sse(["data:  two spaces", ""]))

        self.assertEqual(events[0].data, " two spaces")


class TestPostSse(unittest.IsolatedAsyncioTestCase):
    async def test_streams_events(self):
        expected = ['{"t":"a"}', '{"t":"b"}']
        index = 0

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.headers["accept"], "text/event-stream")
            body = 'event: delta\ndata: {"t":"a"}\n\nevent: delta\ndata: {"t":"b"}\n\n'
            return httpx.Response(200, content=body.encode())

        async with make_client(handler) as client:
            async for item in post_sse(
                URL,
                headers={"accept": "text/event-stream"},
                payload={"model": "x"},
                client=client,
                backoff=0,
            ):
                self.assertNotIsInstance(item, TransportFailure)
                self.assertEqual(item.data, expected[index], f"第 {index} 条记录")
                index += 1

        self.assertEqual(index, len(expected))

    async def test_retries_on_429_then_succeeds(self):
        calls = {"n": 0}
        count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"retry-after": "0"}, content=b"slow down")
            return httpx.Response(200, content=b"data: ok\n\n")

        async with make_client(handler) as client:
            async for item in post_sse(
                URL, headers=HEADERS, payload={}, client=client, max_retries=2, backoff=0
            ):
                self.assertEqual(item.data, "ok")
                count += 1

        self.assertEqual(calls["n"], 2)
        self.assertEqual(count, 1)

    async def test_does_not_retry_401(self):
        calls = {"n": 0}
        count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(401, content=b'{"error":"bad key"}')

        async with make_client(handler) as client:
            async for item in post_sse(URL, headers=HEADERS, payload={}, client=client, backoff=0):
                count += 1
                self.assertIsInstance(item, TransportFailure)
                self.assertEqual(item.status_code, 401)
                self.assertIn("401", item.message)
                self.assertIn("bad key", item.message)

        self.assertEqual(calls["n"], 1)
        self.assertEqual(count, 1)

    async def test_gives_up_after_max_retries(self):
        calls = {"n": 0}
        count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.ConnectError("refused")

        async with make_client(handler) as client:
            async for item in post_sse(
                URL, headers=HEADERS, payload={}, client=client, max_retries=2, backoff=0
            ):
                count += 1
                self.assertIsNone(item.status_code)
                self.assertIn("connection failed", item.message)

        # 一次原始请求 + 两次重试
        self.assertEqual(calls["n"], 3)
        self.assertEqual(count, 1)

    async def test_never_raises_on_binary_garbage(self):
        count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"\xff\xfe not utf8\n\n")

        async with make_client(handler) as client:
            async for _ in post_sse(
                URL, headers=HEADERS, payload={}, client=client, backoff=0
            ):
                count += 1

        # 不抛异常：最差是没解析出事件，或解析出一段乱码
        self.assertLessEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
