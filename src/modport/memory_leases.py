"""Shared memory reservations held while a production worker executes.

Advisory locks, rather than PID numbers, identify live reservations. This also
works when separate Codex launches use different PID namespaces on one host.
"""
from contextlib import contextmanager
from . import platform_files as fcntl
import json
import math
from .platform_files import file_os as os
from pathlib import Path
import re
import stat
import time
import uuid
import tempfile

from .memory_admission import MemoryPolicy, host_memory_snapshot
from .platform_files import make_private_directory, metadata_is_host_owned, host_user_key


_LEASE_NAME = re.compile(r"[a-f0-9]{32}\.lease")
_STALE_NAME = re.compile(r"([a-f0-9]{32})\.stale-([0-9]+)")
# dispatcher-sdk 0.6 gives its in-process tree reaper one second and its
# outer supervisor teardown another one-second bound. Keep a further second
# of margin before an unlocked worker lease stops reserving capacity.
_STALE_LEASE_GRACE_SECONDS = 3.0


class MemoryLeaseError(RuntimeError):
    pass


def _open_directory(directory):
    path = Path(directory).absolute()
    if os.name == "nt":
        make_private_directory(path)
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.lstat()
    if (not stat.S_ISDIR(metadata.st_mode) or path.resolve() != path
            or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077):
        raise MemoryLeaseError("memory reservation directory must be private and owned by this user")
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)


def _open_file(directory, name, flags, mode=0o600):
    descriptor = os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=directory)
    metadata = os.fstat(descriptor)
    if (not stat.S_ISREG(metadata.st_mode)
            or not metadata_is_host_owned(metadata, private_mask=0o077)):
        os.close(descriptor)
        raise MemoryLeaseError("unsafe memory reservation file")
    return descriptor


def _reservation(descriptor):
    os.lseek(descriptor, 0, os.SEEK_SET)
    data = os.read(descriptor, 4097)
    if len(data) > 4096:
        raise MemoryLeaseError("oversized memory reservation")
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError) as error:
        raise MemoryLeaseError("invalid memory reservation") from error
    if (not isinstance(value, dict) or value.get("version") != 1
            or type(value.get("bytes")) is not int or value["bytes"] <= 0
            or type(value.get("guard")) is not int or value["guard"] < 0):
        raise MemoryLeaseError("invalid memory reservation")
    return value["bytes"], value["guard"]


def _reservations(directory, *, stale_grace_seconds, now):
    total, guard = 0, 0
    for name in os.listdir(directory):
        live = _LEASE_NAME.fullmatch(name)
        stale = _STALE_NAME.fullmatch(name)
        if live is None and stale is None:
            continue
        try:
            descriptor = _open_file(directory, name, os.O_RDWR)
        except FileNotFoundError:
            continue
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                reserved, reservation_guard = _reservation(descriptor)
                total += reserved
                guard = max(guard, reservation_guard)
            else:
                reserved, reservation_guard = _reservation(descriptor)
                if live is not None:
                    # The handler lock can disappear before the SDK supervisor
                    # has finished killing its descendants. Rename atomically so
                    # every process observes the same conservative grace period.
                    expires_ns = int((now() + stale_grace_seconds) * 1_000_000_000)
                    stale_name = live.group(0)[:-len(".lease")] + f".stale-{expires_ns}"
                    os.rename(name, stale_name, src_dir_fd=directory, dst_dir_fd=directory)
                    os.fsync(directory)
                    total += reserved
                    guard = max(guard, reservation_guard)
                elif int(stale.group(2)) > int(now() * 1_000_000_000):
                    total += reserved
                    guard = max(guard, reservation_guard)
                else:
                    os.unlink(name, dir_fd=directory)
                    os.fsync(directory)
        finally:
            os.close(descriptor)
    return total, guard


def _expired(deadline, wait_deadline):
    return ((deadline is not None and time.time() >= deadline)
            or (wait_deadline is not None and time.monotonic() >= wait_deadline))


def _sleep_interval(wait_seconds, deadline, wait_deadline):
    delay = wait_seconds
    if deadline is not None:
        delay = min(delay, max(0.0, deadline - time.time()))
    if wait_deadline is not None:
        delay = min(delay, max(0.0, wait_deadline - time.monotonic()))
    if delay > 0:
        time.sleep(delay)


@contextmanager
def memory_permit(operation, *, probe=host_memory_snapshot, policy=None,
                  is_active=lambda: True, wait_seconds=1.0, directory=None,
                  stale_grace_seconds=_STALE_LEASE_GRACE_SECONDS,
                  max_wait_seconds=None, observer=None):
    """Wait for a shared reservation before entering the actual stage body.

    Reservations are estimates, not RSS hard limits. Current pressure is checked
    again at worker start, and active reservations cover workers yet to grow.
    """
    if (isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float))
            or not math.isfinite(wait_seconds) or wait_seconds <= 0):
        raise ValueError("memory admission polling interval must be positive")
    if (isinstance(stale_grace_seconds, bool)
            or not isinstance(stale_grace_seconds, (int, float))
            or not math.isfinite(stale_grace_seconds) or stale_grace_seconds < 0):
        raise ValueError("stale lease grace must be nonnegative")
    if (max_wait_seconds is not None
            and (isinstance(max_wait_seconds, bool)
                 or not isinstance(max_wait_seconds, (int, float))
                 or not math.isfinite(max_wait_seconds)
                 or max_wait_seconds < 0)):
        raise ValueError("memory admission maximum wait must be nonnegative")
    policy = policy or MemoryPolicy.from_env()
    directory = directory or Path(tempfile.gettempdir()) / f"modport-memory-{host_user_key()}"
    root = _open_directory(directory)
    manager = None
    lease = None
    name = None
    deadline = operation.options.get("deadline_epoch")
    wait_deadline = (None if max_wait_seconds is None
                     else time.monotonic() + max_wait_seconds)
    attempted = False
    wait_started = time.monotonic()
    demand = None

    def observe(reason, *, required=None, reserved=None, snapshot=None):
        if observer is None:
            return
        sample = {
            "stage": operation.stage_id,
            "reason": reason,
            "required_bytes": required,
            "reserved_bytes": reserved,
            "available_bytes": (None if snapshot is None else snapshot.available_bytes),
            "ceiling_bytes": (None if snapshot is None else snapshot.ceiling_bytes),
            "source": ("unknown" if snapshot is None else snapshot.source),
            "sampled_at": time.time(),
            "waited_seconds": max(0.0, time.monotonic() - wait_started),
            "dynamic_memory": None if demand is None else demand.diagnostic,
        }
        try:
            observer(sample)
        except Exception:
            # Observability is not allowed to change execution/admission.
            pass

    try:
        if probe is host_memory_snapshot:
            from .dynamic_memory import DynamicMemoryDemand
            demand = DynamicMemoryDemand()
        manager = _open_file(root, "manager.lock", os.O_CREAT | os.O_RDWR)
        while lease is None:
            if not is_active():
                raise MemoryLeaseError("execution authority expired while waiting for memory")
            if deadline is not None and time.time() >= deadline:
                raise MemoryLeaseError("execution deadline expired while waiting for memory")
            if attempted and wait_deadline is not None and time.monotonic() >= wait_deadline:
                raise MemoryLeaseError("memory admission maximum wait expired")
            try:
                fcntl.flock(manager, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                attempted = True
                observe("reservation_lock_busy")
                _sleep_interval(wait_seconds, deadline, wait_deadline)
                continue
            try:
                reserved, guard = _reservations(
                    root, stale_grace_seconds=stale_grace_seconds, now=time.time)
                observation = probe()
                required = reserved + policy.heavy_slot_bytes + max(guard, policy.host_guard_bytes)
                enough_memory = (type(observation.available_bytes) is int
                        and type(observation.ceiling_bytes) is int
                        and min(observation.available_bytes, observation.ceiling_bytes) >= required
                        and not _expired(deadline, None)
                        and (not attempted or not _expired(None, wait_deadline)))
                if demand is not None:
                    demand.update(required, observation)
                if enough_memory:
                    name = uuid.uuid4().hex + ".lease"
                    lease = _open_file(root, name, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                    fcntl.flock(lease, fcntl.LOCK_EX)
                    # No prompt, path or credential belongs in a host-global ledger.
                    encoded = json.dumps({"version": 1, "bytes": policy.heavy_slot_bytes,
                                          "guard": policy.host_guard_bytes}).encode()
                    os.write(lease, encoded)
                    os.fsync(lease)
                    os.fsync(root)
                else:
                    reason = ("memory_metrics_unavailable"
                              if type(observation.available_bytes) is not int
                              or type(observation.ceiling_bytes) is not int
                              else "memory_pressure")
                    observe(reason, required=required, reserved=reserved,
                            snapshot=observation)
            finally:
                fcntl.flock(manager, fcntl.LOCK_UN)
            if lease is None:
                attempted = True
                if _expired(deadline, None):
                    observe("execution_deadline_expired")
                    raise MemoryLeaseError("execution deadline expired while waiting for memory")
                if wait_deadline is not None and time.monotonic() >= wait_deadline:
                    observe("memory_wait_timeout")
                    raise MemoryLeaseError("memory admission maximum wait expired (memory_wait_timeout)")
                _sleep_interval(wait_seconds, deadline, wait_deadline)
        if not is_active():
            raise MemoryLeaseError("execution authority expired before memory admission")
        yield
    finally:
        try:
            if lease is not None:
                # Serialize release with new grants. If the manager remains
                # unavailable, closing the lease leaves a file that other
                # processes conservatively retain through the stale grace.
                acquired = False
                release_deadline = time.monotonic() + wait_seconds
                try:
                    while not acquired:
                        try:
                            fcntl.flock(manager, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            remaining = release_deadline - time.monotonic()
                            if remaining <= 0:
                                break
                            time.sleep(min(0.05, remaining))
                        else:
                            acquired = True
                    if acquired and name is not None:
                        try:
                            os.unlink(name, dir_fd=root)
                        except FileNotFoundError:
                            pass
                        os.fsync(root)
                finally:
                    os.close(lease)
                    if acquired:
                        fcntl.flock(manager, fcntl.LOCK_UN)
        finally:
            if demand is not None:
                demand.close()
            if manager is not None:
                os.close(manager)
            os.close(root)
