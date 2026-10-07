"""Native Windows process trees with bounded lifetime and explicit handles.

Job containment is not a permissions sandbox. ``windows_sandbox`` supplies
AppContainer security capabilities for untrusted commands. Trusted commands
may use this launcher without capabilities.

Win32 contracts:
https://learn.microsoft.com/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute
https://learn.microsoft.com/windows/win32/api/jobapi2/nf-jobapi2-setinformationjobobject
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Any, BinaryIO, Mapping, Sequence


DWORD = ctypes.c_uint32
HANDLE = ctypes.c_void_p
SIZE_T = ctypes.c_size_t
BOOL = ctypes.c_int
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("length", DWORD), ("descriptor", ctypes.c_void_p), ("inherit", BOOL)]


class STARTUPINFO(ctypes.Structure):
    _fields_ = [("cb", DWORD), ("reserved", ctypes.c_wchar_p),
                ("desktop", ctypes.c_wchar_p), ("title", ctypes.c_wchar_p)]
    _fields_ += [(name, DWORD) for name in (
        "x", "y", "xsize", "ysize", "xchars", "ychars", "fill", "flags")]
    _fields_ += [("show", ctypes.c_uint16), ("reserved_size", ctypes.c_uint16),
                ("reserved_data", ctypes.c_void_p), ("stdin", HANDLE),
                ("stdout", HANDLE), ("stderr", HANDLE)]


class STARTUPINFOEX(ctypes.Structure):
    _fields_ = [("startup", STARTUPINFO), ("attributes", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("process", HANDLE), ("thread", HANDLE), ("pid", DWORD), ("tid", DWORD)]


class BASIC_LIMITS(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                ("flags", DWORD), ("minimum_ws", SIZE_T), ("maximum_ws", SIZE_T),
                ("active_limit", DWORD), ("affinity", SIZE_T),
                ("priority", DWORD), ("scheduling", DWORD)]


class EXTENDED_LIMITS(ctypes.Structure):
    _fields_ = [("basic", BASIC_LIMITS), ("io", ctypes.c_uint64 * 6),
                ("process_memory", SIZE_T), ("job_memory", SIZE_T),
                ("peak_process_memory", SIZE_T), ("peak_job_memory", SIZE_T)]


class ACCOUNTING(ctypes.Structure):
    _fields_ = [("times", ctypes.c_int64 * 4), ("faults", DWORD),
                ("total", DWORD), ("active", DWORD), ("terminated", DWORD)]


def require_windows() -> None:
    if os.name != "nt":
        raise RuntimeError("native Windows execution requires Windows 10 or later")


def win_error(operation: str, code: int | None = None) -> OSError:
    code = ctypes.get_last_error() if code is None else code
    error = ctypes.WinError(code)
    error.strerror = f"{operation}: {error.strerror}"
    return error


def kernel32():
    require_windows()
    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    signatures = {
        "CreateJobObjectW": ([pointer, ctypes.c_wchar_p], HANDLE),
        "SetInformationJobObject": ([HANDLE, ctypes.c_int, pointer, DWORD], BOOL),
        "QueryInformationJobObject": ([HANDLE, ctypes.c_int, pointer, DWORD, pointer], BOOL),
        "TerminateJobObject": ([HANDLE, ctypes.c_uint], BOOL),
        "CloseHandle": ([HANDLE], BOOL),
        "CreatePipe": ([pointer, pointer, pointer, DWORD], BOOL),
        "SetHandleInformation": ([HANDLE, DWORD, DWORD], BOOL),
        "GetCurrentProcess": ([], HANDLE),
        "DuplicateHandle": ([HANDLE, HANDLE, HANDLE, pointer, DWORD, BOOL, DWORD], BOOL),
        "InitializeProcThreadAttributeList": ([pointer, DWORD, DWORD, pointer], BOOL),
        "UpdateProcThreadAttribute": ([pointer, DWORD, SIZE_T, pointer, SIZE_T, pointer, pointer], BOOL),
        "DeleteProcThreadAttributeList": ([pointer], None),
        "CreateProcessW": ([ctypes.c_wchar_p, ctypes.c_wchar_p, pointer, pointer,
                            BOOL, DWORD, pointer, ctypes.c_wchar_p, pointer, pointer], BOOL),
        "ResumeThread": ([HANDLE], DWORD),
        "TerminateProcess": ([HANDLE, ctypes.c_uint], BOOL),
        "WaitForSingleObject": ([HANDLE, DWORD], DWORD),
        "GetExitCodeProcess": ([HANDLE, pointer], BOOL),
        "GetProcessTimes": ([HANDLE, pointer, pointer, pointer, pointer], BOOL),
        "OpenProcess": ([DWORD, BOOL, DWORD], HANDLE),
        "IsProcessInJob": ([HANDLE, HANDLE, pointer], BOOL),
        "CreateFileW": ([ctypes.c_wchar_p, DWORD, DWORD, pointer, DWORD, DWORD, HANDLE], HANDLE),
        "GetFileInformationByHandle": ([HANDLE, pointer], BOOL),
        "LockFileEx": ([HANDLE, DWORD, DWORD, DWORD, DWORD, pointer], BOOL),
        "UnlockFileEx": ([HANDLE, DWORD, DWORD, DWORD, pointer], BOOL),
        "LocalFree": ([pointer], pointer),
        "GetFileAttributesW": ([ctypes.c_wchar_p], DWORD),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(dll, name)
        function.argtypes, function.restype = arguments, result
    return dll


def check(value: Any, operation: str) -> Any:
    if not value:
        raise win_error(operation)
    return value


def handle_birth(api, process: Any) -> str:
    creation, exit_time, kernel, user = (ctypes.c_uint64() for _ in range(4))
    check(api.GetProcessTimes(process, ctypes.byref(creation), ctypes.byref(exit_time),
                              ctypes.byref(kernel), ctypes.byref(user)), "GetProcessTimes")
    return "windows:" + str(creation.value)


def process_birth(pid: int) -> str | None:
    api = kernel32()
    process = api.OpenProcess(0x1000 | 0x100000, False, pid)  # query + SYNCHRONIZE
    if not process:
        return None
    try:
        if api.WaitForSingleObject(process, 0) == 0:
            return None
        return handle_birth(api, process)
    finally:
        api.CloseHandle(process)


def process_identity_state(pid: int, birth: str) -> bool | None:
    """Read-only identity check; access denial is unknown, not process death."""
    api = kernel32()
    process = api.OpenProcess(0x1000 | 0x100000, False, pid)
    if not process:
        return False if ctypes.get_last_error() == 87 else None
    try:
        outcome = api.WaitForSingleObject(process, 0)
        if outcome == 0:
            return False
        if outcome != 258:
            return None
        return handle_birth(api, process) == birth
    except OSError:
        return None
    finally:
        api.CloseHandle(process)


class WindowsProcess:
    """A process and its unnamed, non-inheritable kill-on-close Job.

Consumers must call ``close`` even after the leader exits: descendants may
still hold stdout/stderr or continue working. Close verifies zero active Job
members before reporting cleanup. Numeric PIDs are never used for signalling.
"""
    def __init__(self, api, job: Any, info: PROCESS_INFORMATION,
                 stdout: BinaryIO, stderr: BinaryIO, args: Sequence[str]):
        self._api, self._job, self._handle = api, job, info.process
        self.pid, self.args = int(info.pid), list(args)
        self.stdout, self.stderr = stdout, stderr
        self.stdin = None
        self.returncode: int | None = None
        self.birth = handle_birth(api, self._handle)
        self.cleanup_confirmed = False
        self._lock = threading.RLock()

    def poll(self) -> int | None:
        with self._lock:
            if self.returncode is not None:
                return self.returncode
            if self._api.WaitForSingleObject(self._handle, 0) == 258:
                return None
            code = DWORD()
            check(self._api.GetExitCodeProcess(self._handle, ctypes.byref(code)), "GetExitCodeProcess")
            self.returncode = int(code.value)
            return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        milliseconds = 0xFFFFFFFF if timeout is None else min(0xFFFFFFFE, max(0, int(timeout * 1000)))
        result = self._api.WaitForSingleObject(self._handle, milliseconds)
        if result == 258:
            raise subprocess.TimeoutExpired(self.args, timeout)
        if result != 0:
            raise win_error("WaitForSingleObject")
        return self.poll()

    def terminate_tree(self, timeout: float = 5.0) -> None:
        with self._lock:
            if not self._job:
                if not self.cleanup_confirmed:
                    raise RuntimeError("Windows Job cleanup is unconfirmed")
                return
            check(self._api.TerminateJobObject(self._job, 1), "TerminateJobObject")
            deadline = time.monotonic() + timeout
            while True:
                accounting = ACCOUNTING()
                check(self._api.QueryInformationJobObject(self._job, 1, ctypes.byref(accounting),
                                                          ctypes.sizeof(accounting), None),
                      "QueryInformationJobObject")
                if accounting.active == 0:
                    self.cleanup_confirmed = True
                    return
                if time.monotonic() >= deadline:
                    raise TimeoutError("Windows Job descendants did not exit within cleanup reserve")
                time.sleep(0.01)

    kill = terminate_tree
    terminate = terminate_tree

    def close(self) -> None:
        with self._lock:
            try:
                self.terminate_tree()
                if self._handle:
                    self.wait(timeout=5)
            finally:
                for stream in (self.stdout, self.stderr):
                    if not stream.closed:
                        stream.close()
                for name in ("_job", "_handle"):
                    handle = getattr(self, name)
                    if handle:
                        self._api.CloseHandle(handle)
                        setattr(self, name, None)


def launch(argv: Sequence[str], *, cwd: Path | str, environment: Mapping[str, str],
           stdin: BinaryIO, security_capabilities: ctypes.Structure | None = None,
           memory_limit_bytes: int | None = None) -> WindowsProcess:
    """Create suspended with Job and optional AppContainer attributes atomically.

Only the three deliberately duplicated standard handles are inherited. An
incompatible outer Job or failed security attribute is an error, never a
reason to retry without containment.
"""
    api = kernel32()
    import msvcrt
    arguments = [os.fspath(value) for value in argv]
    if not arguments or not Path(arguments[0]).is_absolute():
        raise ValueError("Windows launcher requires an absolute executable path")
    if any("\x00" in value for value in arguments):
        raise ValueError("NUL in process arguments")
    entries = []
    seen = set()
    for key, value in sorted(environment.items(), key=lambda item: item[0].upper()):
        if (not key or "=" in key or "\x00" in key or "\x00" in value
                or key.upper() in seen):
            raise ValueError("invalid or duplicate Windows environment key")
        seen.add(key.upper())
        entries.append(key + "=" + value)
    environment_block = ctypes.create_unicode_buffer("\x00".join(entries) + "\x00\x00")
    job = check(api.CreateJobObjectW(None, None), "CreateJobObjectW")
    info = PROCESS_INFORMATION()
    handles: list[Any] = []
    streams: list[BinaryIO] = []
    attributes = None
    initialized = False
    try:
        limits = EXTENDED_LIMITS()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if memory_limit_bytes is not None:
            if type(memory_limit_bytes) is not int or memory_limit_bytes <= 0:
                raise ValueError("memory limit must be positive")
            limits.basic.flags |= 0x200  # JOB_OBJECT_LIMIT_JOB_MEMORY
            limits.job_memory = memory_limit_bytes
        check(api.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)),
              "SetInformationJobObject")
        child_pipes = []
        attributes_sa = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), None, True)
        for _ in range(2):
            reader, writer = HANDLE(), HANDLE()
            check(api.CreatePipe(ctypes.byref(reader), ctypes.byref(writer),
                                  ctypes.byref(attributes_sa), 0), "CreatePipe")
            handles.extend([reader.value, writer.value])
            check(api.SetHandleInformation(reader, 1, 0), "SetHandleInformation")
            fd = msvcrt.open_osfhandle(reader.value, os.O_RDONLY | os.O_BINARY)
            handles.remove(reader.value)  # FileIO now owns this handle.
            streams.append(os.fdopen(fd, "rb", buffering=0))
            child_pipes.append(writer.value)
        stdin_handle = HANDLE()
        current = api.GetCurrentProcess()
        check(api.DuplicateHandle(current, msvcrt.get_osfhandle(stdin.fileno()), current,
                                   ctypes.byref(stdin_handle), 0, True, 2), "DuplicateHandle(stdin)")
        handles.append(stdin_handle.value)
        inherited = (HANDLE * 3)(stdin_handle.value, *child_pipes)
        jobs = (HANDLE * 1)(job)
        count = 3 if security_capabilities is not None else 2
        size = SIZE_T()
        api.InitializeProcThreadAttributeList(None, count, 0, ctypes.byref(size))
        if not size.value:
            raise win_error("InitializeProcThreadAttributeList(size)")
        attributes = ctypes.create_string_buffer(size.value)
        check(api.InitializeProcThreadAttributeList(attributes, count, 0, ctypes.byref(size)),
              "InitializeProcThreadAttributeList")
        initialized = True
        for attribute, value in ((0x2000D, jobs), (0x20002, inherited)):
            check(api.UpdateProcThreadAttribute(attributes, 0, attribute, ctypes.byref(value),
                                               ctypes.sizeof(value), None, None),
                  "UpdateProcThreadAttribute(Job/handles)")
        if security_capabilities is not None:
            check(api.UpdateProcThreadAttribute(attributes, 0, 0x20009,
                                               ctypes.byref(security_capabilities),
                                               ctypes.sizeof(security_capabilities), None, None),
                  "UpdateProcThreadAttribute(AppContainer)")
        startup = STARTUPINFOEX()
        startup.startup.cb = ctypes.sizeof(startup)
        startup.startup.flags = 0x100  # STARTF_USESTDHANDLES
        startup.startup.stdin = stdin_handle
        startup.startup.stdout, startup.startup.stderr = child_pipes
        startup.attributes = ctypes.cast(attributes, ctypes.c_void_p)
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline(arguments))
        flags = 0x4 | 0x80000 | 0x400 | 0x08000000  # suspended, extended, Unicode, no console
        check(api.CreateProcessW(arguments[0], command, None, None, True, flags,
                                  environment_block, os.fspath(cwd), ctypes.byref(startup),
                                  ctypes.byref(info)), "CreateProcessW(Job/AppContainer)")
        assigned = BOOL()
        check(api.IsProcessInJob(info.process, job, ctypes.byref(assigned)), "IsProcessInJob")
        if not assigned.value:
            raise RuntimeError("child is outside its containment Job")
        process = WindowsProcess(api, job, info, *streams, arguments)
        if api.ResumeThread(info.thread) == 0xFFFFFFFF:
            raise win_error("ResumeThread")
        job = None  # WindowsProcess owns these resources after resume.
        info.process = None
        streams = []
        return process
    except BaseException:
        if job:
            api.TerminateJobObject(job, 70)
        if info.process:
            api.TerminateProcess(info.process, 70)
            api.WaitForSingleObject(info.process, 5000)
        raise
    finally:
        if initialized:
            api.DeleteProcThreadAttributeList(attributes)
        for stream in streams:
            stream.close()
        for handle in [*handles, info.thread, info.process, job]:
            if handle:
                api.CloseHandle(handle)
