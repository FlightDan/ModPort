"""Bounded demand signal for Hyper-V dynamic memory hot-add.

The mapping is private, anonymous and deliberately left untouched. Hyper-V's
guest integration observes committed memory and may hot-add guest RAM; normal
admission continues to rely on fresh ``/proc/meminfo`` and cgroup snapshots.
"""
from __future__ import annotations

from . import platform_files as fcntl
import math
import mmap
from .platform_files import file_os as os
from pathlib import Path
import re
import threading
import tempfile
from .platform_files import host_user_key


MIB = 1024 * 1024
MIN_DEMAND_BYTES = 2 * 1024 * MIB
MAX_DEMAND_BYTES = 4 * 1024 * MIB
DEMAND_LIFETIME_SECONDS = 120.0
_HV_STATUS = Path("/sys/kernel/debug/hv-balloon")
_HV_HOT_ADD_PARAM = Path("/sys/module/hv_balloon/parameters/hot_add")


def _hyperv_hot_add_ready() -> bool:
    """Return true only when the guest reports an initialized hot-add driver."""
    try:
        values = {}
        for line in _HV_STATUS.read_text(encoding="utf-8").splitlines():
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            values[name.strip()] = value.strip()
        capabilities = set(values["capabilities"].split())
        state = re.fullmatch(r"[0-9]+\s*\(([^)]+)\)", values["state"])
        module_hot_add = _HV_HOT_ADD_PARAM.read_text(encoding="ascii").strip()
    except (OSError, KeyError, UnicodeError):
        return False
    return ("enabled" in capabilities and "hot_add" in capabilities
            and state is not None and state.group(1) == "Initialized"
            and module_hot_add in {"Y", "1"})


def _finite_cgroup_headroom() -> tuple[int | None, bool]:
    """Return the tightest finite v2 headroom, or fail closed on bad metrics."""
    try:
        from .memory_admission import _cgroup2_memory

        ceilings, available, error, found = _cgroup2_memory(Path("/proc"))
    except (OSError, ValueError):
        return None, False
    if error is not None:
        return None, False
    if not found or not ceilings:
        return None, True
    if not available or any(type(value) is not int or value < 0 for value in available):
        return None, False
    return min(available), True


class DynamicMemoryDemand:
    """Keep one short-lived VM demand mapping to encourage Hyper-V hot-add."""

    def __init__(self, *, directory: str | Path | None = None,
                 lifetime_seconds: float = DEMAND_LIFETIME_SECONDS):
        if (isinstance(lifetime_seconds, bool)
                or not isinstance(lifetime_seconds, (int, float))
                or not math.isfinite(lifetime_seconds)
                or lifetime_seconds <= 0
                or lifetime_seconds > DEMAND_LIFETIME_SECONDS):
            raise ValueError("dynamic memory demand lifetime must be in (0, 120]")
        self._directory = (Path(directory) if directory is not None else
                           Path(tempfile.gettempdir()) / f"modport-memory-{host_user_key()}")
        self._lifetime_seconds = float(lifetime_seconds)
        self._lock = threading.RLock()
        self._directory_fd: int | None = None
        self._lock_fd: int | None = None
        self._mapping: mmap.mmap | None = None
        self._timer: threading.Timer | None = None
        self._generation = 0
        self._required_bytes: int | None = None
        self._demand_bytes: int | None = None
        self._last_reason = "idle"

    @property
    def required_bytes(self) -> int | None:
        """The active admission threshold this mapping is intended to satisfy."""
        with self._lock:
            return self._required_bytes if self._mapping is not None else None

    @property
    def diagnostic(self) -> dict[str, int | str | None]:
        """Return a bounded, path-free snapshot of the latest helper decision."""
        with self._lock:
            return {"reason": self._last_reason,
                    "required_bytes": self._required_bytes,
                    "demand_bytes": self._demand_bytes}

    def update(self, required_bytes, snapshot) -> None:
        """Maintain one bounded demand until RAM is available or it expires."""
        if os.name == "nt":
            with self._lock:
                self._last_reason = "linux_hyperv_balloon_not_applicable"
            return
        if type(required_bytes) is not int or required_bytes <= 0:
            return
        try:
            available = getattr(snapshot, "available_bytes", None)
            ceiling = getattr(snapshot, "ceiling_bytes", None)
        except Exception:
            available = ceiling = None
        if (type(available) is not int or available < 0
                or type(ceiling) is not int or ceiling < 0):
            with self._lock:
                self._last_reason = "memory_metrics_unavailable"
            return

        with self._lock:
            active_required = self._required_bytes if self._mapping is not None else None
            if active_required is not None:
                if available >= active_required:
                    self._release_locked("memory_available")
                else:
                    # One VM-wide mapping serves the largest current threshold.
                    self._required_bytes = max(active_required, required_bytes)
                    self._last_reason = "demand_active"
                    return
            if available >= required_bytes:
                self._last_reason = "memory_available"
                return

        if not _hyperv_hot_add_ready():
            with self._lock:
                self._last_reason = "hyperv_hot_add_unavailable"
            return

        deficit = required_bytes - available
        try:
            page_size = os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError):
            page_size = None
        if type(page_size) is not int or page_size <= 0:
            with self._lock:
                self._last_reason = "page_size_unavailable"
            return
        demand_bytes = min(MAX_DEMAND_BYTES,
                           max(MIN_DEMAND_BYTES, deficit))
        demand_bytes = ((demand_bytes + page_size - 1) // page_size) * page_size
        cgroup_headroom, metrics_valid = _finite_cgroup_headroom()
        if not metrics_valid:
            with self._lock:
                self._last_reason = "cgroup_metrics_unavailable"
            return
        if cgroup_headroom is not None and cgroup_headroom < required_bytes:
            with self._lock:
                self._last_reason = "cgroup_headroom_insufficient"
            return

        directory_fd = lock_fd = None
        mapping = None
        try:
            # Import locally to avoid coupling admission's snapshot path to this
            # optional demand mechanism.
            from .memory_leases import _open_directory, _open_file

            directory_fd = _open_directory(self._directory)
            lock_fd = _open_file(directory_fd, "demand.lock",
                                 os.O_CREAT | os.O_RDWR)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(lock_fd)
                lock_fd = None
                os.close(directory_fd)
                directory_fd = None
                with self._lock:
                    self._last_reason = "demand_owned_elsewhere"
                return
            mapping = mmap.mmap(-1, demand_bytes,
                                flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS,
                                prot=mmap.PROT_READ | mmap.PROT_WRITE)
            if not hasattr(mapping, "madvise") or not hasattr(mmap, "MADV_DONTFORK"):
                raise OSError("MADV_DONTFORK is unavailable")
            mapping.madvise(mmap.MADV_DONTFORK)
        except Exception:
            if mapping is not None:
                try:
                    mapping.close()
                except OSError:
                    pass
            if lock_fd is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                try:
                    os.close(lock_fd)
                except OSError:
                    pass
            if directory_fd is not None:
                try:
                    os.close(directory_fd)
                except OSError:
                    pass
            with self._lock:
                if self._last_reason != "demand_owned_elsewhere":
                    self._last_reason = "demand_mapping_unavailable"
            return

        with self._lock:
            # Another update on this instance may have won while metrics were
            # being checked. Keep one mapping and release the redundant one.
            if self._mapping is not None:
                mapping.close()
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
                os.close(directory_fd)
                self._required_bytes = max(self._required_bytes or 0, required_bytes)
                self._last_reason = "demand_active"
                return
            self._directory_fd = directory_fd
            self._lock_fd = lock_fd
            self._mapping = mapping
            self._required_bytes = required_bytes
            self._demand_bytes = demand_bytes
            self._generation += 1
            generation = self._generation
            timer = threading.Timer(self._lifetime_seconds, self._expire, (generation,))
            timer.daemon = True
            self._timer = timer
            self._last_reason = "demand_requested"
            try:
                timer.start()
            except RuntimeError:
                self._release_locked("timer_unavailable")

    def _expire(self, generation: int) -> None:
        with self._lock:
            if generation == self._generation and self._mapping is not None:
                self._release_locked("demand_expired")

    def _release_locked(self, reason: str) -> None:
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()
        mapping, self._mapping = self._mapping, None
        if mapping is not None:
            try:
                mapping.close()
            except OSError:
                pass
        lock_fd, self._lock_fd = self._lock_fd, None
        directory_fd, self._directory_fd = self._directory_fd, None
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(lock_fd)
            except OSError:
                pass
        if directory_fd is not None:
            try:
                os.close(directory_fd)
            except OSError:
                pass
        self._required_bytes = None
        self._demand_bytes = None
        self._last_reason = reason

    def close(self) -> None:
        """Release the demand mapping and its VM-wide lock immediately."""
        with self._lock:
            self._release_locked("closed")
