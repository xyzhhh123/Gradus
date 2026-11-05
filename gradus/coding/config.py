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
- `context_window` / `max_output_tokens` 可在配置或环境变量里覆盖，缺省用模块常量。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast, get_args

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

_EXPANSION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(ValueError):
    """配置缺失或非法；消息里必须指明出错的变量名。"""


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
    values = _read_config(config)
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


def _read_config(config: str | Path | None) -> dict[str, str]:
    """读 JSON 配置并转成与环境变量同名的键；不存在时返回空。"""
    explicit = config is not None
    path = Path(config).expanduser() if explicit else default_config_path()
    if not path.exists():
        if explicit:
            raise ConfigError(f"配置文件不存在：{path}")
        return {}

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"读不了配置文件 {path}：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} 不是合法 JSON：{exc}") from None

    if not isinstance(payload, dict):
        raise ConfigError(f"{path} 的顶层必须是对象")

    unknown = set(payload) - set(_FILE_KEYS)
    if unknown:
        allowed = " / ".join(sorted(_FILE_KEYS))
        raise ConfigError(f"{path} 有未知字段 {sorted(unknown)}，可用字段：{allowed}")

    values: dict[str, str] = {}
    for key, raw in payload.items():
        if isinstance(raw, bool) or not isinstance(raw, (str, int)):
            raise ConfigError(f"{path}: '{key}' 必须是字符串或整数")
        values[_FILE_KEYS[key]] = _expand(str(raw), path, key)
    return values


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
