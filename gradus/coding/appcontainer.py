"""Windows AppContainer 执行环境。

**已弃用**：Windows 后端后续走「受限令牌 + 写受限 SID」的 `restricted` 后端，
本模块只保留可用、不再演进；显式选 `sandbox=appcontainer` 仍会工作，但会给出
弃用提示。用 `warnings.deprecated`（PEP 702）标记，不在单独目录里存放。

与 Docker 后端的差别：不用装任何东西，直接用系统自带的 AppContainer。

- 用「固定名字 + 用户标识」的 profile，跨会话复用同一个 SID（崩溃后残留的
  ACE 下次还能清掉），同机不同用户拿到不同 SID；
- 子进程用当前用户 token + `SECURITY_CAPABILITIES` 启动，带上这个 SID；
- 文件系统默认全拒，只给 workspace、临时目录和 Python 安装目录授 ACE；
- 没有 capability，所以**默认断网**，也没有联网开关；
- 进程放进 Job Object，父进程一死就被连带杀掉，不留孤儿。

限制：
- 只有 Windows 能用；
- 容器里没有 Git Bash（它的安装目录不在授权范围），所以只注册 `powershell`；
- Python 由 `sys.executable` 提供，只给「跑起来用得到的那几块」授 ACE，
  不给整个安装目录授权（conda 根目录动辄几十万文件，逐文件传播 ACE 会卡几分钟）；
- 这是进程级隔离，不是虚拟机；AppContainer 的边界来自系统内核。

授权策略：
- workspace 与临时目录：递归授「修改」，会话结束撤销；
- 解释器：递归授「读+执行」`prefix`（venv）与 `base_prefix` 里的
  `DLLs` / `Library\\bin` / `Lib` 子项，跳过 `Lib\\site-packages`（venv 不继承它）；
  这些 ACE 不撤销——只对固定 profile 的 SID 可读，留着下次免重算。
"""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Sequence
from ctypes import wintypes
from pathlib import Path
from warnings import deprecated

from .sandbox import Environment, SandboxError, _pid_alive, current_user
from .win32 import (
    CREATE_UNICODE_ENVIRONMENT,
    EXTENDED_STARTUPINFO_PRESENT,
    ICACLS_TIMEOUT as _ICACLS_TIMEOUT,
    PROCESS_INFORMATION,
    SECURITY_ATTRIBUTES,
    STARTF_USESTDHANDLES,
    STARTUPINFOEXW,
    PipeReader as _PipeReader,
    PipeWriter as _PipeWriter,
    Process as _AppContainerProcess,
    advapi32,
    assign_job as _assign_job,
    create_pipes as _create_pipes,
    environment_block as _environment_block,
    has_ace as _has_ace,
    icacls as _icacls,
    kernel32,
    quote_argv as _quote,
    state_dirs as _state_dirs,
)

ERROR_ALREADY_EXISTS_HRESULT = -2147024713  # 0x800700B7
ERROR_NOT_FOUND_HRESULT = -2147023728  # 0x80070490
PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009

if sys.platform == "win32":
    userenv = ctypes.WinDLL("userenv", use_last_error=True)
else:  # pragma: no cover - 只有 Windows 会用到这个后端
    userenv = None


class SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [
        ("AppContainerSid", ctypes.c_void_p),
        ("Capabilities", ctypes.c_void_p),
        ("CapabilityCount", wintypes.DWORD),
        ("Reserved", wintypes.DWORD),
    ]


def _bind() -> None:
    userenv.CreateAppContainerProfile.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    userenv.CreateAppContainerProfile.restype = ctypes.c_long
    userenv.DeleteAppContainerProfile.argtypes = [wintypes.LPCWSTR]
    userenv.DeleteAppContainerProfile.restype = ctypes.c_long
    userenv.DeriveAppContainerSidFromAppContainerName.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    userenv.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long

    kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.POINTER(SECURITY_ATTRIBUTES),
        ctypes.POINTER(SECURITY_ATTRIBUTES),
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFOEXW),
        ctypes.POINTER(PROCESS_INFORMATION),
    ]
    kernel32.CreateProcessW.restype = wintypes.BOOL
    kernel32.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    kernel32.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL


if sys.platform == "win32":
    _bind()


def problem() -> str | None:
    """AppContainer 不可用时返回原因；可用时返回 `None`。"""
    if sys.platform != "win32":
        return "AppContainer is only available on Windows"
    try:
        userenv.CreateAppContainerProfile
        userenv.DeriveAppContainerSidFromAppContainerName
    except AttributeError as exc:
        return f"AppContainer APIs are missing: {exc}"
    return None


#: profile 名带用户标识：AppContainer SID 只由名字派生、不含用户，固定名字会让
#: 同一台机器上所有用户的 SID 相同，共享 workspace 上的 ACE 就互相生效。
PROFILE_PREFIX = "gradus-sandbox"

#: 旧版本用的固定名字；`--reset-sandbox` 一并回收。
LEGACY_PROFILE_NAME = "gradus-sandbox"

#: 解释器根目录下需要的文件类型（Python 二进制与依赖 DLL）。
_INTERPRETER_SUFFIXES = frozenset({".dll", ".exe", ".pyd", ".pth"})

#: 记录「授过 ACE 的路径 -> owner pid」，用来清理被强杀会话的残留。
GRANT_RECORD = "acl-grants.json"


@deprecated("AppContainer backend is deprecated; the Windows target is `restricted`")
class AppContainerEnvironment:
    """用 Windows AppContainer 跑命令与脚本。

    已弃用：保留现有行为，不再演进；新的 Windows 后端是受限令牌方案。
    """

    name = "appcontainer"

    def __init__(self, root: str | Path, *, python: str | None = None) -> None:
        self._root = Path(root).expanduser().resolve()
        self._python = python or sys.executable
        self._scratch = Path(tempfile.mkdtemp(prefix="gradus-ac-"))
        self._profile = profile_name()
        self._sid: int | None = None
        self._sid_text: str | None = None
        self._granted: list[Path] = []
        self._prepared = False
        self._closed = False

    # ---- Environment 协议 ----

    def scratch_dir(self) -> Path:
        return self._scratch

    def map_path(self, path: str | Path) -> str:
        return str(path)

    def python(self) -> str:
        return self._python

    async def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        merge_stderr: bool = False,
    ):
        await self._prepare()
        return await asyncio.to_thread(
            self._spawn_sync, list(argv), cwd, merge_stderr
        )

    async def recycle(self) -> None:
        """没有长驻实例可回收：超时方 TerminateProcess，Job Object 会带走子进程。"""
        return None

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        granted = list(self._granted)
        await self._revoke()
        _forget_grants(granted)
        shutil.rmtree(self._scratch, ignore_errors=True)
        if self._sid is not None:
            # profile 保留：跨会话复用同一个 SID，崩溃后残留的 ACE 下次还能清掉。
            kernel32.LocalFree(ctypes.c_void_p(self._sid))
            self._sid = None

    # ---- 准备 ----

    async def _prepare(self) -> None:
        if self._prepared:
            return
        sid = ctypes.c_void_p()
        result = userenv.CreateAppContainerProfile(
            wintypes.LPCWSTR(self._profile),
            wintypes.LPCWSTR(self._profile),
            wintypes.LPCWSTR("Gradus sandbox"),
            None,
            0,
            ctypes.byref(sid),
        )
        if result != 0:
            if result != ERROR_ALREADY_EXISTS_HRESULT:
                raise SandboxError(
                    f"CreateAppContainerProfile failed: 0x{result & 0xFFFFFFFF:08x}"
                )
            result = userenv.DeriveAppContainerSidFromAppContainerName(
                wintypes.LPCWSTR(self._profile), ctypes.byref(sid)
            )
            if result != 0:
                raise SandboxError(
                    f"DeriveAppContainerSid failed: 0x{result & 0xFFFFFFFF:08x}"
                )

        self._sid = sid.value
        text = wintypes.LPWSTR()
        advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text))
        self._sid_text = text.value
        kernel32.LocalFree(text)

        await self._grant(self._scratch, "M")
        await self._grant(self._root, "M")
        _record_grants(self._granted)
        await self._grant_interpreter()
        self._prepared = True

    async def _grant_interpreter(self) -> None:
        """只授「解释器跑起来用得到」的子树，避开几十万文件的安装根目录。"""
        marker = self._markers()
        if any(path.exists() for path in marker):
            return
        prefix = Path(sys.prefix)
        base = Path(sys.base_prefix)

        # venv：整树都要（依赖装在它自己的 site-packages 里）。
        if prefix.is_dir():
            await self._grant(prefix, "RX", revoke=False)
        if prefix == base or not base.is_dir():
            _write_marker(marker)
            return

        # base：目录本身只授「可遍历」，再逐个补要用的子树。
        await self._grant(base, "RX", inherit=False, revoke=False)
        await self._grant(base / "Library", "RX", inherit=False, revoke=False)
        for name in ("DLLs", "Library\\bin"):
            path = base / name
            if path.is_dir():
                await self._grant(path, "RX", revoke=False)
        for child in sorted(base.iterdir()):
            if child.is_file() and child.suffix.lower() in _INTERPRETER_SUFFIXES:
                await self._grant(child, "RX", inherit=False, revoke=False)

        lib = base / "Lib"
        if not lib.is_dir():
            _write_marker(marker)
            return
        await self._grant(lib, "RX", inherit=False, revoke=False)
        directories: list[Path] = []
        files: list[Path] = []
        for child in sorted(lib.iterdir()):
            # venv 不继承系统 site-packages，跳过它是省下几万次 ACE 传播的关键。
            if child.name.lower() == "site-packages":
                continue
            (directories if child.is_dir() else files).append(child)
        for child in directories:
            await self._grant(child, "RX", revoke=False)
        await self._grant_by_suffix(files)
        _write_marker(marker)

    def _markers(self) -> list[Path]:
        """解释器授权的一次性标记：同 SID + 同解释器就不再重算。

        优先放在 `%LOCALAPPDATA%`，写不进去就退到临时目录。
        """
        assert self._sid_text is not None
        try:
            stamp = int(Path(self._python).stat().st_mtime)
        except OSError:
            stamp = 0
        key = f"{self._sid_text}|{sys.prefix}|{sys.base_prefix}|{stamp}"
        name = f"acl-{hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]}.done"
        return [directory / name for directory in _state_dirs()]

    async def _grant_by_suffix(self, files: Sequence[Path]) -> None:
        """同一目录下同后缀的散文件用通配符一次性授权，省掉逐个调用。"""
        if not files:
            return
        counts: dict[str, int] = {}
        for path in files:
            counts[path.suffix.lower()] = counts.get(path.suffix.lower(), 0) + 1
        directory = files[0].parent
        for suffix, count in counts.items():
            if suffix and count > 1:
                await self._grant(
                    directory / f"*{suffix}", "RX", inherit=False, revoke=False
                )
        for path in files:
            suffix = path.suffix.lower()
            if not suffix or counts[suffix] == 1:
                await self._grant(path, "RX", inherit=False, revoke=False)

    async def _grant(
        self,
        target: str | Path,
        rights: str,
        *,
        inherit: bool = True,
        revoke: bool = True,
    ) -> None:
        """给单个路径授 ACE；`inherit=True` 时连带子树（只对目录有意义）。"""
        assert self._sid_text is not None
        # 解释器那批 ACE 不撤销：已经授过就跳过。Windows 修改目录 DACL 会遍历整棵子树，
        # 对 conda 这种几十万文件的根目录，重复授权要卡几分钟。
        if not revoke and "*" not in str(target) and await _has_ace(str(target), self._sid_text):
            return
        suffix = f":(OI)(CI){rights}" if inherit else f":{rights}"
        code = await _icacls(
            [str(target), "/grant", f"*{self._sid_text}{suffix}", "/C", "/Q"]
        )
        if code != 0:
            raise SandboxError(f"icacls grant failed on {target} (exit {code})")
        if revoke:
            self._granted.append(Path(target))

    async def _revoke(self) -> None:
        if self._sid_text is None:
            return
        for path in reversed(self._granted):
            await _icacls(
                [str(path), "/remove:g", f"*{self._sid_text}", "/C", "/Q"]
            )
        self._granted.clear()

    # ---- 启动进程 ----

    def _spawn_sync(
        self,
        argv: list[str],
        cwd: Path | None,
        merge_stderr: bool,
    ) -> _AppContainerProcess:
        assert self._sid is not None
        workdir = str(cwd or self._root)

        pipes = _create_pipes()
        if pipes is None:
            raise SandboxError("CreatePipe failed")
        stdin_read, stdin_write, stdout_read, stdout_write, stderr_read, stderr_write = pipes

        attr_list, attr_buffer, capabilities = _attribute_list(self._sid)
        startup = STARTUPINFOEXW()
        startup.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES
        startup.StartupInfo.hStdInput = stdin_read
        startup.StartupInfo.hStdOutput = stdout_write
        startup.StartupInfo.hStdError = stdout_write if merge_stderr else stderr_write
        startup.lpAttributeList = attr_list

        env_buffer = _environment_block(self._scratch)
        command = ctypes.create_unicode_buffer(_quote(argv))
        info = PROCESS_INFORMATION()

        # AppContainer 的身份来自 `SECURITY_CAPABILITIES` 属性，不需要换 token：
        # `CreateProcessW` 会按属性把当前用户的 token 约束成 AppContainer token。
        ok = kernel32.CreateProcessW(
            None,
            command,
            None,
            None,
            True,
            EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT,
            ctypes.cast(env_buffer, ctypes.c_void_p),
            workdir,
            ctypes.byref(startup),
            ctypes.byref(info),
        )
        if not ok:
            for handle in (
                stdin_read,
                stdin_write,
                stdout_read,
                stdout_write,
                stderr_read,
                stderr_write,
            ):
                kernel32.CloseHandle(handle)
            raise SandboxError(
                f"CreateProcess failed: {ctypes.WinError(ctypes.get_last_error())}"
            )

        job = _assign_job(info.hProcess)
        kernel32.CloseHandle(info.hThread)
        kernel32.CloseHandle(stdin_read)
        kernel32.CloseHandle(stdout_write)
        kernel32.CloseHandle(stderr_write)
        if merge_stderr:
            kernel32.CloseHandle(stderr_read)
            stderr = None
        else:
            stderr = _PipeReader(stderr_read)

        # attr_buffer / capabilities 必须活到 CreateProcess 之后；挂在进程对象上保活。
        process = _AppContainerProcess(
            info.hProcess,
            job,
            int(info.dwProcessId),
            _PipeWriter(stdin_write),
            _PipeReader(stdout_read),
            stderr,
        )
        process._attribute_buffer = attr_buffer
        process._capabilities = capabilities
        return process


def profile_name() -> str:
    """当前用户专属的 profile 名；同机不同用户拿到不同的 SID。"""
    user = current_user()
    if not user:
        return PROFILE_PREFIX
    digest = hashlib.sha1(user.encode("utf-8")).hexdigest()[:8]
    return f"{PROFILE_PREFIX}-{digest}"


def _read_grants() -> dict[str, int]:
    for directory in _state_dirs():
        try:
            data = json.loads((directory / GRANT_RECORD).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return {
                str(key): int(value)
                for key, value in data.items()
                if isinstance(value, int) and not isinstance(value, bool)
            }
    return {}


def _write_grants(grants: dict[str, int]) -> None:
    # 每个状态目录都写一份：只写一份的话，清空时另一份会在下次读到旧内容。
    for directory in _state_dirs():
        path = directory / GRANT_RECORD
        try:
            if grants:
                directory.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(grants, ensure_ascii=False), encoding="utf-8")
            else:
                path.unlink(missing_ok=True)
        except OSError:
            continue


def _key(path: str | Path) -> str:
    """记录里的路径统一成解析后的绝对路径（Temp 这类目录可能是 junction）。"""
    return str(Path(path).expanduser().resolve())


def _record_grants(paths: Sequence[Path]) -> None:
    grants = _read_grants()
    pid = os.getpid()
    for path in paths:
        grants[_key(path)] = pid
    _write_grants(grants)


def _forget_grants(paths: Sequence[Path]) -> None:
    grants = _read_grants()
    pid = os.getpid()
    for path in paths:
        key = _key(path)
        if grants.get(key) == pid:
            del grants[key]
    _write_grants(grants)


def _profile_sid_text() -> str | None:
    """不建 profile，只把固定名字换算成 SID 文本。"""
    if sys.platform != "win32" or userenv is None:
        return None
    sid = ctypes.c_void_p()
    result = userenv.DeriveAppContainerSidFromAppContainerName(
        wintypes.LPCWSTR(profile_name()), ctypes.byref(sid)
    )
    if result != 0:
        return None
    text = wintypes.LPWSTR()
    advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text))
    value = text.value
    kernel32.LocalFree(text)
    kernel32.LocalFree(sid)
    return value


async def cleanup_orphans(keep: str | Path | None = None) -> tuple[str, ...]:
    """撤销「启动它的 Gradus 进程已经不在」的 workspace 授权。

    强杀（Ctrl-C 两次、任务管理器结束）时 `aclose` 跑不到，workspace 上的
    `(OI)(CI)M` 会一直留着。每次启动前按记录里的 pid 判断孤儿：
    pid 被系统复用时最多多留一次，不会误删正在用的。
    """
    grants = _read_grants()
    if not grants:
        return ()
    sid = _profile_sid_text()
    if sid is None:
        return ()
    keep_path = _key(keep) if keep is not None else None

    removed: list[str] = []
    for path, pid in list(grants.items()):
        if _key(path) == keep_path or _pid_alive(pid):
            continue
        if not Path(path).exists():
            del grants[path]
            continue
        code = await _icacls([path, "/remove:g", f"*{sid}", "/C", "/Q"])
        if code == 0:
            removed.append(path)
            del grants[path]
    _write_grants(grants)
    return tuple(removed)


async def reset_state() -> tuple[str, ...]:
    """清掉 appcontainer 的持久状态：残留授权、解释器标记、AppContainer profile。

    profile 用固定名字跨会话复用，正常关闭不删除；这里给一个显式入口，
    在升级、换解释器或怀疑授权错乱时清一次。还有别的 Gradus 进程在用
    appcontainer 时拒绝执行——删 profile 会让它手里的 SID 失效。

    解释器的 ACE 不单独记录、也不撤销：SID 由固定名字派生，profile 删掉
    后重建还是同一个 SID，那些 ACE 会重新生效，不会变成野授权。
    """
    if sys.platform != "win32" or userenv is None:
        return ()

    owners = sorted(
        {
            pid
            for pid in _read_grants().values()
            if pid != os.getpid() and _pid_alive(pid)
        }
    )
    if owners:
        raise SandboxError(
            "AppContainer is in use by another Gradus process: "
            + ", ".join(str(pid) for pid in owners)
        )

    notes: list[str] = []
    sid = _profile_sid_text()
    if sid is not None:
        for path in sorted(_read_grants()):
            if not Path(path).exists():
                continue
            if await _icacls([path, "/remove:g", f"*{sid}", "/C", "/Q"]) == 0:
                notes.append(f"revoked {path}")
    _write_grants({})

    for directory in _state_dirs():
        for marker in directory.glob("acl-*.done"):
            try:
                marker.unlink()
            except OSError:
                continue
            notes.append(f"removed {marker}")

    for name in (profile_name(), LEGACY_PROFILE_NAME):
        if not _profile_exists(name):
            continue
        result = userenv.DeleteAppContainerProfile(wintypes.LPCWSTR(name))
        if result == 0:
            notes.append(f"deleted AppContainer profile {name}")
        elif (result & 0xFFFFFFFF) != (ERROR_NOT_FOUND_HRESULT & 0xFFFFFFFF):
            raise SandboxError(
                f"DeleteAppContainerProfile({name}) failed: "
                f"0x{result & 0xFFFFFFFF:08x}"
            )
    return tuple(notes)


def _profile_exists(name: str) -> bool:
    """profile 不存在时 `DeleteAppContainerProfile` 也返回 S_OK，只能先看目录。

    profile 落在 `%LOCALAPPDATA%\\Packages\\<name>`；判断不了时按存在处理，
    让 API 自己给出结果。
    """
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return True
    return (Path(local) / "Packages" / name).is_dir()


def _write_marker(markers: list[Path]) -> None:
    # 每份都写：临时目录可能被系统清理，留一份在 %LOCALAPPDATA% 更稳。
    for marker in markers:
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("ok", encoding="utf-8")
        except OSError:
            continue


def _attribute_list(sid: int):
    size = ctypes.c_size_t()
    kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
    buffer = ctypes.create_string_buffer(size.value)
    attr_list = ctypes.cast(buffer, ctypes.c_void_p)
    if not kernel32.InitializeProcThreadAttributeList(
        attr_list, 1, 0, ctypes.byref(size)
    ):
        raise SandboxError(
            f"InitializeProcThreadAttributeList failed: "
            f"{ctypes.WinError(ctypes.get_last_error())}"
        )

    capabilities = SECURITY_CAPABILITIES(
        AppContainerSid=ctypes.c_void_p(sid),
        Capabilities=None,
        CapabilityCount=0,
        Reserved=0,
    )
    if not kernel32.UpdateProcThreadAttribute(
        attr_list,
        0,
        PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
        ctypes.byref(capabilities),
        ctypes.sizeof(capabilities),
        None,
        None,
    ):
        raise SandboxError(
            f"UpdateProcThreadAttribute failed: {ctypes.WinError(ctypes.get_last_error())}"
        )
    # `capabilities` 必须活到 CreateProcess 之后：属性列表里存的是它的地址。
    return attr_list, buffer, capabilities

