"""Bounded process-exit diagnostics with scoped cgroup OOM evidence.

An exit status is not an OOM observation.  Callers take a snapshot of the
worker's cgroup before it starts and another from the same cgroup after it
stops.  Only an increase in a kill counter can confirm an OOM classification.
The helpers are read-only and never signal, restart, or otherwise manage a
process.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
from pathlib import Path
import signal
import time
from typing import Callable, Mapping


_OOM_KILL_COUNTERS = ("oom_kill", "oom_group_kill")
_KNOWN_COUNTERS = frozenset({"low", "high", "max", "oom", *_OOM_KILL_COUNTERS})


@dataclass(frozen=True, slots=True)
class CgroupMemoryEvents:
    """One immutable observation of a particular cgroup v2 scope."""

    scope: str | None
    counters: Mapping[str, int]
    observed_at: float
    error: str | None = None
    memory: Mapping[str, int | str | None] = field(default_factory=dict)
    memory_errors: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.scope is not None and (not isinstance(self.scope, str) or not self.scope):
            raise ValueError("cgroup scope must be a nonempty string or None")
        if (isinstance(self.observed_at, bool)
                or not isinstance(self.observed_at, (int, float))
                or not math.isfinite(self.observed_at)):
            raise ValueError("cgroup observation time must be finite")
        values = dict(self.counters)
        if any(not isinstance(key, str) or type(value) is not int or value < 0
               for key, value in values.items()):
            raise ValueError("cgroup event counters must be nonnegative integers")
        object.__setattr__(self, "counters", values)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ProcessDiagnostic:
    """A process outcome kept separate from SDK and acceptance state."""

    classification: str
    returncode: int | None
    signal_number: int | None
    cgroup_scope: str | None
    memory_event_delta: Mapping[str, int]
    evidence_status: str
    attribution: str
    detail: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "memory_event_delta", dict(self.memory_event_delta))

    def to_dict(self) -> dict:
        return asdict(self)


def read_memory_events(cgroup: str | Path, *,
                       clock: Callable[[], float] = time.time) -> CgroupMemoryEvents:
    """Read ``memory.events`` from one already-resolved cgroup v2 directory."""

    directory = Path(cgroup).absolute()
    scope = str(directory)
    try:
        path = directory / "memory.events"
        if path.is_symlink() or not path.is_file():
            raise OSError("memory.events is not a regular file")
        counters: dict[str, int] = {}
        for line in path.read_text(encoding="ascii").splitlines():
            fields = line.split()
            if len(fields) != 2 or fields[0] not in _KNOWN_COUNTERS:
                continue
            value = int(fields[1])
            if value < 0:
                raise ValueError("negative cgroup event counter")
            counters[fields[0]] = value
        if not all(name in counters for name in _OOM_KILL_COUNTERS):
            # Older kernels can omit oom_group_kill, but oom_kill is required
            # to make the strong confirmed_oom claim.
            if "oom_kill" not in counters:
                raise ValueError("memory.events has no oom_kill counter")
            counters.setdefault("oom_group_kill", 0)
        memory, errors = {}, {}
        for name in ('memory.max', 'memory.peak', 'memory.current'):
            metric = directory / name
            try:
                if metric.is_symlink() or not metric.is_file():
                    raise OSError('memory metric is not a regular file')
                with metric.open(encoding='ascii') as stream:
                    raw = stream.read(128).strip()
                value = raw if raw == 'max' and name == 'memory.max' else int(raw)
                if isinstance(value, int) and value < 0:
                    raise ValueError('negative memory metric')
                memory[name] = value
            except (OSError, UnicodeError, ValueError) as error:
                memory[name] = None
                errors[name] = type(error).__name__
        return CgroupMemoryEvents(scope, counters, clock(), memory=memory, memory_errors=errors)
    except (OSError, UnicodeError, ValueError) as exc:
        return CgroupMemoryEvents(scope, {}, clock(), type(exc).__name__)


def _mount_path(value: str) -> str:
    for encoded, decoded in (("\\040", " "), ("\\011", "\t"),
                             ("\\012", "\n"), ("\\134", "\\")):
        value = value.replace(encoded, decoded)
    return value


def process_cgroup(pid: int, *, proc_root: str | Path = "/proc") -> tuple[Path | None, str | None]:
    """Resolve a process's exact cgroup v2 directory without using ``self``."""

    if type(pid) is not int or pid <= 0:
        raise ValueError("pid must be a positive integer")
    process = Path(proc_root) / str(pid)
    try:
        membership_text = (process / "cgroup").read_text(encoding="utf-8")
        mount_text = (process / "mountinfo").read_text(encoding="utf-8")
    except OSError as exc:
        return None, "process_cgroup_unavailable:" + type(exc).__name__
    membership = None
    for line in membership_text.splitlines():
        fields = line.split(":", 2)
        if len(fields) == 3 and fields[0] == "0" and fields[1] == "":
            membership = Path(fields[2])
            break
    if membership is None:
        return None, "cgroup_v2_membership_unavailable"
    if not membership.is_absolute() or ".." in membership.parts:
        return None, "cgroup_v2_membership_invalid"
    for line in mount_text.splitlines():
        fields = line.split()
        try:
            separator = fields.index("-")
        except ValueError:
            continue
        if separator + 1 >= len(fields) or fields[separator + 1] != "cgroup2" or len(fields) < 5:
            continue
        root = Path(_mount_path(fields[3]))
        mount = Path(_mount_path(fields[4]))
        if not root.is_absolute() or not mount.is_absolute() or ".." in mount.parts:
            continue
        try:
            relative = membership.relative_to(root)
        except ValueError:
            continue
        return (mount / relative).absolute(), None
    return None, "cgroup_v2_mount_unresolved"


def read_process_memory_events(pid: int, *, proc_root: str | Path = "/proc",
                               clock: Callable[[], float] = time.time) -> CgroupMemoryEvents:
    """Capture the cgroup containing ``pid`` without claiming it is exclusive.

    Pass the snapshot to :func:`diagnose_process_exit`; that function defaults
    to shared-scope attribution unless the caller independently proves and
    explicitly declares exclusivity.
    """

    cgroup, error = process_cgroup(pid, proc_root=proc_root)
    if cgroup is None:
        return CgroupMemoryEvents(None, {}, clock(), error)
    return read_memory_events(cgroup, clock=clock)


def memory_event_delta(before: CgroupMemoryEvents | None,
                       after: CgroupMemoryEvents | None) -> tuple[dict[str, int], str]:
    """Compare two observations only when they name the exact same scope."""

    if before is None or after is None:
        return {}, "missing"
    if before.error is not None or after.error is not None:
        return {}, "unavailable"
    if before.scope is None or before.scope != after.scope:
        return {}, "scope_mismatch"
    delta: dict[str, int] = {}
    for name in sorted(set(before.counters) | set(after.counters)):
        old = before.counters.get(name)
        new = after.counters.get(name)
        if old is None or new is None or new < old:
            return {}, "counter_reset"
        delta[name] = new - old
    return delta, "observed"


def _exit_signal(returncode: int | None) -> int | None:
    if returncode is None or isinstance(returncode, bool):
        return None
    if returncode < 0:
        return -returncode
    # Shells commonly encode signal termination as 128 + signal.
    if 128 < returncode and returncode - 128 < signal.NSIG:
        return returncode - 128
    return None


def diagnose_process_exit(
        returncode: int | None, *,
        before: CgroupMemoryEvents | None = None,
        after: CgroupMemoryEvents | None = None,
        exclusive_scope: bool = False,
        admission: str | None = None,
        timed_out: bool = False,
        deadline_exceeded: bool = False) -> ProcessDiagnostic:
    """Classify an outcome without promoting exit 137/SIGKILL to OOM.

    ``admission`` is an explicit host observation and may be ``timeout`` or
    ``rejected``.  Process timeout/deadline observations outrank an ambiguous
    exit signal. A cgroup event is attributable to this process only when the
    caller has separately proved that the scope was exclusive and the process
    exited through SIGKILL (including shell status 137).
    """

    if admission not in {None, "timeout", "rejected"}:
        raise ValueError("admission must be timeout, rejected, or None")
    if type(exclusive_scope) is not bool:
        raise ValueError("exclusive_scope must be a boolean")
    if returncode is not None and type(returncode) is not int:
        raise ValueError("returncode must be an integer or None")
    delta, evidence_status = memory_event_delta(before, after)
    scope = before.scope if before is not None and before.scope == getattr(after, "scope", None) else None
    signum = _exit_signal(returncode)
    cgroup_oom_observed = any(delta.get(name, 0) > 0 for name in _OOM_KILL_COUNTERS)
    confirmed_oom = (exclusive_scope and signum == signal.SIGKILL
                     and cgroup_oom_observed)
    attribution = "unknown" if cgroup_oom_observed else "not_applicable"

    if admission == "timeout":
        classification, detail = "memory_admission_timeout", "memory admission wait expired"
    elif admission == "rejected":
        classification, detail = "memory_admission_rejected", "memory admission rejected the worker"
    elif deadline_exceeded:
        classification, detail = "deadline_exceeded", "the effective execution deadline expired"
    elif timed_out:
        classification, detail = "process_timeout", "the bounded process wait expired"
    elif confirmed_oom:
        classification = "confirmed_oom"
        detail = "exclusive cgroup OOM kill counter increased and the process exited through SIGKILL"
        attribution = "confirmed"
    elif cgroup_oom_observed:
        classification = "cgroup_oom_observed"
        detail = "cgroup OOM kill counter increased, but the killed process cannot be attributed"
    elif returncode == 0:
        classification, detail = "completed", "process exited successfully"
    elif signum is not None:
        classification = "signal_exit"
        detail = f"process exited from signal {signum}; no scoped OOM kill increase was observed"
    elif returncode is not None:
        classification, detail = "nonzero_exit", f"process exited with status {returncode}"
    else:
        classification, detail = "unknown", "no conclusive process outcome was observed"
    return ProcessDiagnostic(
        classification, returncode, signum, scope, delta, evidence_status,
        attribution, detail)


__all__ = [
    "CgroupMemoryEvents", "ProcessDiagnostic", "diagnose_process_exit",
    "memory_event_delta", "process_cgroup", "read_memory_events",
    "read_process_memory_events",
]
