"""回放测试：把整段厂商原始 SSE 存成 fixture，离线重跑一遍解析链路。

与测试目录下其它文件的关系：
- `fixtures/*.sse` 是本文件唯一的数据来源。文件名 `<provider>_<场景>.sse`，
  内容是从厂商接口抓下来的响应体原样，只把 key 一类敏感值替换掉。
  当前样本：`anthropic_text_tool.sse` 是照协议手写的；其余九条是用 `scripts/record.py`
  录的 Kimi k2.6 真实流，按场景分三组——`_text`（无工具，纯文本）、
  `_parallel_tools`（一次两个调用）、`_thinking_tool`（单工具，最早那条）。
  真实样本不要手改，重录时整份覆盖。
- `test_<provider>.py` 用手写的 `SseEvent` 列表逐条验证适配器的分支，粒度细、定位快；
  本文件不重复那些断言，只验证"一整段真实流能被完整吃下去"。
- `../transport.py` 的 `parse_sse()`：本文件把 fixture 的每一行交给它切分，
  顺便覆盖"文件格式合法"这一层。
- `adapter.py`：汇总入口，本模块已加入 `MODULES`。

回放走与运行时相同的异步链路：`replay()` 是 async，用例在 `IsolatedAsyncioTestCase`
里 await，不存在"测试同步、运行时异步"的双轨。

运行（在 Gradus 目录下）：
    uv run python -m gradus.ai.tests.adapter
    uv run pytest gradus/ai/tests/test_replay.py
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from gradus.ai.base import ModelSpec, ProviderName, Request
from gradus.ai.providers.anthropic import AnthropicProvider
from gradus.ai.providers.openai_compat import OpenAIChatProvider
from gradus.ai.providers.openai_responses import OpenAIResponsesProvider
from gradus.ai.tests.support import fake_sse
from gradus.ai.transport import SseEvent, parse_sse
from gradus.ai.types import AssistantMessage, StreamDone, UserMessage

FIXTURES = Path(__file__).parent / "fixtures"

# 回放不联网，只有 provider 与 base_url 参与 URL 拼接，其余字段不参与解析
SPECS: dict[str, tuple[type, str, str]] = {
    "anthropic": (AnthropicProvider, "gradus.ai.providers.anthropic", "https://api.anthropic.com"),
    "openai_compat": (OpenAIChatProvider, "gradus.ai.providers.openai_compat", "https://api.moonshot.cn"),
    "openai_responses": (
        OpenAIResponsesProvider,
        "gradus.ai.providers.openai_responses",
        "https://api.moonshot.cn",
    ),
}


def fixture_lines(name: str) -> list[str]:
    """按行读出 fixture，保留换行符，和从 HTTP 流里读到的形状一致。"""
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines(keepends=True)


def replay_events(name: str) -> list[SseEvent]:
    """把 fixture 切成 SseEvent 列表，供适配器消费。"""
    return list(parse_sse(fixture_lines(name)))


async def replay(provider: str, fixture: str) -> StreamDone:
    """整段 fixture -> 适配器，返回结尾事件。

    顶掉适配器的 `post_sse`，其余链路照旧。结尾不是 StreamDone 时直接用属性访问失败，
    不必在 helper 里另写断言。
    """
    provider_cls, module_path, base_url = SPECS[provider]
    spec = ModelSpec(
        provider=provider,  # type: ignore[arg-type]
        model="replay",
        base_url=base_url,
        context_window=200000,
        max_output_tokens=8192,
    )
    instance = provider_cls(spec, api_key="sk-test")
    with patch(f"{module_path}.post_sse") as fake:
        fake.return_value = fake_sse(replay_events(fixture))
        last = None
        async for event in instance.stream(
            Request(system="", messages=[UserMessage("replay")])
        ):
            last = event
    return last  # type: ignore[return-value]


def block_kinds(message: AssistantMessage) -> list[str]:
    return [type(block).__name__ for block in message.content]


class TestAnthropicReplay(unittest.IsolatedAsyncioTestCase):
    async def test_text_then_tool_use(self):
        done = await replay("anthropic", "anthropic_text_tool.sse")

        self.assertEqual(
            block_kinds(done.message),
            ["TextBlock", "ToolCallBlock"],
        )
        self.assertEqual(done.stop_reason, "tool_use")
        self.assertEqual(done.message.content[0].text, "我先看下这个文件")
        self.assertEqual(done.message.content[1].arguments, {"path": "a.py"})
        # 25 未缓存 + 10 缓存读 + 4 缓存写 = 39 总输入
        self.assertEqual(done.usage.input_tokens, 39)
        self.assertEqual(done.usage.output_tokens, 15)
        self.assertEqual(done.usage.cache_read_tokens, 10)
        self.assertEqual(done.usage.cache_write_tokens, 4)

    async def test_kimi_thinking_then_tool(self):
        # 真实流：思考 + 工具，且不给 signature
        done = await replay("anthropic", "anthropic_kimi_thinking_tool.sse")

        self.assertEqual(block_kinds(done.message), ["ThinkingBlock", "ToolCallBlock"])
        self.assertEqual(done.stop_reason, "tool_use")
        self.assertGreater(len(done.message.content[0].thinking), 0)
        self.assertEqual(done.message.content[1].arguments, {"path": "a.py"})
        self.assertEqual((done.usage.input_tokens, done.usage.output_tokens), (65, 42))

    async def test_kimi_text(self):
        done = await replay("anthropic", "anthropic_kimi_text.sse")

        self.assertEqual(block_kinds(done.message), ["ThinkingBlock", "TextBlock"])
        self.assertEqual(done.stop_reason, "stop")
        # 原始 SSE 是 input=0 / cache_read=18，适配器出口补成总输入 18（D25）
        # message_delta 会把整份 usage 再报一遍，覆盖而不是累加，所以这里是 18 不是 36
        self.assertEqual(done.usage.input_tokens, 18)
        self.assertEqual(done.usage.output_tokens, 480)
        self.assertEqual(done.usage.cache_read_tokens, 18)

    async def test_kimi_parallel_tools(self):
        done = await replay("anthropic", "anthropic_kimi_parallel_tools.sse")

        self.assertEqual(
            block_kinds(done.message), ["ThinkingBlock", "ToolCallBlock", "ToolCallBlock"]
        )
        self.assertEqual(done.stop_reason, "tool_use")
        self.assertEqual(
            [block.arguments for block in done.message.content[1:]],
            [{"path": "a.py"}, {"path": "b.py"}],
        )
        self.assertEqual((done.usage.output_tokens, done.usage.cache_read_tokens), (95, 78))


class TestOpenAICompatReplay(unittest.IsolatedAsyncioTestCase):
    async def test_kimi_thinking_then_tool(self):
        done = await replay("openai_compat", "openai_compat_kimi_thinking_tool.sse")

        self.assertEqual(block_kinds(done.message), ["ThinkingBlock", "ToolCallBlock"])
        self.assertEqual(done.stop_reason, "tool_use")
        self.assertGreater(len(done.message.content[0].thinking), 0)
        self.assertEqual(done.message.content[1].arguments, {"path": "a.py"})
        # 用量在 choices[0] 里而不是顶层，读错时这里会全是 0（D24）
        self.assertEqual(done.usage.input_tokens, 65)
        self.assertEqual(done.usage.output_tokens, 44)
        self.assertEqual(done.usage.cache_read_tokens, 65)

    async def test_kimi_text(self):
        done = await replay("openai_compat", "openai_compat_kimi_text.sse")

        self.assertEqual(block_kinds(done.message), ["ThinkingBlock", "TextBlock"])
        self.assertEqual(done.stop_reason, "stop")
        self.assertEqual((done.usage.input_tokens, done.usage.output_tokens), (18, 332))

    async def test_kimi_parallel_tools(self):
        done = await replay("openai_compat", "openai_compat_kimi_parallel_tools.sse")

        self.assertEqual(
            block_kinds(done.message), ["ThinkingBlock", "ToolCallBlock", "ToolCallBlock"]
        )
        self.assertEqual(done.stop_reason, "tool_use")
        self.assertEqual(
            [block.arguments for block in done.message.content[1:]],
            [{"path": "a.py"}, {"path": "b.py"}],
        )
        self.assertEqual((done.usage.input_tokens, done.usage.output_tokens), (78, 111))


class TestOpenAIResponsesReplay(unittest.IsolatedAsyncioTestCase):
    async def test_kimi_thinking_then_tool(self):
        # reasoning item 转成 ThinkingBlock 后仍是 sig=None：本端点不给 encrypted_content
        done = await replay("openai_responses", "openai_responses_kimi_thinking_tool.sse")

        self.assertEqual(block_kinds(done.message), ["ThinkingBlock", "ToolCallBlock"])
        self.assertEqual(done.stop_reason, "tool_use")
        self.assertGreater(len(done.message.content[0].thinking), 0)
        self.assertEqual(done.message.content[1].arguments, {"path": "a.py"})
        self.assertEqual(
            (done.usage.input_tokens, done.usage.output_tokens, done.usage.cache_read_tokens),
            (65, 60, 65),
        )

    async def test_kimi_text(self):
        done = await replay("openai_responses", "openai_responses_kimi_text.sse")

        self.assertEqual(block_kinds(done.message), ["ThinkingBlock", "TextBlock"])
        self.assertEqual(done.stop_reason, "stop")
        self.assertEqual(
            (done.usage.input_tokens, done.usage.output_tokens, done.usage.cache_read_tokens),
            (18, 351, 18),
        )

    async def test_kimi_parallel_tools(self):
        done = await replay("openai_responses", "openai_responses_kimi_parallel_tools.sse")

        self.assertEqual(
            block_kinds(done.message), ["ThinkingBlock", "ToolCallBlock", "ToolCallBlock"]
        )
        self.assertEqual(done.stop_reason, "tool_use")
        self.assertEqual(
            [block.arguments for block in done.message.content[1:]],
            [{"path": "a.py"}, {"path": "b.py"}],
        )
        self.assertEqual(
            (done.usage.input_tokens, done.usage.output_tokens, done.usage.cache_read_tokens),
            (78, 120, 78),
        )


if __name__ == "__main__":
    unittest.main()
