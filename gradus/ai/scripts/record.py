"""录一条真实的流式响应，原样存成 fixture。

用法（在 Gradus 目录下）：
    uv run python -m gradus.ai.scripts.record <provider> <请求体.json>
    uv run python -m gradus.ai.scripts.record openai_compat gradus/ai/scripts/bodies/kimi-compat.json --service kimi-compat

要点：
- key 先读 `.env`（默认 Gradus 根目录，可用 `--env` 指定），文件里没有的键再从环境变量兜底；
  解析在 `gradus/ai/envfile.py`，与 coding CLI 共用同一份规则
- key 先按协议取（`OPENAI_COMPAT_API_KEY` 等），取不到再用通用 `GRADUS_API_KEY`；
  `.env` 里一套连接只写一行 key 就够，换 `--service` 即可
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
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

import httpx

from gradus.ai.envfile import load_env_file
from gradus.ai.services import SERVICES

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

# 常用服务商的快捷名来自 `gradus.ai.services`，与 coding CLI 共用一张表。
# 这里只在 `--service` 时取它的 base_url，再拼上本协议的路径。

#: 通用 key：与 `coding/config.py` 的 `GRADUS_API_KEY` 同名（层间不互相 import，
#: 这里写字面量）。`.env` 里一套连接只写这一行，按协议的 key 仍然优先。
GENERIC_KEY = "GRADUS_API_KEY"


def resolve_key(env: Mapping[str, str], provider: str) -> tuple[str, str]:
    """按协议 key 取，取不到再退回通用 key；返回 (key, 实际命中的变量名)。"""
    _, protocol_key = PROVIDERS[provider]
    for name in (protocol_key, GENERIC_KEY):
        value = env.get(name, "").strip()
        if value:
            return value, name
    return "", protocol_key


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
        target, base = SERVICES[service]
        if target != provider:
            raise SystemExit(f"{service} 是 {target} 的服务，与位置参数 {provider} 不匹配")
        path, _ = PROVIDERS[provider]
        return f"{base.rstrip('/')}{path}"
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

    api_key, key_env = resolve_key(os.environ, args.provider)
    if not api_key:
        print(
            f"缺少 {key_env}（或通用 {GENERIC_KEY}）："
            f"既不在环境变量里，也不在 {Path(args.env).resolve()} 里",
            file=sys.stderr,
        )
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
