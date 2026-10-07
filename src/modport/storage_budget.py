"""Host storage admission for ModPort entry points.

The checks in this module observe file metadata only.  They protect entry points
before an SDK writer is opened; they are not an SDK transaction quota and they
do not change workflow acceptance semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import stat
from typing import Any, Mapping

from .evidence import atomic_json


MIB = 1024 * 1024
GIB = 1024 * MIB
DEFAULT_SDK_LIMIT_BYTES = 512 * MIB
DEFAULT_SDK_SAFETY_BYTES = 64 * MIB
DEFAULT_RUN_SOFT_LIMIT_BYTES = 20 * GIB
DEFAULT_MIN_FREE_BYTES = 2 * GIB

_SDK_FILES = tuple(
    name + suffix
    for name in ("kernel.sqlite3", "orchestrator.sqlite3")
    for suffix in ("", "-wal", "-journal")
)
_FULL_SCAN_PHASES = frozenset({"submit", "continue", "recover", "execute", "maintenance"})
_STATUS_RELATIVE = Path("artifacts/storage/status.json")
_MAX_STATUS_BYTES = 256 * 1024


class StorageBudgetError(RuntimeError):
    """A writer cannot start without violating a host storage policy."""

    def __init__(self, message: str, report: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.report = None if report is None else dict(report)


@dataclass(frozen=True, slots=True)
class StoragePolicy:
    """Finite host limits kept outside frozen Run inputs."""

    sdk_limit_bytes: int = DEFAULT_SDK_LIMIT_BYTES
    sdk_safety_bytes: int = DEFAULT_SDK_SAFETY_BYTES
    run_soft_limit_bytes: int = DEFAULT_RUN_SOFT_LIMIT_BYTES
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES

    def __post_init__(self) -> None:
        fields = {
            "sdk_limit_bytes": self.sdk_limit_bytes,
            "sdk_safety_bytes": self.sdk_safety_bytes,
            "run_soft_limit_bytes": self.run_soft_limit_bytes,
            "min_free_bytes": self.min_free_bytes,
        }
        if any(type(value) is not int or value <= 0 for value in fields.values()):
            raise ValueError("storage policy sizes must be finite positive integers")
        if self.sdk_safety_bytes < DEFAULT_SDK_SAFETY_BYTES:
            raise ValueError("SDK storage safety reserve must be at least 64 MiB")
        if self.min_free_bytes < DEFAULT_MIN_FREE_BYTES:
            raise ValueError("filesystem free-space reserve must be at least 2 GiB")
        if self.sdk_safety_bytes >= self.sdk_limit_bytes:
            raise ValueError("SDK storage safety reserve must be smaller than its limit")

    @classmethod
    def from_env(cls, environment: Mapping[str, str] | None = None) -> "StoragePolicy":
        """Load positive decimal MiB settings from the host environment."""
        values = os.environ if environment is None else environment

        def mib(name: str, default: int) -> int:
            if name not in values:
                return default
            raw = values[name]
            if not isinstance(raw, str) or re.fullmatch(r"[0-9]+", raw) is None:
                raise ValueError(f"{name} must be a positive decimal MiB integer")
            parsed = int(raw)
            if parsed <= 0:
                raise ValueError(f"{name} must be positive")
            return parsed * MIB

        return cls(
            sdk_limit_bytes=mib("MODPORT_SDK_STORAGE_LIMIT_MIB", DEFAULT_SDK_LIMIT_BYTES),
            sdk_safety_bytes=mib("MODPORT_SDK_STORAGE_SAFETY_MIB", DEFAULT_SDK_SAFETY_BYTES),
            run_soft_limit_bytes=mib(
                "MODPORT_RUN_STORAGE_LIMIT_MIB", DEFAULT_RUN_SOFT_LIMIT_BYTES),
            min_free_bytes=mib("MODPORT_STORAGE_MIN_FREE_MIB", DEFAULT_MIN_FREE_BYTES),
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "sdk_limit_bytes": self.sdk_limit_bytes,
            "sdk_safety_bytes": self.sdk_safety_bytes,
            "run_soft_limit_bytes": self.run_soft_limit_bytes,
            "min_free_bytes": self.min_free_bytes,
        }


def _regular_size(path: Path, *, required: bool = False) -> int:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if required:
            raise
        return 0
    if not stat.S_ISREG(info.st_mode):
        raise StorageBudgetError(f"storage path is not a regular file: {path.name}")
    return info.st_size


def _sdk_bytes(root: Path) -> int:
    return sum(_regular_size(root / name) for name in _SDK_FILES)


def _filesystem_probe(root: Path) -> Path:
    """Find an existing path on the filesystem that would contain ``root``."""
    probe = root
    while True:
        try:
            probe.lstat()
        except FileNotFoundError:
            parent = probe.parent
            if parent == probe:
                raise StorageBudgetError(f"no existing parent filesystem for {root}")
            probe = parent
            continue
        return probe


def _category(relative: Path) -> str:
    if len(relative.parts) == 1 and relative.name in _SDK_FILES:
        return "sdk"
    first = relative.parts[0] if relative.parts else ""
    if first == "audit-blobs":
        return "audit_blobs"
    if first in {"reports", "audit-report", "audit-logs"}:
        return "reports"
    if first == "artifacts":
        return "artifacts"
    if first in {"workspaces", "toolchains"}:
        return "workspaces_toolchains"
    return "other"


def sample_storage_footprint(root: str | Path) -> dict[str, Any]:
    """Stat a complete Run tree once, without following links or reading files."""
    root_path = Path(root).absolute()
    categories = {
        "sdk": 0,
        "audit_blobs": 0,
        "reports": 0,
        "artifacts": 0,
        "workspaces_toolchains": 0,
        "other": 0,
    }
    if not root_path.exists():
        return {"total_bytes": 0, "categories": categories, "files": 0}
    if not root_path.is_dir() or root_path.is_symlink():
        raise StorageBudgetError("Run storage root must be a real directory")

    files = 0
    pending = [root_path]
    try:
        while pending:
            directory = pending.pop()
            try:
                entries = os.scandir(directory)
            except FileNotFoundError:
                if directory == root_path:
                    raise
                # A child directory may be atomically retired after its parent
                # was listed. It no longer contributes to the live footprint.
                continue
            with entries:
                for entry in entries:
                    path = Path(entry.path)
                    relative = path.relative_to(root_path)
                    if relative == _STATUS_RELATIVE or (
                            relative.parent == _STATUS_RELATIVE.parent
                            and relative.name.startswith(".modport-")):
                        continue
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        # Atomic writers remove or replace temporary entries
                        # between scandir() and stat(). A vanished entry has no
                        # bytes in the live footprint represented by this pass.
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(path)
                    elif stat.S_ISREG(info.st_mode):
                        categories[_category(relative)] += info.st_size
                        files += 1
    except OSError as error:
        raise StorageBudgetError(
            f"cannot observe complete Run storage footprint: {type(error).__name__}") from error
    return {"total_bytes": sum(categories.values()), "categories": categories, "files": files}


def _full_scan(phase: str) -> bool:
    # Only the leading, host-owned entrypoint selects a full Run traversal.
    # Later components may contain arbitrary stage ids such as ``test_execute``
    # and must not accidentally opt a concurrent handler into a full scan.
    entrypoint = phase.lower().partition(":")[0].partition("/")[0]
    base = entrypoint.partition("-")[0]
    return base in _FULL_SCAN_PHASES or entrypoint == "audit-maintenance"


def _prior_status(path: Path) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > _MAX_STATUS_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _record(root: Path, report: dict[str, Any]) -> None:
    if not root.is_dir() or root.is_symlink():
        return
    path = root / _STATUS_RELATIVE
    for component in (root / "artifacts", path.parent, path):
        if component.is_symlink() or component.resolve() != component.absolute():
            raise StorageBudgetError("unsafe storage diagnostic path")
    if _prior_status(path) == report:
        return
    atomic_json(path, report)


def _message(report: Mapping[str, Any]) -> str:
    details = []
    violations = report["violations"]
    if "sdk" in violations:
        details.append(
            "sdk current=%d pending=%d limit=%d safety_reserve=%d" % (
                report["sdk_bytes"], report["pending_bytes"],
                report["policy"]["sdk_limit_bytes"],
                report["policy"]["sdk_safety_bytes"],
            ))
    if "run" in violations:
        details.append(
            "run current=%d pending=%d limit=%d" % (
                report["run_bytes"], report["pending_bytes"],
                report["policy"]["run_soft_limit_bytes"],
            ))
    if "free_space" in violations:
        details.append(
            "filesystem free=%d pending=%d reserve=%d" % (
                report["free_bytes"], report["pending_bytes"],
                report["policy"]["min_free_bytes"],
            ))
    return "storage budget exceeded during phase=%s: %s; source storage was preserved" % (
        report["phase"], "; ".join(details))


def check_storage_budget(
    root: str | Path,
    *,
    policy: StoragePolicy | None = None,
    phase: str = "observation",
    pending_bytes: int = 0,
    record: bool = True,
) -> dict[str, Any]:
    """Reject a prospective writer that would cross a host storage boundary.

    Frequent phases inspect only SDK database metadata and filesystem free space.
    Submit, continue, recover and maintenance phases additionally sample the full
    Run footprint.  Callers must still arrange this check before opening writers.
    """
    if not isinstance(phase, str) or not phase or len(phase) > 128:
        raise ValueError("phase must be a non-empty string of at most 128 characters")
    if type(pending_bytes) is not int or pending_bytes < 0:
        raise ValueError("pending_bytes must be a nonnegative integer")
    selected = policy or StoragePolicy.from_env()
    if not isinstance(selected, StoragePolicy):
        raise TypeError("policy must be a StoragePolicy")

    root_path = Path(root).absolute()
    complete = sample_storage_footprint(root_path) if _full_scan(phase) else None
    sdk_bytes = _sdk_bytes(root_path)
    filesystem = _filesystem_probe(root_path)
    usage = shutil.disk_usage(filesystem)
    free_bytes = usage.free
    run_bytes = None if complete is None else complete["total_bytes"]

    violations = []
    if sdk_bytes + pending_bytes + selected.sdk_safety_bytes > selected.sdk_limit_bytes:
        violations.append("sdk")
    if run_bytes is not None and run_bytes + pending_bytes > selected.run_soft_limit_bytes:
        violations.append("run")
    if free_bytes < pending_bytes + selected.min_free_bytes:
        violations.append("free_space")

    report: dict[str, Any] = {
        "schema_version": 1,
        "phase": phase,
        "status": "rejected" if violations else "ok",
        "full_scan": complete is not None,
        "sdk_bytes": sdk_bytes,
        "sdk_projected_bytes": sdk_bytes + pending_bytes,
        "run_bytes": run_bytes,
        "run_projected_bytes": None if run_bytes is None else run_bytes + pending_bytes,
        "pending_bytes": pending_bytes,
        "free_bytes": free_bytes,
        "free_after_pending_bytes": max(0, free_bytes - pending_bytes),
        "filesystem_path": str(filesystem),
        "policy": selected.to_dict(),
        "violations": violations,
    }
    if complete is not None:
        report["categories"] = complete["categories"]
        report["files"] = complete["files"]
    if record:
        _record(root_path, report)
    if violations:
        raise StorageBudgetError(_message(report), report)
    return report


__all__ = [
    "StorageBudgetError",
    "StoragePolicy",
    "check_storage_budget",
    "sample_storage_footprint",
]
