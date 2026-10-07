"""Host memory observations and conservative admission for heavy workers.

This module does not persist policy in a Run.  Callers may inject both the
probe and policy, keeping host resource limits separate from frozen workflow
inputs.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Callable, Mapping


MIB = 1024 * 1024
DEFAULT_HEAVY_SLOT_BYTES = 2048 * MIB
DEFAULT_HOST_GUARD_BYTES = 512 * MIB
HEAVY_STAGES = frozenset({"environment", "development_prepare", "coder", "goal_prepare", "agent_rework", "baseline_build",
    "contract_verify", "early_compile", "target_build", "code_cleanup", "final_cleanup", "test_execute", "acceptance_build", "client_smoke"})


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    """Memory usable by this host, or ``None`` when it cannot be proved."""

    available_bytes: int | None
    ceiling_bytes: int | None
    source: str


MemoryProbe = Callable[[], MemorySnapshot]


@dataclass(frozen=True, slots=True)
class MemoryAdmissionDecision:
    starts: int
    reason: str
    hard_cap: int
    active: int
    ceiling_capacity: int | None
    available_capacity: int | None


@dataclass(frozen=True, slots=True)
class MemoryPolicy:
    """Reserve peak memory for active workers before admitting new starts."""

    heavy_slot_bytes: int = DEFAULT_HEAVY_SLOT_BYTES
    host_guard_bytes: int = DEFAULT_HOST_GUARD_BYTES

    def __post_init__(self) -> None:
        if (type(self.heavy_slot_bytes) is not int or self.heavy_slot_bytes <= 0
                or type(self.host_guard_bytes) is not int or self.host_guard_bytes < 0):
            raise ValueError("memory admission sizes must be nonnegative integers with a positive slot")

    @classmethod
    def from_env(cls, environment: Mapping[str, str] | None = None) -> "MemoryPolicy":
        """Load host-only MiB settings without changing frozen Run inputs."""
        values = os.environ if environment is None else environment

        def mib(name: str, default: int, *, positive: bool) -> int:
            raw = values.get(name)
            if raw is None:
                return default
            if not isinstance(raw, str) or re.fullmatch(r"[0-9]+", raw) is None:
                raise ValueError(f"{name} must be a decimal MiB integer")
            parsed = int(raw)
            if (positive and parsed <= 0) or (not positive and parsed < 0):
                requirement = "positive" if positive else "nonnegative"
                raise ValueError(f"{name} must be {requirement}")
            return parsed * MIB

        return cls(
            heavy_slot_bytes=mib(
                "MODPORT_CODER_MEMORY_MIB", DEFAULT_HEAVY_SLOT_BYTES, positive=True),
            host_guard_bytes=mib(
                "MODPORT_MEMORY_RESERVE_MIB", DEFAULT_HOST_GUARD_BYTES, positive=False),
        )

    def for_stage(self, stage: str) -> "MemoryPolicy | None":
        from .workflow import AGENT_STAGES, SUPERVISOR_STAGE
        if stage in HEAVY_STAGES:
            return self
        if stage in AGENT_STAGES or stage == SUPERVISOR_STAGE:
            return MemoryPolicy(min(self.heavy_slot_bytes, 512 * MIB), self.host_guard_bytes)
        return None

    def decide(self, snapshot: MemorySnapshot, *, hard_cap: int, active: int,
               slot_bytes: int | None = None,
               active_reservation_bytes: int | None = None) -> MemoryAdmissionDecision:
        """Return starts allowed by the hard cap and one immutable observation.

        Active work is subtracted from both total capacity and current available
        capacity.  The latter is intentionally conservative: a live or pending
        worker may not yet have reached its peak allocation.
        """
        if type(hard_cap) is not int or hard_cap < 0:
            raise ValueError("hard_cap must be a nonnegative integer")
        if type(active) is not int or active < 0:
            raise ValueError("active must be a nonnegative integer")
        requested_slot = self.heavy_slot_bytes if slot_bytes is None else slot_bytes
        if type(requested_slot) is not int or requested_slot <= 0:
            raise ValueError("slot_bytes must be a positive integer")
        active_bytes = (active * requested_slot if active_reservation_bytes is None
                        else active_reservation_bytes)
        if type(active_bytes) is not int or active_bytes < 0:
            raise ValueError("active_reservation_bytes must be a nonnegative integer")
        hard_remaining = max(0, hard_cap - active)
        if hard_remaining == 0:
            return MemoryAdmissionDecision(0, "hard_cap_reached", hard_cap, active, None, None)
        available = snapshot.available_bytes
        ceiling = snapshot.ceiling_bytes
        if (type(available) is not int or available < 0
                or type(ceiling) is not int or ceiling < 0):
            return MemoryAdmissionDecision(
                0, "memory_metrics_unavailable", hard_cap, active, None, None)
        available = min(available, ceiling)
        ceiling_capacity = max(0, (ceiling - self.host_guard_bytes) // requested_slot)
        available_capacity = max(0, (available - self.host_guard_bytes) // requested_slot)
        ceiling_remaining = max(
            0, (ceiling - self.host_guard_bytes - active_bytes) // requested_slot)
        available_remaining = max(
            0, (available - self.host_guard_bytes - active_bytes) // requested_slot)
        starts = min(hard_remaining, ceiling_remaining, available_remaining)
        if starts:
            reason = "admitted"
        elif ceiling_remaining == 0:
            reason = "memory_capacity_exhausted"
        else:
            reason = "memory_pressure"
        return MemoryAdmissionDecision(
            starts, reason, hard_cap, active, ceiling_capacity, available_capacity)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _proc_memory(path: Path) -> tuple[int | None, int | None, str | None]:
    text = _read_text(path)
    if text is None:
        return None, None, "proc_meminfo_unavailable"
    values: dict[str, int] = {}
    try:
        for line in text.splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[0] in {"MemTotal:", "MemAvailable:"}:
                if len(fields) != 3 or fields[2] != "kB":
                    raise ValueError
                value = int(fields[1])
                if value < 0:
                    raise ValueError
                values[fields[0]] = value * 1024
    except ValueError:
        return None, None, "proc_meminfo_malformed"
    total = values.get("MemTotal:")
    available = values.get("MemAvailable:")
    if total is None or available is None or total <= 0:
        return None, None, "proc_meminfo_malformed"
    return total, min(total, available), None


def _mount_field(value: str) -> str:
    """Decode the escapes permitted in proc mountinfo path fields."""
    for encoded, decoded in (("\\040", " "), ("\\011", "\t"),
                             ("\\012", "\n"), ("\\134", "\\")):
        value = value.replace(encoded, decoded)
    return value


def _cgroup2_location(proc_root: Path) -> tuple[tuple[Path, Path] | None, str | None]:
    try:
        cgroup = (proc_root / "self" / "cgroup").read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except OSError:
        return None, "cgroup_membership_unavailable"
    membership = None
    v1_memory = False
    for line in cgroup.splitlines():
        fields = line.split(":", 2)
        if len(fields) == 3 and fields[0] == "0" and fields[1] == "":
            membership = Path(fields[2])
            break
        if len(fields) == 3 and "memory" in fields[1].split(","):
            v1_memory = True
    if membership is None:
        return (None, "cgroup1_memory_unsupported") if v1_memory else (None, None)
    if not membership.is_absolute() or ".." in membership.parts:
        return None, "cgroup2_membership_invalid"
    try:
        mounts = (proc_root / "self" / "mountinfo").read_text(encoding="utf-8")
    except OSError:
        return None, "cgroup2_mount_unresolved"
    for line in mounts.splitlines():
        fields = line.split()
        try:
            separator = fields.index("-")
        except ValueError:
            continue
        if separator + 1 >= len(fields) or fields[separator + 1] != "cgroup2" or len(fields) < 5:
            continue
        mount_root = Path(_mount_field(fields[3]))
        mount_point = Path(_mount_field(fields[4]))
        if not mount_root.is_absolute() or not mount_point.is_absolute():
            continue
        try:
            relative = membership.relative_to(mount_root)
        except ValueError:
            continue
        return (mount_point, mount_point / relative), None
    return None, "cgroup2_mount_unresolved"


def _limit(path: Path) -> tuple[int | None, bool]:
    """Return (finite value, valid). Missing and ``max`` are valid unlimited."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, True
    except OSError:
        return None, False
    value = text.strip()
    if value == "max":
        return None, True
    try:
        parsed = int(value)
    except ValueError:
        return None, False
    return (parsed, True) if parsed >= 0 else (None, False)


def _current(path: Path) -> tuple[int | None, bool]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None, False
    try:
        value = int(text.strip())
    except ValueError:
        return None, False
    return (value, True) if value >= 0 else (None, False)


def _cgroup2_memory(proc_root: Path) -> tuple[list[int], list[int], str | None, bool]:
    location, location_error = _cgroup2_location(proc_root)
    if location_error is not None:
        return [], [], location_error, True
    if location is None:
        return [], [], None, False
    mount, leaf = location
    try:
        leaf.relative_to(mount)
    except ValueError:
        return [], [], "cgroup2_location_invalid", True
    ceilings: list[int] = []
    available: list[int] = []
    current_path = leaf
    while True:
        limits = []
        for name in ("memory.max", "memory.high"):
            limit, valid = _limit(current_path / name)
            if not valid:
                return [], [], "cgroup2_metrics_malformed", True
            if limit is not None:
                limits.append(limit)
        if limits:
            current, valid = _current(current_path / "memory.current")
            if not valid or current is None:
                return [], [], "cgroup2_metrics_malformed", True
            for limit in limits:
                ceilings.append(limit)
                available.append(max(0, limit - current))
        if current_path == mount:
            break
        current_path = current_path.parent
    return ceilings, available, None, True


def host_memory_snapshot(*, proc_root: Path | str = Path("/proc")) -> MemorySnapshot:
    """Sample physical and containment memory without counting swap as capacity."""
    if os.name == "nt":
        from .platform_runtime import windows_memory_snapshot
        return MemorySnapshot(*windows_memory_snapshot())
    proc_root = Path(proc_root)
    ceilings: list[int] = []
    available: list[int] = []
    sources = []
    total, free, proc_error = _proc_memory(proc_root / "meminfo")
    if total is not None and free is not None:
        ceilings.append(total)
        available.append(free)
        sources.append("proc_meminfo")
    cgroup_ceilings, cgroup_available, cgroup_error, found = _cgroup2_memory(proc_root)
    if cgroup_error is not None:
        return MemorySnapshot(None, None, cgroup_error)
    if found:
        sources.append("cgroup2")
        ceilings.extend(cgroup_ceilings)
        available.extend(cgroup_available)
    if not ceilings or not available:
        errors = [error for error in (proc_error, cgroup_error) if error]
        return MemorySnapshot(None, None, "+".join(errors) or "memory_metrics_unavailable")
    return MemorySnapshot(min(available), min(ceilings), "+".join(sources))
