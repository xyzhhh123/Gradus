"""选择器的离线测试：输出解析、id 校验、封顶、失败不抛。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from gradus.ai.base import ModelSpec
from gradus.ai.types import StreamDone, TextDelta, Usage
from gradus.coding import selector
from gradus.coding.config import Settings


def _settings() -> Settings:
    return Settings(
        spec=ModelSpec(
            provider="anthropic",
            model="m",
            base_url="https://api.anthropic.com",
            context_window=200_000,
            max_output_tokens=8_192,
        ),
        api_key="k",
    )


class _Recorder:
    """假的 provider：回放给定的文本增量。"""

    def __init__(self, text: str) -> None:
        self._text = text
        self.requests = []

    def stream(self, request):
        self.requests.append(request)

        async def run():
            yield TextDelta(content_index=0, delta=self._text, partial=None)
            yield StreamDone(
                stop_reason="stop", message=None, usage=Usage(input_tokens=100, output_tokens=10)
            )

        return run()


class ParseTest(unittest.TestCase):
    def test_keeps_only_known_ids_and_dedupes(self):
        parsed = selector._parse_ids('{"ids": ["e4", "e9", "e4"]}', ["e4", "e5"])

        self.assertEqual(parsed, ("e4",))

    def test_tolerates_code_fences(self):
        parsed = selector._parse_ids('```json\n{"ids": ["e2"]}\n```', ["e2"])

        self.assertEqual(parsed, ("e2",))

    def test_stops_at_the_cap(self):
        parsed = selector._parse_ids('{"ids": ["e1", "e2", "e3", "e4"]}', ["e1", "e2", "e3", "e4"])

        self.assertEqual(len(parsed), selector.MAX_PICKS)

    def test_garbage_gives_empty(self):
        self.assertEqual(selector._parse_ids("我觉得是 e4", ["e4"]), ())
        self.assertEqual(selector._parse_ids('{"ids": "e4"}', ["e4"]), ())


class SelectIdsTest(unittest.IsolatedAsyncioTestCase):
    async def test_parses_and_reports_usage(self):
        recorder = _Recorder('{"ids": ["e2"]}')
        candidates = [("e1", "改重试次数"), ("e2", "缓存用 LRU")]

        with patch.object(selector, "create_provider", lambda *a, **k: recorder):
            result = await selector.select_ids(_settings(), "缓存", candidates)

        self.assertEqual(result.ids, ("e2",))
        self.assertIsNotNone(result.usage)
        self.assertEqual(result.usage.input_tokens, 100)
        sent = recorder.requests[0].messages[0].content
        self.assertIn("e2", sent)

    async def test_no_candidates_skips_the_call(self):
        recorder = _Recorder('{"ids": ["e1"]}')

        with patch.object(selector, "create_provider", lambda *a, **k: recorder):
            result = await selector.select_ids(_settings(), "随便", [])

        self.assertEqual(result.ids, ())
        self.assertEqual(recorder.requests, [])

    async def test_provider_failure_returns_empty(self):
        def boom(*args, **kwargs):
            raise RuntimeError("network down")

        with patch.object(selector, "create_provider", boom):
            result = await selector.select_ids(_settings(), "缓存", [("e1", "x")])

        self.assertEqual(result.ids, ())


if __name__ == "__main__":
    unittest.main()
