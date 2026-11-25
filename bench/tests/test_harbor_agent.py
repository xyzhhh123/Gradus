"""Tests for the optional Harbor adapter.

The main Gradus test environment does not install Harbor, so this module skips
there. Run it with Harbor's tool environment after editing the adapter.
"""

from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path

try:
    from harbor.models.agent.context import AgentContext

    from bench.harbor_agent import GradusAgent
except ImportError:
    AgentContext = None
    GradusAgent = None


@unittest.skipUnless(GradusAgent is not None, "Harbor is not installed")
class HarborAgentErrorTest(unittest.TestCase):
    class _Owner:
        logger = logging.getLogger("test-harbor-agent")

    def test_provider_error_becomes_metadata(self):
        context = AgentContext()

        GradusAgent._record_agent_error(
            self._Owner(),
            "[error] provider: timeout: ConnectTimeout:\n",
            context,
        )

        self.assertEqual(
            context.metadata,
            {
                "gradus_error": {
                    "kind": "provider",
                    "message": "timeout: ConnectTimeout:",
                },
                "gradus_errors": [
                    {
                        "kind": "provider",
                        "message": "timeout: ConnectTimeout:",
                    }
                ],
            },
        )

    def test_success_output_has_no_error_metadata(self):
        context = AgentContext()

        GradusAgent._record_agent_error(
            self._Owner(),
            "[done] 3 turns, in 100 / out 20 tokens",
            context,
        )

        self.assertIsNone(context.metadata)

    def test_recovered_error_is_recorded_without_final_error(self):
        context = AgentContext()

        GradusAgent._record_agent_error(
            self._Owner(),
            (
                "[error] provider: timeout: ConnectTimeout:\n"
                "[done] 3 turns, in 100 / out 20 tokens\n"
            ),
            context,
        )

        self.assertNotIn("gradus_error", context.metadata)
        self.assertEqual(
            context.metadata["gradus_errors"],
            [{"kind": "provider", "message": "timeout: ConnectTimeout:"}],
        )

    def test_usage_is_summed_across_review_runs(self):
        context = AgentContext()

        GradusAgent._record_usage(
            self._Owner(),
            (
                "[in 100 · out 20 · cache 40 (40%)]\n"
                "[in 300 · out 50 · cache 120 (40%)]\n"
            ),
            context,
        )

        self.assertEqual(context.n_input_tokens, 400)
        self.assertEqual(context.n_output_tokens, 70)
        self.assertEqual(context.n_cache_tokens, 160)

    def test_declares_skill_support(self):
        self.assertTrue(GradusAgent.capabilities.skills)

    def test_session_usage_overrides_incomplete_cli_summary(self):
        class Owner:
            logger = logging.getLogger("test-harbor-agent-usage")

            def __init__(self, logs_dir: Path) -> None:
                self.logs_dir = logs_dir

        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / "sessions"
            sessions.mkdir()
            (sessions / "one.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps({"type": "session"}),
                        json.dumps(
                            {
                                "type": "usage",
                                "input": 100,
                                "output": 20,
                                "cacheRead": 40,
                                "cacheWrite": 0,
                            }
                        ),
                        json.dumps(
                            {
                                "type": "usage",
                                "input": 300,
                                "output": 50,
                                "cacheRead": 120,
                                "cacheWrite": 5,
                            }
                        ),
                    ]
                ),
                encoding="utf-8",
            )
            context = AgentContext()

            recorded = GradusAgent._record_session_usage(
                Owner(Path(tmp)),
                context,
            )

        self.assertTrue(recorded)
        self.assertEqual(context.n_input_tokens, 400)
        self.assertEqual(context.n_output_tokens, 70)
        self.assertEqual(context.n_cache_tokens, 165)

    def test_harbor_kwargs_override_extra_env(self):
        agent = object.__new__(GradusAgent)
        agent._agent_options = {
            "max_turns": 40,
            "max_output_tokens": 16384,
            "timeout_sec": 1200,
            "review_prompt": "",
        }
        agent._extra_env = {"max_turns": "250", "timeout_sec": "28800"}

        self.assertEqual(agent._option("max_turns", 250), 40)
        self.assertEqual(agent._option("max_output_tokens", 32768), 16384)
        self.assertEqual(agent._option("timeout_sec", 28800), 1200)
        self.assertEqual(agent._option("review_prompt", "default"), "")

    def test_truncation_is_recorded_in_metadata(self):
        context = AgentContext()

        GradusAgent._flag_truncation(
            self._Owner(),
            "[done] 7 turns, in 100 / out 20 tokens "
            "(truncated: response hit the output-token limit)\n",
            context,
        )

        self.assertEqual(context.metadata, {"gradus_truncated": True})

    def test_workflow_metadata_summarizes_tools_and_context(self):
        class Owner:
            logger = logging.getLogger("test-harbor-workflow")

            def __init__(self, logs_dir: Path) -> None:
                self.logs_dir = logs_dir

        with tempfile.TemporaryDirectory() as tmp:
            logs_dir = Path(tmp)
            sessions = logs_dir / "sessions"
            sessions.mkdir()
            (sessions / "session.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps({"type": "usage", "input": 100, "output": 10}),
                        json.dumps({"type": "compaction", "summary": "old"}),
                        json.dumps(
                            {
                                "type": "message",
                                "message": {
                                    "role": "user",
                                    "content": "Before finishing, review the task.",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "message",
                                "message": {
                                    "role": "assistant",
                                    "content": [
                                        {
                                            "type": "tool_call",
                                            "name": "bash",
                                            "arguments": {"command": "cat > app.py"},
                                        },
                                        {
                                            "type": "tool_call",
                                            "name": "write_file",
                                            "arguments": {"path": "app.py"},
                                        },
                                        {
                                            "type": "tool_call",
                                            "name": "write_file",
                                            "arguments": {"path": "app.py"},
                                        },
                                    ],
                                },
                            }
                        ),
                    ]
                ),
                encoding="utf-8",
            )
            (logs_dir / "agent-events.jsonl").write_text(
                json.dumps(
                    {
                        "event": "tool_end",
                        "tool": "bash",
                        "is_error": True,
                    }
                ),
                encoding="utf-8",
            )
            context = AgentContext()

            GradusAgent._record_workflow_metadata(
                Owner(logs_dir),
                context,
            )

        workflow = context.metadata["gradus_workflow"]
        self.assertEqual(workflow["model_turns"], 1)
        self.assertEqual(workflow["max_input_tokens"], 100)
        self.assertEqual(workflow["compactions"], 1)
        self.assertEqual(workflow["tool_calls"], {"bash": 1, "write_file": 2})
        self.assertEqual(workflow["tool_errors"], {"bash": 1})
        self.assertEqual(workflow["rewritten_paths"], {"app.py": 2})
        self.assertEqual(workflow["bash_write_patterns"], 1)
        self.assertTrue(workflow["review_seen"])
        self.assertEqual(workflow["review_tool_calls"], 3)


@unittest.skipUnless(GradusAgent is not None, "Harbor is not installed")
class HarborAgentArchiveTest(unittest.IsolatedAsyncioTestCase):
    class _Owner:
        logger = logging.getLogger("test-harbor-agent-archive")

        def __init__(self, logs_dir: Path) -> None:
            self.logs_dir = logs_dir

    class _Environment:
        def __init__(self, fail: bool = False) -> None:
            self.fail = fail
            self.calls: list[tuple[str, str, str]] = []

        async def download_dir(self, source: str, target: Path) -> None:
            self.calls.append(("dir", source, str(target)))
            if self.fail:
                raise RuntimeError("archive failed")

        async def download_file(self, source: str, target: Path) -> None:
            self.calls.append(("file", source, str(target)))
            if self.fail:
                raise RuntimeError("archive failed")

    async def test_archives_sessions_and_event_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            environment = self._Environment()
            await GradusAgent._archive_trajectory(
                self._Owner(Path(tmp)),
                environment,
            )

        self.assertEqual(
            environment.calls,
            [
                ("dir", "/app/.gradus/sessions", str(Path(tmp) / "sessions")),
                (
                    "file",
                    "/tmp/gradus-agent-events.jsonl",
                    str(Path(tmp) / "agent-events.jsonl"),
                ),
            ],
        )

    async def test_archive_failure_does_not_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            await GradusAgent._archive_trajectory(
                self._Owner(Path(tmp)),
                self._Environment(fail=True),
            )


@unittest.skipUnless(GradusAgent is not None, "Harbor is not installed")
class HarborAgentSkillsTest(unittest.IsolatedAsyncioTestCase):
    class _Result:
        return_code = 0
        stdout = "BUNDLE_OK"
        stderr = ""

    class _Owner:
        logger = logging.getLogger("test-harbor-agent-skills")
        skills_dir = "/app/project/.agents/skills"

    class _Environment:
        def __init__(self) -> None:
            self.commands: list[str] = []

        async def upload_dir(self, source: Path, target: str) -> None:
            pass

        async def exec(self, command: str, **kwargs):
            self.commands.append(command)
            return HarborAgentSkillsTest._Result()

    async def test_installs_harbor_skills_for_gradus(self):
        environment = self._Environment()

        await GradusAgent.setup(self._Owner(), environment)

        self.assertIn(
            "cp -R /app/project/.agents/skills/. ~/.gradus/extensions/skills/",
            environment.commands[-1],
        )

