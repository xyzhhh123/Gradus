"""config 的离线测试。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gradus.ai import envfile
from gradus.ai.base import ModelSpec
from gradus.coding import config
from gradus.coding.config import (
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_MAX_OUTPUT_TOKENS,
    ConfigError,
    load,
)

FULL_ENV = {
    "GRADUS_PROVIDER": "openai_compat",
    "GRADUS_MODEL": "deepseek-chat",
    "GRADUS_BASE_URL": "https://api.deepseek.com/v1",
    "GRADUS_API_KEY": "sk-test",
}

_default_path_patch = None


def setUpModule() -> None:
    """把默认配置路径指到不存在的文件，避免读到开发机上的 ~/.gradus/config.json。"""
    global _default_path_patch
    _default_path_patch = patch.object(
        config, "default_config_path", lambda: Path("missing-gradus-config.json")
    )
    _default_path_patch.start()


def tearDownModule() -> None:
    if _default_path_patch is not None:
        _default_path_patch.stop()


class LoadTest(unittest.TestCase):
    def test_builds_spec_from_env(self):
        settings = load(env=FULL_ENV)

        self.assertEqual(
            settings.spec,
            ModelSpec(
                provider="openai_compat",
                model="deepseek-chat",
                base_url="https://api.deepseek.com/v1",
                context_window=DEFAULT_CONTEXT_WINDOW,
                max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
            ),
        )
        self.assertEqual(settings.api_key, "sk-test")

    def test_missing_required_names_variable(self):
        cases = [
            ("GRADUS_PROVIDER", {}),
            ("GRADUS_MODEL", {"GRADUS_PROVIDER": "anthropic"}),
            (
                "GRADUS_BASE_URL",
                {"GRADUS_PROVIDER": "anthropic", "GRADUS_MODEL": "claude"},
            ),
        ]

        for name, env in cases:
            with self.subTest(name=name), patch.dict("os.environ", {}, clear=True):
                with self.assertRaises(ConfigError) as ctx:
                    load(env=env)
                self.assertIn(name, str(ctx.exception))

    def test_fake_provider_is_rejected(self):
        env = dict(FULL_ENV, GRADUS_PROVIDER="fake")

        with self.assertRaises(ConfigError) as ctx:
            load(env=env)

        self.assertIn("GRADUS_PROVIDER", str(ctx.exception))
        self.assertIn("fake", str(ctx.exception))

    def test_injected_env_overrides_and_falls_back(self):
        with patch.dict(
            "os.environ",
            {"GRADUS_MODEL": "from-os", "GRADUS_API_KEY": "sk-os"},
            clear=True,
        ):
            settings = load(
                env={
                    "GRADUS_PROVIDER": "anthropic",
                    "GRADUS_MODEL": "from-injected",
                    "GRADUS_BASE_URL": "https://api.anthropic.com",
                }
            )

        self.assertEqual(settings.spec.model, "from-injected")
        self.assertEqual(settings.api_key, "sk-os")

    def test_api_key_defaults_to_empty(self):
        env = dict(FULL_ENV)
        env.pop("GRADUS_API_KEY")

        with patch.dict("os.environ", {}, clear=True):
            settings = load(env=env)

        self.assertEqual(settings.api_key, "")

    def test_windows_are_configurable(self):
        settings = load(
            env=dict(
                FULL_ENV,
                GRADUS_CONTEXT_WINDOW="65536",
                GRADUS_MAX_OUTPUT_TOKENS="4096",
            )
        )

        self.assertEqual(settings.spec.context_window, 65_536)
        self.assertEqual(settings.spec.max_output_tokens, 4_096)

    def test_invalid_window_names_variable(self):
        for name in ("GRADUS_CONTEXT_WINDOW", "GRADUS_MAX_OUTPUT_TOKENS"):
            for raw in ("abc", "0", "-1", ""):
                with self.subTest(name=name, raw=raw):
                    with self.assertRaises(ConfigError) as ctx:
                        load(env=dict(FULL_ENV, **{name: raw}))
                    self.assertIn(name, str(ctx.exception))

    def test_api_key_is_stripped(self):
        settings = load(env=dict(FULL_ENV, GRADUS_API_KEY="  sk-test  "))

        self.assertEqual(settings.api_key, "sk-test")

    def test_base_url_requires_http_scheme(self):
        with self.assertRaises(ConfigError) as ctx:
            load(env=dict(FULL_ENV, GRADUS_BASE_URL="api.deepseek.com"))

        self.assertIn("GRADUS_BASE_URL", str(ctx.exception))
        self.assertIn("http", str(ctx.exception))

    def test_overrides_win_over_env_and_os_environ(self):
        with patch.dict("os.environ", {"GRADUS_MODEL": "from-os"}, clear=True):
            settings = load(
                env=dict(FULL_ENV, GRADUS_MODEL="from-env"),
                overrides={"GRADUS_MODEL": "from-override"},
            )

        self.assertEqual(settings.spec.model, "from-override")

    def test_repr_hides_api_key(self):
        settings = load(env=FULL_ENV)

        self.assertNotIn("sk-test", repr(settings))


class EnvFileTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / ".env"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_file_overrides_existing_env(self):
        self.path.write_text("GRADUS_MODEL=from-file\nGRADUS_API_KEY=sk-file\n", encoding="utf-8")

        with patch.dict("os.environ", {"GRADUS_MODEL": "from-os"}, clear=True):
            envfile.load_env_file(self.path)
            self.assertEqual(os.environ["GRADUS_MODEL"], "from-file")
            self.assertEqual(os.environ["GRADUS_API_KEY"], "sk-file")

    def test_blank_values_are_skipped(self):
        self.path.write_text("GRADUS_MODEL=\n", encoding="utf-8")

        with patch.dict("os.environ", {"GRADUS_MODEL": "from-os"}, clear=True):
            envfile.load_env_file(self.path)
            self.assertEqual(os.environ["GRADUS_MODEL"], "from-os")

    def test_ignores_comments_blanks_and_junk(self):
        self.path.write_text(
            "# comment\n\nnot-a-pair\nexport GRADUS_PROVIDER=anthropic\n",
            encoding="utf-8",
        )

        with patch.dict("os.environ", {}, clear=True):
            envfile.load_env_file(self.path)
            self.assertEqual(os.environ["GRADUS_PROVIDER"], "anthropic")
            self.assertNotIn("not-a-pair", os.environ)

    def test_strips_matching_quotes(self):
        self.path.write_text(
            "A='single value'\nB=\"double value\"\nC=plain\n", encoding="utf-8"
        )

        with patch.dict("os.environ", {}, clear=True):
            envfile.load_env_file(self.path)
            self.assertEqual(os.environ["A"], "single value")
            self.assertEqual(os.environ["B"], "double value")
            self.assertEqual(os.environ["C"], "plain")

    def test_missing_file_is_noop(self):
        with patch.dict("os.environ", {}, clear=True):
            before = dict(os.environ)
            envfile.load_env_file(self.path)
            self.assertEqual(dict(os.environ), before)


class ConfigFileTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, payload, name: str = "config.json") -> Path:
        path = self.folder / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_reads_every_field_from_file(self):
        path = self._write(
            {
                "provider": "anthropic",
                "model": "claude-test",
                "base_url": "https://api.anthropic.com",
                "api_key": "sk-file",
                "context_window": 65536,
                "max_output_tokens": 4096,
            }
        )

        with patch.dict("os.environ", {}, clear=True):
            settings = load(config=path)

        self.assertEqual(settings.spec.provider, "anthropic")
        self.assertEqual(settings.spec.model, "claude-test")
        self.assertEqual(settings.api_key, "sk-file")
        self.assertEqual(settings.spec.context_window, 65_536)
        self.assertEqual(settings.spec.max_output_tokens, 4_096)

    def test_env_overrides_file_but_file_fills_gaps(self):
        path = self._write(
            {
                "provider": "anthropic",
                "model": "from-file",
                "base_url": "https://api.anthropic.com",
                "api_key": "sk-file",
            }
        )

        with patch.dict("os.environ", {"GRADUS_MODEL": "from-os"}, clear=True):
            settings = load(config=path)

        self.assertEqual(settings.spec.model, "from-os")
        self.assertEqual(settings.api_key, "sk-file")

    def test_overrides_beat_both(self):
        path = self._write(
            {
                "provider": "anthropic",
                "model": "from-file",
                "base_url": "https://api.anthropic.com",
            }
        )

        with patch.dict("os.environ", {"GRADUS_MODEL": "from-os"}, clear=True):
            settings = load(
                config=path,
                env={"GRADUS_MODEL": "from-env"},
                overrides={"GRADUS_MODEL": "from-flag"},
            )

        self.assertEqual(settings.spec.model, "from-flag")

    def test_missing_default_file_is_fine(self):
        with patch.dict("os.environ", {}, clear=True):
            settings = load(env=FULL_ENV)

        self.assertEqual(settings.spec.model, "deepseek-chat")

    def test_explicit_missing_file_errors(self):
        with self.assertRaises(ConfigError) as ctx:
            load(config=self.folder / "none.json")

        self.assertIn("不存在", str(ctx.exception))

    def test_invalid_json_errors(self):
        path = self.folder / "bad.json"
        path.write_text("{not json", encoding="utf-8")

        with self.assertRaises(ConfigError) as ctx:
            load(config=path)

        self.assertIn("JSON", str(ctx.exception))

    def test_unknown_field_errors(self):
        path = self._write({"provider": "anthropic", "temperature": 0.5})

        with self.assertRaises(ConfigError) as ctx:
            load(config=path)

        self.assertIn("temperature", str(ctx.exception))

    def test_bool_and_object_values_are_rejected(self):
        for value in (True, {"a": 1}, [1]):
            with self.subTest(value=value):
                path = self._write({"provider": "anthropic", "model": value})
                with self.assertRaises(ConfigError):
                    load(config=path)

    def test_expands_environment_variable(self):
        path = self._write(
            {
                "provider": "anthropic",
                "model": "claude-test",
                "base_url": "https://api.anthropic.com",
                "api_key": "${MY_KEY}",
            }
        )

        with patch.dict("os.environ", {"MY_KEY": "sk-real"}, clear=True):
            settings = load(config=path)

        self.assertEqual(settings.api_key, "sk-real")

    def test_missing_expansion_variable_errors(self):
        path = self._write(
            {
                "provider": "anthropic",
                "model": "claude-test",
                "base_url": "https://api.anthropic.com",
                "api_key": "${NOT_SET_ANYWHERE}",
            }
        )

        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ConfigError) as ctx:
                load(config=path)

        self.assertIn("NOT_SET_ANYWHERE", str(ctx.exception))

    def test_service_field_fills_provider_and_base_url(self):
        path = self._write({"service": "kimi-compat", "model": "kimi-k2"})

        with patch.dict("os.environ", {}, clear=True):
            settings = load(config=path)

        self.assertEqual(settings.spec.provider, "openai_compat")
        self.assertEqual(settings.spec.base_url, "https://api.moonshot.cn/v1")

    def test_file_still_validates_fields(self):
        path = self._write(
            {
                "provider": "nope",
                "model": "claude-test",
                "base_url": "not-a-url",
            }
        )

        with self.assertRaises(ConfigError):
            load(config=path)


class ServiceTest(unittest.TestCase):
    def test_service_fills_provider_and_base_url(self):
        with patch.dict("os.environ", {}, clear=True):
            settings = load(env={"GRADUS_SERVICE": "deepseek", "GRADUS_MODEL": "chat"})

        self.assertEqual(settings.spec.provider, "openai_compat")
        self.assertEqual(settings.spec.base_url, "https://api.deepseek.com")

    def test_explicit_base_url_wins_over_service(self):
        with patch.dict("os.environ", {}, clear=True):
            settings = load(
                env={
                    "GRADUS_SERVICE": "deepseek",
                    "GRADUS_MODEL": "chat",
                    "GRADUS_BASE_URL": "https://proxy.test/v1",
                }
            )

        self.assertEqual(settings.spec.base_url, "https://proxy.test/v1")

    def test_service_conflicting_with_provider_errors(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ConfigError) as ctx:
                load(
                    env={
                        "GRADUS_SERVICE": "deepseek",
                        "GRADUS_PROVIDER": "anthropic",
                        "GRADUS_MODEL": "chat",
                    }
                )

        self.assertIn("GRADUS_SERVICE", str(ctx.exception))

    def test_unknown_service_errors_with_choices(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ConfigError) as ctx:
                load(env={"GRADUS_SERVICE": "nope", "GRADUS_MODEL": "chat"})

        self.assertIn("deepseek", str(ctx.exception))


class SandboxConfigTest(unittest.TestCase):
    def test_defaults_are_auto_with_network_off(self):
        with patch.dict("os.environ", {}, clear=True):
            settings = load(env=FULL_ENV)

        self.assertEqual(settings.sandbox, "auto")
        self.assertFalse(settings.sandbox_network)
        self.assertEqual(settings.sandbox_image, "python:3.13-slim")

    def test_env_overrides_sandbox_fields(self):
        env = {
            **FULL_ENV,
            "GRADUS_SANDBOX": "docker",
            "GRADUS_SANDBOX_IMAGE": "my-image:1",
            "GRADUS_SANDBOX_NETWORK": "yes",
        }

        with patch.dict("os.environ", {}, clear=True):
            settings = load(env=env)

        self.assertEqual(settings.sandbox, "docker")
        self.assertEqual(settings.sandbox_image, "my-image:1")
        self.assertTrue(settings.sandbox_network)

    def test_unknown_backend_errors(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ConfigError) as ctx:
                load(env={**FULL_ENV, "GRADUS_SANDBOX": "nsjail"})

        self.assertIn("GRADUS_SANDBOX", str(ctx.exception))

    def test_bad_network_flag_errors(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ConfigError) as ctx:
                load(env={**FULL_ENV, "GRADUS_SANDBOX_NETWORK": "maybe"})

        self.assertIn("GRADUS_SANDBOX_NETWORK", str(ctx.exception))

    def test_file_field_is_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "service": "deepseek",
                        "model": "chat",
                        "sandbox": "docker",
                        "sandbox_network": 1,
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict("os.environ", {}, clear=True):
                settings = load(config=path)

        self.assertEqual(settings.sandbox, "docker")
        self.assertTrue(settings.sandbox_network)


if __name__ == "__main__":
    unittest.main()
