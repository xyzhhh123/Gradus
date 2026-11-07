"""两个 Windows 后端共用的 Win32 原语。

`appcontainer.py`（已弃用）与 `restricted.py`（目标后端）都要用同一套东西：
管道、Job Object、`icacls`、命令行拼接、最小环境块。放在这里而不是互相 import，
是为了让两个后端互不依赖——删掉任何一个都不影响另一个。

非 Windows 上 `kernel32` / `advapi32` 为 `None`，调用方先看 `problem()`。
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import re
import sys
import tempfile
from collections.abc import Sequence
from ctypes import wintypes
from pathlib import Path

EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
STARTF_USESTDHANDLES = 0x00000100
HANDLE_FLAG_INHERIT = 0x00000001
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
STILL_ACTIVE = 259

#: 单次 `icacls` 的时间上限；超过就当作失败，避免整个会话卡死。
ICACLS_TIMEOUT = 120.0

if sys.platform == "win32":
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
else:  # pragma: no cover - 只有 Windows 会用到这些后端
    kernel32 = advapi32 = None


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [
        ("StartupInfo", STARTUPINFOW),
        ("lpAttributeList", ctypes.c_void_p),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def bind() -> None:
    """绑定两个后端都要用的函数签名。后端再各自补自己那部分。"""
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CreatePipe.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(SECURITY_ATTRIBUTES),
        wintypes.DWORD,
    ]
    kernel32.CreatePipe.restype = wintypes.BOOL
    kernel32.SetHandleInformation.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    kernel32.SetHandleInformation.restype = wintypes.BOOL
    kernel32.ReadFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    kernel32.ReadFile.restype = wintypes.BOOL
    kernel32.WriteFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    kernel32.WriteFile.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]

    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi32.ConvertStringSidToSidW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL


if sys.platform == "win32":
    bind()


class PipeReader:
    def __init__(self, handle: int) -> None:
        self._handle = handle
        self._buffer = b""
        self._done = False

    async def readline(self) -> bytes:
        while b"\n" not in self._buffer and not self._done:
            chunk = await asyncio.to_thread(self._read, 65536)
            if not chunk:
                self._done = True
                break
            self._buffer += chunk
        if b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            return line + b"\n"
        line, self._buffer = self._buffer, b""
        return line

    async def read(self) -> bytes:
        while not self._done:
            chunk = await asyncio.to_thread(self._read, 65536)
            if not chunk:
                self._done = True
                break
            self._buffer += chunk
        data, self._buffer = self._buffer, b""
        return data

    def _read(self, size: int) -> bytes:
        buffer = ctypes.create_string_buffer(size)
        read = wintypes.DWORD()
        if not kernel32.ReadFile(
            self._handle, buffer, size, ctypes.byref(read), None
        ):
            return b""
        return buffer.raw[: read.value]

    def close(self) -> None:
        if self._handle:
            kernel32.CloseHandle(self._handle)
            self._handle = 0


class PipeWriter:
    """对齐 `asyncio.StreamWriter`：`write` 同步入缓冲，`drain` 异步刷出。"""

    def __init__(self, handle: int) -> None:
        self._handle = handle
        self._buffer = b""

    def write(self, data: bytes) -> None:
        self._buffer += data

    async def drain(self) -> None:
        if not self._buffer:
            return
        data, self._buffer = self._buffer, b""
        await asyncio.to_thread(self._write, data)

    def _write(self, data: bytes) -> None:
        written = wintypes.DWORD()
        kernel32.WriteFile(self._handle, data, len(data), ctypes.byref(written), None)

    def close(self) -> None:
        if self._handle:
            kernel32.CloseHandle(self._handle)
            self._handle = 0


class Process:
    """与 `asyncio.subprocess.Process` 相近的最小接口，够 shell 与 codemode 用。"""

    def __init__(self, handle, job, pid: int, stdin, stdout, stderr) -> None:
        self._handle = handle
        self._job = job
        self._keepalive: list[object] = []
        self.pid = pid
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.returncode: int | None = None

    async def wait(self) -> int:
        if self.returncode is None:
            await asyncio.to_thread(
                kernel32.WaitForSingleObject, self._handle, 0xFFFFFFFF
            )
            code = wintypes.DWORD()
            kernel32.GetExitCodeProcess(self._handle, ctypes.byref(code))
            self.returncode = code.value
            self._release()
        return self.returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        assert self.stdout is not None
        out = await self.stdout.read()
        err = b""
        if self.stderr is not None:
            err = await self.stderr.read()
        await self.wait()
        return out, err

    def kill(self) -> None:
        if self._handle:
            kernel32.TerminateProcess(self._handle, 1)
        self._close_pipes()

    def _release(self) -> None:
        self._close_pipes()
        if self._job:
            kernel32.CloseHandle(self._job)
            self._job = 0
        if self._handle:
            kernel32.CloseHandle(self._handle)
            self._handle = 0

    def _close_pipes(self) -> None:
        for pipe in (self.stdin, self.stdout, self.stderr):
            if pipe is not None:
                pipe.close()


def create_pipes():
    sa = SECURITY_ATTRIBUTES()
    sa.nLength = ctypes.sizeof(sa)
    sa.bInheritHandle = True

    def pair() -> tuple[int, int] | None:
        read = wintypes.HANDLE()
        write = wintypes.HANDLE()
        if not kernel32.CreatePipe(
            ctypes.byref(read), ctypes.byref(write), ctypes.byref(sa), 0
        ):
            return None
        return read.value, write.value

    stdin_pair = pair()
    stdout_pair = pair()
    stderr_pair = pair()
    if stdin_pair is None or stdout_pair is None or stderr_pair is None:
        return None

    stdin_read, stdin_write = stdin_pair
    stdout_read, stdout_write = stdout_pair
    stderr_read, stderr_write = stderr_pair
    kernel32.SetHandleInformation(stdin_write, HANDLE_FLAG_INHERIT, 0)
    kernel32.SetHandleInformation(stdout_read, HANDLE_FLAG_INHERIT, 0)
    kernel32.SetHandleInformation(stderr_read, HANDLE_FLAG_INHERIT, 0)
    return (
        stdin_read,
        stdin_write,
        stdout_read,
        stdout_write,
        stderr_read,
        stderr_write,
    )


def close_pipes(handles: Sequence[int]) -> None:
    for handle in handles:
        if handle:
            kernel32.CloseHandle(handle)


def environment_block(scratch: Path) -> ctypes.c_void_p:
    """只给子进程最小环境；宿主里的 key 一律不外传。

    `TEMP` / `TMP` 指到 scratch：受限写只放行副本与 scratch，工具往
    `%TEMP%` 写会失败，所以要把它们引到能写的地方。
    """
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    values = {
        "SystemRoot": system_root,
        "SystemDrive": os.environ.get("SystemDrive", "C:"),
        "windir": system_root,
        "PATH": f"{system_root}\\System32;{system_root};{system_root}\\System32\\WindowsPowerShell\\v1.0",
        "PATHEXT": os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD"),
        "COMSPEC": f"{system_root}\\System32\\cmd.exe",
        "TEMP": str(scratch),
        "TMP": str(scratch),
        "APPDATA": str(scratch),
        "LOCALAPPDATA": str(scratch),
        "USERPROFILE": str(scratch),
        "HOMEDRIVE": Path(scratch).drive or "C:",
        "HOMEPATH": str(scratch)[2:] if Path(scratch).drive else str(scratch),
    }
    raw = "\0".join(f"{key}={value}" for key, value in values.items()) + "\0\0"
    encoded = raw.encode("utf-16-le")
    return ctypes.create_string_buffer(encoded, len(encoded))


def assign_job(process_handle: int) -> int:
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return 0
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    kernel32.SetInformationJobObject(
        job,
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not kernel32.AssignProcessToJobObject(job, process_handle):
        kernel32.CloseHandle(job)
        return 0
    return job


def quote_argv(argv: Sequence[str]) -> str:
    """按 Windows 命令行规则拼 argv，处理空格、引号与反斜杠。"""
    return " ".join(_quote_arg(item) for item in argv)


def _quote_arg(arg: str) -> str:
    if not arg:
        return '""'
    if not re.search(r'[ \t\n\v"]', arg):
        return arg

    parts = ['"']
    backslashes = 0
    for char in arg:
        if char == "\\":
            backslashes += 1
            continue
        if char == '"':
            parts.append("\\" * (backslashes * 2 + 1))
            parts.append('"')
        else:
            parts.append("\\" * backslashes)
            parts.append(char)
        backslashes = 0
    parts.append("\\" * (backslashes * 2))
    parts.append('"')
    return "".join(parts)


def state_dirs() -> list[Path]:
    """状态目录：优先 `%LOCALAPPDATA%\\gradus`，写不进去就退到临时目录。"""
    local = os.environ.get("LOCALAPPDATA")
    directories = [Path(local) / "gradus"] if local else []
    directories.append(Path(tempfile.gettempdir()) / "gradus")
    return directories


async def icacls(argv: list[str]) -> int:
    try:
        process = await asyncio.create_subprocess_exec(
            "icacls",
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return 1
    try:
        return await asyncio.wait_for(process.wait(), timeout=ICACLS_TIMEOUT)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return 1


async def has_ace(target: str, sid: str) -> bool:
    """读一次访问控制列表，判断这个 SID 上是否已有 ACE（只读，不触发传播）。"""
    try:
        process = await asyncio.create_subprocess_exec(
            "icacls",
            target,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return False
    out, _ = await asyncio.wait_for(process.communicate(), timeout=ICACLS_TIMEOUT)
    return sid.encode() in out
