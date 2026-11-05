"""执行环境：命令和脚本跑在本地进程，或跑在 Docker 容器里。

设计原则（对齐 Hermes 的 `terminal.backend`）：

- 工具不直接 `create_subprocess_exec`，而是拿一个 `Environment`；
- 只有两种后端：`local`（现状）与 `docker`（共享 workspace 的读写挂载）；
- Docker 后端默认断网、限制内存 / CPU / 进程数、不继承宿主环境变量；
- 拿不到 Docker 时**不回落 local**：由调用方禁用命令类工具并告警。

容器内的路径约定：

- workspace -> `/workspace`
- 临时目录（脚本等） -> `/gradus-tmp`
- codemode harness -> `/opt/gradus/codemode_harness.py`（只读挂载）

这不是完整的沙箱：workspace 是读写挂载，容器内的命令仍然能改坏项目文件；
它挡住的是 workspace 之外的文件、网络、宿主凭证与残留进程。
"""

from __future__ import annotations

import asyncio
import getpass
import os
import shutil
import sys
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Protocol
from uuid import uuid4

DEFAULT_IMAGE = "python:3.13-slim"
DEFAULT_MEMORY = "2g"
DEFAULT_CPUS = "2"
DEFAULT_PIDS = "512"

BACKENDS = ("local", "docker", "appcontainer")

CONTAINER_WORKSPACE = "/workspace"
CONTAINER_TMP = "/gradus-tmp"
CONTAINER_HARNESS = "/opt/gradus/codemode_harness.py"

#: 容器内跑 Python 用的解释器名；本地用 `sys.executable`。
CONTAINER_PYTHON = "python3"

HARNESS_PATH = (
    Path(__file__).resolve().parent / "tools" / "codemode_harness.py"
)


class SandboxError(RuntimeError):
    """环境启动失败，或路径不在任何挂载点内。"""


class Environment(Protocol):
    """一次会话使用的执行环境。"""

    name: str

    def scratch_dir(self) -> Path:
        """宿主上一块环境内可见的临时目录。"""

    def map_path(self, path: str | Path) -> str:
        """把宿主路径翻译成环境内可见的路径。"""

    def python(self) -> str:
        """环境内可用的 Python 解释器。"""

    async def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        merge_stderr: bool = False,
    ) -> asyncio.subprocess.Process:
        """起一个进程，stdin / stdout 都是管道。"""

    async def recycle(self) -> None:
        """丢弃当前运行实例（超时后用）。"""

    async def aclose(self) -> None:
        """释放环境与临时目录。"""


class LocalEnvironment:
    """现状：直接在本机用户权限下跑。"""

    name = "local"

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).expanduser().resolve()
        self._scratch: Path | None = None

    def scratch_dir(self) -> Path:
        if self._scratch is None:
            self._scratch = Path(tempfile.mkdtemp(prefix="gradus-tmp-"))
        return self._scratch

    def map_path(self, path: str | Path) -> str:
        return str(path)

    def python(self) -> str:
        return sys.executable

    async def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        merge_stderr: bool = False,
    ) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd or self._root),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=(
                asyncio.subprocess.STDOUT
                if merge_stderr
                else asyncio.subprocess.PIPE
            ),
            limit=4 * 1024 * 1024,
        )

    async def recycle(self) -> None:
        return None

    async def aclose(self) -> None:
        if self._scratch is not None:
            shutil.rmtree(self._scratch, ignore_errors=True)
            self._scratch = None


class DockerEnvironment:
    """长驻容器 + `docker exec`：用 Docker Desktop / Engine 提供边界。"""

    name = "docker"

    def __init__(
        self,
        root: str | Path,
        *,
        image: str = DEFAULT_IMAGE,
        network: bool = False,
        docker: str = "docker",
    ) -> None:
        self._root = Path(root).expanduser().resolve()
        self._image = image
        self._network = network
        self._docker = docker
        self._name = f"gradus-{uuid4().hex[:12]}"
        self._scratch = Path(tempfile.mkdtemp(prefix="gradus-sandbox-"))
        self._started = False

    def scratch_dir(self) -> Path:
        return self._scratch

    def map_path(self, path: str | Path) -> str:
        resolved = Path(path).expanduser().resolve()
        if resolved == HARNESS_PATH:
            return CONTAINER_HARNESS
        if resolved.is_relative_to(self._root):
            relative = resolved.relative_to(self._root).as_posix()
            return f"{CONTAINER_WORKSPACE}/{relative}" if relative else CONTAINER_WORKSPACE
        if resolved.is_relative_to(self._scratch):
            relative = resolved.relative_to(self._scratch).as_posix()
            return f"{CONTAINER_TMP}/{relative}" if relative else CONTAINER_TMP
        raise SandboxError(f"path is outside the sandbox mounts: {resolved}")

    def python(self) -> str:
        return CONTAINER_PYTHON

    def run_argv(self) -> list[str]:
        """`docker run` 的参数；单独暴露便于离线断言。"""
        argv = [
            self._docker,
            "run",
            "-d",
            "--rm",
            "--name",
            self._name,
            "--label",
            "gradus.sandbox=1",
            "--label",
            f"gradus.owner={os.getpid()}",
            "--label",
            f"gradus.user={current_user() or 'unknown'}",
            "--memory",
            DEFAULT_MEMORY,
            "--cpus",
            DEFAULT_CPUS,
            "--pids-limit",
            DEFAULT_PIDS,
            "-v",
            f"{_volume(self._root)}:{CONTAINER_WORKSPACE}",
            "-v",
            f"{_volume(self._scratch)}:{CONTAINER_TMP}",
            "-v",
            f"{_volume(HARNESS_PATH)}:{CONTAINER_HARNESS}:ro",
            "-w",
            CONTAINER_WORKSPACE,
        ]
        if not self._network:
            argv += ["--network", "none"]
        argv += [self._image, "sleep", "infinity"]
        return argv

    def exec_argv(self, argv: Sequence[str], workdir: str) -> list[str]:
        """`docker exec` 的参数；单独暴露便于离线断言。"""
        return [
            self._docker,
            "exec",
            "-i",
            "-w",
            workdir,
            self._name,
            *argv,
        ]

    async def _ensure(self) -> None:
        if self._started:
            return
        try:
            process = await asyncio.create_subprocess_exec(
                *self.run_argv(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise SandboxError(f"cannot start docker: {exc}") from exc

        _, stderr = await process.communicate()
        if process.returncode != 0:
            raise SandboxError(
                f"docker sandbox failed to start: "
                f"{stderr.decode('utf-8', errors='replace').strip()}"
            )
        self._started = True

    async def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        merge_stderr: bool = False,
    ) -> asyncio.subprocess.Process:
        await self._ensure()
        workdir = self.map_path(cwd) if cwd is not None else CONTAINER_WORKSPACE
        try:
            return await asyncio.create_subprocess_exec(
                *self.exec_argv(argv, workdir),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=(
                    asyncio.subprocess.STDOUT
                    if merge_stderr
                    else asyncio.subprocess.PIPE
                ),
                limit=4 * 1024 * 1024,
            )
        except OSError as exc:
            raise SandboxError(f"cannot run in docker: {exc}") from exc

    async def recycle(self) -> None:
        """超时后整只容器丢弃；下次调用会重新创建。"""
        if not self._started:
            return
        self._started = False
        try:
            process = await asyncio.create_subprocess_exec(
                self._docker,
                "rm",
                "-f",
                self._name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return
        await process.wait()

    async def aclose(self) -> None:
        await self.recycle()
        shutil.rmtree(self._scratch, ignore_errors=True)


async def docker_problem(docker: str = "docker") -> str | None:
    """Docker 不可用时返回一句原因；可用时返回 `None`。"""
    code, text = await _run([docker, "version", "--format", "{{.Server.Version}}"])
    if code is None:
        return f"{docker} not found"
    if code != 0:
        return text.strip() or "docker daemon is not running"
    return None


Runner = Callable[[Sequence[str]], Awaitable[tuple[int | None, str]]]


async def cleanup_orphans(
    docker: str = "docker",
    *,
    runner: Runner | None = None,
) -> list[str]:
    """删掉「启动它的 Gradus 进程已经不在」的容器。

    强杀（Ctrl-C 两次、任务管理器结束）时 `--rm` 不会触发，容器会以
    `sleep infinity` 一直留着。每个容器都带 `gradus.owner=<pid>` 与
    `gradus.user=<用户名>` 标签，启动时按 pid 是否存活判断孤儿；pid 被
    系统复用时最多留下一个旧容器，不会误删正在用的。别的用户的容器不碰
    ——Docker daemon 是机器级共享资源。
    """
    run = runner or _run
    code, text = await run(
        [docker, "ps", "-aq", "--filter", "label=gradus.sandbox=1"]
    )
    if code != 0 or not text.strip():
        return []

    ids = text.split()
    code, text = await run(
        [
            docker,
            "inspect",
            "--format",
            '{{.Id}} {{index .Config.Labels "gradus.owner"}} '
            '{{index .Config.Labels "gradus.user"}}',
            *ids,
        ]
    )
    if code != 0:
        return []

    user = current_user()
    removed: list[str] = []
    for line in text.splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) < 2:
            continue
        container, owner = parts[0], parts[1]
        owner_user = parts[2] if len(parts) > 2 else ""
        # 老容器没有 user 标签：按自己的处理，避免永远清不掉。
        if owner_user and user and owner_user != user:
            continue
        try:
            pid = int(owner)
        except ValueError:
            continue
        if _pid_alive(pid):
            continue
        code, _ = await run([docker, "rm", "-f", container])
        if code == 0:
            removed.append(container[:12])
    return removed


def current_user() -> str:
    """当前操作系统用户名；取不到时返回空串（调用方按「不区分用户」处理）。"""
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return ""


def _pid_alive(pid: int) -> bool:
    """跨平台判断进程是否还在；判断不了时按「活着」处理，避免误删。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _windows_pid_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not handle:
        # 5 = 拒绝访问：进程存在但没权限打开，按活着处理
        return ctypes.get_last_error() == 5

    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


async def _run(argv: Sequence[str]) -> tuple[int | None, str]:
    """跑一条 docker 命令；起不来时返回 `(None, "")`。"""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        return None, ""

    stdout, stderr = await process.communicate()
    text = (stdout + stderr).decode("utf-8", errors="replace")
    return process.returncode, text


def _volume(path: Path) -> str:
    """Docker Desktop 接受正斜杠；反斜杠在 `-v` 里容易被当成转义。"""
    return str(path).replace("\\", "/")
