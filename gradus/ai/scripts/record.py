"""录一条真实的流式响应，原样存成 fixture。

用法（在 Gradus 目录下）：
    uv run python -m gradus.ai.scripts.record <provider> <请求体.json>
    uv run python -m gradus.ai.scripts.record openai_compat gradus/ai/scripts/bodies/kimi-compat.json --service kimi-compat

要点：
- key 先读 `.env`（默认 Gradus 根目录，可用 `--env` 指定），文件里没有的键再从环境变量兜底
- key 名按协议取，与选哪家服务无关：换 `--service` 时把对应厂商的 key 填进同一个变量
- 请求体必须是 stream=true 的真实请求；本脚本不替你编造提示词
- 现成请求体放在 `scripts/bodies/`，文件名与 `--service` 同名（如 `kimi-compat.json`）；
  形状绑定协议，换协议要另写一份
- 输出自动命名为 `<协议>_<模型>_<时间戳>.sse`，落在 `scripts/recorded/`（可用 `--out` 覆盖）；
  模型名取自请求体的 `model` 字段
- 用 `replay` 确认事件无误后，再手动挪进 `tests/fixtures/` 并改成 `<协议>_<场景>.sse`；
  根目录不放任何录制产物
- 先写 .tmp，成功后再改名，避免中断留下半截文件被回放测试当成样本
- 非 200、连接失败都打印原因并退出，不落盘
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import httpx

BASE_URLS = {
    "openai_compat": "https://api.deepseek.com",
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com",
    "openai_responses": "https://api.openai.com/v1",
}

# provider -> (默认路径, key 的环境变量名)
# key 名与 PROVIDERS 的键一一对应，按「端点协议」而不是「某一家厂商」命名：
# openai_compat 能对接 DeepSeek、Qwen、Kimi、GLM、Ollama 等多家，写成某一家会误导；
# openai_responses 与 openai_compat 同为 OpenAI 系，用 OPENAI_API_KEY 会分不清给谁。
PROVIDERS = {
    "openai_compat": ("/chat/completions", "OPENAI_COMPAT_API_KEY"),
    "anthropic": ("/v1/messages", "ANTHROPIC_API_KEY"),
    # Gemini 的路径里带模型名，默认路径留空，必须用 --url 指定
    "gemini": ("", "GEMINI_API_KEY"),
    "openai_responses": ("/responses", "OPENAI_RESPONSES_API_KEY"),
}

# 常用服务商，名字 -> (协议, 完整 URL)。只是 --url 的快捷方式，协议必须与位置参数一致。
# 命名规则：同一家出现在多个协议下时，每个名字都带协议后缀（kimi-compat / kimi-anthropic /
# kimi-responses），避免 `kimi` 这种看起来像"整个平台"而其实只是一种形状的名字；
# 只有一种协议的服务保持原名（deepseek、qwen、siliconflow、ark）。
# 国内可用的 Anthropic / Responses 兼容端点主要服务 Claude Code 一类工具，
# 可能不返回 thinking / signature，录样本时以状态码正常为准。
SERVICES = {
    # openai_compat：Chat Completions 形状
    "deepseek": ("openai_compat", "https://api.deepseek.com/chat/completions"),
    "kimi-compat": ("openai_compat", "https://api.moonshot.cn/v1/chat/completions"),
    "zhipu-compat": (
        "openai_compat",
        "https://open.bigmodel.cn/api/paas/v4/chat/completions",
    ),
    "qwen": (
        "openai_compat",
        "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
    ),
    "siliconflow": ("openai_compat", "https://api.siliconflow.cn/v1/chat/completions"),
    # 火山方舟的 model 要填控制台里创建的接入点 ID
    "ark": ("openai_compat", "https://ark.cn-beijing.volces.com/api/v3/chat/completions"),
    # anthropic：Messages 形状
    "anthropic": ("anthropic", "https://api.anthropic.com/v1/messages"),
    # 国内 Kimi 平台（platform.kimi.com）三种协议都在 api.moonshot.cn 上
    "kimi-anthropic": ("anthropic", "https://api.moonshot.cn/anthropic/v1/messages"),
    "zhipu-anthropic": ("anthropic", "https://open.bigmodel.cn/api/anthropic/v1/messages"),
    # openai_responses：Responses 形状
    "openai": ("openai_responses", "https://api.openai.com/v1/responses"),
    "kimi-responses": ("openai_responses", "https://api.moonshot.cn/v1/responses"),
}


def load_env_file(path: Path) -> None:
    """用 `.env` 覆盖同名变量，文件里没有的键保持原样。

    `.env` 优先，是为了让项目自带的配置盖过机器上残留的同名变量；
    空值视为未配置，避免一行 `KEY=` 把本来存在的环境变量顶掉。
    支持 `KEY=VALUE`、`export KEY=VALUE`、单双引号包裹和 `#` 注释行。
    不引入 python-dotenv，避免为一个脚本加依赖。
    """
    if not path.exists():
        return

    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, sep, value = line.partition("=")
        if not sep:
            continue
        value = value.strip().strip("\"'")
        if value:
            os.environ[key.strip()] = value


def build_headers(provider: str, api_key: str) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    if provider == "anthropic":
        # 官方 Anthropic 认 x-api-key；国内的 Anthropic 兼容端点（Kimi、智谱）认 Bearer，
        # 两个都带，避免为了录一条样本还要区分是哪一家
        headers["x-api-key"] = api_key
        headers["Authorization"] = f"Bearer {api_key}"
        headers["anthropic-version"] = "2023-06-01"
    elif provider == "gemini":
        headers["x-goog-api-key"] = api_key
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def resolve_url(provider: str, service: str | None, override: str | None) -> str:
    """--url 优先，其次 --service，最后用协议的默认域名。"""
    if override:
        return override
    if service:
        target, url = SERVICES[service]
        if target != provider:
            raise SystemExit(f"{service} 是 {target} 的服务，与位置参数 {provider} 不匹配")
        return url
    path, _ = PROVIDERS[provider]
    if not path:
        raise SystemExit("gemini 的端点带模型名，请用 --url 指定完整地址")
    return f"{BASE_URLS[provider].rstrip('/')}{path}"


def default_output(provider: str, body: dict) -> Path:
    """按「协议_模型_时间戳」生成输出路径，落在 `scripts/recorded/` 下。

    模型名取自请求体的 `model` 字段；gemini 这类把模型写在 URL 里的协议会缺这一项，
    用 unknown 兜底。`/` 与 `:` 在 Windows 文件名里非法，统一换成 `_`。
    时间戳到秒，同一秒内重复录制会覆盖——手动工具，不为此再加随机后缀。
    """
    model = str(body.get("model") or "unknown")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", model).strip("_") or "unknown"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path(__file__).parent / "recorded" / f"{provider}_{safe}_{stamp}.sse"


async def record(url: str, headers: dict[str, str], body: dict, output: Path) -> int:
    """流式写入 output，返回写入的字节数；失败返回 -1，且不留下文件。"""
    tmp = output.with_suffix(output.suffix + ".tmp")
    size = 0
    failure: str | None = None

    try:
        with tmp.open("wb") as sink:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("POST", url, headers=headers, json=body) as response:
                    if response.status_code != 200:
                        detail = (await response.aread()).decode("utf-8", errors="replace")
                        failure = f"HTTP {response.status_code}\n{detail}"
                    else:
                        async for chunk in response.aiter_bytes():
                            sink.write(chunk)
                            size += len(chunk)
    except httpx.HTTPError as exc:
        failure = f"请求失败：{type(exc).__name__}: {exc}"

    if failure is not None:
        # 文件此时已关闭，Windows 上才能删掉
        tmp.unlink(missing_ok=True)
        print(failure, file=sys.stderr)
        return -1

    tmp.replace(output)
    return size


def main() -> int:
    parser = argparse.ArgumentParser(description="录制厂商流式响应到 fixture")
    parser.add_argument("provider", choices=sorted(PROVIDERS))
    parser.add_argument("body", help="请求体 JSON 文件，必须含 stream=true")
    parser.add_argument(
        "--out",
        help="输出路径；默认 scripts/recorded/<协议>_<模型>_<时间戳>.sse",
    )
    parser.add_argument("--url", help="覆盖默认端点（gemini 必填，中转地址也用它）")
    parser.add_argument(
        "--service",
        choices=sorted(SERVICES),
        help="常用服务商快捷名，等价于对应的 --url",
    )
    parser.add_argument("--env", default=".env", help="key 文件，默认 ./.env")
    args = parser.parse_args()

    load_env_file(Path(args.env))

    _, key_env = PROVIDERS[args.provider]
    api_key = os.environ.get(key_env, "")
    if not api_key:
        print(f"缺少 {key_env}：既不在环境变量里，也不在 {Path(args.env).resolve()} 里", file=sys.stderr)
        return 1

    body_path = Path(args.body)
    if not body_path.exists():
        print(f"请求体不存在：{body_path}", file=sys.stderr)
        return 1
    body = json.loads(body_path.read_text(encoding="utf-8-sig"))
    if not body.get("stream"):
        print("请求体里 stream 必须为 true", file=sys.stderr)
        return 1

    output = Path(args.out) if args.out else default_output(args.provider, body)
    output.parent.mkdir(parents=True, exist_ok=True)

    url = resolve_url(args.provider, args.service, args.url)
    print(f"POST {url}")
    size = asyncio.run(record(url, build_headers(args.provider, api_key), body, output))
    if size < 0:
        return 1

    print(f"写入 {output}（{size} 字节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
