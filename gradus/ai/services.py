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
