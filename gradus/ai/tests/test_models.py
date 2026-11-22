"""模型能力表的离线测试：查表、解析元数据、探测失败时的静默。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx

from gradus.ai import models


class LimitsTableTest(unittest.TestCase):
    def test_verified_model_is_found(self):
        limits = models.limits_for("deepseek-v4.1-flash")

        self.assertIsNotNone(limits)
        assert limits is not None
        self.assertEqual(limits.window, 1_000_000)
        self.assertEqual(limits.output, 384_000)

    def test_namespaced_id_falls_back_to_bare_name(self):
        limits = models.limits_for("opencode/deepseek-v4.1-flash-1017")

        self.assertIsNotNone(limits)

    def test_unknown_model_returns_none(self):
        self.assertIsNone(models.limits_for("some-model-nobody-verified"))


class ParseLimitsTest(unittest.TestCase):
    def test_reads_openai_style_limit_object(self):
        limits = models.parse_limits({"id": "m", "limit": {"context": 1000, "output": 100}})

        self.assertEqual((limits.window, limits.output), (1000, 100))

    def test_reads_flat_fields(self):
        limits = models.parse_limits({"context_window": 200_000, "max_output_tokens": 8_192})

        self.assertEqual((limits.window, limits.output), (200_000, 8_192))

    def test_partial_metadata_keeps_the_missing_side_empty(self):
        limits = models.parse_limits({"limit": {"context": 5000}})

        self.assertEqual(limits.window, 5000)
        self.assertIsNone(limits.output)

    def test_boolean_is_not_a_limit(self):
        self.assertIsNone(models.parse_limits({"context": True}))

    def test_empty_entry_returns_none(self):
        self.assertIsNone(models.parse_limits({"id": "m"}))


class FetchLimitsTest(unittest.IsolatedAsyncioTestCase):
    def _client(self, payload, status: int = 200) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/v1/models")
            return httpx.Response(status, json=payload)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def test_picks_the_matching_model(self):
        client = self._client(
            {
                "data": [
                    {"id": "other", "limit": {"context": 1, "output": 1}},
                    {
                        "id": "deepseek-v4.1-flash",
                        "limit": {"context": 1_000_000, "output": 384_000},
                    },
                ]
            }
        )

        limits = await models.fetch_limits(
            "https://example.test/v1", "deepseek-v4.1-flash", client=client
        )

        self.assertIsNotNone(limits)
        assert limits is not None
        self.assertEqual(limits.window, 1_000_000)

    async def test_http_error_is_silent(self):
        client = self._client({"error": "nope"}, status=500)

        self.assertIsNone(
            await models.fetch_limits("https://example.test/v1", "m", client=client)
        )

    async def test_network_failure_is_silent(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        self.assertIsNone(
            await models.fetch_limits("https://example.test/v1", "m", client=client)
        )

    async def test_model_missing_from_the_list_is_silent(self):
        client = self._client({"data": [{"id": "other"}]})

        self.assertIsNone(
            await models.fetch_limits("https://example.test/v1", "m", client=client)
        )

    async def test_authorization_header_is_sent(self):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("authorization", ""))
            return httpx.Response(200, json={"data": []})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await models.fetch_limits(
            "https://example.test/v1", "m", api_key="sk-test", client=client
        )

        self.assertEqual(seen, ["Bearer sk-test"])


if __name__ == "__main__":
    unittest.main()
