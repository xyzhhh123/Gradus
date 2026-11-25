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
from ..ai.models import limits_for
from ..ai.services import SERVICE_HEADERS, SERVICES
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
#: 自定义请求头，`k=v;k=v`。有些网关要求带会话 id / org id 才能路由。
ENV_HEADERS = "GRADUS_HEADERS"
ENV_CONTEXT_WINDOW = "GRADUS_CONTEXT_WINDOW"
ENV_MAX_OUTPUT_TOKENS = "GRADUS_MAX_OUTPUT_TOKENS"
ENV_SANDBOX = "GRADUS_SANDBOX"
ENV_SANDBOX_IMAGE = "GRADUS_SANDBOX_IMAGE"
#: `sandbox: exec` 时 attach 的容器名；容器由外部（bench / 用户）启动。
ENV_SANDBOX_CONTAINER = "GRADUS_SANDBOX_CONTAINER"
#: 容器内的仓库挂载点（SWE-bench 实例镜像约定为 /testbed）。
ENV_SANDBOX_CONTAINER_WORKSPACE = "GRADUS_SANDBOX_CONTAINER_WORKSPACE"
#: 容器内的 Python 解释器（codemode 用）。
ENV_SANDBOX_CONTAINER_PYTHON = "GRADUS_SANDBOX_CONTAINER_PYTHON"
ENV_SANDBOX_NETWORK = "GRADUS_SANDBOX_NETWORK"
ENV_CONTEXT_RESERVE = "GRADUS_CONTEXT_RESERVE"
ENV_SUMMARY_LAYERS = "GRADUS_SUMMARY_LAYERS"
ENV_SUMMARY_TOKENS = "GRADUS_SUMMARY_TOKENS"
ENV_RECALL_TOKENS = "GRADUS_RECALL_TOKENS"
ENV_OUTPUT_CHARS = "GRADUS_OUTPUT_CHARS"
ENV_CONTEXT_KEEP = "GRADUS_CONTEXT_KEEP"
#: 保留量预设：conservative / balanced / aggressive（见 `context.KEEP_PRESETS`）。
ENV_KEEP_PRESET = "GRADUS_KEEP_PRESET"
#: 启动时向 `{base}/models` 要一次模型上限；默认关（多一次网络请求）。
ENV_PROBE_LIMITS = "GRADUS_PROBE_LIMITS"
#: 检索零命中时，是否允许开一次独立的小窗口让模型挑（默认开）。
ENV_SELECTOR = "GRADUS_SELECTOR"
#: 一轮任务里最多几次模型请求；默认 20，长任务（如仓库级修复）要调大。
ENV_MAX_TURNS = "GRADUS_MAX_TURNS"

#: 默认配置文件，放在用户目录，避免把 key 提交进仓库。
CONFIG_RELPATH = Path(".gradus") / "config.json"

#: 配置文件字段到环境变量名的映射；两种入口共用一套校验。
_FILE_KEYS = {
    "service": ENV_SERVICE,
    "provider": ENV_PROVIDER,
    "model": ENV_MODEL,
    "base_url": ENV_BASE_URL,
    "api_key": ENV_API_KEY,
    "headers": ENV_HEADERS,
    "context_window": ENV_CONTEXT_WINDOW,
    "max_output_tokens": ENV_MAX_OUTPUT_TOKENS,
    "sandbox": ENV_SANDBOX,
    "sandbox_image": ENV_SANDBOX_IMAGE,
    "sandbox_container": ENV_SANDBOX_CONTAINER,
    "sandbox_container_workspace": ENV_SANDBOX_CONTAINER_WORKSPACE,
    "sandbox_container_python": ENV_SANDBOX_CONTAINER_PYTHON,
    "sandbox_network": ENV_SANDBOX_NETWORK,
    "context_reserve_tokens": ENV_CONTEXT_RESERVE,
    "context_keep_tokens": ENV_CONTEXT_KEEP,
    "summary_layers": ENV_SUMMARY_LAYERS,
    "summary_tokens": ENV_SUMMARY_TOKENS,
    "recall_tokens": ENV_RECALL_TOKENS,
    "tool_output_chars": ENV_OUTPUT_CHARS,
    "keep_preset": ENV_KEEP_PRESET,
    "probe_limits": ENV_PROBE_LIMITS,
    "selector": ENV_SELECTOR,
    "max_turns": ENV_MAX_TURNS,
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
    `context_window` / `max_output_tokens` 记的是**这个条目显式写了没有**：
    为 `None` 时切换过去沿用当前会话（顶层/环境）的预算口径，而不是被条目的默认值
    悄悄盖掉。
    """

    name: str
    spec: ModelSpec
    api_key: str | None = field(default=None, repr=False)
    context_window: int | None = None
    max_output_tokens: int | None = None
    #: 这个条目选的服务商快捷名；换过去时要按它重算服务默认请求头。
    service: str | None = None


@dataclass(frozen=True)
class Settings:
    """一次会话的启动配置。

    `api_key` 不进 `repr`，避免打进日志或异常回显。
    """

    spec: ModelSpec
    api_key: str = field(repr=False)
    #: 实际进每次请求的头：服务默认头 + 用户显式写的（后者覆盖前者）。
    headers: Mapping[str, str] = field(default_factory=dict)
    #: 用户显式配的那部分（配置文件的 `headers` 对象 + `GRADUS_HEADERS`）。
    #: 单独留着是为了换模型时能重算服务默认头，不把上一个服务的头带过去。
    explicit_headers: Mapping[str, str] = field(default_factory=dict)
    #: `auto` / `local` / `docker` / `appcontainer`；命令与 codemode 跑在哪个环境里。
    #: 字段缺省是 `local`（直接构造 `Settings` 时行为确定）；`load()` 的缺省是 `auto`。
    sandbox: str = "local"
    sandbox_image: str = DEFAULT_IMAGE
    #: `sandbox: exec`：attach 到这个已存在的容器（由 bench 或用户启动）。
    sandbox_container: str = ""
    #: 容器内的仓库挂载点。
    sandbox_container_workspace: str = "/testbed"
    #: 容器内的 Python（codemode 脚本用）。
    sandbox_container_python: str = "python3"
    #: Docker 后端是否放行网络；默认断网。
    sandbox_network: bool = False
    #: 配置文件 `models` 里列的候选模型；为空表示只能停在当前模型。
    models: tuple[ModelChoice, ...] = ()
    #: 最近在界面上选中的候选显示名；`None` 表示没记过。由 `with_active` 应用。
    active: str | None = None
    #: 上下文预算的两个可调项；`None` 表示按模型与模块默认推导。
    #: 预留取 `max(它, max_output_tokens)`；保留量按 `keep_preset` 推导（见 `context.KEEP_PRESETS`）。
    context_reserve_tokens: int | None = None
    context_keep_tokens: int | None = None
    #: 保留量预设：`conservative` / `balanced` / `aggressive`；窗口越大保留越多。
    keep_preset: str = "balanced"
    #: 启动时是否向 `{base}/models` 探测一次模型上限（失败静默）。
    probe_limits: bool = False
    #: 检索零命中时开一次小窗口让模型挑条目（见 `selector.py` 的护栏）。
    selector: bool = True
    #: 一轮里最多几次模型请求。默认 20 够日常对话；仓库级修复要几十轮。
    max_turns: int = 20
    #: 分层摘要与检索额度；`None` 表示用模块默认（3 层 / 窗口 10% 且不低于 2,000）。
    summary_layers: int | None = None
    summary_tokens: int | None = None
    recall_tokens: int | None = None
    #: 单个工具输出超过这个字符数就落盘换指针；`None` 表示用各工具自己的常量。
    tool_output_chars: int | None = None


@dataclass(frozen=True)
class FileConfig:
    """配置文件的解析结果。

    `values` 的键与环境变量同名（便于和环境变量一起走同一套校验）；
    `models` / `active` 只有文件才有，环境变量装不下。
    """

    values: Mapping[str, str] = field(default_factory=dict)
    models: tuple[ModelChoice, ...] = ()
    active: str | None = None
    #: 文件里的 `headers` 对象；环境变量里的头在 `load()` 里按名字覆盖它。
    headers: Mapping[str, str] = field(default_factory=dict)


def with_choice(settings: Settings, choice: ModelChoice) -> Settings:
    """换到某个候选模型；候选没写 key 时沿用当前 key，并记下这次选择。

    预算口径是**显式优先**：条目自己写了窗口/输出上限就用它（每个模型可以有各自的
    操作点），没写就沿用当前会话的（顶层或环境变量给的），而不是退回默认值——
    否则「条目忘了写」和「条目显式写了默认值」看起来一样，用户配的窗口会被悄悄改掉。

    请求头跟着**服务**走：切到别的服务时要重新算一遍（不能把上一个服务的
    `x-opencode-session` 带过去），用户显式配的头始终覆盖服务默认头。
    """
    spec = replace(
        choice.spec,
        context_window=choice.context_window or settings.spec.context_window,
        max_output_tokens=(
            choice.max_output_tokens or settings.spec.max_output_tokens
        ),
    )
    return replace(
        settings,
        spec=spec,
        api_key=choice.api_key or settings.api_key,
        headers=_effective_headers(choice.service, settings.explicit_headers),
        active=choice.name,
    )


def _effective_headers(
    service: str | None,
    explicit: Mapping[str, str],
) -> dict[str, str]:
    """实际发出去的请求头 = 服务默认头（只对该服务生效）+ 用户显式写的。"""
    headers = dict(SERVICE_HEADERS.get(service or "", {}))
    headers.update(explicit)
    return headers


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

    条目里**没提交的字段沿用原文件同名条目**的值：界面只编辑一部分字段（比如不回显
    key、也没有窗口输入框），保存一遍不能把已经配好的 key / 窗口 / 输出上限抹掉。
    名字对得上就按名字找，对不上退回同下标。
    先写临时文件并用 `_read_models` 校验，通过后才 `os.replace`，失败时目标文件不动。
    """
    path = Path(config).expanduser() if config is not None else default_config_path()
    payload = _load_payload(path)

    previous = payload.get("models")
    old_entries = previous if isinstance(previous, list) else []
    by_name = {
        item["name"]: item
        for item in old_entries
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }

    cleaned: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(f"models[{index}] 必须是对象")
        item = {key: value for key, value in entry.items() if key in _MODEL_KEYS}
        # 界面不回显 key：空值等于「没改」，不能让下面那句 pop 把旧 key 一起带走。
        if not item.get("api_key"):
            item.pop("api_key", None)

        name = item.get("name")
        old = by_name.get(name) if isinstance(name, str) else None
        if old is None:
            old = old_entries[index] if index < len(old_entries) else None
        if isinstance(old, dict):
            # 没提交的字段沿用旧条目：key、窗口、输出上限都可能在界面上没有输入框。
            for key, value in old.items():
                if key in _MODEL_KEYS and key not in item:
                    item[key] = value
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


def save_keep_preset(config: str | Path | None, preset: str | None) -> Path:
    """把保留量预设写回配置文件的顶层；空值表示回到默认（删掉这一项）。

    只动一个键，其余字段原样保留；名字必须是我们认识的预设，写错直接报错，
    不然下次启动才发现配置读不进来。
    """
    from .context import DEFAULT_KEEP_PRESET, KEEP_PRESETS

    name = (preset or "").strip()
    if name and name not in KEEP_PRESETS:
        allowed = " / ".join(sorted(KEEP_PRESETS))
        raise ConfigError(f"保留量预设必须是 {allowed}，当前为 {name!r}")

    path = Path(config).expanduser() if config is not None else default_config_path()
    payload = _load_payload(path)
    if name and name != DEFAULT_KEEP_PRESET:
        payload["keep_preset"] = name
    else:
        payload.pop("keep_preset", None)
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

    # 显式请求头两个来源：文件里是对象，环境变量里是 `k=v;k=v`；后者按名字覆盖前者。
    explicit_headers = dict(file.headers)
    explicit_headers.update(_headers_from_env(values.get(ENV_HEADERS, "")))
    service = values.get(ENV_SERVICE, "").strip() or None
    # 实际发出去的头还要并上「这个服务自己的必需头」——只有选了它才会有。
    headers = _effective_headers(service, explicit_headers)

    base_url = values.get(ENV_BASE_URL, "").strip() or service_base
    if not base_url:
        raise ConfigError(
            f"缺少必填配置 {ENV_BASE_URL}（或改用 {ENV_SERVICE} 选一个内置服务商）"
        )
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError(
            f"{ENV_BASE_URL} 必须以 http:// 或 https:// 开头，当前为 {base_url!r}"
        )

    limits = limits_for(model)
    window, window_limit = _budget_window(
        _positive_int_or_none(values, ENV_CONTEXT_WINDOW),
        limits.window if limits is not None else None,
        DEFAULT_CONTEXT_WINDOW,
    )
    output, output_limit = _budget_output(
        _positive_int_or_none(values, ENV_MAX_OUTPUT_TOKENS),
        limits.output if limits is not None else None,
        DEFAULT_MAX_OUTPUT_TOKENS,
    )

    return Settings(
        spec=ModelSpec(
            provider=cast(ProviderName, provider),
            model=model,
            base_url=base_url,
            context_window=window,
            max_output_tokens=output,
            window_limit=window_limit,
            output_limit=output_limit,
        ),
        api_key=values.get(ENV_API_KEY, "").strip(),
        headers=headers,
        explicit_headers=explicit_headers,
        sandbox=_sandbox_backend(values),
        sandbox_image=values.get(ENV_SANDBOX_IMAGE, "").strip() or DEFAULT_IMAGE,
        sandbox_container=values.get(ENV_SANDBOX_CONTAINER, "").strip(),
        sandbox_container_workspace=(
            values.get(ENV_SANDBOX_CONTAINER_WORKSPACE, "").strip() or "/testbed"
        ),
        sandbox_container_python=(
            values.get(ENV_SANDBOX_CONTAINER_PYTHON, "").strip() or "python3"
        ),
        sandbox_network=_flag(values, ENV_SANDBOX_NETWORK, default=False),
        models=file.models,
        active=file.active,
        context_reserve_tokens=_optional_positive_int(values, ENV_CONTEXT_RESERVE),
        context_keep_tokens=_optional_positive_int(values, ENV_CONTEXT_KEEP),
        keep_preset=_keep_preset(values),
        probe_limits=_flag(values, ENV_PROBE_LIMITS, default=False),
        selector=_flag(values, ENV_SELECTOR, default=True),
        max_turns=_positive_int_or(
            values, ENV_MAX_TURNS, default=20, name="max_turns"
        ),
        summary_layers=_optional_positive_int(values, ENV_SUMMARY_LAYERS),
        summary_tokens=_optional_positive_int(values, ENV_SUMMARY_TOKENS),
        recall_tokens=_optional_positive_int(values, ENV_RECALL_TOKENS),
        tool_output_chars=_optional_positive_int(values, ENV_OUTPUT_CHARS),
    )


def _budget_window(
    explicit: int | None,
    limit: int | None,
    default: int,
) -> tuple[int, int | None]:
    """定出**窗口**的预算口径与能力上限。

    窗口默认是**工作预算**，不是模型能力上限。模型能装 1M 不代表每次请求都该
    带着 1M 历史；`window_limit` 单独记录真实能力。显式值只能往下压，超过
    已知上限时收敛到上限。
    """
    if explicit is None:
        return (min(limit, default) if limit is not None else default, limit)
    if limit is not None and explicit > limit:
        return (limit, limit)
    return (explicit, limit)


def _budget_output(
    explicit: int | None,
    limit: int | None,
    default: int,
) -> tuple[int, int | None]:
    """定出**输出上限**的预算口径与能力上限。

    这里不能「按能力给」：`max_output_tokens` 会原样进请求，模型支持 384k 不代表
    每次都该要 384k（慢且贵，有些端还会拒）。所以缺省仍是保守值，只做两件事：
    显式值不超过能力上限，能力上限单独记下来供预算与界面使用。
    """
    value = explicit if explicit is not None else default
    if limit is not None:
        value = min(value, limit)
    return (value, limit)


def _keep_preset(values: Mapping[str, str]) -> str:
    """保留量预设：必须是 `KEEP_PRESETS` 里的名字。

    函数内导入：`context` 在模块级 import 了本模块的 `Settings`，顶层再反向 import
    会成环；这里只在调用时取名字，那时两边都已加载完。
    """
    from .context import DEFAULT_KEEP_PRESET, KEEP_PRESETS

    raw = values.get(ENV_KEEP_PRESET, "").strip()
    if not raw:
        return DEFAULT_KEEP_PRESET
    if raw not in KEEP_PRESETS:
        allowed = " / ".join(sorted(KEEP_PRESETS))
        raise ConfigError(f"{ENV_KEEP_PRESET} 必须是 {allowed}，当前为 {raw!r}")
    return raw


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


def gradus_home() -> Path:
    """客户端自己的根目录（`~/.gradus`）。

    配置、信任记录、用户级扩展都从这里推导，避免各自写 `Path.home() / ".gradus"`
    ——以后要支持 `GRADUS_HOME` 时只改这一个地方。
    """
    return Path.home() / ".gradus"


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
        if key in ("models", "active", "headers"):
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
        headers=_headers_from_payload(payload.get("headers"), path),
    )


def _headers_from_payload(raw: Any, path: Path) -> dict[str, str]:
    """校验配置文件里的 `headers` 对象：键值都必须是字符串。"""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: 'headers' 必须是对象")
    headers: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            raise ConfigError(f"{path}: 'headers' 的键必须是非空字符串")
        if not isinstance(value, str):
            raise ConfigError(f"{path}: 'headers.{key}' 必须是字符串")
        headers[key.strip()] = value
    return headers


def _headers_from_env(raw: str) -> dict[str, str]:
    """把 `k=v;k=v` 解析成字典；空值返回空表。"""
    headers: dict[str, str] = {}
    for chunk in raw.split(";"):
        item = chunk.strip()
        if not item:
            continue
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise ConfigError(
                f"{ENV_HEADERS} 的每一项必须是 name=value，当前为 {item!r}"
            )
        headers[key.strip()] = value.strip()
    return headers


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
    limits = limits_for(model)
    explicit_window = _positive_int_or_none(values, ENV_CONTEXT_WINDOW)
    explicit_output = _positive_int_or_none(values, ENV_MAX_OUTPUT_TOKENS)
    window, window_limit = _budget_window(
        explicit_window, limits.window if limits is not None else None, DEFAULT_CONTEXT_WINDOW
    )
    output, output_limit = _budget_output(
        explicit_output,
        limits.output if limits is not None else None,
        DEFAULT_MAX_OUTPUT_TOKENS,
    )
    return ModelChoice(
        name=name,
        spec=ModelSpec(
            provider=cast(ProviderName, provider),
            model=model,
            base_url=base_url,
            context_window=window,
            max_output_tokens=output,
            window_limit=window_limit,
            output_limit=output_limit,
        ),
        api_key=values.get(ENV_API_KEY, "").strip() or None,
        service=values.get(ENV_SERVICE, "").strip() or None,
        # 记下「这个条目自己写了没有」：切过去时没写的沿用当前会话的口径。
        context_window=explicit_window,
        max_output_tokens=explicit_output,
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


def _positive_int_or_none(values: Mapping[str, str], name: str) -> int | None:
    """窗口/输出上限：不填是 `None`，填了就必须是正整数（空串也算填错）。

    和「可选旋钮」不同：这两项一旦写了空值，多半是笔误，静默回落默认值更难查。
    """
    raw = values.get(name)
    if raw is None:
        return None
    try:
        value = int(raw.strip())
    except ValueError:
        raise ConfigError(f"{name} 必须是正整数，当前为 {raw!r}") from None
    if value <= 0:
        raise ConfigError(f"{name} 必须是正整数，当前为 {raw!r}")
    return value

def _positive_int_or(values: Mapping[str, str], key: str, *, default: int, name: str) -> int:
    """读一个正整数配置；没给用默认值，给了就必须是正整数。"""
    raw = str(values.get(key) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是正整数，当前为 {raw!r}") from exc
    if value <= 0:
        raise ConfigError(f"{name} 必须是正整数，当前为 {raw!r}")
    return value


def _optional_positive_int(values: Mapping[str, str], name: str) -> int | None:
    """缺省为 `None` 的正整数项：不填表示「按模块默认推导」。"""
    raw = values.get(name)
    if raw is None or not raw.strip():
        return None

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
    if backend == "exec" and not values.get(ENV_SANDBOX_CONTAINER, "").strip():
        raise ConfigError(
            f"{ENV_SANDBOX}=exec 需要同时给出 {ENV_SANDBOX_CONTAINER}（要 attach 的容器名）"
        )
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
