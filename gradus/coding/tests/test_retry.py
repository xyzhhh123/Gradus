"""运行级重试的离线测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gradus.ai.base import ModelSpec
from gradus.ai.types import AssistantMessage, TextBlock, Usage, UserMessage
from gradus.agent.types import AgentDone, AgentError
from gradus.coding import retry, session
from gradus.coding.config import Settings
from gradus.coding.permissions import Policy
from gradus.coding.tools.files import FileTracker


def _settings() -> Settings:
    return Settings(
        spec=ModelSpec(
            provider="anthropic",
            model="claude-test",
            base_url="https://api.anthropic.com",
            context_window=200_000,
            max_output_tokens=8_192,
        ),
        api_key="sk-test",
    )


def _error(kind: str = "provider", message: str = "boom", new_messages=None) -> AgentError:
    return AgentError(
        kind=kind,  # type: ignore[arg-type]
        message=message,
        partial=None,
        new_messages=new_messages or [],
        usage=Usage(),
    )


def _done(new_messages=None) -> AgentDone:
    return AgentDone(
        new_messages=new_messages or [],
        usage=Usage(),
        turns=1,
        stop_reason="stop",
    )


class _FakeAgent:
    """按剧本产事件的替身，只实现 `_run` 用到的接口。"""

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.calls: list[str] = []

    @property
    def messages(self):
        return ()

    def prompt(self, text: str):
        self.calls.append("prompt")
        return self._events(self.scripts.pop(0))

    def continue_(self, prompt=None):
        self.calls.append("continue")
        return self._events(self.scripts.pop(0))

    async def _events(self, events):
        for event in events:
            yield event

    async def aclose(self) -> None:
        pass


def _session(agent, log=None) -> session.Session:
    return session.Session(
        agent,
        _settings(),
        "system",
        Path.cwd(),
        Policy(workspace=Path.cwd()),
        FileTracker(),
        None,
        (),
        None,
        None,
        (),
        log,
    )


def _policy(attempts: int = 2):
    return SimpleNamespace(attempts=attempts, delay=lambda attempt: 0.0)


class RetryPolicyTest(unittest.TestCase):
    def test_delay_doubles(self):
        policy = retry.RetryPolicy(attempts=3, backoff=0.5)

        self.assertEqual(policy.delay(1), 0.5)
        self.assertEqual(policy.delay(2), 1.0)

    def test_delay_is_capped(self):
        policy = retry.RetryPolicy(backoff=100.0)

        self.assertEqual(policy.delay(3), retry.MAX_DELAY)


class IsRetryableTest(unittest.TestCase):
    def test_provider_failure_without_status_is_retryable(self):
        self.assertTrue(retry.is_retryable(_error(message="stream ended without StreamDone")))

    def test_retryable_status(self):
        self.assertTrue(retry.is_retryable(_error(message="HTTP 429: slow down")))
        self.assertTrue(retry.is_retryable(_error(message="HTTP 503: busy")))

    def test_client_error_is_not_retryable(self):
        self.assertFalse(retry.is_retryable(_error(message="HTTP 401: bad key")))
        self.assertFalse(retry.is_retryable(_error(message="HTTP 400: bad request")))

    def test_other_kinds_are_not_retryable(self):
        for kind in ("aborted", "max_turns", "config"):
            self.assertFalse(retry.is_retryable(_error(kind=kind)), kind)


class SessionRetryTest(unittest.IsolatedAsyncioTestCase):
    async def _collect(self, one: session.Session):
        return [event async for event in one._run("hi")]

    async def test_retries_provider_failure_with_same_model(self):
        agent = _FakeAgent([[_error(message="HTTP 503: busy")], [_done()]])
        one = _session(agent)

        with patch.object(session, "RetryPolicy", _policy):
            events = await self._collect(one)

        self.assertEqual(agent.calls, ["prompt", "continue"])
        self.assertEqual(len(events), 1)
        self.assertIsInstance(events[0], AgentDone)

    async def test_does_not_retry_client_error(self):
        agent = _FakeAgent([[_error(message="HTTP 401: bad key")]])
        one = _session(agent)

        with patch.object(session, "RetryPolicy", _policy):
            events = await self._collect(one)

        self.assertEqual(agent.calls, ["prompt"])
        self.assertIsInstance(events[-1], AgentError)

    async def test_does_not_retry_aborted(self):
        agent = _FakeAgent([[_error(kind="aborted", message="cancelled")]])
        one = _session(agent)

        with patch.object(session, "RetryPolicy", _policy):
            events = await self._collect(one)

        self.assertEqual(agent.calls, ["prompt"])
        self.assertEqual(events[-1].kind, "aborted")

    async def test_stops_after_attempts(self):
        agent = _FakeAgent(
            [[_error(message="HTTP 500: oops")], [_error(message="HTTP 500: oops")]]
        )
        one = _session(agent)

        with patch.object(session, "RetryPolicy", lambda: _policy(1)):
            events = await self._collect(one)

        self.assertEqual(agent.calls, ["prompt", "continue"])
        self.assertIsInstance(events[-1], AgentError)

    async def test_completed_turns_are_logged_on_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = session.SessionFile.create(root / "sessions", _settings(), root)
            completed = [
                UserMessage("hi"),
                AssistantMessage([TextBlock("ok")], stop_reason="stop"),
            ]
            agent = _FakeAgent(
                [[_error(message="HTTP 401: bad key", new_messages=completed)]]
            )
            one = _session(agent, log)

            with patch.object(session, "RetryPolicy", _policy):
                await self._collect(one)

            # 提问随 new_messages 一起回来，文件里不会缺问题。
            self.assertEqual(log.messages, completed)
            loaded = session.SessionFile.load(log.path)
            self.assertEqual(loaded.messages, completed)

    async def test_prompt_is_logged_with_the_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = session.SessionFile.create(root / "sessions", _settings(), root)
            answer = AssistantMessage([TextBlock("ok")], stop_reason="stop")
            agent = _FakeAgent([[_done(new_messages=[UserMessage("hi"), answer])]])
            one = _session(agent, log)

            await self._collect(one)

            self.assertEqual(len(log.messages), 2)
            self.assertIsInstance(log.messages[0], UserMessage)
            self.assertEqual(log.messages[0].content, "hi")
            self.assertIs(log.messages[1], answer)

    async def test_abort_without_turns_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = session.SessionFile.create(root / "sessions", _settings(), root)
            agent = _FakeAgent([[_error(kind="aborted", message="cancelled")]])
            one = _session(agent, log)

            await self._collect(one)

            self.assertEqual(log.messages, [])

    async def test_final_failure_without_turns_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = session.SessionFile.create(root / "sessions", _settings(), root)
            agent = _FakeAgent([[_error(message="HTTP 401: bad key")]])
            one = _session(agent, log)

            with patch.object(session, "RetryPolicy", _policy):
                await self._collect(one)

            # 没有回答就不写提问，否则恢复时会多出一条没人回答的问题。
            self.assertEqual(log.messages, [])

    async def test_retry_after_empty_failure_writes_prompt_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = session.SessionFile.create(root / "sessions", _settings(), root)
            answer = AssistantMessage([TextBlock("ok")], stop_reason="stop")
            agent = _FakeAgent(
                [
                    [_error(message="HTTP 503: busy")],
                    [_done(new_messages=[UserMessage("hi"), answer])],
                ]
            )
            one = _session(agent, log)

            with patch.object(session, "RetryPolicy", _policy):
                await self._collect(one)

            self.assertEqual(agent.calls, ["prompt", "continue"])
            self.assertEqual(log.messages, [UserMessage("hi"), answer])


if __name__ == "__main__":
    unittest.main()
