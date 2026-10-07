"""Host-owned single-driver lease and health snapshot support.

This module only coordinates the process driving an existing Run.  It never
opens an SDK writer, recovers work, retries effects, or changes a budget.
Callers must leave unknown SDK effects pending for explicit coordination.
"""

from __future__ import annotations

import errno
from . import platform_files as fcntl
import json
from .platform_files import file_os as os
from .platform_files import safe_open
from pathlib import Path
import secrets
import select
import stat
import threading
import time
from types import TracebackType
from typing import Any


DRIVER_LOCK_NAME = ".modport-driver.lock"
DRIVER_HEALTH_PARTS = ("artifacts", "monitor", "monitor-driver.json")
HEALTH_SCHEMA_VERSION = 1
MAX_HEALTH_BYTES = 16 * 1024
FORK_ACK_TIMEOUT = 5.0


class DriverLeaseError(RuntimeError):
    """Base error for driver lease and health operations."""


class DriverLeaseBusyError(DriverLeaseError):
    """Raised when another process already drives the same Run root."""


class DriverHealthError(DriverLeaseError):
    """Raised when persisted driver health is unsafe or malformed."""


class _ForkCoordinationFailure(DriverLeaseError):
    """The child did not confirm inherited lease FD closure in time."""


_lease_fds: set[int] = set()
_lease_owners: dict[int, Any] = {}
_lease_fds_guard = threading.Lock()
_fork_barrier: tuple[int, int] | None = None


def _close_inherited_lease_fds() -> None:
    for descriptor in tuple(_lease_fds):
        try:
            os.close(descriptor)
        except OSError:
            pass
    _lease_fds.clear()
    _lease_owners.clear()


def _wait_for_fork_ack(descriptor: int) -> bool:
    deadline = time.monotonic() + FORK_ACK_TIMEOUT
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            readable, _, _ = select.select([descriptor], [], [], remaining)
        except InterruptedError:
            continue
        if not readable:
            return False
        try:
            return os.read(descriptor, 1) == b"\0"
        except InterruptedError:
            continue


def _mark_fork_coordination_failed() -> None:
    failure = _ForkCoordinationFailure(
        "forked worker did not acknowledge inherited driver lock closure; "
        "manual coordination is required"
    )
    for lease in tuple(_lease_owners.values()):
        if lease._heartbeat_error is None:
            lease._heartbeat_error = failure
        lease._stop.set()


def _before_fork() -> None:
    global _fork_barrier
    _lease_fds_guard.acquire()
    try:
        _fork_barrier = os.pipe2(os.O_CLOEXEC) if _lease_fds else None
    except BaseException:
        _lease_fds_guard.release()
        raise


def _after_fork_parent() -> None:
    global _fork_barrier
    barrier, _fork_barrier = _fork_barrier, None
    try:
        if barrier is not None:
            read_descriptor, write_descriptor = barrier
            acknowledged = False
            try:
                os.close(write_descriptor)
                acknowledged = _wait_for_fork_ack(read_descriptor)
            except BaseException:
                # At-fork callbacks cannot safely recover here.  Convert any
                # local handshake failure into the same coordination state.
                acknowledged = False
            finally:
                try:
                    os.close(read_descriptor)
                except OSError:
                    pass
            if not acknowledged:
                _mark_fork_coordination_failed()
    finally:
        _lease_fds_guard.release()


def _after_fork_child() -> None:
    global _fork_barrier
    # A fork-only worker must not keep its parent's advisory lock alive after
    # the driver dies.  The parent waits only for a bounded acknowledgement;
    # on timeout its local lease becomes unhealthy and requires coordination.
    # FD_CLOEXEC separately protects exec-based workers.
    barrier, _fork_barrier = _fork_barrier, None
    try:
        _close_inherited_lease_fds()
    finally:
        if barrier is not None:
            read_descriptor, write_descriptor = barrier
            try:
                os.close(read_descriptor)
                try:
                    os.write(write_descriptor, b"\0")
                except OSError:
                    pass
            finally:
                try:
                    os.close(write_descriptor)
                except OSError:
                    pass
        _lease_fds_guard.release()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_before_fork,
        after_in_parent=_after_fork_parent,
        after_in_child=_after_fork_child,
    )


def _absolute_root(root: str | os.PathLike[str]) -> Path:
    path = Path(os.path.abspath(os.fspath(root)))
    if path == Path(path.anchor):
        raise ValueError("driver root must not be a filesystem root")
    return path


def _open_root(root: Path) -> int:
    """Open every root component without following a symlink."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(root.anchor, flags)
    try:
        for component in root.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        os.set_inheritable(descriptor, False)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_directory(parent: int, name: str, *, create: bool) -> int:
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=parent,
    )
    os.set_inheritable(descriptor, False)
    return descriptor


def _open_monitor_directory(root: Path, *, create: bool) -> int:
    descriptor = _open_root(root)
    try:
        for component in DRIVER_HEALTH_PARTS[:-1]:
            child = _open_directory(descriptor, component, create=create)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _process_birth(pid: int) -> str:
    # Keep one definition of the PID-reuse-resistant identity shared with the
    # independent observer.  Importing it has no SDK write side effects.
    from .run_monitor import process_birth

    birth = process_birth(pid)
    if not birth:
        raise DriverLeaseError("cannot determine current driver process birth")
    return birth


def _pid_namespace() -> str | None:
    from .run_monitor import pid_namespace

    return pid_namespace()


def _validated_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not run_id or len(run_id) > 512:
        raise ValueError("run_id must be a non-empty string of at most 512 characters")
    if any(ord(character) < 32 for character in run_id):
        raise ValueError("run_id must not contain control characters")
    return run_id


def _atomic_health(root: Path, snapshot: dict[str, Any]) -> None:
    directory = _open_monitor_directory(root, create=True)
    temporary = f".monitor-driver.{os.getpid()}.{secrets.token_hex(8)}"
    descriptor: int | None = None
    try:
        destination = DRIVER_HEALTH_PARTS[-1]
        try:
            existing = os.stat(destination, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise DriverHealthError("driver health path must be a regular file")
        payload = (json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n").encode()
        if len(payload) > MAX_HEALTH_BYTES:
            raise DriverHealthError("driver health snapshot exceeds size limit")
        descriptor = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=directory,
        )
        os.set_inheritable(descriptor, False)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def _validate_snapshot(value: Any, root: Path, *, file_timestamp: float) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DriverHealthError("driver health snapshot must be an object")
    identity = {"run_id", "run_dir", "pid", "birth"}
    if not identity.issubset(value):
        raise DriverHealthError("driver health snapshot is missing required fields")
    legacy = not {"schema_version", "timestamp", "status"}.intersection(value)
    if not legacy and not {"schema_version", "timestamp", "status"}.issubset(value):
        raise DriverHealthError("driver health snapshot has incomplete health fields")
    if not legacy and value["schema_version"] != HEALTH_SCHEMA_VERSION:
        raise DriverHealthError("unsupported driver health schema")
    if not isinstance(value["run_id"], str) or not value["run_id"]:
        raise DriverHealthError("invalid driver run_id")
    if value["run_dir"] != str(root):
        raise DriverHealthError("driver health snapshot belongs to another root")
    if type(value["pid"]) is not int or value["pid"] <= 0:
        raise DriverHealthError("invalid driver pid")
    if not isinstance(value["birth"], str) or not value["birth"]:
        raise DriverHealthError("invalid driver birth identity")
    if not legacy and (type(value["timestamp"]) not in {int, float} or value["timestamp"] < 0):
        raise DriverHealthError("invalid driver health timestamp")
    if not legacy and value["status"] not in {"running", "stopped", "failed"}:
        raise DriverHealthError("invalid driver status")
    snapshot = dict(value)
    if legacy:
        # run_monitor historically published identity only.  Projecting that
        # record keeps the read API compatible while DriverLease heartbeats
        # always persist the complete schema.
        snapshot.update({
            "schema_version": 0,
            "timestamp": file_timestamp,
            "status": "running",
        })
    return snapshot


def read_driver_health(root: str | os.PathLike[str]) -> dict[str, Any] | None:
    """Return the last atomic driver snapshot, or ``None`` if none exists.

    The snapshot is historical evidence.  Consumers decide freshness from its
    timestamp and authenticate a live process with both ``pid`` and ``birth``.
    """
    path = _absolute_root(root)
    try:
        directory = _open_monitor_directory(path, create=False)
    except FileNotFoundError:
        return None
    try:
        try:
            descriptor = os.open(
                DRIVER_HEALTH_PARTS[-1],
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=directory,
            )
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_HEALTH_BYTES:
                raise DriverHealthError("driver health path is not a bounded regular file")
            payload = stream.read(MAX_HEALTH_BYTES + 1)
        if len(payload) > MAX_HEALTH_BYTES:
            raise DriverHealthError("driver health snapshot exceeds size limit")
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DriverHealthError("driver health snapshot is not valid JSON") from error
        return _validate_snapshot(value, path, file_timestamp=info.st_mtime)
    finally:
        os.close(directory)


class DriverLease:
    """Exclusive root-level driver lease with an independent heartbeat."""

    def __init__(self, root: str | os.PathLike[str], run_id: str,
                 heartbeat_interval: float = 10) -> None:
        if isinstance(heartbeat_interval, bool) or not isinstance(heartbeat_interval, (int, float)):
            raise ValueError("heartbeat_interval must be a positive number")
        if heartbeat_interval <= 0:
            raise ValueError("heartbeat_interval must be positive")
        self.root = _absolute_root(root)
        self.run_id = _validated_run_id(run_id)
        self.heartbeat_interval = float(heartbeat_interval)
        self.pid = os.getpid()
        self.birth: str | None = None
        self._descriptor: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._heartbeat_error: BaseException | None = None

    @property
    def lock_path(self) -> Path:
        return self.root / DRIVER_LOCK_NAME

    @property
    def health_path(self) -> Path:
        return self.root.joinpath(*DRIVER_HEALTH_PARTS)

    def _snapshot(self, status: str, *, failure_type: str | None = None) -> dict[str, Any]:
        snapshot: dict[str, Any] = {
            "schema_version": HEALTH_SCHEMA_VERSION,
            "run_id": self.run_id,
            "run_dir": str(self.root),
            "pid": self.pid,
            "birth": self.birth,
            "pid_namespace": _pid_namespace(),
            "timestamp": time.time(),
            "status": status,
        }
        if failure_type:
            snapshot["failure_type"] = failure_type
        return snapshot

    def _publish(self, status: str, *, failure_type: str | None = None) -> None:
        _atomic_health(self.root, self._snapshot(status, failure_type=failure_type))

    def _heartbeat(self) -> None:
        while not self._stop.wait(self.heartbeat_interval):
            try:
                self._publish("running")
            except BaseException as error:
                self._heartbeat_error = error
                self._stop.set()
                return

    def check_health(self) -> None:
        """Raise promptly if this lease's heartbeat thread has failed.

        This is a local driver check only.  It does not inspect, recover, or
        otherwise mutate SDK state.
        """
        if self._descriptor is None:
            raise DriverLeaseError("driver lease is not active")
        error = self._heartbeat_error
        if error is not None:
            if isinstance(error, _ForkCoordinationFailure):
                raise DriverLeaseError(
                    "driver fork coordination failed; manual coordination is required"
                ) from error
            raise DriverLeaseError("driver heartbeat persistence failed") from error
        thread = self._thread
        if thread is None or not thread.is_alive():
            raise DriverLeaseError("driver heartbeat thread stopped unexpectedly")

    def _acquire(self) -> None:
        root_descriptor = _open_root(self.root)
        try:
            if os.name == "nt":
                descriptor = safe_open(self.root, DRIVER_LOCK_NAME, os.O_CREAT | os.O_RDWR)
            else:
                descriptor = os.open(
                    DRIVER_LOCK_NAME,
                    os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                    dir_fd=root_descriptor,
                )
        finally:
            os.close(root_descriptor)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise DriverLeaseError("driver lock path must be a regular file")
            os.set_inheritable(descriptor, False)
            with _lease_fds_guard:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as error:
                    if error.errno in {errno.EACCES, errno.EAGAIN}:
                        raise DriverLeaseBusyError(
                            f"another driver already owns root {self.root}"
                        ) from error
                    raise
                _lease_fds.add(descriptor)
                _lease_owners[descriptor] = self
            self._descriptor = descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _release(self) -> None:
        descriptor, self._descriptor = self._descriptor, None
        if descriptor is None:
            return
        with _lease_fds_guard:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                try:
                    os.close(descriptor)
                finally:
                    _lease_fds.discard(descriptor)
                    _lease_owners.pop(descriptor, None)

    def __enter__(self) -> DriverLease:
        if self._descriptor is not None or self._thread is not None:
            raise DriverLeaseError("driver lease cannot be entered twice")
        self.pid = os.getpid()
        self.birth = _process_birth(self.pid)
        self._stop.clear()
        self._heartbeat_error = None
        self._acquire()
        try:
            self._publish("running")
            self._thread = threading.Thread(
                target=self._heartbeat,
                name=f"modport-driver-heartbeat-{self.run_id[:32]}",
                daemon=True,
            )
            self._thread.start()
            return self
        except BaseException:
            self._release()
            raise

    def __exit__(self, exc_type: type[BaseException] | None,
                 exc_value: BaseException | None,
                 traceback: TracebackType | None) -> bool:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join()
        heartbeat_error = self._heartbeat_error
        status = "failed" if exc_type is not None or heartbeat_error is not None else "stopped"
        failure = exc_type.__name__ if exc_type is not None else (
            type(heartbeat_error).__name__ if heartbeat_error is not None else None
        )
        persistence_error: BaseException | None = None
        try:
            self._publish(status, failure_type=failure)
        except BaseException as error:
            persistence_error = error
        finally:
            self._release()
        if exc_type is None and (heartbeat_error is not None or persistence_error is not None):
            if isinstance(heartbeat_error, _ForkCoordinationFailure):
                raise DriverLeaseError(
                    "driver fork coordination failed; manual coordination is required"
                ) from heartbeat_error
            cause = persistence_error or heartbeat_error
            raise DriverLeaseError("driver heartbeat persistence failed") from cause
        return False
