"""在「已存在的容器」里执行命令（attach 模式）。

把「跑 Gradus 的环境」和「执行命令的环境」拆开：Gradus 留在自己的解释器里（>=3.13），
命令通过 `docker exec` 进入别人给的容器（比如 SWE-bench 实例镜像，仓库在 `/testbed`）。

与 `DockerEnvironment` 的区别：容器**不是本模块建的**，所以 `recycle()` 只杀 exec 子进程、
`aclose()` 只清理临时目录，绝不 `docker rm`。

文件工具不需要走这里：宿主 workspace 通过 bind mount 挂进容器，两边看到的是同一份文件。

设计细节见 `docs/exec_environment.md`。
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path

from .sandbox import SandboxError

#: 容器内默认的仓库挂载点（SWE-bench 实例镜像的约定）。
DEFAULT_CONTAINER_WORKSPACE = "/testbed"

#: 容器内默认解释器；实例镜像里通常在 PATH 上。
DEFAULT_CONTAINER_PYTHON = "python3"


class ExecEnvironment:
    """把命令送进一个**已经存在**的容器；环境本身不负责容器的生命周期。"""

    name = "exec"

    def __init__(
        self,
        container: str,
        *,
        workspace: str | Path,
        container_workspace: str = DEFAULT_CONTAINER_WORKSPACE,
        python: str = DEFAULT_CONTAINER_PYTHON,
        docker: str | Sequence[str] = "docker",
    ) -> None:
        if not container:
            raise SandboxError("exec environment requires a running container name")
        self._container = container
        self._root = Path(workspace).expanduser().resolve()
        stripped = container_workspace.strip("/")
        self._container_root = f"/{stripped}" if stripped else "/"
        self._python = python or DEFAULT_CONTAINER_PYTHON
        self._docker: tuple[str, ...] = (
            (docker,) if isinstance(docker, str) else tuple(docker)
        )
        self._scratch: Path | None = None
        self._procs: set[asyncio.subprocess.Process] = set()

    # -- 协议实现 ---------------------------------------------------------

    def scratch_dir(self) -> Path:
        """宿主侧临时目录；它**不在**容器挂载点里，需要容器可见时请用 workspace。"""
        if self._scratch is None:
            self._scratch = Path(tempfile.mkdtemp(prefix="gradus-exec-"))
        return self._scratch

    def map_path(self, path: str | Path) -> str:
        """宿主路径 → 容器内路径；只允许 workspace 之下。"""
        resolved = Path(path).expanduser().resolve()
        try:
            relative = resolved.relative_to(self._root)
        except ValueError:
            raise SandboxError(
                f"path is outside the exec workspace ({self._root}): {resolved}"
            ) from None
        return (
            self._container_root
            if not relative.parts
            else f"{self._container_root}/{relative.as_posix()}"
        )

    def python(self) -> str:
        return self._python

    def exec_argv(self, argv: Sequence[str], workdir: str) -> list[str]:
        """拼 `docker exec` 的参数；单独暴露便于离线断言。"""
        return [
            *self._docker,
            "exec",
            "-i",
            "-w",
            workdir,
            self._container,
            *argv,
        ]

    async def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        merge_stderr: bool = False,
    ) -> asyncio.subprocess.Process:
        workdir = self.map_path(cwd) if cwd is not None else self._container_root
        try:
            process = await asyncio.create_subprocess_exec(
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
            raise SandboxError(f"cannot run in container {self._container}: {exc}") from exc
        self._procs.add(process)
        return process

    async def recycle(self) -> None:
        """杀掉还在跑的 exec 子进程；容器保持原样（attach 语义）。"""
        for process in list(self._procs):
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
        self._procs.clear()
        await asyncio.sleep(0)

    async def aclose(self) -> None:
        await self.recycle()
        if self._scratch is not None:
            shutil.rmtree(self._scratch, ignore_errors=True)
            self._scratch = None

    # -- 便于诊断 ---------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"ExecEnvironment(container={self._container!r}, "
            f"workspace={str(self._root)!r}, container_workspace={self._container_root!r})"
        )
