"""配置与模型选择。

负责：从 JSON 配置文件、注入的环境变量（缺省为 `os.environ`）与 `overrides` 读 API key、
`base_url`、模型名，产出 `ModelSpec` 与 `Settings`。
不负责：实例化 Provider（`session.py` 用 spec 建），也不碰 `agent`。

约束：
- 本模块只做读入与校验，不做网络请求，不打印；
- 优先级：`overrides` > `env` > `os.environ` > 配置文件；缺的键依次回落；
- 配置文件默认 `~/.gradus/config.json`，可用 `--config` 指定；字符串里的 `${NAME}`
  会替换成环境变量，便于不把 key 写进文件；
- `service` 是内置服务商快捷名（表在 `gradus/ai/services.py`），可带出 `provider` 与
  `base_url`，少填两项；显式写的值仍然优先；
- `context_window` / `max_output_tokens` 可在配置或环境变量里覆盖，缺省用模块常量；
- 配置文件还可以带 `models` 数组，列出可切换的候选模型（含各自的 provider / key），
  供界面切换使用；环境变量只描述当前模型；顶层 `active` 记住上次选中的候选名，
  由 `with_active` 在启动时切回去（名字对不上就保持顶层配置的模型）。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast, get_args

from ..ai.base import ModelSpec, ProviderName
from ..ai.services import SERVICES
from .sandbox import BACKENDS, DEFAULT_IMAGE

DEFAULT_CONTEXT_WINDOW = 128_000
DEFAULT_MAX_OUTPUT_TOKENS = 8_192

# `fake` 只用于测试剧本，不通过配置选择。
_ALLOWED_PROVIDERS = frozenset(get_args(ProviderName)) - {"fake"}

ENV_SERVICE = "GRADUS_SERVICE"
ENV_PROVIDER = "GRADUS_PROVIDER"
ENV_MODEL = "GRADUS_MODEL"
ENV_BASE_URL = "GRADUS_BASE_URL"
ENV_API_KEY = "GRADUS_API_KEY"
ENV_CONTEXT_WINDOW = "GRADUS_CONTEXT_WINDOW"
ENV_MAX_OUTPUT_TOKENS = "GRADUS_MAX_OUTPUT_TOKENS"
ENV_SANDBOX = "GRADUS_SANDBOX"
ENV_SANDBOX_IMAGE = "GRADUS_SANDBOX_IMAGE"
ENV_SANDBOX_NETWORK = "GRADUS_SANDBOX_NETWORK"

#: 默认配置文件，放在用户目录，避免把 key 提交进仓库。
CONFIG_RELPATH = Path(".gradus") / "config.json"

#: 配置文件字段到环境变量名的映射；两种入口共用一套校验。
_FILE_KEYS = {
    "service": ENV_SERVICE,
    "provider": ENV_PROVIDER,
    "model": ENV_MODEL,
    "base_url": ENV_BASE_URL,
    "api_key": ENV_API_KEY,
    "context_window": ENV_CONTEXT_WINDOW,
    "max_output_tokens": ENV_MAX_OUTPUT_TOKENS,
    "sandbox": ENV_SANDBOX,
    "sandbox_image": ENV_SANDBOX_IMAGE,
    "sandbox_network": ENV_SANDBOX_NETWORK,
}

#: `models` 数组里每一项允许的字段；比顶层少了 sandbox 相关项。
_MODEL_KEYS = frozenset(
    {
        "name",
        "service",
        "provider",
        "model",
        "base_url",
        "api_key",
        "context_window",
        "max_output_tokens",
    }
)

#: 顶层配置键：`_FILE_KEYS` 之外还允许 `models` 候选表与 `active` 上次选择。
_TOP_KEYS = frozenset({*_FILE_KEYS, "models", "active"})

_EXPANSION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(ValueError):
    """配置缺失或非法；消息里必须指明出错的变量名。"""


@dataclass(frozen=True)
class ModelChoice:
    """一个可切换的目标：显示名 + 模型规格。

    `api_key` 为 `None` 表示沿用当前 `Settings.api_key`；只在同一份配置里有多个
    服务商、各自 key 不同时才需要单独写。
    """

    name: str
    spec: ModelSpec
    api_key: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Settings:
    """一次会话的启动配置。

    `api_key` 不进 `repr`，避免打进日志或异常回显。
    """

    spec: ModelSpec
    api_key: str = field(repr=False)
    #: `auto` / `local` / `docker` / `appcontainer`；命令与 codemode 跑在哪个环境里。
    #: 字段缺省是 `local`（直接构造 `Settings` 时行为确定）；`load()` 的缺省是 `auto`。
    sandbox: str = "local"
    sandbox_image: str = DEFAULT_IMAGE
    #: Docker 后端是否放行网络；默认断网。
    sandbox_network: bool = False
    #: 配置文件 `models` 里列的候选模型；为空表示只能停在当前模型。
    models: tuple[ModelChoice, ...] = ()
    #: 上次在界面上选中的候选显示名；`None` 表示没记过。由 `with_active` 应用。
    active: str | None = None


@dataclass(frozen=True)
class FileConfig:
    """配置文件的解析结果。

    `values` 的键与环境变量同名（便于和环境变量一起走同一套校验）；
    `models` / `active` 只有文件才有，环境变量装不下。
    """

    values: Mapping[str, str] = field(default_factory=dict)
    models: tuple[ModelChoice, ...] = ()
    active: str | None = None


def with_choice(settings: Settings, choice: ModelChoice) -> Settings:
    """换到某个候选模型；候选没写 key 时沿用当前 key，并记下这次选择。"""
    return replace(
        settings,
        spec=choice.spec,
        api_key=choice.api_key or settings.api_key,
        active=choice.name,
    )


def with_active(settings: Settings) -> Settings:
    """记住的选择仍存在时切过去；名字对不上（被删或改过名）就保持原样。"""
    if not settings.active:
        return settings
    for choice in settings.models:
        if choice.name == settings.active:
            return with_choice(settings, choice)
    return settings


def save_models(config: str | Path | None, entries: list[dict[str, Any]]) -> Path:
    """把候选模型写回配置文件，其它顶层字段原样保留。

    `api_key` 为空或缺失时沿用原文件**同位置**的值：界面不回显 key，
    保存一遍不能把已经配好的 key 抹掉。先写临时文件并用 `_read_models`
    校验，通过后才 `os.replace`，失败时目标文件不动。
    """
    path = Path(config).expanduser() if config is not None else default_config_path()
    payload = _load_payload(path)

    previous = payload.get("models")
    old_entries = previous if isinstance(previous, list) else []

    cleaned: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(f"models[{index}] 必须是对象")
        item = {key: value for key, value in entry.items() if key in _MODEL_KEYS}
        if not item.get("api_key"):
            item.pop("api_key", None)
            old = old_entries[index] if index < len(old_entries) else None
            kept = old.get("api_key") if isinstance(old, dict) else None
            if isinstance(kept, str) and kept:
                item["api_key"] = kept
        cleaned.append(item)

    payload["models"] = cleaned
    # 记住的选择如果已经不在表里，一并清掉，避免启动时指向不存在的名字。
    names = {item.get("name") for item in cleaned if isinstance(item.get("name"), str)}
    active = payload.get("active")
    if isinstance(active, str) and active not in names:
        payload.pop("active", None)

    _write_payload(path, payload, validate=True)
    return path


def save_active(config: str | Path | None, name: str | None) -> Path:
    """记住界面最后一次选中的候选（按显示名）；空值表示清掉。"""
    path = Path(config).expanduser() if config is not None else default_config_path()
    payload = _load_payload(path)
    if name:
        payload["active"] = name
    else:
        payload.pop("active", None)
    _write_payload(path, payload, validate=False)
    return path


def _load_payload(path: Path) -> dict[str, Any]:
    """读配置文件的顶层对象；文件不存在时返回空表。"""
    return _read_payload(path) or {}


def _write_payload(path: Path, payload: dict[str, Any], *, validate: bool) -> None:
    """先写临时文件（可选校验），再原子替换；中途失败目标文件不动。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if validate:
        try:
            _read_models(temp)
        except ConfigError:
            temp.unlink(missing_ok=True)
            raise
    os.replace(temp, path)


def load(
    env: Mapping[str, str] | None = None,
    overrides: Mapping[str, str] | None = None,
    config: str | Path | None = None,
) -> Settings:
    """读取并校验启动配置。

    优先级：`overrides` > `env` > `os.environ` > 配置文件。
    `env` 为 `None` 时只读 `os.environ`；传入时先合并，注入的键覆盖同名环境变量。
    `config=None` 时读默认路径，文件不存在视为没有配置；显式给出但不存在则报错。
    """
    file = _read_config(config)
    values = dict(file.values)
    values.update(os.environ)
    if env is not None:
        values.update(env)
    if overrides is not None:
        values.update(overrides)

    provider, service_base = _resolve_provider(values)
    model = _required(values, ENV_MODEL)

    base_url = values.get(ENV_BASE_URL, "").strip() or service_base
    if not base_url:
        raise ConfigError(
            f"缺少必填配置 {ENV_BASE_URL}（或改用 {ENV_SERVICE} 选一个内置服务商）"
        )
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError(
            f"{ENV_BASE_URL} 必须以 http:// 或 https:// 开头，当前为 {base_url!r}"
        )

    return Settings(
        spec=ModelSpec(
            provider=cast(ProviderName, provider),
            model=model,
            base_url=base_url,
            context_window=_positive_int(
                values, ENV_CONTEXT_WINDOW, DEFAULT_CONTEXT_WINDOW
            ),
            max_output_tokens=_positive_int(
                values, ENV_MAX_OUTPUT_TOKENS, DEFAULT_MAX_OUTPUT_TOKENS
            ),
        ),
        api_key=values.get(ENV_API_KEY, "").strip(),
        sandbox=_sandbox_backend(values),
        sandbox_image=values.get(ENV_SANDBOX_IMAGE, "").strip() or DEFAULT_IMAGE,
        sandbox_network=_flag(values, ENV_SANDBOX_NETWORK, default=False),
        models=file.models,
        active=file.active,
    )


def _resolve_provider(values: Mapping[str, str]) -> tuple[str, str | None]:
    """定出 provider，并带回 `service` 对应的 base_url。

    `service` 只是快捷方式：给一个名字就带出协议与地址，少填两项；
    显式写的 `provider` / `base_url` 仍然生效，但 `provider` 与 `service` 的协议
    冲突时直接报错，避免把 key 发到错误的形状上。
    """
    service = values.get(ENV_SERVICE, "").strip()
    service_provider: str | None = None
    service_base: str | None = None
    if service:
        if service not in SERVICES:
            allowed = " / ".join(sorted(SERVICES))
            raise ConfigError(f"未知服务商 {service!r}；可用的有：{allowed}")
        service_provider, service_base = SERVICES[service]

    provider = values.get(ENV_PROVIDER, "").strip()
    if service_provider is not None:
        if not provider:
            provider = service_provider
        elif provider != service_provider:
            raise ConfigError(
                f"{ENV_SERVICE}={service} 对应 {service_provider}，"
                f"与 {ENV_PROVIDER}={provider} 不一致"
            )
    else:
        provider = _required(values, ENV_PROVIDER)

    if provider not in _ALLOWED_PROVIDERS:
        allowed = " / ".join(sorted(_ALLOWED_PROVIDERS))
        raise ConfigError(f"{ENV_PROVIDER} 必须是 {allowed}，当前为 {provider!r}")
    return provider, service_base


def default_config_path() -> Path:
    """默认配置文件；不存在表示只用环境变量。"""
    return Path.home() / CONFIG_RELPATH


def _read_config(config: str | Path | None) -> FileConfig:
    """读 JSON 配置并拆成「同名键 / 候选表 / 上次选择」；不存在时返回空。"""
    explicit = config is not None
    path = Path(config).expanduser() if explicit else default_config_path()
    payload = _read_payload(path, explicit=explicit)
    if payload is None:
        return FileConfig()

    unknown = set(payload) - _TOP_KEYS
    if unknown:
        allowed = " / ".join(sorted(_TOP_KEYS))
        raise ConfigError(f"{path} 有未知字段 {sorted(unknown)}，可用字段：{allowed}")

    values: dict[str, str] = {}
    for key, raw in payload.items():
        if key in ("models", "active"):
            continue
        if isinstance(raw, bool) or not isinstance(raw, (str, int)):
            raise ConfigError(f"{path}: '{key}' 必须是字符串或整数")
        values[_FILE_KEYS[key]] = _expand(str(raw), path, key)

    active = payload.get("active")
    if active is not None and not isinstance(active, str):
        raise ConfigError(f"{path}: 'active' 必须是字符串")
    return FileConfig(
        values=values,
        models=_models_from(payload, path),
        active=active.strip() if isinstance(active, str) else None,
    )


def _read_models(config: str | Path | None) -> tuple[ModelChoice, ...]:
    """读配置文件里的 `models` 数组；用于写回前的校验。"""
    path = Path(config).expanduser() if config is not None else default_config_path()
    payload = _read_payload(path)
    if payload is None:
        return ()
    return _models_from(payload, path)


def _models_from(payload: Mapping[str, Any], path: Path) -> tuple[ModelChoice, ...]:
    """解析 `models` 数组；没有该键时返回空表。"""
    entries = payload.get("models")
    if entries is None:
        return ()
    if not isinstance(entries, list):
        raise ConfigError(f"{path}: 'models' 必须是数组")
    return tuple(
        _parse_model(entry, path, index) for index, entry in enumerate(entries)
    )


def _read_payload(path: Path, *, explicit: bool = False) -> dict[str, Any] | None:
    """读配置文件的顶层对象；不存在时显式指定报错，否则返回 `None`。"""
    if not path.exists():
        if explicit:
            raise ConfigError(f"配置文件不存在：{path}")
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"读不了配置文件 {path}：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} 不是合法 JSON：{exc}") from None
    if not isinstance(payload, dict):
        raise ConfigError(f"{path} 的顶层必须是对象")
    return payload


def _parse_model(entry: Any, path: Path, index: int) -> ModelChoice:
    """校验 `models[index]`，字段口径与顶层一致（不含 sandbox）。"""
    where = f"{path}: models[{index}]"
    if not isinstance(entry, dict):
        raise ConfigError(f"{where} 必须是对象")

    unknown = set(entry) - _MODEL_KEYS
    if unknown:
        allowed = " / ".join(sorted(_MODEL_KEYS))
        raise ConfigError(f"{where} 有未知字段 {sorted(unknown)}，可用字段：{allowed}")

    values: dict[str, str] = {}
    for key, raw in entry.items():
        if isinstance(raw, bool) or not isinstance(raw, (str, int)):
            raise ConfigError(f"{where}: '{key}' 必须是字符串或整数")
        values[_FILE_KEYS.get(key, key)] = _expand(str(raw), path, f"models[{index}].{key}")

    try:
        provider, service_base = _resolve_provider(values)
        model = _required(values, ENV_MODEL)
    except ConfigError as exc:
        raise ConfigError(f"{where}: {exc}") from None
    base_url = values.get(ENV_BASE_URL, "").strip() or service_base or ""
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError(
            f"{where}: base_url 必须以 http:// 或 https:// 开头，当前为 {base_url!r}"
        )

    name = values.get("name", "").strip() or f"{model} · {provider}"
    return ModelChoice(
        name=name,
        spec=ModelSpec(
            provider=cast(ProviderName, provider),
            model=model,
            base_url=base_url,
            context_window=_positive_int(
                values, ENV_CONTEXT_WINDOW, DEFAULT_CONTEXT_WINDOW
            ),
            max_output_tokens=_positive_int(
                values, ENV_MAX_OUTPUT_TOKENS, DEFAULT_MAX_OUTPUT_TOKENS
            ),
        ),
        api_key=values.get(ENV_API_KEY, "").strip() or None,
    )


def _expand(value: str, path: Path, key: str) -> str:
    """把 `${NAME}` 换成环境变量；缺失直接报错，避免把空串当 key。"""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        found = os.environ.get(name)
        if found is None:
            raise ConfigError(f"{path}: '{key}' 引用了未设置的环境变量 {name}")
        return found

    return _EXPANSION.sub(replace, value)


def _required(values: Mapping[str, str], name: str) -> str:
    value = values.get(name, "").strip()
    if not value:
        raise ConfigError(f"缺少必填配置 {name}（环境变量或 ~/.gradus/config.json）")
    return value


def _positive_int(values: Mapping[str, str], name: str, default: int) -> int:
    raw = values.get(name)
    if raw is None:
        return default

    try:
        value = int(raw.strip())
    except ValueError:
        raise ConfigError(f"{name} 必须是正整数，当前为 {raw!r}") from None
    if value <= 0:
        raise ConfigError(f"{name} 必须是正整数，当前为 {raw!r}")
    return value


def _sandbox_backend(values: Mapping[str, str]) -> str:
    """缺省 `auto`：按 appcontainer -> docker -> local 的顺序挑第一个可用的。"""
    backend = values.get(ENV_SANDBOX, "").strip() or "auto"
    if backend not in ("auto", *BACKENDS):
        allowed = " / ".join(("auto", *BACKENDS))
        raise ConfigError(f"{ENV_SANDBOX} 必须是 {allowed}，当前为 {backend!r}")
    return backend


def _flag(values: Mapping[str, str], name: str, *, default: bool) -> bool:
    raw = values.get(name)
    if raw is None or not raw.strip():
        return default
    text = raw.strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} 必须是 true / false，当前为 {raw!r}")
