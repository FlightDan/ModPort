"""Reusable host process, memory and isolated-import platform primitives.

Trusted process containment and untrusted command permissions are separate.
Project code must use ``windows_sandbox`` or the Linux bubblewrap build gate.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, BinaryIO, Callable, Mapping, Sequence


@dataclass(frozen=True)
class ProcessCapture:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    stdout_bytes: int
    stderr_bytes: int
    timed_out: bool
    drain_incomplete: bool

    @property
    def stdout_truncated(self) -> bool:
        return self.stdout_bytes > len(self.stdout)

    @property
    def stderr_truncated(self) -> bool:
        return self.stderr_bytes > len(self.stderr)


def process_birth(pid: int) -> str | None:
    if type(pid) is not int or pid <= 0:
        return None
    if os.name == "nt":
        from .windows_process import process_birth as windows_birth
        return windows_birth(pid)
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return f"{boot}:{fields[19]}"
    except (OSError, IndexError):
        return None


def spawn_trusted(argv: Sequence[str], *, cwd: Path | str,
                  environment: Mapping[str, str] | None = None,
                  stdin: BinaryIO | None = None, pass_fds: Sequence[int] = (),
                  memory_limit_bytes: int | None = None):
    """Start host-owned code with killable descendants, never execute a shell implicitly."""
    environment = dict(os.environ if environment is None else environment)
    if os.name == "nt":
        if pass_fds:
            raise ValueError("Windows host commands cannot inherit arbitrary Unix descriptors")
        from .windows_process import launch
        arguments = list(argv)
        if not arguments:
            raise ValueError("trusted command requires an executable")
        executable = (str(Path(arguments[0])) if Path(arguments[0]).is_absolute()
                      else shutil.which(arguments[0], path=environment.get("PATH", os.defpath)))
        if executable is None:
            raise FileNotFoundError("trusted executable is unavailable: " + arguments[0])
        if Path(executable).suffix.lower() in {".cmd", ".bat"}:
            raise ValueError("trusted batch files require an explicit native shell invocation")
        arguments[0] = str(Path(executable).absolute())
        if stdin is not None:
            return launch(arguments, cwd=cwd, environment=environment, stdin=stdin,
                          memory_limit_bytes=memory_limit_bytes)
        with open(os.devnull, "rb") as input_file:
            return launch(arguments, cwd=cwd, environment=environment, stdin=input_file,
                          memory_limit_bytes=memory_limit_bytes)
    if memory_limit_bytes is not None:
        raise ValueError("POSIX trusted memory limits must be installed by the isolated child")
    return subprocess.Popen(list(argv), cwd=cwd, env=environment,
                            stdin=stdin if stdin is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True, pass_fds=tuple(pass_fds))


def terminate_tree(process, *, cleanup_seconds: float = 5.0) -> None:
    """Terminate the owned containment boundary, including an exited leader's children."""
    if os.name == "nt":
        process.terminate_tree(timeout=cleanup_seconds)
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=cleanup_seconds)


def capture_started_process(process, *, timeout: float | None,
                            max_output_bytes: int = 1024 * 1024,
                            on_chunk: Callable[[str, bytes], None] | None = None,
                            cleanup_seconds: float = 5.0) -> ProcessCapture:
    """Drain both pipes concurrently with bounded memory and optional caller deadline.

The callback receives the full stream. The result retains only each stream's
bounded tail. Pipe reads use threads because Windows selectors do not support
anonymous process pipes. A full queue applies backpressure rather than growing.
"""
    if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("capture timeout must be positive and finite when provided")
    if type(max_output_bytes) is not int or max_output_bytes < 0:
        raise ValueError("output limit must be a nonnegative integer")
    events: queue.Queue[tuple[str, bytes | BaseException | None]] = queue.Queue(maxsize=16)
    stop = threading.Event()

    def offer(value):
        while not stop.is_set():
            try:
                events.put(value, timeout=0.05)
                return
            except queue.Full:
                pass

    def read_pipe(name, pipe):
        try:
            while not stop.is_set():
                data = os.read(pipe.fileno(), 65536)
                if not data:
                    break
                offer((name, data))
        except BaseException as exc:
            if not stop.is_set():
                offer((name, exc))
        finally:
            offer((name, None))

    threads = [threading.Thread(target=read_pipe, args=(name, pipe), daemon=True,
                                name="modport-pipe-" + name)
               for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr))]
    tails = {"stdout": bytearray(), "stderr": bytearray()}
    sizes = {"stdout": 0, "stderr": 0}
    finished = set()
    deadline = math.inf if timeout is None else time.monotonic() + timeout
    timed_out = False
    drain_incomplete = False
    killed = False
    for thread in threads:
        thread.start()
    try:
        while len(finished) < 2 or process.poll() is None:
            now = time.monotonic()
            if not killed and (now >= deadline or process.poll() is not None):
                timed_out = now >= deadline and process.poll() is None
                terminate_tree(process, cleanup_seconds=cleanup_seconds)
                killed = True
                deadline = time.monotonic() + cleanup_seconds
            if killed and now >= deadline:
                drain_incomplete = len(finished) < 2
                break
            try:
                name, data = events.get(timeout=min(0.05, max(0.001, deadline - now)))
            except queue.Empty:
                continue
            if data is None:
                finished.add(name)
                continue
            if isinstance(data, BaseException):
                raise data
            sizes[name] += len(data)
            if on_chunk is not None:
                on_chunk(name, data)
            if max_output_bytes:
                tails[name].extend(data)
                if len(tails[name]) > max_output_bytes:
                    del tails[name][:-max_output_bytes]
        returncode = process.poll()
        return ProcessCapture(returncode, bytes(tails["stdout"]), bytes(tails["stderr"]),
                              sizes["stdout"], sizes["stderr"], timed_out, drain_incomplete)
    finally:
        stop.set()
        try:
            terminate_tree(process, cleanup_seconds=cleanup_seconds)
        finally:
            # On Windows close also verifies Job quiescence and closes handles.
            if os.name == "nt":
                process.close()
            else:
                for pipe in (process.stdout, process.stderr):
                    pipe.close()
            for thread in threads:
                thread.join(timeout=0.1)


def capture_process(argv: Sequence[str], *, cwd: Path | str,
                    timeout: float | None, environment: Mapping[str, str] | None = None,
                    input_bytes: bytes | None = None, pass_fds: Sequence[int] = (),
                    max_output_bytes: int = 1024 * 1024,
                    on_chunk: Callable[[str, bytes], None] | None = None,
                    memory_limit_bytes: int | None = None) -> ProcessCapture:
    # File-backed stdin avoids a pipe deadlock with large prompts and early output.
    with tempfile.TemporaryFile() as input_file:
        if input_bytes:
            input_file.write(input_bytes)
            input_file.seek(0)
        process = spawn_trusted(argv, cwd=cwd, environment=environment,
                                stdin=input_file, pass_fds=pass_fds,
                                memory_limit_bytes=memory_limit_bytes)
        return capture_started_process(process, timeout=timeout,
                                       max_output_bytes=max_output_bytes, on_chunk=on_chunk)


def windows_memory_snapshot() -> tuple[int | None, int | None, str]:
    """Observe physical memory constrained by the caller's Windows Job.

Unknown Job inspection returns unavailable metrics; it never admits heavy
work by treating a missing constraint as unlimited. Values exclude swap.
"""
    from .windows_process import kernel32, check, DWORD, SIZE_T, EXTENDED_LIMITS, BOOL
    import ctypes
    api = kernel32()

    class MEMORYSTATUS(ctypes.Structure):
        _fields_ = [("length", DWORD), ("load", DWORD)]
        _fields_ += [(name, ctypes.c_uint64) for name in (
            "total_physical", "available_physical", "total_page", "available_page",
            "total_virtual", "available_virtual", "extended_virtual")]

    api.GlobalMemoryStatusEx.argtypes = [ctypes.c_void_p]
    api.GlobalMemoryStatusEx.restype = BOOL
    status = MEMORYSTATUS()
    status.length = ctypes.sizeof(status)
    check(api.GlobalMemoryStatusEx(ctypes.byref(status)), "GlobalMemoryStatusEx")
    in_job = BOOL()
    check(api.IsProcessInJob(api.GetCurrentProcess(), None, ctypes.byref(in_job)), "IsProcessInJob")
    available, ceiling = int(status.available_physical), int(status.total_physical)
    if in_job.value:
        limits = EXTENDED_LIMITS()
        if not api.QueryInformationJobObject(None, 9, ctypes.byref(limits), ctypes.sizeof(limits), None):
            return None, None, "windows_job_metrics_unavailable"
        if limits.basic.flags & (0x100 | 0x200):
            # Nested/outer Job usage cannot be reconstructed from peak counters.
            # Admission must not subtract a peak as though it were current usage.
            return None, None, "windows_job_memory_usage_unavailable"
    return available, ceiling, "windows_physical_memory"


def require_windows_job_memory_limit(maximum_bytes: int) -> int:
    """Verify the isolated child's actual Job memory ceiling before reading data."""
    from .windows_process import kernel32, check, EXTENDED_LIMITS, BOOL
    import ctypes
    api = kernel32()
    in_job = BOOL()
    check(api.IsProcessInJob(api.GetCurrentProcess(), None, ctypes.byref(in_job)), "IsProcessInJob")
    if not in_job.value:
        raise RuntimeError("isolated reader requires an enforced Windows Job memory ceiling")
    limits = EXTENDED_LIMITS()
    check(api.QueryInformationJobObject(None, 9, ctypes.byref(limits), ctypes.sizeof(limits), None),
          "QueryInformationJobObject(memory ceiling)")
    if not limits.basic.flags & 0x200 or not 0 < limits.job_memory <= maximum_bytes:
        raise RuntimeError("isolated reader Windows Job memory ceiling is absent or too large")
    return int(limits.job_memory)


def trusted_mcp_argv(module: str, source_root: Path | str, session: Path | str) -> list[str]:
    if module not in {"modport.opencode_shell_mcp", "modport.rework_mcp"}:
        raise ValueError("unsupported trusted MCP module")
    source_root = Path(source_root).absolute()
    code = ("import sys; " + f"sys.path.insert(0, {str(source_root)!r}); " +
            f"from {module} import main; raise SystemExit(main())")
    return [sys.executable, "-I", "-c", code, "--session", os.fspath(session)]


def trusted_mcp_environment() -> dict[str, str]:
    """Explicit transport environment; never inherit credential-bearing variables."""
    environment = {"PATH": os.defpath}
    if os.name == "nt":
        for name in ("SystemRoot", "WINDIR"):
            if name in os.environ:
                environment[name] = os.environ[name]
    return environment
