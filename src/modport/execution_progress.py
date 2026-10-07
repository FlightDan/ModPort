"""Bounded, host-owned stage markers for SDK executions.

These records are an observational projection only. Dispatcher SDK remains the
source of task, attempt, lease and effect authority. A marker is deliberately
not treated as proof of process liveness; readers bind it to the current SDK
execution and lease before displaying it.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import math
import os
from pathlib import Path
import time
from typing import Any, Mapping

from .evidence import atomic_json, read_json


_WIRE_MARKER = "__modport_operation_payload__"
_MAX_TRANSITIONS = 12
_MAX_WAIT_SAMPLES = 8
_WAIT_SAMPLE_INTERVAL = 30.0
_CURRENT: ContextVar[dict[str, Any] | None] = ContextVar(
    "modport_execution_progress", default=None
)


def _identity(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    marker = payload.get(_WIRE_MARKER)
    identity = marker.get("identity") if isinstance(marker, Mapping) else payload
    required = ("run_id", "task_id", "stage_id", "command_id", "run_dir", "attempt")
    if not isinstance(identity, Mapping) or any(key not in identity for key in required):
        return None
    if (not isinstance(identity.get("run_dir"), str)
            or not Path(identity["run_dir"]).is_absolute()
            or type(identity.get("attempt")) is not int):
        return None
    return {key: identity[key] for key in required}


def _workflow_version(root: str | Path, payload: Any) -> int | None:
    """Resolve the frozen Run version without hydrating external payload blobs."""
    if not isinstance(payload, Mapping):
        return None
    body = payload
    marker = payload.get(_WIRE_MARKER)
    if isinstance(marker, Mapping):
        body = marker.get("body")
    options = body.get("options") if isinstance(body, Mapping) else None
    payload_version = options.get("workflow_version") if isinstance(options, Mapping) else None
    if isinstance(payload_version, bool) or not isinstance(payload_version, int):
        payload_version = None

    run_marker = Path(root) / "run.json"
    if run_marker.exists() or run_marker.is_symlink():
        try:
            if run_marker.is_symlink() or not run_marker.is_file():
                return None
            header = read_json(run_marker)
        except (OSError, ValueError, TypeError):
            return None
        definition = header.get("definition") if isinstance(header, Mapping) else None
        frozen_version = (definition.get("workflow_version")
                          if isinstance(definition, Mapping) else None)
        if isinstance(frozen_version, bool) or not isinstance(frozen_version, int):
            return None
        if payload_version is not None and payload_version != frozen_version:
            return None
        return frozen_version
    return payload_version


def _tracks_progress(version: int | None) -> bool:
    # v23/v24 Runs retain their frozen marker policy. v25 uses these records
    # for process-isolated timeout reconciliation as well as monitoring.
    return version == 22 or (version is not None and version >= 25)


def progress_path(root: str | Path, execution_id: str) -> Path:
    token = hashlib.sha256(execution_id.encode("utf-8")).hexdigest()
    return Path(root) / "artifacts" / "execution-progress" / (token + ".json")


def _progress_directory(root: str | Path, *, create: bool) -> Path | None:
    run_root = Path(root)
    try:
        if (run_root.is_symlink() or not run_root.is_dir()
                or run_root.resolve(strict=True) != run_root):
            return None
        artifacts = run_root / "artifacts"
        directory = artifacts / "execution-progress"
        if create:
            if artifacts.exists() and artifacts.is_symlink():
                return None
            artifacts.mkdir(exist_ok=True)
            if directory.exists() and directory.is_symlink():
                return None
            directory.mkdir(exist_ok=True)
        if (artifacts.is_symlink() or directory.is_symlink()
                or not artifacts.is_dir() or not directory.is_dir()):
            return None
        return directory
    except OSError:
        return None


def read_execution_progress(root: str | Path, execution_id: str) -> dict[str, Any] | None:
    directory = _progress_directory(root, create=False)
    if directory is None:
        return None
    path = progress_path(root, execution_id)
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
            return None
        value = read_json(path)
    except (OSError, ValueError, TypeError):
        return None
    timestamp = value.get("last_progress_at") if isinstance(value, dict) else None
    if (not isinstance(value, dict) or value.get("schema_version") != 1
            or value.get("execution_id") != execution_id
            or not isinstance(value.get("phase"), str)
            or isinstance(timestamp, bool) or not isinstance(timestamp, (int, float))
            or not math.isfinite(timestamp)):
        return None
    return value


def write_execution_progress(root: str | Path, identity: Mapping[str, Any], phase: str,
                             *, kernel_attempt: int | None = None,
                             fence: int | None = None,
                             registry_revision: str | None = None,
                             deadline_epoch: float | None = None,
                             wait: Mapping[str, Any] | None = None,
                             strict: bool = False,
                             now: float | None = None) -> bool:
    """Atomically publish one bounded transition and optional wait sample."""
    allowed = {
        "planned", "dispatched", "worker_entered", "input_loading", "input_ready",
        "restoring_inputs",
        "waiting_memory", "waiting_workspace", "handler_entered", "model_started",
        "settling", "finished", "failed",
    }
    if phase not in allowed:
        raise ValueError("unknown execution progress phase")
    execution_id = identity.get("command_id", identity.get("execution_id"))
    if not isinstance(execution_id, str) or not execution_id:
        raise ValueError("execution progress requires an execution identity")
    current_time = time.time() if now is None else float(now)
    directory = _progress_directory(root, create=True)
    if directory is None:
        if strict:
            raise OSError("execution progress directory is not a trusted Run artifact directory")
        return False
    path = directory / progress_path(root, execution_id).name
    try:
        previous = read_execution_progress(root, execution_id) or {}
        if previous and any(previous.get(key) != identity.get(source)
                for key, source in (("run_id", "run_id"), ("task_id", "task_id"),
                                    ("stage_id", "stage_id"),
                                    ("application_attempt", "attempt"))):
            if strict:
                raise ValueError("execution progress identity changed")
            return False
        # The SDK may dispatch immediately after its durable commit, before
        # the host publishes its post-commit marker. Never let that slower
        # host write erase worker/fence evidence that already arrived.
        if (phase in {"planned", "dispatched"}
                and previous.get("phase") not in {None, "planned", "dispatched"}):
            return True
        timestamp = previous.get("last_progress_at")
        transitions = previous.get("transitions", [])
        if not isinstance(transitions, list):
            transitions = []
        same_phase = previous.get("phase") == phase
        if same_phase and wait is None:
            return True
        updated_at = current_time
        if same_phase and wait is not None and isinstance(timestamp, (int, float)):
            last_wait = previous.get("wait")
            last_wait = last_wait if isinstance(last_wait, Mapping) else {}
            old_sample = last_wait.get("sampled_at") if isinstance(last_wait, Mapping) else None
            changed = any(last_wait.get(key) != wait.get(key)
                          for key in ("reason", "required_bytes", "reserved_bytes",
                                      "available_bytes", "ceiling_bytes", "dynamic_memory"))
            if (not changed and isinstance(old_sample, (int, float))
                    and current_time - old_sample < _WAIT_SAMPLE_INTERVAL):
                return True
            # Keep ``phase_started_at`` stable while refreshing the observation
            # freshness clock used by the monitor.
            updated_at = current_time
        if not same_phase:
            transitions = [*transitions[-(_MAX_TRANSITIONS - 1):],
                           {"phase": phase, "at": current_time}]
        waits = previous.get("wait_samples", [])
        if not isinstance(waits, list):
            waits = []
        sample = None
        if wait is not None:
            sample = {key: value for key, value in wait.items()
                      if key in {"reason", "required_bytes", "reserved_bytes",
                                 "available_bytes", "ceiling_bytes", "source",
                                 "sampled_at", "waited_seconds", "stage", "dynamic_memory"}}
            waits = [*waits[-(_MAX_WAIT_SAMPLES - 1):], sample]
        pid = os.getpid()
        try:
            from .run_monitor import process_birth
            birth = process_birth(pid)
        except (ImportError, OSError, ValueError):
            birth = None
        record = {
            "schema_version": 1,
            "run_id": identity.get("run_id"),
            "task_id": identity.get("task_id"),
            "stage_id": identity.get("stage_id"),
            "execution_id": execution_id,
            "application_attempt": identity.get("attempt"),
            "kernel_attempt": kernel_attempt,
            "fence": fence,
            "registry_revision": registry_revision,
            "phase": phase,
            "phase_started_at": (previous.get("phase_started_at", current_time)
                                 if same_phase else current_time),
            "last_progress_at": updated_at,
            "worker_pid": pid if phase not in {"planned", "dispatched"} else None,
            "worker_birth": birth if phase not in {"planned", "dispatched"} else None,
            "deadline_epoch": deadline_epoch,
            "wait": sample,
            "wait_samples": waits,
            "transitions": transitions,
        }
        atomic_json(path, record)
        return True
    except (OSError, ValueError, TypeError):
        if strict:
            raise
        return False


def record_command_progress(root: str | Path, command: Mapping[str, Any], phase: str,
                             *, kernel_attempt: int | None = None,
                             fence: int | None = None,
                             strict: bool = False, now: float | None = None) -> bool:
    """Record a host-side transition using the small identity in an SDK command."""
    payload = command.get("payload")
    identity = _identity(payload)
    if identity is None or not _tracks_progress(_workflow_version(root, payload)):
        return False
    return write_execution_progress(
        root, identity, phase,
        kernel_attempt=kernel_attempt, fence=fence,
        registry_revision=command.get("registry_revision"),
        deadline_epoch=(None if not isinstance(payload, Mapping)
                        else (payload.get("options", {}).get("deadline_epoch")
                              if isinstance(payload.get("options"), Mapping)
                              else None)),
        strict=strict, now=now,
    )


def mark_current_execution_progress(phase: str, *, wait: Mapping[str, Any] | None = None,
                                    strict: bool = False) -> bool:
    state = _CURRENT.get()
    if state is None:
        return False
    return write_execution_progress(
        state["root"], state["identity"], phase,
        kernel_attempt=state["kernel_attempt"], fence=state["fence"],
        registry_revision=state["registry_revision"],
        deadline_epoch=state["deadline_epoch"], wait=wait, strict=strict,
    )


@contextmanager
def track_execution_progress(root: str | Path, operation: Any, context: Any,
                             registry_revision: str | None):
    payload = {
        "run_id": operation.run_id, "task_id": operation.task_id,
        "stage_id": operation.stage_id, "command_id": operation.command_id,
        "run_dir": operation.run_dir, "attempt": operation.attempt,
        "options": operation.options,
    }
    if not _tracks_progress(_workflow_version(root, payload)):
        token = _CURRENT.set(None)
        try:
            yield
        finally:
            _CURRENT.reset(token)
        return
    lease = getattr(context, "lease", None)
    state = {
        "root": Path(root),
        "identity": {"run_id": operation.run_id, "task_id": operation.task_id,
                     "stage_id": operation.stage_id, "command_id": operation.command_id,
                     "attempt": operation.attempt},
        "kernel_attempt": getattr(lease, "attempt", None),
        "fence": getattr(lease, "fence", None),
        "registry_revision": registry_revision,
        "deadline_epoch": operation.options.get("deadline_epoch"),
    }
    token = _CURRENT.set(state)
    try:
        mark_current_execution_progress("input_ready")
        yield
    finally:
        _CURRENT.reset(token)


__all__ = [
    "mark_current_execution_progress", "progress_path", "read_execution_progress",
    "record_command_progress", "track_execution_progress", "write_execution_progress",
]
