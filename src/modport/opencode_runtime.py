"""Managed HTTP client for the OpenCode 1.18.32 local server API.

The server is bound to loopback and owns only OpenCode HTTP sessions. Goal
ownership, budgets, recovery, and host acceptance remain in ModPort.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from contextvars import copy_context
import base64
import copy
from hashlib import sha256
from http.client import IncompleteRead
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import threading
import time
from typing import Any, Iterator, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .platform_files import safe_open
from .evidence import current_lock_fds
from .process_diagnostics import (
    diagnose_process_exit,
    read_memory_events,
    read_process_memory_events,
)
from .telemetry import (OpenCodeActivity, report_sdk_bytes, report_sdk_model,
                        _sdk_activity_call)
from .execution_budget import current_sdk_context


def _windows_host() -> bool:
    return os.name == "nt"


EXPECTED_OPENCODE_VERSION = "1.18.32"
PROCESS_GROUP_EXIT_WAIT_SECONDS = 5.0
_CONFIG_SCHEMA = "https://opencode.ai/config.json"
_SHELL_TOOL_NAMES = ("bash", "shell", "terminal")
_SENSITIVE_PROVIDER_KEYS = {
    "api_key", "apikey", "key", "token", "secret", "authorization", "headers",
}


class OpenCodeError(RuntimeError):
    """Base exception for managed OpenCode server failures."""


class OpenCodeCleanupError(OpenCodeError):
    """Owned server process-tree cleanup could not be confirmed."""

    def __init__(self, diagnostic: Mapping[str, Any]):
        self.cleanup_diagnostic = dict(diagnostic)
        super().__init__("OpenCode process cleanup is unconfirmed")


class OpenCodeEventConnectTimeout(OpenCodeError):
    """An SSE response header timed out before the assignment deadline."""


class OpenCodeHTTPError(OpenCodeError):
    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"OpenCode HTTP {status}: {body[:500]}")


class OpenCodeResponseError(OpenCodeError):
    """A successful HTTP response that contains an assistant-side error."""

    def __init__(self, error: Mapping[str, Any], response: Mapping[str, Any]):
        self.error = dict(error)
        self.response = dict(response)
        name = self.error.get("name", "OpenCodeError")
        data = self.error.get("data", {})
        message = data.get("message") if isinstance(data, dict) else None
        super().__init__(f"OpenCode response {name}: {message or 'request failed'}")


class OpenCodeEventStop:
    """Cancellation handle that wakes a thread blocked on one SSE socket."""

    def __init__(self):
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._socket: socket.socket | None = None

    def is_set(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)

    def set(self) -> None:
        self._event.set()
        with self._lock:
            connection = self._socket
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RD)
            except OSError:
                pass

    def attach(self, connection: socket.socket) -> None:
        with self._lock:
            self._socket = connection
        if self.is_set():
            try:
                connection.shutdown(socket.SHUT_RD)
            except OSError:
                pass

    def detach(self, connection: socket.socket) -> None:
        with self._lock:
            if self._socket is connection:
                self._socket = None


def _deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(dict(base))
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


@dataclass(frozen=True)
class OpenCodeConfig:
    """JSON-native OpenCode overrides for one managed server/workspace.

    ``mcp`` is the v1.18 server map, for example
    ``{"rework": {"type": "local", "command": ["python", "-m", "tool"]}}``.
    ``permission`` is OpenCode's application permission configuration. It does
    not provide operating-system sandboxing.
    """

    mcp: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    permission: Mapping[str, Any] | None = None
    provider: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    model: str | None = None
    small_model: str | None = None
    agent: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    default_agent: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result = copy.deepcopy(dict(self.extra))
        if self.mcp:
            result["mcp"] = _deep_merge(result.get("mcp", {}), self.mcp)
        if self.permission is not None:
            result["permission"] = _deep_merge(
                result.get("permission", {}), self.permission)
        if self.provider:
            result["provider"] = _deep_merge(result.get("provider", {}), self.provider)
        if self.agent:
            result["agent"] = _deep_merge(result.get("agent", {}), self.agent)
        if self.model is not None:
            result["model"] = self.model
        if self.small_model is not None:
            result["small_model"] = self.small_model
        if self.default_agent is not None:
            result["default_agent"] = self.default_agent
        result.setdefault("$schema", _CONFIG_SCHEMA)
        # OpenCode has no OS sandbox. Do not let a caller accidentally expose
        # its built-in shell to model-authenticated processes by default.
        tools = result.get("tools", {})
        if not isinstance(tools, Mapping):
            tools = {}
        result["tools"] = {**tools, **{name: False for name in _SHELL_TOOL_NAMES}}
        return result


def _as_config(config: OpenCodeConfig | Mapping[str, Any] | None) -> OpenCodeConfig:
    if config is None:
        return OpenCodeConfig()
    if isinstance(config, OpenCodeConfig):
        return config
    if not isinstance(config, Mapping):
        raise TypeError("OpenCode config must be an OpenCodeConfig or mapping")
    known = {"mcp", "permission", "provider", "model", "small_model",
             "agent", "default_agent", "extra"}
    values = dict(config)
    unknown = set(values) - known
    extra = values.pop("extra", {})
    if not isinstance(extra, Mapping):
        raise TypeError("OpenCode config extra must be a mapping")
    values["extra"] = _deep_merge(extra, {key: values[key] for key in unknown})
    for key in unknown:
        values.pop(key, None)
    return OpenCodeConfig(**values)


def resolve_model_id(model: str, provider_id: str = "openai") -> str:
    """Make a bare model ID explicit without substituting another model."""
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model ID must be a non-empty string")
    model = model.strip()
    if "/" in model:
        provider, _, model_part = model.partition("/")
        if not provider or not model_part:
            raise ValueError(f"invalid provider/model ID: {model!r}")
        return model
    if not isinstance(provider_id, str) or not provider_id.strip() or "/" in provider_id:
        raise ValueError("provider ID must be a non-empty ID without a slash")
    return f"{provider_id.strip()}/{model}"


def _model_ref(model: str, variant: str | None = None) -> dict[str, str]:
    resolved = resolve_model_id(model)
    provider_id, model_and_variant = resolved.split("/", 1)
    model_id, marker, embedded_variant = model_and_variant.partition("#")
    if not model_id:
        raise ValueError(f"invalid provider/model ID: {model!r}")
    if variant is not None and embedded_variant and variant != embedded_variant:
        raise ValueError("model ID and explicit variant disagree")
    selected_variant = variant or (embedded_variant if marker else None)
    result = {"providerID": provider_id, "modelID": model_id}
    if selected_variant:
        result["variant"] = selected_variant
    return result


def _redact_provider(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _redact_provider(item) for key, item in value.items()
                if key.lower().replace("-", "_") not in _SENSITIVE_PROVIDER_KEYS}
    if isinstance(value, list):
        return [_redact_provider(item) for item in value]
    return value


def _ensure_managed_directory(path: Path) -> Path:
    """Create a private XDG directory only when its full path is real."""
    candidate = Path(os.path.abspath(path))
    if _windows_host():
        from .platform_files import make_private_directory
        return make_private_directory(candidate)
    for ancestor in reversed((candidate, *candidate.parents)):
        try:
            details = os.lstat(ancestor)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(details.st_mode):
            raise OpenCodeError("managed OpenCode path contains a symlink")
        if not stat.S_ISDIR(details.st_mode):
            raise OpenCodeError("managed OpenCode path contains a non-directory")
    candidate.mkdir(mode=0o700, parents=True, exist_ok=True)
    if candidate.resolve(strict=True) != candidate:
        raise OpenCodeError("managed OpenCode path resolves outside its configured root")
    os.chmod(candidate, 0o700)
    return candidate


def _process_birth(pid: int) -> str | None:
    """Native birth identity prevents confusing a recycled PID with our child."""
    if _windows_host():
        from .platform_runtime import process_birth
        return process_birth(pid)
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return f"{boot}:{stat[19]}"
    except (OSError, IndexError):
        return None


def _proc_identity(pid: int) -> dict[str, Any] | None:
    """Read the process identity used by model-free cleanup reconciliation."""
    if _windows_host():
        return {"pid": pid, "birth": _process_birth(pid), "containment": "windows_job"}
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        namespace = os.readlink(f"/proc/{pid}/ns/pid")
        argv = [part.decode("utf-8", errors="strict") for part in
                Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if part]
        return {
            "pid": pid,
            "birth": f"{boot}:{fields[19]}",
            "start_ticks": int(fields[19]),
            "state": fields[0],
            "parent_pid": int(fields[1]),
            "process_group_id": int(fields[2]),
            "session_id": int(fields[3]),
            "pid_namespace": namespace,
            "cwd": os.readlink(f"/proc/{pid}/cwd"),
            "argv": argv,
            "executable": os.readlink(f"/proc/{pid}/exe").removesuffix(" (deleted)"),
        }
    except (OSError, UnicodeError, ValueError, IndexError):
        return None


def _process_group_members(pgid: int, *, proc_root: Path = Path("/proc"),
                           limit: int = 32, max_entries: int = 4096) -> dict[str, Any]:
    """Capture bounded PID/state evidence when group cleanup is uncertain."""
    members: list[dict[str, Any]] = []
    errors: list[str] = []
    truncated = False
    scan_limited = False
    scanned = 0
    try:
        entries = (entry for entry in proc_root.iterdir() if entry.name.isdecimal())
        for entry in entries:
            scanned += 1
            if scanned > max_entries:
                scan_limited = True
                break
            try:
                # The comm field may contain spaces or parentheses; fields
                # after its final ')' begin with state, PPID, and process group.
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                if int(fields[2]) != pgid:
                    continue
                if len(members) >= limit:
                    truncated = True
                    break
                members.append({
                    "pid": int(entry.name), "state": fields[0],
                    "ppid": int(fields[1]), "start_ticks": int(fields[19]),
                })
            except FileNotFoundError:
                continue
            except (OSError, ValueError, IndexError) as exc:
                if len(errors) < 8:
                    errors.append(type(exc).__name__)
    except OSError as exc:
        errors.append(type(exc).__name__)
    return {"members": sorted(members, key=lambda item: item["pid"]),
            "truncated": truncated, "scan_complete": not errors and not scan_limited,
            "scan_limited": scan_limited, "scanned_entries": min(scanned, max_entries),
            "scan_errors": errors}


def _zombie_only_group(observation: Mapping[str, Any] | None) -> bool:
    """A complete, nonempty zombie-only group has no executable producer."""
    if not isinstance(observation, Mapping) or observation.get("scan_complete") is not True:
        return False
    if observation.get("truncated") is not False:
        return False
    if observation.get("scan_limited") is not False or observation.get("scan_errors") != []:
        return False
    members = observation.get("members")
    return (isinstance(members, list) and bool(members)
            and all(isinstance(member, Mapping) and member.get("state") == "Z"
                    for member in members))


def _child_exited_without_reap(process: subprocess.Popen) -> bool:
    """Observe an owned child exit while retaining its PID for group cleanup."""
    if process.returncode is not None:
        return True
    if _windows_host():
        return process.poll() is not None  # The native process handle remains owned.
    try:
        return os.waitid(os.P_PID, process.pid,
                         os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
    except (AttributeError, ChildProcessError, OSError):
        return False


def _managed_environment(
    *, cwd: Path, config: OpenCodeConfig, env: Mapping[str, str] | None,
    xdg_root: Path,
) -> dict[str, str]:
    inherited = dict(os.environ)
    if env:
        inherited.update({str(key): str(value) for key, value in env.items()})

    # Only pass process essentials, the selected OpenAI provider credentials,
    # and explicit OpenCode installation/data locations.  In particular,
    # arbitrary cloud, Git, SSH, CI, and Codex secrets must not reach nested
    # model-facing MCP processes through environment inheritance.
    allowed = {
        "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL",
        "LC_CTYPE", "TERM", "TMPDIR", "TMP", "TEMP", "NO_COLOR",
        "XDG_DATA_HOME", "MODPORT_OPENCODE_BIN", "OPENCODE_BIN",
        "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID",
        "OPENAI_ORGANIZATION", "OPENAI_PROJECT_ID", "OPENAI_PROJECT",
        "BAILIAN_BASE_URL", "BAILIAN_API_KEY",
        "SystemRoot", "WINDIR", "USERPROFILE", "LOCALAPPDATA", "APPDATA",
    }
    from .desktop_model_settings import provider_environment_key
    allowed.update(key for key in inherited if provider_environment_key(key))
    if _windows_host():
        normalized = {key.upper(): value for key, value in inherited.items()}
        merged = {key: normalized[key.upper()] for key in allowed if key.upper() in normalized}
    else:
        merged = {key: inherited[key] for key in allowed if key in inherited}

    # Configuration is host-owned. Do not discover project or user MCP servers,
    # plugins, tools, agents, or commands. Preserve only OpenCode's login data
    # directory; Codex credentials are never read or copied.
    managed = xdg_root.resolve()
    config_home = managed / "config" / secrets.token_hex(8)
    cache_home = managed / "cache"
    state_home = managed / "state"
    data_dir = managed / "data"
    for path in (config_home, cache_home, state_home, data_dir):
        _ensure_managed_directory(path)
    config_home_mode = list(config_home.iterdir())
    if config_home_mode:
        raise OpenCodeError("managed OpenCode config directory is not empty")
    database_path = data_dir / "opencode.db"
    try:
        database_stat = os.lstat(database_path)
    except FileNotFoundError:
        database_stat = None
    if (database_stat is not None
            and (not stat.S_ISREG(database_stat.st_mode) or database_stat.st_nlink != 1)):
        raise OpenCodeError("managed OpenCode database path is unsafe")

    merged["XDG_CONFIG_HOME"] = str(config_home)
    merged["XDG_CACHE_HOME"] = str(cache_home)
    merged["XDG_STATE_HOME"] = str(state_home)
    merged["OPENCODE_DB"] = str(data_dir / "opencode.db")
    merged.pop("OPENCODE_CONFIG", None)
    merged.pop("OPENCODE_CONFIG_DIR", None)
    merged["OPENCODE_DISABLE_PROJECT_CONFIG"] = "true"
    merged["OPENCODE_CONFIG_CONTENT"] = json.dumps(
        config.to_dict(), ensure_ascii=False,
        separators=(",", ":"))

    # The data home remains the user's OpenCode login directory so existing
    # OpenCode OAuth/API credentials remain available and refreshed credentials
    # persist normally. Tests may explicitly pass XDG_DATA_HOME under /tmp.
    merged["OPENCODE_LOG_LEVEL"] = "ERROR"
    merged["OPENCODE_DISABLE_AUTOUPDATE"] = "true"
    merged["OPENCODE_SERVER_USERNAME"] = "modport"
    merged["OPENCODE_SERVER_PASSWORD"] = secrets.token_urlsafe(32)
    return merged


class OpenCodeServer:
    """A loopback-only, process-owned OpenCode server and HTTP client."""

    def __init__(self, *, process: subprocess.Popen, base_url: str,
                 env: Mapping[str, str], xdg_root: Path, version: str,
                 executable: Path, executable_sha256: str,
                 cwd: Path | None = None):
        self.process = process
        self.base_url = base_url.rstrip("/")
        self.env = dict(env)
        self.xdg_root = xdg_root
        self.version = version
        self.executable = str(executable)
        self.executable_sha256 = executable_sha256
        self.cwd = str(cwd.resolve()) if cwd is not None else None
        self.argv = list(process.args) if isinstance(process.args, (list, tuple)) else None
        self.stderr_path = str(xdg_root / "server.stderr.log")
        self._session_directories: dict[str, Path] = {}
        self._closed = False
        self.process_birth = getattr(process, "birth", None) or _process_birth(process.pid)
        self.process_identity = _proc_identity(process.pid)
        self.memory_before = None
        self.diagnostic_errors: list[dict[str, str]] = []
        try:
            self.memory_before = read_process_memory_events(process.pid)
        except Exception as exc:
            self.diagnostic_errors.append({
                "phase": "cgroup_before", "error": type(exc).__name__})
        self.process_diagnostic: dict[str, Any] | None = None

    def ownership_record(self) -> dict[str, Any]:
        """Return the nonsecret launch identity needed by later host cleanup."""
        identity = self.process_identity or {}
        argv = self.argv
        argv_bytes = json.dumps(argv, ensure_ascii=False, separators=(",", ":")).encode()
        return {
            "pid": self.process.pid,
            "birth": self.process_birth,
            "start_ticks": identity.get("start_ticks"),
            "pid_namespace": identity.get("pid_namespace"),
            "process_group_id": identity.get("process_group_id"),
            "session_id": identity.get("session_id"),
            "cwd": self.cwd,
            "argv": argv,
            "argv_sha256": sha256(argv_bytes).hexdigest(),
            "executable": self.executable,
            "executable_sha256": self.executable_sha256,
        }

    @classmethod
    def start(
        cls, *, cwd: Path, config: OpenCodeConfig | Mapping[str, Any] | None = None,
        env: Mapping[str, str] | None = None, xdg_root: Path | None = None,
        deadline: float | None = None, lock_fd: int | None = None,
        executable: str | None = None,
    ) -> "OpenCodeServer":
        cwd = Path(cwd).resolve()
        if not cwd.is_dir():
            raise FileNotFoundError(f"OpenCode workspace does not exist: {cwd}")
        selected_config = _as_config(config)
        runtime_root = (Path(xdg_root) if xdg_root is not None else
                        Path(tempfile.mkdtemp(prefix="modport-opencode-")))
        runtime_root = _ensure_managed_directory(runtime_root)
        child_env = _managed_environment(
            cwd=cwd, config=selected_config, env=env, xdg_root=runtime_root)
        command = (executable or child_env.get("MODPORT_OPENCODE_BIN")
                   or child_env.get("OPENCODE_BIN")
                   or shutil.which("opencode", path=child_env.get("PATH")))
        if not command:
            raise FileNotFoundError("OpenCode 1.18.32 executable was not found on PATH")
        executable_path = Path(command).resolve()
        try:
            executable_sha256 = sha256(executable_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise OpenCodeError("OpenCode executable cannot be read for identity") from exc

        port = _available_loopback_port()
        pass_fds = set(current_lock_fds())
        if lock_fd is not None:
            pass_fds.add(lock_fd)
        stderr_path = runtime_root / "server.stderr.log"
        stderr_fd = safe_open(runtime_root.resolve(), stderr_path.name,
                              os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        stderr_stat = os.fstat(stderr_fd)
        if not stat.S_ISREG(stderr_stat.st_mode) or stderr_stat.st_nlink != 1:
            os.close(stderr_fd)
            raise OpenCodeError("managed OpenCode stderr log path is unsafe")
        log_threads: list[threading.Thread] = []
        log_errors: list[dict[str, str]] = []
        server_log = None
        argv = [str(executable_path), "serve", "--hostname", "127.0.0.1", "--port", str(port),
                "--log-level", "ERROR"]
        if not _windows_host():
            os.fchmod(stderr_fd, 0o600)
        try:
            if _windows_host():
                from .windows_process import launch
                server_log = os.fdopen(stderr_fd, "ab")
                with open(os.devnull, "rb") as input_file:
                    process = launch(argv, cwd=cwd, environment=child_env, stdin=input_file)
                log_lock = threading.Lock()
                def drain(stream, name):
                    try:
                        while True:
                            chunk = getattr(stream, "read1", stream.read)(65536)
                            if not chunk:
                                break
                            report_sdk_bytes(name, chunk)
                            with log_lock:
                                server_log.write(chunk)
                                server_log.flush()
                    except Exception as exc:
                        log_errors.append({"phase": "log_" + name, "error": type(exc).__name__})
                for name in ("stdout", "stderr"):
                    _sdk_activity_call("enable_stream", name)
                    captured = copy_context()
                    worker = threading.Thread(target=captured.run,
                        args=(drain, getattr(process, name), name),
                        name="modport-opencode-" + name, daemon=True)
                    log_threads.append(worker)
                    worker.start()
            else:
                with os.fdopen(stderr_fd, "ab") as server_log:
                    process = subprocess.Popen(
                        argv, cwd=cwd, env=child_env, stdin=subprocess.DEVNULL,
                        stdout=server_log, stderr=server_log,
                        start_new_session=True, pass_fds=tuple(sorted(pass_fds)))
        except BaseException:
            if _windows_host():
                owned = locals().get("process")
                if owned is not None:
                    try:
                        owned.close()
                    except Exception as cleanup_error:
                        raise OpenCodeCleanupError({
                            "cleanup_confirmed": False, "classification": "unknown",
                            "containment": "windows_job", "target_pid": owned.pid,
                            "target_birth": getattr(owned, "birth", None),
                            "error_type": type(cleanup_error).__name__}) from cleanup_error
                if server_log is not None:
                    server_log.close()
            try:
                os.close(stderr_fd)
            except OSError:
                pass
            raise
        server = cls(process=process, base_url=f"http://127.0.0.1:{port}",
                     env=child_env, xdg_root=runtime_root.resolve(), version="unknown",
                     executable=executable_path, executable_sha256=executable_sha256,
                     cwd=cwd)
        server._log_threads = log_threads
        server._log_errors = log_errors
        server._server_log = server_log if _windows_host() else None
        end = deadline if deadline is not None else time.monotonic() + 30.0
        last_probe_error: str | None = None
        try:
            while time.monotonic() < end:
                if _child_exited_without_reap(process):
                    raise OpenCodeError(
                        "OpenCode server exited during startup; "
                        f"stderr log: {server.stderr_path}")
                probe_deadline = min(end, time.monotonic() + 1.0)
                try:
                    health = server._request(
                        "GET", "/global/health", deadline=probe_deadline)
                except (OpenCodeError, URLError, TimeoutError, ConnectionError) as exc:
                    last_probe_error = f"{type(exc).__name__}: {str(exc)[:200]}"
                    time.sleep(min(0.05, max(0, end - time.monotonic())))
                    continue
                version = health.get("version") if isinstance(health, dict) else None
                if version != EXPECTED_OPENCODE_VERSION:
                    raise OpenCodeError(
                        f"OpenCode server version {version!r} does not match "
                        f"required {EXPECTED_OPENCODE_VERSION}; stderr log: {server.stderr_path}")
                server.version = version
                return server
            raise TimeoutError(
                "OpenCode server startup deadline exhausted"
                f"; last health probe: {last_probe_error or 'no response'}"
                f"; stderr log: {server.stderr_path}")
        except BaseException as startup_error:
            try:
                cleanup = server.close(cleanup_reason="startup_failed")
            except Exception as close_error:
                cleanup = {"cleanup_confirmed": False, "classification": "unknown",
                           "target_pid": process.pid, "target_birth": server.process_birth,
                           "error_type": type(close_error).__name__}
            if not isinstance(cleanup, Mapping) or cleanup.get("cleanup_confirmed") is not True:
                raise OpenCodeCleanupError(cleanup if isinstance(cleanup, Mapping) else {
                    "cleanup_confirmed": False, "classification": "unknown",
                    "target_pid": process.pid, "target_birth": server.process_birth,
                }) from startup_error
            raise

    def __enter__(self) -> "OpenCodeServer":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        diagnostic = self.close(cleanup_reason="context_exit")
        if not isinstance(diagnostic, Mapping) or diagnostic.get("cleanup_confirmed") is not True:
            raise OpenCodeCleanupError(diagnostic if isinstance(diagnostic, Mapping) else {
                "cleanup_confirmed": False, "classification": "unknown",
                "target_pid": self.process.pid, "target_birth": self.process_birth,
            }) from exc

    def _deadline_timeout(self, deadline: float | None) -> float:
        if deadline is None:
            return 30.0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("OpenCode HTTP deadline exhausted")
        return max(0.001, remaining)

    def _auth_header(self) -> str:
        user = self.env.get("OPENCODE_SERVER_USERNAME", "modport")
        password = self.env.get("OPENCODE_SERVER_PASSWORD", "")
        token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
        return f"Basic {token}"

    def _request(
        self, method: str, path: str, *, query: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None, deadline: float | None = None,
        accept: str = "application/json",
    ) -> Any:
        suffix = f"?{urlencode(query)}" if query else ""
        url = f"{self.base_url}{path}{suffix}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Accept": accept, "Authorization": self._auth_header()}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self._deadline_timeout(deadline)) as response:
                payload = response.read()
        except HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise OpenCodeHTTPError(exc.code, error_body) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise TimeoutError("OpenCode HTTP deadline exhausted") from exc
        except IncompleteRead as exc:
            raise OpenCodeError(
                "OpenCode HTTP response body ended before completion; "
                "request outcome unknown"
            ) from exc
        except URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise TimeoutError("OpenCode HTTP deadline exhausted") from exc
            raise OpenCodeError(f"OpenCode server request failed: {exc.reason}") from exc
        if not payload:
            return None
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OpenCodeError("OpenCode server returned invalid JSON") from exc

    def _directory(self, cwd: Path | None) -> Path:
        if cwd is not None:
            return Path(cwd).resolve()
        if self._session_directories:
            directories = set(self._session_directories.values())
            if len(directories) == 1:
                return next(iter(directories))
        raise ValueError("workspace directory is required for this OpenCode request")

    def create_session(
        self, *, cwd: Path, title: str, model: str | None = None,
        variant: str | None = None, agent: str | None = None,
        permission: Any | None = None, deadline: float | None = None,
    ) -> dict[str, Any]:
        directory = Path(cwd).resolve()
        body: dict[str, Any] = {"title": title}
        if model is not None:
            reference = _model_ref(model, variant)
            body["model"] = {
                "id": reference["modelID"],
                "providerID": reference["providerID"],
            }
            if "variant" in reference:
                body["model"]["variant"] = reference["variant"]
        if agent is not None:
            body["agent"] = agent
        if permission is not None:
            body["permission"] = permission
        session = self._request(
            "POST", "/session", query={"directory": str(directory)}, body=body,
            deadline=deadline)
        if not isinstance(session, dict) or not isinstance(session.get("id"), str):
            raise OpenCodeError("OpenCode server returned a session without an ID")
        self._session_directories[session["id"]] = directory
        return session

    def send_message(
        self, session_id: str, text: str, *, cwd: Path | None = None,
        model: str | None = None, variant: str | None = None,
        agent: str | None = None, system: str | None = None,
        tools: Mapping[str, bool] | None = None,
        output_format: Mapping[str, Any] | None = None,
        deadline: float | None = None,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        directory = self._directory(cwd)
        body: dict[str, Any] = {"parts": [{"type": "text", "text": text}]}
        if message_id is not None:
            if not message_id.startswith("msg"):
                raise ValueError("OpenCode message ID must start with 'msg'")
            body["messageID"] = message_id
        if model is not None:
            reference = _model_ref(model, variant)
            body["model"] = {
                "providerID": reference["providerID"],
                "modelID": reference["modelID"],
            }
            if "variant" in reference:
                body["variant"] = reference["variant"]
        elif variant is not None:
            body["variant"] = variant
        if agent is not None:
            body["agent"] = agent
        if system is not None:
            body["system"] = system
        if tools is not None:
            if not all(isinstance(name, str) and type(enabled) is bool
                       for name, enabled in tools.items()):
                raise TypeError("OpenCode tool settings must map names to booleans")
            body["tools"] = dict(tools)
        else:
            body["tools"] = {}
        # This caller-side deny is deliberate and cannot be overridden by a
        # global config, agent profile, or per-message tools map.
        body["tools"].update({name: False for name in _SHELL_TOOL_NAMES})
        if output_format is not None:
            body["format"] = copy.deepcopy(dict(output_format))
        token_root = getattr(self, "_token_budget_root", None)
        if token_root is not None:
            from .token_budget import admit_token_call
            if message_id is None:
                message_id = "msg" + secrets.token_hex(16)
                body["messageID"] = message_id
            admit_token_call(token_root, session_id, message_id)
        try:
            report_sdk_model("request")
            result = self._request(
                "POST", f"/session/{quote(session_id, safe='')}/message",
                query={"directory": str(directory)}, body=body, deadline=deadline)
            report_sdk_model("response")
            if token_root is not None and isinstance(result, Mapping):
                from .token_budget import record_token_message
                info = result.get("info")
                if isinstance(info, Mapping):
                    record_token_message(token_root, info)
        finally:
            if token_root is not None:
                from .token_budget import finish_token_call, reconcile_token_session
                complete = reconcile_token_session(
                    self, token_root, session_id, cwd=directory,
                    deadline=time.monotonic() + 2.0, message_id=message_id)
                finish_token_call(token_root, session_id, message_id, complete=complete)
        if not isinstance(result, dict):
            raise OpenCodeError("OpenCode server returned an invalid message response")
        info = result.get("info")
        error = info.get("error") if isinstance(info, dict) else None
        if isinstance(error, Mapping):
            raise OpenCodeResponseError(error, result)
        return result

    def abort_session(self, session_id: str, *, cwd: Path | None = None,
                      deadline: float | None = None) -> bool:
        directory = self._directory(cwd)
        result = self._request(
            "POST", f"/session/{quote(session_id, safe='')}/abort",
            query={"directory": str(directory)}, deadline=deadline)
        if type(result) is not bool:
            raise OpenCodeError("OpenCode abort endpoint returned a non-boolean result")
        return result

    def messages(self, session_id: str, *, cwd: Path | None = None,
                 deadline: float | None = None) -> list[dict[str, Any]]:
        directory = self._directory(cwd)
        result = self._request(
            "GET", f"/session/{quote(session_id, safe='')}/message",
            query={"directory": str(directory)}, deadline=deadline)
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise OpenCodeError("OpenCode message list has an invalid shape")
        token_root = getattr(self, "_token_budget_root", None)
        if token_root is not None:
            from .token_budget import record_token_message
            infos = [message["info"] for message in result
                     if isinstance(message.get("info"), Mapping)
                     and message["info"].get("role") == "assistant"]
            for info in infos:
                record_token_message(token_root, info)
            from .token_budget import settle_observed_token_calls
            settle_observed_token_calls(token_root, session_id, infos)
        return result

    def children(self, session_id: str, *, cwd: Path | None = None,
                 deadline: float | None = None) -> list[dict[str, Any]]:
        directory = self._directory(cwd)
        result = self._request(
            "GET", f"/session/{quote(session_id, safe='')}/children",
            query={"directory": str(directory)}, deadline=deadline)
        if not isinstance(result, list) or any(
                not isinstance(item, dict) or not isinstance(item.get("id"), str)
                for item in result):
            raise OpenCodeError("OpenCode child-session list has an invalid shape")
        return result

    def get_message(self, session_id: str, message_id: str, *, cwd: Path | None = None,
                    deadline: float | None = None) -> dict[str, Any]:
        """Retrieve one message and its parts without listing session history."""
        if not session_id.startswith("ses") or not message_id.startswith("msg"):
            raise ValueError("OpenCode message lookup requires session and message IDs")
        directory = self._directory(cwd)
        result = self._request(
            "GET", f"/session/{quote(session_id, safe='')}/message/"
            f"{quote(message_id, safe='')}",
            query={"directory": str(directory)}, deadline=deadline)
        if not isinstance(result, dict):
            raise OpenCodeError("OpenCode single-message response has an invalid shape")
        token_root = getattr(self, "_token_budget_root", None)
        if token_root is not None and isinstance(result.get("info"), Mapping):
            from .token_budget import record_token_message
            record_token_message(token_root, result["info"])
        return result

    def get_session(self, session_id: str, *, cwd: Path | None = None,
                    deadline: float | None = None) -> dict[str, Any]:
        directory = self._directory(cwd)
        result = self._request(
            "GET", f"/session/{quote(session_id, safe='')}",
            query={"directory": str(directory)}, deadline=deadline)
        if not isinstance(result, dict):
            raise OpenCodeError("OpenCode session response has an invalid shape")
        return result

    def sessions(self, *, cwd: Path, deadline: float | None = None) -> list[dict[str, Any]]:
        """List sessions for this workspace to reconcile interrupted creation."""
        result = self._request(
            "GET", "/session", query={"directory": str(Path(cwd).resolve())},
            deadline=deadline)
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise OpenCodeError("OpenCode session list has an invalid shape")
        return result

    def delete_session(self, session_id: str, *, cwd: Path | None = None,
                       deadline: float | None = None) -> bool:
        """Permanently delete the session and its transcript from OpenCode."""
        directory = self._directory(cwd)
        result = self._request(
            "DELETE", f"/session/{quote(session_id, safe='')}",
            query={"directory": str(directory)}, deadline=deadline)
        if type(result) is not bool:
            raise OpenCodeError("OpenCode delete-session endpoint returned a non-boolean result")
        self._session_directories.pop(session_id, None)
        return result

    def providers(self, *, cwd: Path, deadline: float | None = None) -> dict[str, Any]:
        result = self._request("GET", "/provider", query={"directory": str(Path(cwd).resolve())},
                               deadline=deadline)
        if not isinstance(result, dict) or not isinstance(result.get("all"), list):
            raise OpenCodeError("OpenCode provider response has an invalid shape")
        return _redact_provider(result)

    def tool_ids(self, *, cwd: Path, deadline: float | None = None) -> list[str]:
        result = self._request(
            "GET", "/experimental/tool/ids",
            query={"directory": str(Path(cwd).resolve())}, deadline=deadline)
        if not isinstance(result, list) or any(not isinstance(item, str) for item in result):
            raise OpenCodeError("OpenCode tool ID response has an invalid shape")
        return result

    def mcp_status(self, *, cwd: Path, deadline: float | None = None) -> dict[str, Any]:
        """Return actual configured MCP connection statuses from OpenCode."""
        result = self._request("GET", "/mcp", query={"directory": str(Path(cwd).resolve())},
                               deadline=deadline)
        if not isinstance(result, dict):
            raise OpenCodeError("OpenCode MCP status response has an invalid shape")
        return result

    def require_model(
        self, *, cwd: Path, model: str, variant: str | None = None,
        deadline: float | None = None,
    ) -> dict[str, str]:
        reference = _model_ref(model, variant)
        providers = self.providers(cwd=cwd, deadline=deadline)
        provider_id = reference["providerID"]
        if provider_id not in providers.get("connected", []):
            raise OpenCodeError(f"OpenCode provider {provider_id!r} is not connected")
        provider = next((item for item in providers["all"]
                         if item.get("id") == provider_id), None)
        model_entry = (provider or {}).get("models", {}).get(reference["modelID"])
        if model_entry is None:
            raise OpenCodeError(
                f"OpenCode model {provider_id}/{reference['modelID']} is unavailable")
        selected_variant = reference.get("variant")
        variants = model_entry.get("variants", {})
        if selected_variant and selected_variant not in variants:
            raise OpenCodeError(
                f"OpenCode variant {selected_variant!r} is unavailable for "
                f"{provider_id}/{reference['modelID']}")
        return reference

    def events(self, *, cwd: Path, deadline: float,
               stop_event: OpenCodeEventStop | threading.Event | None = None
               ) -> Iterator[dict[str, Any]]:
        directory = Path(cwd).resolve()
        url = f"{self.base_url}/event?{urlencode({'directory': str(directory)})}"
        request = Request(url, headers={
            "Accept": "text/event-stream", "Authorization": self._auth_header()})
        connection = None
        headers_received = False
        sdk_context = current_sdk_context()
        activity = getattr(self, "_sdk_activity", None)
        if activity is None or getattr(self, "_sdk_activity_context", None) is not sdk_context:
            activity = OpenCodeActivity()
            self._sdk_activity = activity
            self._sdk_activity_context = sdk_context
        _sdk_activity_call("enable_stream", "stdout")
        try:
            # A stop handle cannot reach the socket until response headers arrive.
            # Bound that window so reader.close() can join a reconnect.
            with urlopen(request, timeout=min(2.0, self._deadline_timeout(deadline))) as response:
                headers_received = True
                sock = getattr(getattr(response, "fp", None), "raw", None)
                connection = getattr(sock, "_sock", None)
                attach = getattr(stop_event, "attach", None)
                if connection is not None and callable(attach):
                    attach(connection)
                data_lines: list[str] = []
                while time.monotonic() < deadline and not (
                        stop_event is not None and stop_event.is_set()):
                    sock = getattr(getattr(response, "fp", None), "raw", None)
                    sock = getattr(sock, "_sock", None)
                    if sock is not None:
                        sock.settimeout(self._deadline_timeout(deadline))
                    try:
                        line = response.readline()
                    except (TimeoutError, socket.timeout):
                        if stop_event is not None and stop_event.is_set():
                            return
                        if time.monotonic() >= deadline:
                            raise TimeoutError("OpenCode event deadline exhausted")
                        raise OpenCodeError("OpenCode event stream disconnected before deadline")
                    if not line:
                        return
                    report_sdk_bytes("stdout", line)
                    decoded = line.decode("utf-8", errors="strict").rstrip("\r\n")
                    if not decoded:
                        if data_lines:
                            payload = json.loads("\n".join(data_lines))
                            if isinstance(payload, dict):
                                activity.observe(payload)
                                token_root = getattr(self, "_token_budget_root", None)
                                properties = payload.get("properties")
                                info = properties.get("info") if isinstance(properties, Mapping) else None
                                # Native tasks create sessions inside OpenCode, so they
                                # never pass through this client's create_session().
                                # The managed event stream belongs to this workspace.
                                observed_id = info.get("sessionID") if isinstance(info, Mapping) else None
                                if isinstance(observed_id, str) and observed_id.startswith("ses"):
                                    self._session_directories.setdefault(observed_id, directory)
                                if token_root is not None and isinstance(info, Mapping):
                                    from .token_budget import record_token_message, read_token_budget
                                    record_token_message(token_root, info)
                                    if read_token_budget(token_root)["exhausted"]:
                                        # Public abort stops further OpenCode steps; a
                                        # current provider stream may already overshoot.
                                        sent = getattr(self, "_token_budget_abort_sent", set())
                                        self._token_budget_abort_sent = sent
                                        for active_id in tuple(self._session_directories):
                                            if active_id in sent:
                                                continue
                                            sent.add(active_id)
                                            try:
                                                self.abort_session(active_id, deadline=time.monotonic() + 2.0)
                                            except Exception:
                                                pass  # SDK settlement retains cleanup authority.
                                yield payload
                            data_lines.clear()
                    elif decoded.startswith("data:"):
                        data_lines.append(decoded[5:].lstrip())
            if stop_event is not None and stop_event.is_set():
                return
            raise TimeoutError("OpenCode event deadline exhausted")
        except HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise OpenCodeHTTPError(exc.code, error_body) from exc
        except (TimeoutError, socket.timeout) as exc:
            if not headers_received and time.monotonic() < deadline:
                raise OpenCodeEventConnectTimeout(
                    "OpenCode event response headers timed out before deadline") from exc
            raise TimeoutError("OpenCode event deadline exhausted") from exc
        except URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                if not headers_received and time.monotonic() < deadline:
                    raise OpenCodeEventConnectTimeout(
                        "OpenCode event response headers timed out before deadline") from exc
                raise TimeoutError("OpenCode event deadline exhausted") from exc
            raise OpenCodeError(f"OpenCode event stream failed: {exc.reason}") from exc
        except OSError as exc:
            if stop_event is not None and stop_event.is_set():
                return
            raise OpenCodeError(f"OpenCode event stream failed: {exc}") from exc
        finally:
            activity.close()
            detach = getattr(stop_event, "detach", None)
            if connection is not None and callable(detach):
                detach(connection)

    def _close_windows(self, *, cleanup_reason: str,
                       deadline_exceeded: bool) -> dict[str, Any]:
        """Close the exact owned Job; a dead leader alone proves nothing."""
        host_termination = self.process.returncode is None
        try:
            self.process.terminate_tree(timeout=5.0)
        except Exception as exc:
            self.diagnostic_errors.append({"phase": "job_terminate", "error": type(exc).__name__})
        try:
            self.process.wait(timeout=2.0)
        except Exception as exc:
            self.diagnostic_errors.append({"phase": "job_wait", "error": type(exc).__name__})
        threads = getattr(self, "_log_threads", ())
        end = time.monotonic() + 2.0
        for worker in threads:
            worker.join(timeout=max(0, end - time.monotonic()))
        readers_stopped = all(not worker.is_alive() for worker in threads)
        errors = list(self.diagnostic_errors) + list(getattr(self, "_log_errors", ()))
        leader_exited = self.process.returncode is not None
        job_gone = getattr(self.process, "cleanup_confirmed", False) is True
        cleanup = leader_exited and job_gone and readers_stopped
        if cleanup:
            try:
                self.process.close()
                log = getattr(self, "_server_log", None)
                if log is not None:
                    log.close()
            except Exception as exc:
                errors.append({"phase": "job_close", "error": type(exc).__name__})
                cleanup = False
        diagnostic = {
            "classification": "host_terminated" if cleanup and host_termination else "unknown",
            "returncode": self.process.returncode, "signal_number": None,
            "evidence_status": "windows_job_observation", "attribution": "host" if host_termination else "unknown",
            "containment": "windows_job", "target_pid": self.process.pid,
            "target_birth": self.process_birth, "target_argv": self.argv,
            "target_cwd": self.cwd, "target_executable": self.executable,
            "target_executable_sha256": self.executable_sha256,
            "leader_exited": leader_exited, "job_empty": job_gone,
            "log_readers_stopped": readers_stopped, "log_capture_complete": readers_stopped and not getattr(self, "_log_errors", ()),
            "cleanup_confirmed": cleanup, "collection_errors": errors,
            "host_requested_termination": host_termination, "host_requested_signal": None,
            "cleanup_reason": cleanup_reason, "deadline_exceeded": deadline_exceeded,
            "opencode_version": self.version, "opencode_executable": self.executable,
            "opencode_executable_sha256": self.executable_sha256, "stderr_path": self.stderr_path,
        }
        if cleanup:
            self.process_diagnostic = diagnostic
            self._closed = True
        return diagnostic

    def close(self, *, deadline_exceeded: bool = False,
              cleanup_reason: str = "host_cleanup") -> dict[str, Any] | None:
        if self.process_diagnostic is not None:
            return self.process_diagnostic
        if _windows_host():
            return self._close_windows(cleanup_reason=cleanup_reason,
                                       deadline_exceeded=deadline_exceeded)
        # Signal before polling: polling may reap an already-exited leader and
        # release its PID even while a descendant remains in the owned group.
        # An unreaped Popen leader pins that PID, so group signaling cannot hit
        # an unrelated group that reused the numeric ID.
        host_requested_termination = self.process.returncode is None
        if host_requested_termination:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                # The group may have exited between poll and signal. Reap the
                # owned child below; a missing group alone proves nothing.
                pass
            except OSError as exc:
                self.diagnostic_errors.append({
                    "phase": "group_kill", "error": type(exc).__name__})
        try:
            self.process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            # The group signal may not have reached the owned child. Give its
            # PID one final bounded termination attempt before recording an
            # unknown outcome instead of hanging the host indefinitely.
            self.diagnostic_errors.append({"phase": "group_wait", "error": "TimeoutExpired"})
            try:
                self.process.kill()
            except OSError as exc:
                self.diagnostic_errors.append({
                    "phase": "direct_kill", "error": type(exc).__name__})
            try:
                self.process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self.diagnostic_errors.append({
                    "phase": "direct_wait", "error": "TimeoutExpired"})
        leader_exited = self.process.poll() is not None
        group_wait_started = time.monotonic()
        group_wait_deadline = group_wait_started + (
            PROCESS_GROUP_EXIT_WAIT_SECONDS if leader_exited else 0.0)
        group_present = False
        while True:
            try:
                # Reaping the leader does not prove that MCP or model
                # descendants in its owned process group have stopped. A
                # just-killed descendant can remain briefly visible here.
                os.killpg(self.process.pid, 0)
            except ProcessLookupError:
                group_gone = True
                break
            except OSError as exc:
                group_gone = False
                self.diagnostic_errors.append({
                    "phase": "group_identity", "error": type(exc).__name__})
                break
            group_present = True
            remaining = group_wait_deadline - time.monotonic()
            if remaining <= 0:
                group_gone = False
                break
            time.sleep(min(0.05, remaining))
        group_observation = None if group_gone else _process_group_members(self.process.pid)
        initial_group_observation = group_observation
        zombie_only = False
        if group_present and _zombie_only_group(group_observation):
            # /proc is not an atomic snapshot. Confirm the fully observed
            # zombie-only state once more after a short grace, and require a
            # successful group identity probe on both sides of that grace.
            time.sleep(0.05)
            try:
                os.killpg(self.process.pid, 0)
            except ProcessLookupError:
                group_gone = True
                group_observation = None
            except OSError as exc:
                self.diagnostic_errors.append({
                    "phase": "zombie_group_identity", "error": type(exc).__name__})
            else:
                group_observation = _process_group_members(self.process.pid)
                zombie_only = _zombie_only_group(group_observation)
        cleanup_confirmed = leader_exited and (group_gone or zombie_only)
        memory_after = None
        if cleanup_confirmed:
            try:
                if self.memory_before is not None and self.memory_before.scope:
                    memory_after = read_memory_events(self.memory_before.scope)
            except Exception as exc:
                self.diagnostic_errors.append({
                    "phase": "cgroup_after", "error": type(exc).__name__})
        try:
            diagnostic = diagnose_process_exit(
                self.process.returncode, before=self.memory_before, after=memory_after,
                exclusive_scope=False, deadline_exceeded=deadline_exceeded).to_dict()
        except Exception as exc:
            self.diagnostic_errors.append({
                "phase": "classification", "error": type(exc).__name__})
            diagnostic = {
                "classification": "unknown", "returncode": self.process.returncode,
                "signal_number": None, "cgroup_scope": None,
                "memory_event_delta": {}, "evidence_status": "unavailable",
                "attribution": "unknown", "detail": "process diagnostic collection failed",
            }
        diagnostic.update({
            "target_pid": self.process.pid,
            "target_birth": self.process_birth,
            "process_group_id": ((self.process_identity or {}).get("process_group_id")),
            "session_id": ((self.process_identity or {}).get("session_id")),
            "target_pid_namespace": ((self.process_identity or {}).get("pid_namespace")),
            "target_start_ticks": ((self.process_identity or {}).get("start_ticks")),
            "target_argv": self.argv,
            "target_cwd": self.cwd,
            "target_executable": self.executable,
            "target_executable_sha256": self.executable_sha256,
            "cgroup_before": (self.memory_before.to_dict()
                              if self.memory_before is not None else None),
            "cgroup_after": memory_after.to_dict() if memory_after is not None else None,
            "collection_errors": list(self.diagnostic_errors),
            "leader_exited": leader_exited,
            "process_group_gone": group_gone,
            "process_group_quiescent": group_gone or zombie_only,
            "group_quiescence_reason": ("gone" if group_gone else
                                         "zombie_only" if zombie_only else "unconfirmed"),
            "group_exit_wait_seconds": round(time.monotonic() - group_wait_started, 3),
            "process_group_observation": group_observation,
            "process_group_observation_initial": initial_group_observation,
            "cleanup_confirmed": cleanup_confirmed,
            "host_requested_termination": host_requested_termination,
            "host_requested_signal": signal.SIGKILL if host_requested_termination else None,
            "cleanup_reason": cleanup_reason,
            "opencode_version": self.version,
            "opencode_executable": self.executable,
            "opencode_executable_sha256": self.executable_sha256,
            "stderr_path": self.stderr_path,
        })
        if cleanup_confirmed:
            self.process_diagnostic = diagnostic
            self._closed = True
        return diagnostic


def _available_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])
