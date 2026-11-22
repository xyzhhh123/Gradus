"""真容器集成测试：只有 `GRADUS_DOCKER_LIVE=1` 时才跑。

跑的是最小契约：起一个 alpine 容器、在容器里执行命令、验证 `cwd` 映射与输出。
没有 Docker / 未设置环境变量时整组跳过（不影响离线测试）。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import unittest
import uuid
from pathlib import Path

from gradus.coding.execenv import ExecEnvironment

LIVE = os.environ.get("GRADUS_DOCKER_LIVE") == "1"


def docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


@unittest.skipUnless(LIVE, "需要 GRADUS_DOCKER_LIVE=1 与可用的 Docker")
class ExecEnvironmentLiveTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.name = f"gradus-live-{uuid.uuid4().hex[:8]}"
        self.root = Path(r"H:\bench") / f"live-{uuid.uuid4().hex[:6]}"
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "hello.txt").write_text("from-host\n", encoding="utf-8")
        started = docker(
            "run", "-d", "--name", self.name, "-w", "/data",
            "-v", f"{self.root}:/data", "alpine:3.22", "sleep", "infinity",
        )
        self.assertEqual(started.returncode, 0, started.stderr)

    async def asyncTearDown(self) -> None:
        docker("rm", "-f", self.name)
        for path in self.root.glob("*"):
            path.unlink(missing_ok=True)
        self.root.rmdir()

    async def test_runs_command_in_container(self):
        env = ExecEnvironment(self.name, workspace=self.root, container_workspace="/data")

        process = await env.spawn(["sh", "-c", "cat hello.txt"], cwd=self.root)
        out, _ = await process.communicate()

        self.assertEqual(out.decode().strip(), "from-host")

    async def test_path_mapping_into_container(self):
        env = ExecEnvironment(self.name, workspace=self.root, container_workspace="/data")

        self.assertEqual(env.map_path(self.root / "hello.txt"), "/data/hello.txt")

    async def test_recycle_kills_long_command(self):
        env = ExecEnvironment(self.name, workspace=self.root, container_workspace="/data")

        process = await env.spawn(["sleep", "30"], cwd=self.root)
        await asyncio.sleep(0.2)
        await env.recycle()
        await asyncio.wait_for(process.wait(), timeout=10)

        self.assertIsNotNone(process.returncode)


if __name__ == "__main__":
    unittest.main()
