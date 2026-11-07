"""沙箱后端实机一致性检查（直连真实后端，不做离线断言）。

默认跳过。改动沙箱相关代码前后跑一次：

    GRADUS_SANDBOX_LIVE=1 uv run python -m pytest gradus/coding/tests/test_sandbox_live.py -q -s

`GRADUS_SANDBOX_BACKEND` 可指定后端，缺省 `restricted`。

它把 `docs/design.md`「沙箱使用契约」里能自动断言的条款一次性跑完：副本位置与工作
目录、相对路径读写、未跟踪文件、越界读写、删除、网络、超时、codemode、合回宿主
（新增 / 修改 / 删除）与孤儿副本清理。跑不通的条款就是契约被破坏了，不要靠手工试。

两条按后端不同的条款：`restricted` 允许读副本之外的文件（只限制写），并且不阻断
网络；`appcontainer` 两者都拦。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from gradus.ai.base import ModelSpec
from gradus.coding import session, workspace
from gradus.coding.config import Settings

LIVE = os.environ.get("GRADUS_SANDBOX_LIVE") == "1"
BACKEND = os.environ.get("GRADUS_SANDBOX_BACKEND", "restricted")
GIT = shutil.which("git")


def _settings() -> Settings:
    return Settings(
        spec=ModelSpec(
            provider="openai_compat",
            model="dummy",
            base_url="http://127.0.0.1:9/v1",
            context_window=8000,
            max_output_tokens=1024,
        ),
        api_key="dummy",
        sandbox=BACKEND,
    )


def _git(repo: Path, *argv: str) -> None:
    result = subprocess.run(
        ["git", "-C", str(repo), *argv], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def _make_host(base: Path, *, tracked: bool) -> Path:
    """造一个 workspace：git 仓库（可检出 worktree）或普通目录，外加一个未跟踪文件。"""
    host = base / "host"
    host.mkdir()
    (host / "a.txt").write_text("v1\n", encoding="utf-8")
    (host / "keep.txt").write_text("keep\n", encoding="utf-8")
    if tracked and GIT:
        _git(host, "init", "-q")
        _git(host, "add", "a.txt", "keep.txt")
        _git(
            host,
            "-c",
            "user.email=live@example.com",
            "-c",
            "user.name=live",
            "commit",
            "-qm",
            "init",
        )
    (host / ".env").write_text("SECRET=1\n", encoding="utf-8")
    return host


@unittest.skipUnless(LIVE, "set GRADUS_SANDBOX_LIVE=1 to run the sandbox contract check")
class SandboxContractTest(unittest.IsolatedAsyncioTestCase):
    """一次会话里逐条核对沙箱契约。"""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.host = _make_host(self.base, tracked=True)
        self.outside = self.base / "outside"
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text("nope\n", encoding="utf-8")

        self.one = await session.Session.connect(_settings(), workspace=self.host)
        self.copy = Path(self.one.sandbox_workspace or "")
        self.tools = {tool.spec.name: tool for tool in self.one.tools}

    async def asyncTearDown(self) -> None:
        await self.one.aclose()
        await session.close_pool()
        self._tmp.cleanup()

    async def _run(
        self, command: str, *, timeout: float | None = None
    ) -> tuple[str, bool]:
        arguments: dict[str, object] = {"command": command}
        if timeout is not None:
            arguments["timeout"] = timeout
        result = await self.tools["powershell"].execute("live", arguments)
        return result.content, result.is_error

    async def test_backend_and_copy_location(self) -> None:
        self.assertIn(self.one.sandbox_backend, ("restricted", "appcontainer", "docker"))
        self.assertTrue(self.copy.is_dir(), "sandbox workspace copy is missing")
        # 副本必须直接放在临时目录下：再嵌套一层，AppContainer 就设不了工作目录。
        self.assertEqual(self.copy.parent, Path(tempfile.gettempdir()).resolve())
        print(f"[ok] backend={self.one.sandbox_backend} copy={self.copy}", flush=True)

    async def test_cwd_and_relative_paths(self) -> None:
        content, failed = await self._run("(Get-Location).Path")
        self.assertFalse(failed, content)
        self.assertEqual(Path(content.strip()).resolve(), self.copy.resolve())

        _, failed = await self._run("Set-Content -Path rel.txt -Value rel")
        self.assertFalse(failed)
        self.assertTrue((self.copy / "rel.txt").exists())
        self.assertFalse((self.host / "rel.txt").exists(), "write leaked to the host")
        print("[ok] cwd 与相对路径都在副本里", flush=True)

    async def test_existing_and_untracked_files_are_visible(self) -> None:
        content, _ = await self._run("Get-Content a.txt")
        self.assertEqual(content.strip(), "v1")
        content, _ = await self._run("Get-Content .env")
        self.assertEqual(content.strip(), "SECRET=1", "untracked file missing in copy")
        print("[ok] 已跟踪与未跟踪文件都可见", flush=True)

    async def test_reads_and_writes_outside_are_denied(self) -> None:
        secret = str(self.outside / "secret.txt")
        content, failed = await self._run(f"Get-Content '{secret}'")
        if BACKEND == "restricted":
            # 受限令牌只收窄写，读与当前用户一致。
            self.assertFalse(failed, f"restricted backend must be able to read: {content}")
        else:
            self.assertTrue(failed, f"read outside the workspace succeeded: {content}")

        target = str(self.outside / "written.txt")
        _, failed = await self._run(f"Set-Content -Path '{target}' -Value nope")
        self.assertTrue(failed, "write outside the workspace succeeded")
        self.assertFalse((self.outside / "written.txt").exists())
        print("[ok] 越界写被拒（读按后端策略）", flush=True)

    @unittest.skipIf(
        BACKEND != "restricted", "only the restricted backend allows reads outside"
    )
    async def test_restricted_reads_stay_available(self) -> None:
        content, failed = await self._run("Get-Content 'C:\\Windows\\win.ini'")
        self.assertFalse(failed, content)
        print("[ok] restricted 下读系统文件正常", flush=True)

    async def test_delete_works_through_dotnet(self) -> None:
        _, failed = await self._run("Set-Content -Path rel.txt -Value rel")
        self.assertFalse(failed)

        # Remove-Item / cmd del 在 AppContainer 里已知不可用（见 design.md），只记录不判定。
        _, denied = await self._run("Remove-Item rel.txt")
        print(
            f"[info] Remove-Item rel.txt → {'denied' if denied else 'ok'}",
            flush=True,
        )
        if not denied:
            self.assertFalse((self.copy / "rel.txt").exists())

        if (self.copy / "rel.txt").exists():
            _, failed = await self._run(
                "[System.IO.File]::Delete((Resolve-Path rel.txt))"
            )
            self.assertFalse(failed)
            self.assertFalse((self.copy / "rel.txt").exists())
        print("[ok] .NET 删除可用", flush=True)

    @unittest.skipIf(
        BACKEND == "restricted",
        "the restricted backend cannot block the network (see docs/design.md)",
    )
    async def test_network_is_blocked(self) -> None:
        content, _ = await self._run(
            "$c = New-Object Net.Sockets.TcpClient; "
            "$ok = $c.ConnectAsync('1.1.1.1', 53).Wait(3000); "
            "if ($ok) { 'connected' } else { 'blocked' }"
        )
        self.assertIn("blocked", content, f"network is not blocked: {content}")
        print("[ok] 网络被阻断", flush=True)

    async def test_timeout_kills_the_command(self) -> None:
        content, failed = await self._run("Start-Sleep -Seconds 5", timeout=1)
        self.assertTrue(failed, content)
        self.assertIn("timed out", content)
        print("[ok] 超时被杀", flush=True)

    async def test_codemode_runs_in_the_copy(self) -> None:
        code = (
            "result = tools.read_file(path='a.txt')\n"
            "text(result.strip().upper())\n"
        )
        result = await self.tools["codemode"].execute("live", {"code": code})
        self.assertFalse(result.is_error, result.content)
        self.assertIn("V1", result.content)
        print("[ok] codemode 在副本里读到文件", flush=True)


@unittest.skipUnless(LIVE, "set GRADUS_SANDBOX_LIVE=1 to run the sandbox contract check")
class SandboxMergeTest(unittest.IsolatedAsyncioTestCase):
    """关闭会话时的合回与残留清理。"""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    async def asyncTearDown(self) -> None:
        await session.close_pool()
        self._tmp.cleanup()

    async def test_added_edited_deleted_files_merge_on_close(self) -> None:
        host = _make_host(self.base, tracked=True)
        one = await session.Session.connect(_settings(), workspace=host)
        copy = Path(one.sandbox_workspace or "")
        tools = {tool.spec.name: tool for tool in one.tools}

        (copy / "a.txt").write_text("v2\n", encoding="utf-8")
        (copy / "new.txt").write_text("new\n", encoding="utf-8")
        result = await tools["powershell"].execute(
            "live", {"command": "[System.IO.File]::Delete((Resolve-Path keep.txt))"}
        )
        self.assertFalse(result.is_error, result.content)

        await one.aclose()
        report = one.workspace_sync

        self.assertIsNotNone(report)
        self.assertIsNone(report.error, report.error)
        self.assertEqual((host / "a.txt").read_text(encoding="utf-8"), "v2\n")
        self.assertEqual((host / "new.txt").read_text(encoding="utf-8"), "new\n")
        self.assertFalse((host / "keep.txt").exists())
        self.assertTrue((host / ".env").exists())
        self.assertFalse(copy.exists(), "workspace copy was not removed")
        if GIT and (host / ".git").exists():
            listed = subprocess.run(
                ["git", "-C", str(host), "worktree", "list"],
                capture_output=True,
                text=True,
            ).stdout
            self.assertNotIn(str(copy).replace("\\", "/").lower(), listed.replace("\\", "/").lower())
        print("[ok] 新增 / 修改 / 删除都合回，副本已清理", flush=True)

    async def test_delete_file_tool_removes_on_host(self) -> None:
        host = _make_host(self.base, tracked=False)
        one = await session.Session.connect(_settings(), workspace=host)
        copy = Path(one.sandbox_workspace or "")
        tools = {tool.spec.name: tool for tool in one.tools}

        result = await tools["delete_file"].execute("live", {"path": "keep.txt"})
        self.assertFalse(result.is_error, result.content)
        self.assertFalse((copy / "keep.txt").exists())
        self.assertTrue(
            (host / "keep.txt").exists(), "delete leaked to the host before close"
        )

        await one.aclose()
        report = one.workspace_sync

        self.assertIsNotNone(report)
        self.assertIsNone(report.error, report.error)
        self.assertIn("keep.txt", report.deleted)
        self.assertFalse((host / "keep.txt").exists())
        self.assertTrue((host / "a.txt").exists())
        print("[ok] delete_file 在副本里删除，关闭时落到宿主", flush=True)

    async def test_dead_copy_is_removed_on_start(self) -> None:
        host = _make_host(self.base, tracked=False)
        orphan = await workspace.create(host)
        marker = Path(str(orphan.path) + workspace.MARKER_SUFFIX)
        payload = json.loads(marker.read_text(encoding="utf-8"))
        payload["pid"] = 99_999_999
        marker.write_text(json.dumps(payload), encoding="utf-8")

        one = await session.Session.connect(_settings(), workspace=host)
        try:
            self.assertIn(str(orphan.path), one.sandbox_orphans)
            self.assertFalse(orphan.path.exists())
        finally:
            await one.aclose()
        print("[ok] 死 owner 的副本在启动时被清掉", flush=True)
