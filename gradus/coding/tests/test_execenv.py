"""`ExecEnvironment` 的离线测试：不依赖 Docker，用一个假 `docker` 脚本记录 argv。"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

from gradus.coding.execenv import ExecEnvironment
from gradus.coding.sandbox import SandboxError

FAKE_DOCKER = """\
import json, sys, time
record = {"argv": sys.argv[1:]}
with open(sys.argv[0] + ".json", "w", encoding="utf-8") as handle:
    json.dump(record, handle)
print("fake-ok")
if "sleep" in sys.argv:
    time.sleep(5)
"""


class ExecEnvironmentTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "workspace"
        self.root.mkdir()
        self.fake = Path(self._tmp.name) / "fake_docker.py"
        self.fake.write_text(FAKE_DOCKER, encoding="utf-8")

    def make(self, **kwargs) -> ExecEnvironment:
        return ExecEnvironment(
            "box-1",
            workspace=self.root,
            docker=(sys.executable, str(self.fake)),
            **kwargs,
        )

    def recorded(self) -> dict:
        return json.loads(Path(str(self.fake) + ".json").read_text(encoding="utf-8"))

    def test_map_path_inside(self):
        env = self.make()

        self.assertEqual(env.map_path(self.root), "/testbed")
        self.assertEqual(env.map_path(self.root / "requests" / "models.py"), "/testbed/requests/models.py")

    def test_map_path_outside_raises(self):
        env = self.make()

        with self.assertRaises(SandboxError):
            env.map_path(Path(self._tmp.name) / "elsewhere")

    def test_container_workspace_can_be_configured(self):
        env = self.make(container_workspace="/repo")

        self.assertEqual(env.map_path(self.root / "a.py"), "/repo/a.py")

    async def test_spawn_builds_docker_exec_argv(self):
        env = self.make()

        process = await env.spawn(["echo", "hi"], cwd=self.root)
        await process.communicate()

        argv = self.recorded()["argv"]
        self.assertEqual(argv[:5], ["exec", "-i", "-w", "/testbed", "box-1"])
        self.assertEqual(argv[5:], ["echo", "hi"])

    async def test_spawn_maps_relative_cwd(self):
        env = self.make()
        sub = self.root / "src"
        sub.mkdir()

        process = await env.spawn(["ls"], cwd=sub)
        await process.communicate()

        self.assertEqual(
            self.recorded()["argv"][:5], ["exec", "-i", "-w", "/testbed/src", "box-1"]
        )

    async def test_recycle_terminates_children(self):
        env = self.make()

        process = await env.spawn(["sleep", "5"], cwd=self.root)
        await asyncio.sleep(0.1)
        await env.recycle()
        await asyncio.wait_for(process.wait(), timeout=5)

        self.assertIsNotNone(process.returncode)

    async def test_aclose_removes_scratch(self):
        env = self.make()
        scratch = env.scratch_dir()
        self.assertTrue(scratch.exists())

        await env.aclose()

        self.assertFalse(scratch.exists())

    def test_requires_container_name(self):
        with self.assertRaises(SandboxError):
            ExecEnvironment("", workspace=self.root)


class ExecConfigTest(unittest.TestCase):
    """`sandbox: exec` 的配置校验。"""

    ENV = {
        "GRADUS_PROVIDER": "openai_compat",
        "GRADUS_MODEL": "deepseek-chat",
        "GRADUS_BASE_URL": "https://api.deepseek.com/v1",
        "GRADUS_API_KEY": "sk-test",
    }

    def test_exec_requires_container(self):
        from gradus.coding.config import ConfigError, load

        with self.assertRaises(ConfigError):
            load(env=dict(self.ENV, GRADUS_SANDBOX="exec"))

    def test_exec_reads_container_settings(self):
        from gradus.coding.config import load

        settings = load(
            env=dict(
                self.ENV,
                GRADUS_SANDBOX="exec",
                GRADUS_SANDBOX_CONTAINER="box-1",
                GRADUS_SANDBOX_CONTAINER_WORKSPACE="/repo",
                GRADUS_SANDBOX_CONTAINER_PYTHON="python3.9",
            )
        )

        self.assertEqual(settings.sandbox, "exec")
        self.assertEqual(settings.sandbox_container, "box-1")
        self.assertEqual(settings.sandbox_container_workspace, "/repo")
        self.assertEqual(settings.sandbox_container_python, "python3.9")
