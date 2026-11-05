"""执行环境（local / docker / appcontainer）的离线测试。

不依赖真实 Docker 与 AppContainer：只断言参数拼装与路径映射；真正起进程的那一步在
`Environment.spawn()` 里，集成验证留到有 Docker / Windows 的机器上手动跑。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gradus.ai.base import ModelSpec
from gradus.coding import appcontainer, sandbox, session
from gradus.coding.config import Settings


def _settings(**overrides) -> Settings:
    base = dict(
        spec=ModelSpec(
            provider="anthropic",
            model="claude-test",
            base_url="https://api.anthropic.com",
            context_window=200_000,
            max_output_tokens=8_192,
        ),
        api_key="sk-test",
    )
    base.update(overrides)
    return Settings(**base)


def _temp_workspace() -> Path:
    """给会建沙箱副本的用例一个空目录，避免复制仓库。"""
    return Path(tempfile.mkdtemp(prefix="gradus-test-"))


class LocalEnvironmentTest(unittest.IsolatedAsyncioTestCase):
    def test_paths_are_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = sandbox.LocalEnvironment(tmp)
            self.addAsyncCleanup(env.aclose)

            self.assertEqual(env.map_path(Path(tmp) / "a.py"), str(Path(tmp) / "a.py"))
            self.assertEqual(env.python(), sys.executable)
            self.assertTrue(env.scratch_dir().is_dir())

    async def test_runs_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = sandbox.LocalEnvironment(tmp)
            self.addAsyncCleanup(env.aclose)

            process = await env.spawn([sys.executable, "-c", "print('hi')"], cwd=Path(tmp))
            stdout, _ = await process.communicate()

            self.assertEqual(process.returncode, 0)
            self.assertIn(b"hi", stdout)


class DockerEnvironmentTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "ws"
        self.root.mkdir()
        self.env = sandbox.DockerEnvironment(self.root, network=False)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(shutil.rmtree, self.env.scratch_dir(), True)

    def test_maps_workspace_scratch_and_harness(self):
        self.assertEqual(
            self.env.map_path(self.root / "src" / "a.py"), "/workspace/src/a.py"
        )
        self.assertEqual(
            self.env.map_path(self.env.scratch_dir() / "x.py"), "/gradus-tmp/x.py"
        )
        self.assertEqual(
            self.env.map_path(sandbox.HARNESS_PATH), "/opt/gradus/codemode_harness.py"
        )

    def test_rejects_paths_outside_mounts(self):
        with self.assertRaises(sandbox.SandboxError):
            self.env.map_path(Path(self._tmp.name) / "elsewhere.py")

    def test_run_argv_hardens_the_container(self):
        argv = self.env.run_argv()

        self.assertIn("--network", argv)
        self.assertEqual(argv[argv.index("--network") + 1], "none")
        self.assertIn("--memory", argv)
        self.assertIn("--pids-limit", argv)
        self.assertIn("--rm", argv)
        self.assertIn("gradus.sandbox=1", argv)
        self.assertIn(f"gradus.owner={os.getpid()}", argv)
        self.assertTrue(any(item.startswith("gradus.user=") for item in argv))
        self.assertEqual(argv[-2:], ["sleep", "infinity"])
        mounts = [argv[index + 1] for index, item in enumerate(argv) if item == "-v"]
        self.assertTrue(any(item.endswith(":/workspace") for item in mounts))
        self.assertTrue(any(item.endswith(":/gradus-tmp") for item in mounts))
        self.assertTrue(
            any(item.endswith(":/opt/gradus/codemode_harness.py:ro") for item in mounts)
        )

    def test_network_flag_is_opt_in(self):
        env = sandbox.DockerEnvironment(self.root, network=True)
        self.addCleanup(shutil.rmtree, env.scratch_dir(), True)

        self.assertNotIn("--network", env.run_argv())

    def test_exec_argv_uses_configured_workdir(self):
        argv = self.env.exec_argv(["bash", "-lc", "echo hi"], "/workspace")

        self.assertEqual(argv[0:2], ["docker", "exec"])
        self.assertIn("-i", argv)
        self.assertEqual(argv[argv.index("-w") + 1], "/workspace")
        self.assertEqual(argv[-3:], ["bash", "-lc", "echo hi"])


class DockerProbeTest(unittest.IsolatedAsyncioTestCase):
    async def test_missing_binary_reports_problem(self):
        problem = await sandbox.docker_problem("gradus-no-such-docker")

        self.assertIsNotNone(problem)

    async def test_failing_command_reports_problem(self):
        problem = await sandbox.docker_problem(sys.executable)

        self.assertIsNotNone(problem)


class CleanupOrphansTest(unittest.IsolatedAsyncioTestCase):
    async def test_removes_only_containers_with_dead_owners(self):
        alive = "a" * 64
        dead = "b" * 64
        calls: list[list[str]] = []

        async def runner(argv):
            calls.append(list(argv))
            if argv[1] == "ps":
                return 0, f"{alive}\n{dead}\n"
            if argv[1] == "inspect":
                return 0, f"{alive} {os.getpid()}\n{dead} 99999999\n"
            if argv[1] == "rm":
                return 0, ""
            return 1, ""

        removed = await sandbox.cleanup_orphans(runner=runner)

        self.assertEqual(removed, ["b" * 12])
        kills = [call for call in calls if call[1] == "rm"]
        self.assertEqual(len(kills), 1)
        self.assertEqual(kills[0][-1], dead)

    async def test_no_containers_is_noop(self):
        async def runner(argv):
            return 0, ""

        self.assertEqual(await sandbox.cleanup_orphans(runner=runner), [])

    async def test_skips_containers_owned_by_another_user(self):
        other = "c" * 64
        calls: list[list[str]] = []

        async def runner(argv):
            calls.append(list(argv))
            if argv[1] == "ps":
                return 0, f"{other}\n"
            if argv[1] == "inspect":
                return 0, f"{other} 99999999 another-user\n"
            if argv[1] == "rm":
                return 0, ""
            return 1, ""

        self.assertEqual(await sandbox.cleanup_orphans(runner=runner), [])
        self.assertEqual([call for call in calls if call[1] == "rm"], [])

    async def test_pid_liveness(self):
        self.assertTrue(sandbox._pid_alive(os.getpid()))
        self.assertFalse(sandbox._pid_alive(99_999_999))


class SessionSandboxTest(unittest.IsolatedAsyncioTestCase):
    async def test_unavailable_docker_disables_command_tools(self):
        async def unavailable(_docker: str = "docker") -> str:
            return "daemon is not running"

        with patch.object(session, "docker_problem", unavailable):
            one = await session.Session.connect(
                _settings(sandbox="docker"), system="custom"
            )
        try:
            names = {tool.spec.name for tool in one.tools}
            self.assertIn("read_file", names)
            self.assertNotIn("bash", names)
            self.assertNotIn("powershell", names)
            self.assertNotIn("codemode", names)
            self.assertIn("docker unavailable", one.sandbox_warning or "")
        finally:
            await one.aclose()

    async def test_available_docker_keeps_command_tools(self):
        async def available(_docker: str = "docker") -> str | None:
            return None

        async def no_orphans() -> tuple[str, ...]:
            return ()

        root = _temp_workspace()

        with (
            patch.object(session, "docker_problem", available),
            patch.object(session, "cleanup_orphans", no_orphans),
        ):
            one = await session.Session.connect(
                _settings(sandbox="docker"), system="custom", workspace=root
            )
        try:
            names = {tool.spec.name for tool in one.tools}
            self.assertIn("bash", names)
            self.assertIn("codemode", names)
            self.assertIsNone(one.sandbox_warning)
            self.assertIsNotNone(one.sandbox_workspace)
        finally:
            await one.aclose()
            shutil.rmtree(root, ignore_errors=True)

    async def test_startup_cleans_orphans(self):
        async def available(_docker: str = "docker") -> str | None:
            return None

        async def orphans() -> tuple[str, ...]:
            return ("deadbeefcafe",)

        root = _temp_workspace()

        with (
            patch.object(session, "docker_problem", available),
            patch.object(session, "cleanup_orphans", orphans),
        ):
            one = await session.Session.connect(
                _settings(sandbox="docker"), system="custom", workspace=root
            )
        try:
            self.assertEqual(one.sandbox_orphans, ("deadbeefcafe",))
        finally:
            await one.aclose()
            shutil.rmtree(root, ignore_errors=True)

    async def test_tools_follow_the_copy_and_merge_on_close(self):
        async def available(_docker: str = "docker") -> str | None:
            return None

        async def no_orphans() -> tuple[str, ...]:
            return ()

        root = _temp_workspace()

        with (
            patch.object(session, "docker_problem", available),
            patch.object(session, "cleanup_orphans", no_orphans),
        ):
            one = await session.Session.connect(
                _settings(sandbox="docker"), system="custom", workspace=root
            )
        copy = Path(one.sandbox_workspace or "")
        try:
            await one.reload()
            tools = {tool.spec.name: tool for tool in one.tools}
            await tools["write_file"].execute(
                "c1", {"path": "b.txt", "content": "x"}
            )

            self.assertFalse((root / "b.txt").exists())
            self.assertTrue((copy / "b.txt").exists())
        finally:
            await one.aclose()

        self.assertEqual((root / "b.txt").read_text(encoding="utf-8"), "x")
        report = one.workspace_sync
        self.assertIsNotNone(report)
        self.assertEqual(report.applied, ("b.txt",))
        self.assertFalse(copy.exists())
        shutil.rmtree(root, ignore_errors=True)

    async def test_local_backend_does_not_touch_docker(self):
        async def explode(_docker: str = "docker") -> str | None:
            raise AssertionError("docker must not be probed for the local backend")

        with patch.object(session, "docker_problem", explode):
            one = await session.Session.connect(
                _settings(sandbox="local"), system="custom"
            )
        try:
            self.assertIsNone(one.sandbox_warning)
            self.assertEqual(one.sandbox_backend, "local")
            self.assertIn("codemode", {tool.spec.name for tool in one.tools})
        finally:
            await one.aclose()


class SessionAutoBackendTest(unittest.IsolatedAsyncioTestCase):
    async def test_prefers_appcontainer_and_skips_docker(self):
        async def explode(_docker: str = "docker") -> str | None:
            raise AssertionError("docker must not be probed when appcontainer works")

        async def no_orphans(_keep=None) -> tuple[str, ...]:
            return ()

        root = _temp_workspace()

        with (
            patch.object(session.appcontainer, "problem", return_value=None),
            patch.object(session.appcontainer, "cleanup_orphans", no_orphans),
            patch.object(session, "docker_problem", explode),
        ):
            one = await session.Session.connect(
                _settings(sandbox="auto"), system="custom", workspace=root
            )
        try:
            self.assertEqual(one.sandbox_backend, "appcontainer")
            self.assertIn("powershell", {tool.spec.name for tool in one.tools})
            self.assertIsNone(one.sandbox_warning)
        finally:
            await one.aclose()
            shutil.rmtree(root, ignore_errors=True)

    async def test_falls_back_to_docker_when_appcontainer_missing(self):
        async def available(_docker: str = "docker") -> str | None:
            return None

        async def no_orphans() -> tuple[str, ...]:
            return ()

        root = _temp_workspace()

        with (
            patch.object(session.appcontainer, "problem", return_value="not windows"),
            patch.object(session, "docker_problem", available),
            patch.object(session, "cleanup_orphans", no_orphans),
        ):
            one = await session.Session.connect(
                _settings(sandbox="auto"), system="custom", workspace=root
            )
        try:
            self.assertEqual(one.sandbox_backend, "docker")
            self.assertIn("bash", {tool.spec.name for tool in one.tools})
            self.assertIsNone(one.sandbox_warning)
        finally:
            await one.aclose()
            shutil.rmtree(root, ignore_errors=True)

    async def test_falls_back_to_local_when_nothing_available(self):
        async def missing(_docker: str = "docker") -> str | None:
            return "daemon is not running"

        with (
            patch.object(session.appcontainer, "problem", return_value="not windows"),
            patch.object(session, "docker_problem", missing),
        ):
            one = await session.Session.connect(
                _settings(sandbox="auto"), system="custom"
            )
        try:
            self.assertEqual(one.sandbox_backend, "local")
            self.assertIn("no backend available", one.sandbox_warning or "")
        finally:
            await one.aclose()


class SessionAppContainerTest(unittest.IsolatedAsyncioTestCase):
    async def test_unavailable_appcontainer_disables_command_tools(self):
        with patch.object(session.appcontainer, "problem", return_value="not windows"):
            one = await session.Session.connect(
                _settings(sandbox="appcontainer"), system="custom"
            )
        try:
            names = {tool.spec.name for tool in one.tools}
            self.assertIn("read_file", names)
            self.assertNotIn("bash", names)
            self.assertNotIn("powershell", names)
            self.assertNotIn("codemode", names)
            self.assertIn("appcontainer unavailable", one.sandbox_warning or "")
        finally:
            await one.aclose()

    async def test_available_appcontainer_registers_powershell_only(self):
        async def no_orphans(_keep=None) -> tuple[str, ...]:
            return ()

        root = _temp_workspace()

        with (
            patch.object(session.appcontainer, "problem", return_value=None),
            patch.object(session.appcontainer, "cleanup_orphans", no_orphans),
        ):
            one = await session.Session.connect(
                _settings(sandbox="appcontainer", sandbox_network=True),
                system="custom",
                workspace=root,
            )
        try:
            names = {tool.spec.name for tool in one.tools}
            self.assertIn("powershell", names)
            self.assertNotIn("bash", names)
            self.assertIn("codemode", names)
            self.assertIn("no network", one.sandbox_warning or "")
        finally:
            await one.aclose()
            shutil.rmtree(root, ignore_errors=True)

    async def test_startup_cleans_leftover_grants(self):
        async def leftover(_keep=None) -> tuple[str, ...]:
            return ("h:/workspace",)

        root = _temp_workspace()

        with (
            patch.object(session.appcontainer, "problem", return_value=None),
            patch.object(session.appcontainer, "cleanup_orphans", leftover),
        ):
            one = await session.Session.connect(
                _settings(sandbox="appcontainer"), system="custom", workspace=root
            )
        try:
            self.assertEqual(one.sandbox_orphans, ("h:/workspace",))
        finally:
            await one.aclose()
            shutil.rmtree(root, ignore_errors=True)


class AppContainerCleanupTest(unittest.IsolatedAsyncioTestCase):
    async def test_removes_only_grants_with_dead_owners(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "gradus"
            state.mkdir()
            alive = Path(tmp) / "alive"
            dead = Path(tmp) / "dead"
            alive.mkdir()
            dead.mkdir()
            (state / "acl-grants.json").write_text(
                json.dumps({str(alive): 10, str(dead): 20}), encoding="utf-8"
            )
            calls: list[list[str]] = []

            async def fake_icacls(argv):
                calls.append(argv)
                return 0

            with (
                patch.object(appcontainer, "_state_dirs", lambda: [state]),
                patch.object(appcontainer, "_profile_sid_text", lambda: "S-1-15-2-test"),
                patch.object(appcontainer, "_icacls", fake_icacls),
                patch.object(appcontainer, "_pid_alive", lambda pid: pid == 10),
            ):
                removed = await appcontainer.cleanup_orphans()

            self.assertEqual(removed, (str(dead),))
            self.assertEqual([call[0] for call in calls], [str(dead)])
            self.assertIn("/remove:g", calls[0])
            self.assertEqual(json.loads((state / "acl-grants.json").read_text()), {str(alive): 10})

    async def test_keeps_current_workspace_and_missing_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "gradus"
            state.mkdir()
            current = Path(tmp) / "current"
            current.mkdir()
            missing = Path(tmp) / "gone"
            (state / "acl-grants.json").write_text(
                json.dumps({str(current): 30, str(missing): 40}), encoding="utf-8"
            )
            calls: list[list[str]] = []

            async def fake_icacls(argv):
                calls.append(argv)
                return 0

            with (
                patch.object(appcontainer, "_state_dirs", lambda: [state]),
                patch.object(appcontainer, "_profile_sid_text", lambda: "S-1-15-2-test"),
                patch.object(appcontainer, "_icacls", fake_icacls),
                patch.object(appcontainer, "_pid_alive", lambda pid: False),
            ):
                removed = await appcontainer.cleanup_orphans(current)

            self.assertEqual(removed, ())
            self.assertEqual(calls, [])
            self.assertEqual(
                json.loads((state / "acl-grants.json").read_text()), {str(current): 30}
            )


if __name__ == "__main__":
    unittest.main()
