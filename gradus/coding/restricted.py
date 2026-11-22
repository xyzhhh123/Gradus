"""Windows 受限写沙箱（Cordon，后端名 `restricted`）。

读按当前用户、写只能在圈内（副本与 scratch）：不需要管理员，不建账户。

- 每个会话随机生成一个合成 SID（`S-1-5-21-<4 个随机数>`），只对副本与
  scratch 授 `(OI)(CI)M`；
- 用自身主令牌派生受限令牌：`DISABLE_MAX_PRIVILEGE | LUA_TOKEN | WRITE_RESTRICTED`，
  受限 SID 列表里只有这个合成 SID。写访问会做第二次检查，受限 SID 没有 ACE
  的位置一律 `Access is denied`；读访问仍按普通令牌（也就是当前用户）；
- 传给 `CreateProcessAsUserW` 的是**自身主令牌的受限版本**，这种情形不需要
  `SeAssignPrimaryTokenPrivilege`，所以不用提权；
- 进程放进 Job Object，父进程一死就被连带杀掉；
- 子进程只拿到最小环境块，宿主里的 key 不外传，`TEMP` / `TMP` 指到 scratch。

已知短板：**不阻断网络**。unelevated 没有防火墙规则可用，这一层只能做到
文件写边界；需要断网时要靠 `docker` 后端。

与 `appcontainer.py` 的分工：本模块是 Windows 的目标后端，AppContainer 保留
可用、已标记弃用。共用的 Win32 原语在 `win32.py`。
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import secrets
import shutil
import sys
from collections.abc import Sequence
from ctypes import wintypes
from pathlib import Path

from .sandbox import SandboxError, create_scratch
from .win32 import (
    CREATE_UNICODE_ENVIRONMENT,
    PROCESS_INFORMATION,
    STARTF_USESTDHANDLES,
    STARTUPINFOW,
    PipeReader,
    PipeWriter,
    Process,
    advapi32,
    assign_job,
    create_pipes,
    environment_block,
    kernel32,
    quote_argv,
)

TOKEN_ALL_ACCESS = 0x000F01FF
TOKEN_QUERY = 0x00000008
TOKEN_INFORMATION_GROUPS = 2
DISABLE_MAX_PRIVILEGE = 0x00000001
LUA_TOKEN = 0x00000004
WRITE_RESTRICTED = 0x00000008
SE_GROUP_LOGON_ID = 0xC0000000

#: 合成 SID 没有注册过，`icacls *SID` 会报 1332（no mapping），所以直接改 ACL 对象。
#:
#: 不用 `Get-Acl` / `Set-Acl`，两个坑：
#: - `Get-Acl` 在 `Microsoft.PowerShell.Security` 里，父进程是 pwsh 时子进程继承到的
#:   `PSModulePath` 是 PS7 的，5.1 解析到 PS7 的模块加载失败，脚本 exit 1；
#: - `Set-Acl` 在**受保护 DACL** 的目录上要求 `SeSecurityPrivilege`，而
#:   `tempfile.mkdtemp` 建出来的目录正是受保护的（SDDL 里带 `D:P`），非提权跑不通。
#:
#: `DirectoryInfo.GetAccessControl()` / `SetAccessControl()`（Win32 `SetSecurityInfo`）
#: 两个毛病都没有：不依赖模块，改的也只是 DACL。整段只用 .NET 类型，不调 cmdlet。
_ACL_SCRIPT = """
$ErrorActionPreference = 'Stop'
$path = '{path}'
$sid = [System.Security.Principal.SecurityIdentifier]'{sid}'
$rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
    $sid,
    [System.Security.AccessControl.FileSystemRights]'Modify',
    [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit',
    [System.Security.AccessControl.PropagationFlags]'None',
    [System.Security.AccessControl.AccessControlType]'Allow')
$info = [System.IO.DirectoryInfo]::new($path)
$security = $info.GetAccessControl()
$security.{action}($rule)
$info.SetAccessControl($security)
"""

_ACL_TIMEOUT = 120.0


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Sid", ctypes.c_void_p),
        ("Attributes", wintypes.DWORD),
    ]


class TOKEN_GROUPS(ctypes.Structure):
    _fields_ = [
        ("GroupCount", wintypes.DWORD),
        ("Groups", SID_AND_ATTRIBUTES * 1),
    ]


def _bind() -> None:
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.CreateRestrictedToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.CreateRestrictedToken.restype = wintypes.BOOL
    kernel32.CreateProcessAsUserW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFOW),
        ctypes.POINTER(PROCESS_INFORMATION),
    ]
    kernel32.CreateProcessAsUserW.restype = wintypes.BOOL


if sys.platform == "win32":
    _bind()


def problem() -> str | None:
    """受限写后端不可用时返回原因；可用时返回 `None`。"""
    if sys.platform != "win32":
        return "the restricted backend is only available on Windows"
    try:
        advapi32.ConvertStringSidToSidW
        advapi32.CreateRestrictedToken
        kernel32.CreateProcessAsUserW
    except AttributeError as exc:
        return f"restricted backend APIs are missing: {exc}"
    return None


def random_sid() -> str:
    """每个会话一个合成 SID；不需要账户存在，也不会与真实用户撞车。"""
    parts = [secrets.randbits(32) for _ in range(4)]
    return "S-1-5-21-" + "-".join(str(part) for part in parts)


def _legacy_module_env() -> dict[str, str]:
    """给 Windows PowerShell 5.1 子进程一份它自己的模块路径。

    `_acl` 跑的是 5.1（`powershell`，每台 Windows 都有），它继承的 `PSModulePath`
    若来自 pwsh 父进程，里面是 PS7 的目录，5.1 会加载 PS7 的模块而失败。ACL 脚本已经
    不调任何 cmdlet（见 `_ACL_SCRIPT`），这里只留一条保险：以后要在里面用 cmdlet，
    不至于又踩一遍模块加载的坑。
    """
    home = os.environ.get("USERPROFILE")
    program_files = os.environ.get("ProgramFiles")
    windir = os.environ.get("SystemRoot") or os.environ.get("windir")

    parts: list[str] = []
    if home:
        parts.append(str(Path(home) / "Documents" / "WindowsPowerShell" / "Modules"))
    if program_files:
        parts.append(str(Path(program_files) / "WindowsPowerShell" / "Modules"))
    if windir:
        parts.append(
            str(Path(windir) / "System32" / "WindowsPowerShell" / "v1.0" / "Modules")
        )

    env = dict(os.environ)
    if parts:
        env["PSModulePath"] = os.pathsep.join(parts)
    else:
        env.pop("PSModulePath", None)
    return env


async def _acl(target: Path, sid: str, action: str) -> None:
    """给目录增删一条继承的可修改 ACE；`action` 是 ACL 对象的方法名。"""
    escaped = str(target).replace("'", "''")
    script = _ACL_SCRIPT.format(path=escaped, sid=sid, action=action)
    try:
        process = await asyncio.create_subprocess_exec(
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=_legacy_module_env(),
        )
    except OSError as exc:
        raise SandboxError(f"cannot run powershell for ACL: {exc}") from exc
    try:
        code = await asyncio.wait_for(process.wait(), timeout=_ACL_TIMEOUT)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise SandboxError(f"ACL update timed out on {target}") from None
    if code != 0:
        raise SandboxError(f"ACL {action} failed on {target} (exit {code})")


class RestrictedEnvironment:
    """用受限令牌跑命令与脚本：读照常，写只落在副本和 scratch。"""

    name = "restricted"

    def __init__(self, root: str | Path, *, python: str | None = None) -> None:
        self._root = Path(root).expanduser().resolve()
        self._python = python or sys.executable
        self._scratch = create_scratch("gradus-rs-")
        self._sids: list[ctypes.c_void_p] = []
        self._owned_sids: list[ctypes.c_void_p] = []
        self._buffers: list[ctypes.Array] = []
        self._sid = ctypes.c_void_p()
        self._sid_text: str | None = None
        self._token: int | None = None
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
        # 撤销副本与 scratch 上的 ACE：任一方删除失败时也不留下能写的目录。
        if self._sid_text is not None:
            for path in (self._root, self._scratch):
                if path.exists():
                    try:
                        await _acl(path, self._sid_text, "RemoveAccessRule")
                    except SandboxError:
                        continue
        if self._token:
            kernel32.CloseHandle(self._token)
            self._token = None
        for sid in self._owned_sids:
            if sid.value:
                kernel32.LocalFree(sid)
        self._owned_sids = []
        self._sids = []
        self._buffers = []
        self._sid = ctypes.c_void_p()
        shutil.rmtree(self._scratch, ignore_errors=True)

    # ---- 准备 ----

    async def _prepare(self) -> None:
        if self._prepared:
            return
        self._sid_text = random_sid()
        # 受限 SID 清单对齐 Codex 的 workspace-write 形态：自己的合成 SID 负责写，
        # logon SID 与 Everyone 负责让进程能启动、能读系统文件。当前用户 SID 不能
        # 放进来——那等于把用户能写的地方全部放行。
        own = _string_sid(self._sid_text)
        logon, buffer = _logon_sid()
        self._buffers.append(buffer)
        everyone = _string_sid("S-1-1-0")
        # 只释放自己分配的 SID；logon SID 指向上面那块缓冲区，不能 LocalFree。
        self._owned_sids = [own, everyone]
        self._sids = [own, logon, everyone]
        self._sid = own
        await self._grant(self._scratch)
        await self._grant(self._root)
        self._token = _restricted_token(self._sids)
        self._prepared = True

    async def _grant(self, target: Path) -> None:
        assert self._sid_text is not None
        await _acl(target, self._sid_text, "AddAccessRule")

    # ---- 启动进程 ----

    def _spawn_sync(
        self,
        argv: list[str],
        cwd: Path | None,
        merge_stderr: bool,
    ):
        assert self._token is not None, "spawn() must call _prepare() first"
        workdir = str(cwd or self._root)

        pipes = create_pipes()
        if pipes is None:
            raise SandboxError("CreatePipe failed")
        (
            stdin_read,
            stdin_write,
            stdout_read,
            stdout_write,
            stderr_read,
            stderr_write,
        ) = pipes

        startup = STARTUPINFOW()
        startup.cb = ctypes.sizeof(STARTUPINFOW)
        startup.dwFlags = STARTF_USESTDHANDLES
        startup.hStdInput = stdin_read
        startup.hStdOutput = stdout_write
        startup.hStdError = stdout_write if merge_stderr else stderr_write

        env_buffer = environment_block(self._scratch)
        command = ctypes.create_unicode_buffer(quote_argv(argv))
        info = PROCESS_INFORMATION()

        ok = kernel32.CreateProcessAsUserW(
            self._token,
            None,
            command,
            None,
            None,
            True,
            CREATE_UNICODE_ENVIRONMENT,
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
                f"CreateProcessAsUser failed: {ctypes.WinError(ctypes.get_last_error())}"
            )

        job = assign_job(info.hProcess)
        kernel32.CloseHandle(info.hThread)
        kernel32.CloseHandle(stdin_read)
        kernel32.CloseHandle(stdout_write)
        kernel32.CloseHandle(stderr_write)
        if merge_stderr:
            kernel32.CloseHandle(stderr_read)
            stderr = None
        else:
            stderr = PipeReader(stderr_read)

        process = Process(
            info.hProcess,
            job,
            int(info.dwProcessId),
            PipeWriter(stdin_write),
            PipeReader(stdout_read),
            stderr,
        )
        # 环境块与命令行缓冲区只需要活到 CreateProcess 返回，这里保活到进程结束，
        # 与 AppContainer 后端的做法一致，避免任何一个后端出现悬空指针。
        process._keepalive.extend([env_buffer, command])
        return process


def _logon_sid() -> tuple[ctypes.c_void_p, ctypes.Array]:
    """从当前进程令牌的组里取 logon SID（`S-1-5-5-X-Y`）。

    受限令牌里没有它时，进程连 DLL 都初始化不了（0xC0000142）。返回的 SID
    指向内部缓冲区，调用方要把缓冲区一起保活到令牌用完。
    """
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
    ):
        raise SandboxError(
            f"OpenProcessToken failed: {ctypes.WinError(ctypes.get_last_error())}"
        )
    try:
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(
            token, TOKEN_INFORMATION_GROUPS, None, 0, ctypes.byref(size)
        )
        if size.value == 0:
            raise SandboxError("GetTokenInformation(TokenGroups) returned no size")
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(
            token, TOKEN_INFORMATION_GROUPS, buffer, size.value, ctypes.byref(size)
        ):
            raise SandboxError(
                "GetTokenInformation(TokenGroups) failed: "
                f"{ctypes.WinError(ctypes.get_last_error())}"
            )
    finally:
        kernel32.CloseHandle(token)

    count = ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD)).contents.value
    offset = ctypes.sizeof(TOKEN_GROUPS) - ctypes.sizeof(SID_AND_ATTRIBUTES)
    for index in range(count):
        entry = SID_AND_ATTRIBUTES.from_address(
            ctypes.addressof(buffer)
            + offset
            + index * ctypes.sizeof(SID_AND_ATTRIBUTES)
        )
        if (entry.Attributes & SE_GROUP_LOGON_ID) == SE_GROUP_LOGON_ID:
            return ctypes.c_void_p(entry.Sid), buffer
    raise SandboxError("logon SID is not present on the current token")


def _string_sid(text: str) -> ctypes.c_void_p:
    sid = ctypes.c_void_p()
    if not advapi32.ConvertStringSidToSidW(
        wintypes.LPCWSTR(text), ctypes.byref(sid)
    ):
        raise SandboxError(
            f"ConvertStringSidToSid failed for {text}: "
            f"{ctypes.WinError(ctypes.get_last_error())}"
        )
    return sid


def _restricted_token(sids: Sequence[ctypes.c_void_p]) -> int:
    """从自身主令牌派生受限令牌；受限 SID 清单由调用方给。"""
    base = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), TOKEN_ALL_ACCESS, ctypes.byref(base)
    ):
        raise SandboxError(
            f"OpenProcessToken failed: {ctypes.WinError(ctypes.get_last_error())}"
        )

    entries = (SID_AND_ATTRIBUTES * len(sids))()
    for index, sid in enumerate(sids):
        entries[index].Sid = sid
        entries[index].Attributes = 0

    token = wintypes.HANDLE()
    ok = advapi32.CreateRestrictedToken(
        base,
        DISABLE_MAX_PRIVILEGE | LUA_TOKEN | WRITE_RESTRICTED,
        0,
        None,
        0,
        None,
        len(sids),
        ctypes.cast(entries, ctypes.c_void_p),
        ctypes.byref(token),
    )
    kernel32.CloseHandle(base)
    if not ok:
        raise SandboxError(
            f"CreateRestrictedToken failed: {ctypes.WinError(ctypes.get_last_error())}"
        )
    return token.value
