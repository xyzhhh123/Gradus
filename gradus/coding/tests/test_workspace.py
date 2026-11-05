"""沙箱自有 workspace 的离线测试：复制模式与 git worktree 模式。"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from gradus.coding import workspace

GIT = shutil.which("git")


async def _run(*argv: str, cwd: Path | None = None) -> str:
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd) if cwd is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode("utf-8", errors="replace")
    return stdout.decode("utf-8", errors="replace")


class CopyModeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.host = Path(self._tmp.name) / "host"
        (self.host / "sub").mkdir(parents=True)
        (self.host / "a.txt").write_text("v1\n", encoding="utf-8")
        (self.host / "sub" / "b.txt").write_text("keep\n", encoding="utf-8")
        (self.host / "node_modules").mkdir()
        (self.host / "node_modules" / "x.js").write_text("junk\n", encoding="utf-8")
        self.copy = await workspace.create(self.host)

    async def asyncTearDown(self) -> None:
        await self.copy.discard()
        self._tmp.cleanup()

    async def test_copies_tree_and_skips_heavy_directories(self):
        self.assertFalse(self.copy.worktree)
        self.assertTrue((self.copy.path / "a.txt").exists())
        self.assertTrue((self.copy.path / "sub" / "b.txt").exists())
        self.assertFalse((self.copy.path / "node_modules").exists())

    async def test_sync_writes_changed_and_new_files(self):
        (self.copy.path / "a.txt").write_text("v2\n", encoding="utf-8")
        (self.copy.path / "c.txt").write_text("new\n", encoding="utf-8")

        report = await self.copy.sync()

        self.assertIsNone(report.error)
        self.assertEqual(sorted(report.applied), ["a.txt", "c.txt"])
        self.assertEqual((self.host / "a.txt").read_text(encoding="utf-8"), "v2\n")
        self.assertEqual((self.host / "c.txt").read_text(encoding="utf-8"), "new\n")

    async def test_sync_without_changes_is_empty(self):
        report = await self.copy.sync()

        self.assertFalse(report.changed)
        self.assertIsNone(report.error)

    async def test_deletions_are_applied(self):
        (self.copy.path / "sub" / "b.txt").unlink()

        report = await self.copy.sync()

        self.assertEqual(report.applied, ())
        self.assertEqual(report.deleted, ("sub/b.txt",))
        self.assertFalse((self.host / "sub" / "b.txt").exists())

    async def test_host_files_created_during_the_session_are_kept(self):
        (self.host / "later.txt").write_text("user\n", encoding="utf-8")

        report = await self.copy.sync()

        self.assertEqual(report.applied, ())
        self.assertEqual(report.deleted, ())
        self.assertTrue((self.host / "later.txt").exists())

    async def test_discard_removes_copy(self):
        path = self.copy.path

        await self.copy.discard()

        self.assertFalse(path.exists())


@unittest.skipIf(GIT is None, "git is not installed")
class WorktreeModeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.host = Path(self._tmp.name) / "repo"
        self.host.mkdir()
        await _run(GIT, "init", "-q", cwd=self.host)
        (self.host / "a.txt").write_text("v1\n", encoding="utf-8")
        await _run(GIT, "add", "-A", cwd=self.host)
        await _commit(self.host, "init")
        # 未跟踪文件：worktree 检不出，必须由 create() 带进来
        (self.host / ".env").write_text("KEY=1\n", encoding="utf-8")
        self.copy = await workspace.create(self.host)

    async def asyncTearDown(self) -> None:
        await self.copy.discard()
        self._tmp.cleanup()

    async def test_uses_worktree_and_merges_patch(self):
        self.assertTrue(self.copy.worktree)
        (self.copy.path / "a.txt").write_text("v2\n", encoding="utf-8")
        (self.copy.path / "new.txt").write_text("new\n", encoding="utf-8")

        report = await self.copy.sync()

        self.assertIsNone(report.error)
        self.assertEqual(sorted(report.applied), ["a.txt", "new.txt"])
        self.assertEqual((self.host / "a.txt").read_text(encoding="utf-8"), "v2\n")
        self.assertEqual(
            (self.host / "new.txt").read_text(encoding="utf-8"), "new\n"
        )

    async def test_deletions_are_applied(self):
        (self.copy.path / "a.txt").unlink()

        report = await self.copy.sync()

        self.assertIsNone(report.error)
        self.assertEqual(report.deleted, ("a.txt",))
        self.assertFalse((self.host / "a.txt").exists())

    async def test_untracked_files_are_brought_into_the_copy(self):
        self.assertTrue((self.copy.path / ".env").exists())

    async def test_changed_untracked_file_is_copied_back(self):
        (self.copy.path / ".env").write_text("KEY=2\n", encoding="utf-8")

        report = await self.copy.sync()

        self.assertIsNone(report.error)
        self.assertIn(".env", report.applied)
        self.assertEqual((self.host / ".env").read_text(encoding="utf-8"), "KEY=2\n")

    async def test_deleted_untracked_file_is_removed_on_host(self):
        (self.copy.path / ".env").unlink()

        report = await self.copy.sync()

        self.assertIsNone(report.error)
        self.assertIn(".env", report.deleted)
        self.assertFalse((self.host / ".env").exists())

    async def test_discard_unregisters_worktree(self):
        path = self.copy.path

        await self.copy.discard()

        listed = await _run(GIT, "worktree", "list", "--porcelain", cwd=self.host)
        self.assertNotIn(str(path).replace("\\", "/").lower(), listed.replace("\\", "/").lower())
        self.assertFalse(path.exists())


class OrphanCleanupTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.host = Path(self._tmp.name) / "host"
        self.host.mkdir()
        (self.host / "a.txt").write_text("v1\n", encoding="utf-8")
        self.dead = await workspace.create(self.host)
        self.alive = await workspace.create(self.host)

    async def asyncTearDown(self) -> None:
        await self.dead.discard()
        await self.alive.discard()
        self._tmp.cleanup()

    def _kill_owner(self, copy: workspace.WorkspaceCopy) -> Path:
        """把 owner pid 改成一个不存在的进程。"""
        marker = Path(str(copy.path) + workspace.MARKER_SUFFIX)
        payload = json.loads(marker.read_text(encoding="utf-8"))
        payload["pid"] = 99_999_999
        marker.write_text(json.dumps(payload), encoding="utf-8")
        return marker

    async def test_removes_only_copies_with_dead_owners(self):
        marker = self._kill_owner(self.dead)

        removed = await workspace.cleanup_orphans()

        self.assertIn(str(self.dead.path), removed)
        self.assertFalse(self.dead.path.exists())
        self.assertFalse(marker.exists())
        self.assertTrue(self.alive.path.exists())
        self.assertNotIn(str(self.alive.path), removed)

    async def test_keeps_the_path_passed_as_keep(self):
        self._kill_owner(self.dead)

        removed = await workspace.cleanup_orphans(keep=self.dead.path)

        self.assertEqual(removed, ())
        self.assertTrue(self.dead.path.exists())

    async def test_skips_copies_owned_by_another_user(self):
        marker = self._kill_owner(self.dead)
        payload = json.loads(marker.read_text(encoding="utf-8"))
        payload["user"] = "another-user"
        marker.write_text(json.dumps(payload), encoding="utf-8")

        removed = await workspace.cleanup_orphans()

        self.assertNotIn(str(self.dead.path), removed)
        self.assertTrue(self.dead.path.exists())

    @unittest.skipIf(GIT is None, "git is not installed")
    async def test_removes_dead_worktree_registration(self):
        repo = Path(self._tmp.name) / "repo"
        repo.mkdir()
        await _run(GIT, "init", "-q", cwd=repo)
        (repo / "a.txt").write_text("v1\n", encoding="utf-8")
        await _run(GIT, "add", "-A", cwd=repo)
        await _commit(repo, "init")
        copy = await workspace.create(repo)
        self.assertTrue(copy.worktree)
        self._kill_owner(copy)

        removed = await workspace.cleanup_orphans()

        self.assertIn(str(copy.path), removed)
        self.assertFalse(copy.path.exists())
        listed = await _run(GIT, "worktree", "list", "--porcelain", cwd=repo)
        self.assertNotIn(
            str(copy.path).replace("\\", "/").lower(), listed.replace("\\", "/").lower()
        )


async def _commit(repo: Path, message: str) -> None:
    await _run(
        GIT,
        "-c",
        "user.email=test@example.com",
        "-c",
        "user.name=test",
        "commit",
        "-qm",
        message,
        cwd=repo,
    )
