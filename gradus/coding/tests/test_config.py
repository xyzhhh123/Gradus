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
    save_active,
    save_models,
    with_active,
    with_choice,
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

    def test_models_array_is_parsed(self):
        path = self._write(
            {
                "service": "kimi-compat",
                "model": "kimi-k2.7-code",
                "api_key": "sk-file",
                "models": [
                    {"name": "Kimi Chat", "service": "kimi-compat", "model": "kimi-k2.7-code"},
                    {
                        "service": "kimi-anthropic",
                        "model": "kimi-k2.7-code",
                        "api_key": "${OTHER_KEY}",
                    },
                ],
            }
        )

        with patch.dict("os.environ", {"OTHER_KEY": "sk-other"}, clear=True):
            settings = load(config=path)

        self.assertEqual(len(settings.models), 2)
        chat, messages = settings.models
        self.assertEqual(chat.name, "Kimi Chat")
        self.assertEqual(chat.spec.provider, "openai_compat")
        self.assertIsNone(chat.api_key)
        # 没写 name 时用「模型 · 协议」兜底
        self.assertEqual(messages.name, "kimi-k2.7-code · anthropic")
        self.assertEqual(messages.spec.base_url, "https://api.moonshot.cn/anthropic")
        self.assertEqual(messages.api_key, "sk-other")

    def test_with_choice_keeps_current_key_when_choice_has_none(self):
        path = self._write(
            {
                "service": "kimi-compat",
                "model": "kimi-k2.7-code",
                "api_key": "sk-file",
                "models": [{"service": "kimi-anthropic", "model": "kimi-k2.7-code"}],
            }
        )

        with patch.dict("os.environ", {}, clear=True):
            settings = load(config=path)
        switched = with_choice(settings, settings.models[0])

        self.assertEqual(switched.spec.provider, "anthropic")
        self.assertEqual(switched.api_key, "sk-file")
        # 原对象不可变，切完还能切回去
        self.assertEqual(settings.spec.provider, "openai_compat")

    def test_models_entry_needs_resolvable_endpoint(self):
        path = self._write(
            {"service": "kimi-compat", "model": "m", "models": [{"model": "x"}]}
        )

        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ConfigError) as ctx:
                load(config=path)

        self.assertIn("models[0]", str(ctx.exception))

    def test_models_unknown_field_errors(self):
        path = self._write(
            {
                "service": "kimi-compat",
                "model": "m",
                "models": [{"service": "kimi-compat", "model": "x", "temperature": 1}],
            }
        )

        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ConfigError) as ctx:
                load(config=path)

        self.assertIn("temperature", str(ctx.exception))

    def test_save_models_preserves_other_fields_and_keys(self):
        path = self._write(
            {
                "service": "kimi-compat",
                "model": "kimi-k2.7-code",
                "api_key": "sk-global",
                "models": [
                    {
                        "name": "old",
                        "service": "kimi-compat",
                        "model": "kimi-k2.7-code",
                        "api_key": "sk-old",
                    }
                ],
            }
        )

        save_models(
            path,
            [
                # 空 key 表示沿用原文件同位置的值
                {"name": "改名后", "service": "kimi-compat", "model": "kimi-k2.7-code"},
                {"name": "DeepSeek", "service": "deepseek", "model": "deepseek-chat"},
            ],
        )

        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["service"], "kimi-compat")
        self.assertEqual(payload["api_key"], "sk-global")
        self.assertEqual(payload["models"][0]["name"], "改名后")
        self.assertEqual(payload["models"][0]["api_key"], "sk-old")
        self.assertNotIn("api_key", payload["models"][1])

        with patch.dict("os.environ", {}, clear=True):
            settings = load(config=path)

        self.assertEqual(len(settings.models), 2)
        self.assertEqual(
            [choice.spec.provider for choice in settings.models],
            ["openai_compat", "openai_compat"],
        )
        self.assertEqual(settings.models[1].api_key, None)

    def test_save_models_keeps_file_when_entry_is_invalid(self):
        path = self._write({"service": "kimi-compat", "model": "m", "models": []})
        before = path.read_text(encoding="utf-8")

        with self.assertRaises(ConfigError):
            save_models(path, [{"name": "坏行", "model": "x"}])

        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertFalse(path.with_name(path.name + ".tmp").exists())

    def test_active_choice_is_applied_on_load(self):
        path = self._write(
            {
                "service": "kimi-compat",
                "model": "kimi-k2.7-code",
                "api_key": "sk-file",
                "models": [
                    {"name": "Kimi Chat", "service": "kimi-compat", "model": "kimi-k2.7-code"},
                    {"name": "Alt", "service": "kimi-anthropic", "model": "kimi-k2.7-code"},
                ],
                "active": "Alt",
            }
        )

        with patch.dict("os.environ", {}, clear=True):
            settings = with_active(load(config=path))

        self.assertEqual(settings.active, "Alt")
        self.assertEqual(settings.spec.provider, "anthropic")

    def test_active_name_gone_keeps_top_level_model(self):
        path = self._write(
            {
                "service": "kimi-compat",
                "model": "kimi-k2.7-code",
                "models": [{"name": "Alt", "service": "kimi-anthropic", "model": "m"}],
                "active": "被删掉的名字",
            }
        )

        with patch.dict("os.environ", {}, clear=True):
            settings = with_active(load(config=path))

        self.assertEqual(settings.spec.provider, "openai_compat")

    def test_with_choice_records_active_name(self):
        path = self._write(
            {
                "service": "kimi-compat",
                "model": "kimi-k2.7-code",
                "models": [{"name": "Alt", "service": "kimi-anthropic", "model": "m"}],
            }
        )

        with patch.dict("os.environ", {}, clear=True):
            settings = load(config=path)

        self.assertEqual(with_choice(settings, settings.models[0]).active, "Alt")

    def test_save_active_round_trips_and_clears(self):
        path = self._write(
            {
                "service": "kimi-compat",
                "model": "kimi-k2.7-code",
                "models": [{"name": "Alt", "service": "kimi-compat", "model": "m"}],
            }
        )

        save_active(path, "Alt")
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(load(config=path).active, "Alt")

        save_active(path, None)
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(load(config=path).active)
        # 其它字段原样保留
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["model"], "kimi-k2.7-code")

    def test_save_models_drops_stale_active(self):
        path = self._write(
            {
                "service": "kimi-compat",
                "model": "kimi-k2.7-code",
                "models": [{"name": "Alt", "service": "kimi-compat", "model": "m"}],
                "active": "Alt",
            }
        )

        save_models(path, [{"name": "别的", "service": "kimi-compat", "model": "m2"}])

        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("active", payload)
        # 新加的候选不是被删掉的旧选择，不该被当成 active
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(load(config=path).active)

    def test_active_must_be_string(self):
        path = self._write({"service": "kimi-compat", "model": "m", "active": 1})

        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ConfigError) as ctx:
                load(config=path)

        self.assertIn("active", str(ctx.exception))


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
