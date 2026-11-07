"""执行环境：命令和脚本跑在宿主、Docker 容器，或 Windows AppContainer 里。

设计原则（对齐 Hermes 的 `terminal.backend`）：

- 工具不直接 `create_subprocess_exec`，而是拿一个 `Environment`；
- 三种后端：`local`（宿主）、`docker`（长驻容器，**测试中**）、
  `restricted`（Windows 受限令牌，写只能在副本里）与 `appcontainer`
  （Windows 零安装，**已弃用**，见 `appcontainer.py`）；
- Docker 后端默认断网、限制内存 / CPU / 进程数、不继承宿主环境变量；
- 拿不到 Docker 时**不回落 local**：由调用方禁用命令类工具并告警。

Docker 的状态是**测试中**：接口和离线断言都在，但还没在真实 Docker 引擎上
跑完实机契约；Docker 只作为跨平台备用保留。

Windows 的目标后端是 `restricted`（`restricted.py`）：读按当前用户、写只落在
副本与 scratch，不需要管理员。它**不阻断网络**，这是 unelevated 方案的固有
短板。`appcontainer` 是 **已弃用** 层：保留可用、不再演进。

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
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Protocol
from uuid import uuid4

DEFAULT_IMAGE = "python:3.13-slim"
DEFAULT_MEMORY = "2g"
DEFAULT_CPUS = "2"
DEFAULT_PIDS = "512"

BACKENDS = ("local", "docker", "appcontainer", "restricted")

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
            self._scratch = create_scratch("gradus-tmp-")
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
    """长驻容器 + `docker exec`：用 Docker Desktop / Engine 提供边界。

    **测试中**：当前首选后端是 `appcontainer`（Windows 零安装），Docker 只
    作为跨平台备用保留。这条路径只有离线断言与参数拼装测试，还没在真实
    Docker 引擎上跑完 `test_sandbox_live.py` 的实机契约，接口与行为可能调整。
    """

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


#: 我们在临时目录里写下的东西；只按这些前缀回收，别的一概不碰。
TEMP_PREFIXES = ("gradus-mcp-", "gradus-tmp-", "gradus-ac-", "gradus-rs-")

#: scratch 目录里的 owner 标记：记录创建它的 pid，强杀后下次启动可直接回收。
SCRATCH_MARKER = ".owner"

#: MCP 结果落盘是一次性产物，一天后即可回收。
TEMP_FILE_MAX_AGE = 24 * 60 * 60

#: 没有 owner 标记的 scratch 目录只按时间兜底（AppContainer 已冻结，补不了标记）。
TEMP_DIR_MAX_AGE = 7 * 24 * 60 * 60


def create_scratch(prefix: str) -> Path:
    """建会话 scratch 目录并写下 owner pid。"""
    path = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        (path / SCRATCH_MARKER).write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass
    return path


def cleanup_temp(
    *,
    root: Path | None = None,
    now: float | None = None,
) -> tuple[str, ...]:
    """回收临时目录里我们自己留下的东西：MCP 落盘结果与强杀留下的 scratch 目录。

    只按前缀判断。scratch 目录带 owner pid：owner 已死就立刻删，owner 还活着就
    保留；没有标记的（AppContainer 那侧已冻结）按 7 天窗口兜底。MCP 落盘文件是
    一次性产物，超过 1 天就删。删除失败（被占用、权限）就跳过，下次再试。
    """
    directory = root if root is not None else Path(tempfile.gettempdir())
    current = time.time() if now is None else now
    try:
        entries = list(directory.iterdir())
    except OSError:
        return ()

    removed: list[str] = []
    for path in entries:
        if not path.name.startswith(TEMP_PREFIXES):
            continue
        try:
            is_dir = path.is_dir()
            if is_dir and _owner_pid(path) is not None:
                if _pid_alive(_owner_pid(path) or 0):
                    continue
            else:
                limit = TEMP_DIR_MAX_AGE if is_dir else TEMP_FILE_MAX_AGE
                if current - _latest_mtime(path) < limit:
                    continue
            if is_dir:
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink()
        except OSError:
            continue
        if not path.exists():
            removed.append(path.name)
    return tuple(removed)


def _owner_pid(path: Path) -> int | None:
    try:
        return int((path / SCRATCH_MARKER).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _latest_mtime(path: Path) -> float:
    """目录取子树里最新的 mtime：会话还在写，就不算旧。"""
    try:
        latest = path.stat().st_mtime
    except OSError:
        return 0.0
    if not path.is_dir():
        return latest
    try:
        for child in path.rglob("*"):
            try:
                latest = max(latest, child.stat().st_mtime)
            except OSError:
                continue
    except OSError:
        return latest
    return latest


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
