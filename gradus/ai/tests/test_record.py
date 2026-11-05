"""`record.py` 的 key 解析：按协议的 key 优先，其次通用 key。"""

from __future__ import annotations

import unittest

from gradus.ai.scripts.record import GENERIC_KEY, PROVIDERS, resolve_key


class ResolveKeyTest(unittest.TestCase):
    def test_protocol_key_wins(self):
        protocol_key = PROVIDERS["openai_compat"][1]

        key, name = resolve_key(
            {protocol_key: "protocol", GENERIC_KEY: "generic"}, "openai_compat"
        )

        self.assertEqual(key, "protocol")
        self.assertEqual(name, protocol_key)

    def test_falls_back_to_generic_key(self):
        key, name = resolve_key({GENERIC_KEY: "generic"}, "openai_compat")

        self.assertEqual(key, "generic")
        self.assertEqual(name, GENERIC_KEY)

    def test_missing_key_reports_the_protocol_variable(self):
        key, name = resolve_key({}, "gemini")

        self.assertEqual(key, "")
        self.assertEqual(name, PROVIDERS["gemini"][1])

    def test_blank_values_are_ignored(self):
        protocol_key = PROVIDERS["anthropic"][1]

        key, name = resolve_key(
            {protocol_key: "  ", GENERIC_KEY: "generic"}, "anthropic"
        )

        self.assertEqual(key, "generic")
        self.assertEqual(name, GENERIC_KEY)
