"""常用服务商的端点表：服务商名 -> (协议, base_url)。

`coding` 用它把配置里的 `service` 展开成 `provider` + `base_url`；
`ai/scripts/record.py` 用它拼录制用的完整端点（`base_url` + 协议路径）。
放 `ai` 而不是 `coding`：`record.py` 在 `ai` 内，而依赖方向只允许 `coding -> ai`，
两边共用一张表才不会各写一份。

命名规则：同一家出现在多个协议下时，每个名字带协议后缀（`kimi-compat` /
`kimi-anthropic` / `kimi-responses`），避免 `kimi` 这种看起来像"整个平台"而其实
只是一种形状的名字；只有一种协议的服务保持原名（`deepseek`、`qwen`、
`siliconflow`、`ark`）。

`gemini` 的端点里带模型名，录样本时要用 `--url` 指定完整地址，不在这张表里。

国内可用的 Anthropic / Responses 兼容端点主要服务 Claude Code 一类工具，
可能不返回 thinking / signature，录样本时以状态码正常为准。
"""

from __future__ import annotations

from .base import ProviderName

SERVICES: dict[str, tuple[ProviderName, str]] = {
    # openai_compat：Chat Completions 形状
    "deepseek": ("openai_compat", "https://api.deepseek.com"),
    "kimi-compat": ("openai_compat", "https://api.moonshot.cn/v1"),
    "zhipu-compat": ("openai_compat", "https://open.bigmodel.cn/api/paas/v4"),
    "qwen": (
        "openai_compat",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    ),
    "siliconflow": ("openai_compat", "https://api.siliconflow.cn/v1"),
    # OpenCode Go：订阅制编码模型网关。它要求每个请求带一个「会话 id」头，配置里写
    # `"headers": {"x-opencode-session": "{run}"}`（`{run}` 由 `ai.base.expand_headers`
    # 换成本次会话的随机 id）。按量计的 Zen 端点是另一个地址：opencode.ai/zen/v1。
    "opencode-go": ("openai_compat", "https://opencode.ai/inference/go/v1"),
    # 火山方舟的 model 要填控制台里创建的接入点 ID
    "ark": ("openai_compat", "https://ark.cn-beijing.volces.com/api/v3"),
    # anthropic：Messages 形状
    "anthropic": ("anthropic", "https://api.anthropic.com"),
    # 国内 Kimi 平台（platform.kimi.com）三种协议都在 api.moonshot.cn 上
    "kimi-anthropic": ("anthropic", "https://api.moonshot.cn/anthropic"),
    "zhipu-anthropic": (
        "anthropic",
        "https://open.bigmodel.cn/api/anthropic",
    ),
    # openai_responses：Responses 形状
    "openai": ("openai_responses", "https://api.openai.com/v1"),
    "kimi-responses": ("openai_responses", "https://api.moonshot.cn/v1"),
}

#: 有些网关要求每个请求带特定请求头才肯路由，缺了直接 400。表里只登记「选了这项
#: 就必须带」的头，**只对该服务生效**：没选这个 `service` 的会话一个头都不会多。
#:
#: 值里的 `{run}` 由 `base.expand_headers` 换成本次会话的随机 id。用户显式配的
#: `headers` 覆盖这里的同名项（想自己指定会话 id 就自己写）。
SERVICE_HEADERS: dict[str, dict[str, str]] = {
    # OpenCode Go：缺 `x-opencode-session` 会回 400 MissingSessionID
    "opencode-go": {"x-opencode-session": "{run}"},
}
