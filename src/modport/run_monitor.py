"""Independent, bounded, read-only progress monitor for an existing SDK Run.

Run this as its own process (``python -m modport.run_monitor ...``), rather
than from the execution driver. It never creates an Orchestrator or Kernel
writer, acknowledges events, or copies databases. The public SDK work
availability inspector opens the existing stores in read-only mode.
"""

from __future__ import annotations

import argparse
import copy
import errno
from . import platform_files as fcntl
import json
import math
from .platform_files import file_os as os
from pathlib import Path
import re
import stat
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable, Mapping

from dispatcher_sdk.orchestrator import inspect_work_availability

from .evidence import atomic_json
from .monitor_progress import (
    INITIAL_BUDGET_SECONDS,
    MAX_BUDGET_SECONDS,
    budget_extension_decision,
    collect_progress_evidence,
    compare_progress_evidence,
)
from .execution_progress import read_execution_progress
from .sdk_compat import sdk_release


TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
CODER_STAGES = frozenset({"coder", "agent_rework", "agent-rework"})
MAX_EVENTS = 200
SAMPLE_LIMIT = 100
EFFECT_SCAN_LIMIT = 1000
LIGHTWEIGHT_POLL_SECONDS = 10
OPTIONAL_DIAGNOSTIC_INTERVAL_SECONDS = 1800
DRIVER_HEARTBEAT_TIMEOUT_SECONDS = 30


def _validated_diagnostic_interval(value: float | int | None) -> float | None:
    if value is None:
        return None
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise ValueError("diagnostic interval must be a positive finite number")
    return float(value)


def process_birth(pid: int) -> str | None:
    """Identify a Linux process independently from its reusable numeric PID."""
    if type(pid) is not int or pid <= 0:
        return None
    if os.name == "nt":
        from .platform_runtime import process_birth as native_birth
        return native_birth(pid)
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields = raw.rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        return f"{boot}:{fields[19]}"
    except (OSError, IndexError):
        return None


def process_alive(pid: int | None, birth: str | None) -> bool:
    """A missing birth identity never treats a recycled PID as the driver."""
    return bool(pid is not None and birth and process_birth(pid) == birth)


def pid_namespace() -> str | None:
    """Return the local PID namespace identity when procfs exposes it."""
    if os.name == "nt":
        return "windows-native"
    try:
        return os.readlink("/proc/self/ns/pid")
    except OSError:
        return None


def driver_namespace_match(snapshot: Mapping[str, Any] | None) -> bool | None:
    if not isinstance(snapshot, Mapping):
        return None
    if "pid_namespace" not in snapshot:
        return None
    recorded = snapshot.get("pid_namespace")
    current = pid_namespace()
    if not isinstance(recorded, str) or not recorded or current is None:
        return False
    return recorded == current


def process_identity_state(pid: int | None, birth: str | None) -> bool | None:
    """Return True if the identity lives, False if it is gone, else unknown.

    A missing /proc record alone is ambiguous (for example, a permission or
    parsing failure). Only ESRCH or a different process birth proves the
    recorded worker identity is no longer running.
    """
    if type(pid) is not int or pid <= 0 or not isinstance(birth, str) or not birth:
        return None
    if os.name == "nt":
        from .windows_process import process_identity_state as native_identity
        return native_identity(pid, birth)
    current = process_birth(pid)
    if current is not None:
        return current == birth
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return None
    except OSError as error:
        return False if error.errno == errno.ESRCH else None
    return None


def _existing_database(root: Path, name: str) -> str:
    path = root / name
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{name} is not an existing regular database")
    return str(path)


def read_run_availability(root: Path, run_id: str, *,
                          sample_limit: int = SAMPLE_LIMIT,
                          effect_scan_limit: int = EFFECT_SCAN_LIMIT) -> Any:
    """Use the SDK's public bounded inspector without constructing a writer.

    The inspector only needs the two paths, the current clock and the optional
    Runtime (absent here). Its documented clock calculation also takes the
    persisted Kernel watermark, so wall time is sufficient for this reader.
    """
    application = _existing_database(root, "orchestrator.sqlite3")
    kernel = _existing_database(root, "kernel.sqlite3")
    reader = SimpleNamespace(db_path=application,
                             kernel=SimpleNamespace(db_path=kernel, current_time=time.time),
                             runtime=None)
    return inspect_work_availability(
        reader, run_id, sample_limit=sample_limit,
        effect_scan_limit=effect_scan_limit,
    )


def _runner_health(*, driver_alive: bool, run_state: str, now: float,
                   snapshot: Mapping[str, Any] | None = None,
                   worker_activity: bool | None = None,
                   namespace_match: bool | None = None) -> dict[str, Any]:
    heartbeat_age = None
    reported_status = None
    if isinstance(snapshot, Mapping):
        reported_status = snapshot.get("status")
        timestamp = snapshot.get("timestamp")
        if (not isinstance(timestamp, bool)
                and isinstance(timestamp, (int, float))
                and math.isfinite(timestamp)):
            heartbeat_age = max(0.0, now - float(timestamp))
    if run_state in TERMINAL:
        status, reason = "stopped", "sdk_run_terminal"
    elif reported_status in {"failed", "stopped"}:
        status, reason = "interrupted", "driver_reported_" + reported_status
    elif not driver_alive and namespace_match is False:
        # PID absence in another namespace says nothing about the driver.
        # Only the independent heartbeat can establish responsiveness here.
        if heartbeat_age is not None and heartbeat_age > DRIVER_HEARTBEAT_TIMEOUT_SECONDS:
            status, reason = "unresponsive", "driver_heartbeat_stale_pid_namespace_unverified"
        else:
            status, reason = "uncertain", "driver_pid_namespace_unverified"
    elif (not driver_alive and reported_status == "running"
          and heartbeat_age is not None
          and heartbeat_age <= DRIVER_HEARTBEAT_TIMEOUT_SECONDS):
        # A separate PID namespace may not expose the driver's /proc entry.
        # Its recent heartbeat is conflicting evidence, not proof of health
        # or proof of exit. Reclassify only after that evidence becomes stale.
        status, reason = "uncertain", "recent_heartbeat_process_not_visible"
    elif not driver_alive:
        status, reason = "interrupted", "driver_process_not_alive"
    elif (heartbeat_age is not None
          and heartbeat_age > DRIVER_HEARTBEAT_TIMEOUT_SECONDS):
        status, reason = "unresponsive", "driver_heartbeat_stale"
    else:
        status, reason = "healthy", "driver_process_alive"
    return {
        "status": status, "reason": reason,
        "driver_alive": driver_alive,
        "reported_status": reported_status,
        "heartbeat_age_seconds": heartbeat_age,
        "heartbeat_timeout_seconds": DRIVER_HEARTBEAT_TIMEOUT_SECONDS,
        "worker_activity_observed": worker_activity,
        "pid_namespace_match": namespace_match,
    }


def _recent_shell_receipt(root: Path, execution_id: str, now: float) -> float | None:
    """Observe bounded host-owned tool receipts, never project artifact activity as success."""
    if re.fullmatch(r"[A-Za-z0-9_.:-]{1,240}", execution_id) is None:
        return None
    directory = root / "artifacts" / "executions" / execution_id / "opencode-shell"
    try:
        if (directory.is_symlink() or not directory.is_dir()
                or directory.resolve(strict=True) != directory.absolute()):
            return None
        directory_modified = directory.stat().st_mtime
        if not math.isfinite(directory_modified) or not 0 <= now - directory_modified <= 60:
            return None
        session = directory / "session.json"
        session_stat = session.stat()
        if (session.is_symlink() or not session.is_file()
                or session_stat.st_nlink != 1 or session_stat.st_size > 4096
                or json.loads(session.read_text(encoding="utf-8")).get("command_id") != execution_id):
            return None
        latest_path = directory / "latest-receipt.json"
        if latest_path.is_file() and not latest_path.is_symlink():
            latest_stat = latest_path.stat()
            if latest_stat.st_nlink == 1 and latest_stat.st_size <= 4096:
                latest_record = json.loads(latest_path.read_text(encoding="utf-8"))
                name = latest_record.get("receipt") if isinstance(latest_record, Mapping) else None
                if (isinstance(latest_record, Mapping)
                        and latest_record.get("schema_version") == 1
                        and latest_record.get("command_id") == execution_id
                        and isinstance(name, str)
                        and re.fullmatch(r"[0-9a-f]{32}\.json", name)):
                    receipt = directory / name
                    if receipt.is_file() and not receipt.is_symlink():
                        receipt_stat = receipt.stat()
                        modified = receipt_stat.st_mtime
                        if (receipt_stat.st_nlink == 1 and math.isfinite(modified)
                                and 0 <= now - modified <= 60):
                            return modified
        latest = None
        with os.scandir(directory) as entries:
            for index, entry in enumerate(entries):
                if index >= 256:
                    break
                if (re.fullmatch(r"[0-9a-f]{32}\.json", entry.name) is None
                        or not entry.is_file(follow_symlinks=False)):
                    continue
                receipt_stat = entry.stat(follow_symlinks=False)
                if receipt_stat.st_nlink != 1:
                    continue
                modified = receipt_stat.st_mtime
                if math.isfinite(modified) and 0 <= now - modified <= 60:
                    latest = modified if latest is None else max(latest, modified)
        return latest
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _execution_progress_projection(root: Path, rows: Mapping[str, Any], now: float) -> list[dict[str, Any]]:
    """Read only the bounded markers for current SDK leases."""
    result = []
    for row in (rows.get("summaries") or ())[:SAMPLE_LIMIT]:
        if not isinstance(row, Mapping) or row.get("state") not in {"running", "leased"}:
            continue
        execution_id = row.get("execution_id")
        if not isinstance(execution_id, str):
            continue
        marker = read_execution_progress(root, execution_id)
        if marker is None:
            result.append({
                "execution_id": execution_id, "task_id": row.get("task_id"),
                "phase": "startup_unconfirmed", "state": "unconfirmed",
                "phase_age_seconds": None, "last_progress_at": None,
            })
            continue
        application_attempt = row.get("application_attempt")
        if (marker.get("task_id") != row.get("task_id")
                or isinstance(application_attempt, bool)
                or not isinstance(application_attempt, int) or application_attempt < 0
                or marker.get("application_attempt") != application_attempt + 1):
            result.append({
                "execution_id": execution_id, "task_id": row.get("task_id"),
                "phase": "progress_identity_mismatch", "state": "unconfirmed",
                "phase_age_seconds": None, "last_progress_at": None,
            })
            continue
        if (marker.get("kernel_attempt") is not None
                and ((row.get("attempt") is not None
                      and marker.get("kernel_attempt") != row.get("attempt"))
                     or marker.get("fence") != row.get("fence"))):
            result.append({
                "execution_id": execution_id, "task_id": row.get("task_id"),
                "phase": "progress_fence_mismatch", "state": "unconfirmed",
                "phase_age_seconds": None, "last_progress_at": None,
            })
            continue
        age = max(0.0, now - float(marker["last_progress_at"]))
        phase = marker["phase"]
        if phase in {"planned", "dispatched"} and age >= 30:
            visible_phase, state = "startup_unconfirmed", "unconfirmed"
        elif age >= 60:
            visible_phase, state = phase, "no_observed_progress"
        else:
            visible_phase, state = phase, "current"
        shell_activity_at = _recent_shell_receipt(root, execution_id, now)
        if state == "no_observed_progress" and shell_activity_at is not None:
            state = "tool_activity_observed"
        current = {
            "execution_id": execution_id,
            "task_id": marker.get("task_id", row.get("task_id")),
            "stage_id": marker.get("stage_id"),
            "application_attempt": marker.get("application_attempt"),
            "kernel_attempt": marker.get("kernel_attempt"),
            "fence": marker.get("fence"),
            "phase": visible_phase, "state": state,
            "phase_age_seconds": int(max(0.0, now - float(marker.get("phase_started_at", now)))),
            "last_progress_at": marker.get("last_progress_at"),
            "worker_pid": marker.get("worker_pid"),
            "worker_birth": marker.get("worker_birth"),
        }
        if shell_activity_at is not None:
            current["last_shell_receipt_at"] = shell_activity_at
        wait = marker.get("wait")
        if isinstance(wait, Mapping):
            current["wait"] = dict(wait)
        result.append(current)
    return result


def project_status(report: Any, *, driver_alive: bool, coder_seen: bool,
                   now: float, monitor_pid: int | None = None,
                   progress: Mapping[str, Any] | None = None,
                   progress_delta: Mapping[str, Any] | None = None,
                   budget_extension: Mapping[str, Any] | None = None,
                   stalled_seconds: int | float = 0,
                   acceptance: Mapping[str, Any] | None = None,
                   driver_snapshot: Mapping[str, Any] | None = None,
                   execution_progress: list[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Project SDK execution, runner health, and acceptance independently."""
    rows = report.to_dict()
    samples = rows.get("summaries") or []
    coder_seen = coder_seen or any(
        str(row.get("task_id", "")).split(".", 1)[0] in CODER_STAGES
        for row in samples[:SAMPLE_LIMIT] if isinstance(row, dict)
    )
    waits = rows.get("open_waits", 0)
    recovery = rows.get("recovery_required", 0)
    if rows["run_state"] in TERMINAL:
        execution_phase = "terminal"
    elif recovery or waits:
        execution_phase = "recovery_wait" if recovery else "application_wait"
    elif rows.get("active_leases", 0):
        execution_phase = "executing"
    elif (rows.get("queued_ready", 0) or rows.get("future_retries", 0)
          or rows.get("pending_commands", 0) or rows.get("pending_result_sync")
          or rows.get("pending_result_delivery", 0)):
        execution_phase = "pending"
    else:
        execution_phase = "idle"
    # ``phase`` remains as a compatibility summary. New consumers use the
    # projections below and therefore cannot mistake a live SDK lease for a
    # healthy driver, or SDK completion for behavioral acceptance.
    # The SDK/driver projection is deliberately cheap and frequent.  Full
    # artifact progress collection is a separate diagnostic operation owned by
    # RunMonitor, never a reason to delay terminal or driver-loss detection.
    interval = LIGHTWEIGHT_POLL_SECONDS
    counts = {key: rows.get(key) for key in (
        "task_count", "active_leases", "queued_ready", "expired_leases",
        "future_retries", "pending_commands", "pending_result_sync",
        "pending_result_delivery", "open_waits", "recovery_required",
        "unknown_effects", "missing_executions")}
    execution = {
        "state": rows["run_state"], "phase": execution_phase,
        "revision": rows["run_revision"], "complete": bool(rows.get("complete")),
        "snapshot_consistency": rows.get("snapshot_consistency"),
        "reason_codes": list(rows.get("reason_codes") or ())[:20],
        "counts": counts,
    }
    namespace_match = driver_namespace_match(driver_snapshot)
    driver_alive = driver_alive and namespace_match is not False
    runner_health = _runner_health(
        driver_alive=driver_alive, run_state=rows["run_state"], now=now,
        snapshot=driver_snapshot,
        namespace_match=namespace_match,
        worker_activity=bool(rows.get("active_leases", 0)),
    )
    phase = ("driver_exited" if execution_phase == "idle"
             and runner_health["status"] == "interrupted"
             and rows["run_state"] not in TERMINAL else execution_phase)
    overall_status = ("terminal" if rows["run_state"] in TERMINAL
                      else "execution_interrupted"
                      if runner_health["status"] in {"interrupted", "unresponsive"}
                      else execution_phase)
    verification = progress.get("verification") if isinstance(progress, Mapping) else None
    verification = verification if isinstance(verification, Mapping) else {}
    acceptance_projection = {
        "status": "unverified",
        "source": "monitor_default",
        "authenticated_test_count": verification.get("authenticated_test_count", 0),
        "successful_runs": verification.get("successful_runs", 0),
        "failed_runs": verification.get("failed_runs", 0),
        "evidence_sha256": verification.get("evidence_sha256"),
    }
    if isinstance(acceptance, Mapping):
        # This is a projection of an explicitly supplied host record. The SDK
        # state and collected test counts never synthesize acceptance.
        supplied = acceptance.get("status", acceptance.get("acceptance_status"))
        if isinstance(supplied, str) and supplied:
            acceptance_projection["status"] = supplied[:64]
            acceptance_projection["source"] = str(acceptance.get("source", "host_record"))[:160]
    status = {
        "run_id": rows["run_id"], "revision": rows["run_revision"],
        "run_state": rows["run_state"], "phase": phase,
        "observed_at": now, "monitor_pid": os.getpid() if monitor_pid is None else monitor_pid,
        "driver_alive": driver_alive, "coder_seen": coder_seen,
        "next_poll_seconds": interval, "complete": bool(rows.get("complete")),
        "snapshot_consistency": rows.get("snapshot_consistency"),
        "reason_codes": list(rows.get("reason_codes") or ())[:20],
        "counts": counts,
        "overall_status": overall_status,
        "execution": execution,
        "runner_health": runner_health,
        "acceptance": acceptance_projection,
        "summaries_truncated": bool(rows.get("summaries_truncated")),
        "active_samples": [
            {key: str(row.get(key, ""))[:160]
             for key in ("task_id", "state", "execution_id")}
            for row in samples[:SAMPLE_LIMIT] if isinstance(row, dict)
        ],
        "execution_progress": [dict(item) for item in (execution_progress or ())[:SAMPLE_LIMIT]],
    }
    if progress is not None:
        status["progress_evidence"] = progress
    if progress_delta is not None:
        status["progress_delta"] = progress_delta
    status["stalled_seconds"] = max(0, stalled_seconds)
    if budget_extension is not None:
        status["budget_extension"] = budget_extension
    return status


def _signature(status: dict[str, Any]) -> str:
    keys = ("run_state", "phase", "revision", "driver_alive", "coder_seen", "counts",
            "execution", "runner_health", "acceptance", "diagnostic_pending",
            "diagnostic_request")
    value = {key: status[key] for key in keys}
    delta = status.get("progress_delta")
    if isinstance(delta, dict):
        value["progress_delta_signature"] = delta.get("signature")
    budget = status.get("budget_extension")
    if isinstance(budget, dict):
        value["budget_extension"] = {
            "eligible": budget.get("eligible"), "reason": budget.get("reason"),
            "additional_seconds": budget.get("additional_seconds"),
        }
    progress = status.get("execution_progress")
    if isinstance(progress, list):
        value["execution_progress_signature"] = []
        for row in progress:
            if not isinstance(row, dict):
                continue
            wait = row.get("wait")
            value["execution_progress_signature"].append({
                "execution_id": row.get("execution_id"),
                "phase": row.get("phase"), "state": row.get("state"),
                "wait_reason": wait.get("reason") if isinstance(wait, dict) else None,
            })
    return json.dumps(value, sort_keys=True)


class RunMonitor:
    """Stateful poller; run indefinitely after driver exit until SDK terminal."""

    def __init__(self, root: str | Path, run_id: str, output_dir: str | Path,
                 *, driver_pid: int | None = None, driver_birth: str | None = None,
                 coder_seen: bool = False,
                 inspect: Callable[[Path, str], Any] = read_run_availability,
                 clock: Callable[[], float] = time.time,
                 diagnostic_interval_seconds: float | None = None):
        source = Path(root).absolute()
        if source.is_symlink() or source.resolve(strict=True) != source or not source.is_dir():
            raise ValueError("run directory must be a real directory")
        self.root = source
        if not isinstance(run_id, str) or not run_id or len(run_id) > 150:
            raise ValueError("run_id must be a bounded nonempty string")
        self.run_id = run_id
        self.workflow_version = None
        run_input = source / "run.json"
        if run_input.is_file() and not run_input.is_symlink() and run_input.stat().st_size <= 1024 * 1024:
            try:
                definition = json.loads(run_input.read_text(encoding="utf-8")).get("definition", {})
                version = definition.get("workflow_version") if isinstance(definition, Mapping) else None
                if type(version) is int and version > 0:
                    self.workflow_version = version
            except (OSError, ValueError, TypeError):
                pass
        self.output_dir = Path(output_dir).absolute()
        if self.output_dir.resolve() != self.output_dir:
            raise ValueError("monitor output must not resolve through a symlink")
        self.driver_pid = driver_pid
        self.driver_birth = driver_birth if driver_birth is not None else (
            process_birth(driver_pid) if driver_pid is not None else None)
        self.coder_seen = coder_seen
        self.inspect = inspect
        self.clock = clock
        self.diagnostic_interval_seconds = _validated_diagnostic_interval(
            diagnostic_interval_seconds)
        self._previous_signature: str | None = None
        self._events: list[dict[str, Any]] = []
        self._last_event_at: float | None = None
        self._previous_progress: dict[str, Any] | None = None
        self._last_tangible_progress_at: float | None = None
        self._stalled_since: float | None = None
        self._last_diagnostic_at: float | None = None
        self._last_diagnostic_reason: str | None = None
        self._last_progress: dict[str, Any] | None = None
        self._last_progress_delta: dict[str, Any] | None = None
        self._last_budget_extension: dict[str, Any] | None = None
        self._diagnostic_lock = threading.Lock()
        self._diagnostic_thread: threading.Thread | None = None
        self._diagnostic_result: tuple[str, float, dict[str, Any]] | None = None
        self._diagnostic_inflight_reason: str | None = None

    def _restore(self) -> None:
        status_path = self.output_dir / "monitor-status.json"
        if status_path.is_file() and not status_path.is_symlink():
            try:
                with status_path.open("rb") as stream:
                    previous = json.loads(stream.read(1024 * 1024 + 1))
                if previous.get("run_id") == self.run_id:
                    self.coder_seen |= previous.get("coder_seen") is True
                    progress = previous.get("progress_evidence")
                    if isinstance(progress, dict):
                        # Old status files contain only the public projection;
                        # the private issue/candidate maps are restored below.
                        self._previous_progress = {"public": progress}
                        self._last_progress = progress
                    progress_delta = previous.get("progress_delta")
                    if isinstance(progress_delta, dict):
                        self._last_progress_delta = progress_delta
                    budget_extension = previous.get("budget_extension")
                    if isinstance(budget_extension, dict):
                        self._last_budget_extension = budget_extension
                    for name in ("last_tangible_progress_at", "stalled_since"):
                        value = previous.get(name)
                        if isinstance(value, (int, float)) and not isinstance(value, bool):
                            setattr(self, "_" + name, float(value))
                    diagnostic_at = previous.get("diagnostic_at")
                    if (isinstance(diagnostic_at, (int, float))
                            and not isinstance(diagnostic_at, bool)):
                        self._last_diagnostic_at = float(diagnostic_at)
                    reason = previous.get("diagnostic_reason")
                    if isinstance(reason, str):
                        self._last_diagnostic_reason = reason
            except (OSError, ValueError, TypeError):
                pass
        events_path = self.output_dir / "monitor-events.json"
        if events_path.is_file() and not events_path.is_symlink():
            try:
                with events_path.open("rb") as stream:
                    raw = stream.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    return
                prior = json.loads(raw)
                if isinstance(prior, list):
                    self._events = [item for item in prior[-MAX_EVENTS:]
                                    if isinstance(item, dict) and item.get("run_id") == self.run_id]
            except (OSError, ValueError, TypeError):
                pass
        progress_path = self.output_dir / "monitor-progress.json"
        if progress_path.is_file() and not progress_path.is_symlink():
            try:
                with progress_path.open("rb") as stream:
                    progress = json.loads(stream.read(4 * 1024 * 1024 + 1))
                if isinstance(progress, dict) and progress.get("run_id") == self.run_id:
                    evidence = progress.get("evidence")
                    if isinstance(evidence, dict):
                        self._previous_progress = evidence
            except (OSError, ValueError, TypeError):
                pass

    def _run_budget(self) -> tuple[float, float, int, int, str] | None:
        """Read the budget header and any durable progress policy.

        A continuation rewrites ``run.json`` with a fresh segment start time.
        When the host has installed a progress policy, its original start and
        absolute cap remain authoritative so a successor cannot silently earn
        another twelve-hour window.
        """
        path = self.root / "run.json"
        if path.is_symlink() or not path.is_file():
            return None
        try:
            if path.stat().st_size > 1024 * 1024:
                return None
            value = json.loads(path.read_text(encoding="utf-8"))
            started = value.get("started_at")
            deadline = value.get("deadline_epoch")
            if (isinstance(started, bool) or not isinstance(started, (int, float))
                    or not isinstance(deadline, (int, float))):
                return None
            initial = INITIAL_BUDGET_SECONDS
            maximum = MAX_BUDGET_SECONDS
            source = "run_header"
            policies = self.root / "artifacts" / "progress-continuations"
            if policies.is_dir() and not policies.is_symlink():
                for directory in sorted(policies.iterdir(), key=lambda item: item.name):
                    candidate = directory / "policy.json"
                    if (directory.is_symlink() or candidate.is_symlink()
                            or not candidate.is_file()):
                        continue
                    if candidate.stat().st_size > 1024 * 1024:
                        continue
                    policy = json.loads(candidate.read_text(encoding="utf-8"))
                    request = policy.get("request") if isinstance(policy, dict) else None
                    extension = policy.get("extension") if isinstance(policy, dict) else None
                    ids = set()
                    if isinstance(request, dict):
                        ids.add(request.get("next_run_id"))
                    if isinstance(extension, dict):
                        ids.add(extension.get("next_run_id"))
                    if self.run_id not in ids:
                        continue
                    policy_started = policy.get("started_at")
                    if (isinstance(policy_started, (int, float))
                            and not isinstance(policy_started, bool)):
                        started = policy_started
                    if isinstance(request, dict):
                        if type(request.get("initial_seconds")) is int and request["initial_seconds"] > 0:
                            initial = request["initial_seconds"]
                        if type(request.get("maximum_seconds")) is int and request["maximum_seconds"] >= initial:
                            maximum = request["maximum_seconds"]
                    source = "progress_policy"
                    break
            return float(started), float(deadline), initial, maximum, source
        except (OSError, UnicodeError, ValueError, TypeError):
            return None

    def _collect_progress_status(self, now: float,
                                 previous_progress: dict[str, Any] | None,
                                 stalled_since: float | None) -> dict[str, Any]:
        """Collect the expensive evidence without mutating monitor state.

        This function is safe to run in the daemon diagnostic thread. The
        main monitor thread publishes the returned snapshot only after the
        next lightweight poll has already observed the SDK and driver.
        """
        try:
            evidence = collect_progress_evidence(self.root)
            delta = compare_progress_evidence(previous_progress, evidence)
        except Exception as exc:
            # Progress evidence is diagnostic input.  An unreadable artifact
            # must not be turned into a false extension or false success.
            return {
                "progress": {"observation_error": type(exc).__name__},
                "progress_delta": None,
                "budget_extension": {
                    "eligible": False, "reason": "progress_observation_error",
                    "additional_seconds": 0,
                },
                "stalled_seconds": 0,
                "stalled_since": stalled_since,
                "evidence": None,
            }
        if delta.get("tangible_progress") is True:
            next_stalled_since = None
            stalled = 0
        elif stalled_since is None:
            next_stalled_since = now
            stalled = 0
        else:
            next_stalled_since = stalled_since
            stalled = max(0, now - stalled_since)
        budget_context = self._run_budget()
        budget = None
        if budget_context is not None:
            started, deadline, initial, maximum, source = budget_context
            try:
                budget = budget_extension_decision(
                    delta, now=now, started_at=started,
                    current_deadline=deadline,
                    initial_seconds=initial,
                    maximum_seconds=maximum,
                )
                budget["source"] = source
            except ValueError:
                budget = {"eligible": False, "reason": "invalid_budget_header",
                          "additional_seconds": 0}
        public = evidence.get("public") if isinstance(evidence.get("public"), dict) else {}
        return {
            "progress": public,
            "progress_delta": delta,
            "budget_extension": budget,
            "stalled_seconds": stalled,
            "stalled_since": next_stalled_since,
            "evidence": evidence,
        }

    def _apply_diagnostic(self, started_at: float, reason: str,
                          result: dict[str, Any]) -> None:
        self._last_progress = result.get("progress")
        self._last_progress_delta = result.get("progress_delta")
        self._last_budget_extension = result.get("budget_extension")
        self._stalled_since = result.get("stalled_since")
        delta = self._last_progress_delta
        if isinstance(delta, dict) and delta.get("tangible_progress") is True:
            self._last_tangible_progress_at = started_at
        evidence = result.get("evidence")
        if isinstance(evidence, dict):
            self._previous_progress = evidence
        self._last_diagnostic_at = started_at
        self._last_diagnostic_reason = reason

    def _diagnostic_worker(self, previous_progress: dict[str, Any] | None,
                           stalled_since: float | None, started_at: float,
                           reason: str) -> None:
        result = self._collect_progress_status(started_at, previous_progress, stalled_since)
        with self._diagnostic_lock:
            self._diagnostic_result = (reason, started_at, result)

    def _start_diagnostic(self, reason: str, started_at: float) -> bool:
        with self._diagnostic_lock:
            if self._diagnostic_thread is not None and self._diagnostic_thread.is_alive():
                return False
            if self._diagnostic_result is not None:
                return False
            self._diagnostic_inflight_reason = reason
            thread = threading.Thread(
                target=self._diagnostic_worker,
                args=(copy.deepcopy(self._previous_progress), self._stalled_since,
                      started_at, reason),
                name="modport-monitor-diagnostic",
                daemon=True,
            )
            self._diagnostic_thread = thread
            thread.start()
            return True

    def _finish_diagnostic(self) -> bool:
        with self._diagnostic_lock:
            result = self._diagnostic_result
            if result is None:
                return False
            self._diagnostic_result = None
            self._diagnostic_thread = None
            self._diagnostic_inflight_reason = None
        reason, started_at, diagnostic = result
        self._apply_diagnostic(started_at, reason, diagnostic)
        return True

    def _driver_snapshot(self):
        # A resumed driver publishes its own identity. The monitor survives
        # the prior driver and follows this authenticated local Run binding.
        driver_path = self.output_dir / 'monitor-driver.json'
        driver_snapshot = None
        if driver_path.is_file() and not driver_path.is_symlink():
            try:
                with driver_path.open('rb') as stream:
                    driver = json.loads(stream.read(4097))
                if not isinstance(driver, dict):
                    return None
                if driver.get('run_id') == self.run_id and driver.get('run_dir') == str(self.root):
                    if type(driver.get('pid')) is int and isinstance(driver.get('birth'), str):
                        self.driver_pid, self.driver_birth = driver['pid'], driver['birth']
                        driver_snapshot = driver
            except (OSError, ValueError, TypeError):
                pass
        return driver_snapshot

    def _diagnostic_reason(self, report: Any, *, alive: bool,
                           driver_snapshot: Mapping[str, Any] | None,
                           now: float, force: bool,
                           execution_progress: list[Mapping[str, Any]] | None = None) -> str | None:
        with self._diagnostic_lock:
            diagnostic_inflight = self._diagnostic_thread is not None and self._diagnostic_thread.is_alive()
        if diagnostic_inflight:
            return None
        if force or self._last_diagnostic_at is None:
            return "explicit" if force else "initial_baseline"
        if (self.diagnostic_interval_seconds is not None
                and now - self._last_diagnostic_at >= self.diagnostic_interval_seconds):
            return "scheduled_diagnostic"
        rows = report.to_dict()
        health = _runner_health(
            driver_alive=alive, run_state=rows.get("run_state", "unknown"), now=now,
            snapshot=driver_snapshot, worker_activity=bool(rows.get("active_leases", 0)),
            namespace_match=driver_namespace_match(driver_snapshot),
        )
        reasons = []
        if health["status"] != "healthy":
            reasons.append("runner_" + health["status"])
        if rows.get("run_state") in TERMINAL:
            reasons.append("sdk_terminal")
        if rows.get("recovery_required") or rows.get("open_waits"):
            reasons.append("sdk_recovery_or_wait")
        progress = execution_progress or ()
        if any(item.get("phase") in {
                "startup_unconfirmed", "progress_identity_mismatch",
                "progress_fence_mismatch"} for item in progress):
            reasons.append("execution_startup_unconfirmed")
        elif any(item.get("state") == "no_observed_progress" for item in progress):
            reasons.append("execution_no_observed_progress")
        if (rows.get("run_state") == "running"
              and not rows.get("active_leases")
              and not rows.get("queued_ready")
              and not rows.get("pending_commands")
              and not rows.get("pending_result_sync")
              and not rows.get("pending_result_delivery")):
            reasons.append("sdk_idle_while_running")
        reason = "+".join(reasons) or None
        if reason is None:
            self._last_diagnostic_reason = None
            return None
        if reason == self._last_diagnostic_reason:
            return None
        return reason

    def poll(self, *, diagnostic: bool = False) -> dict[str, Any]:
        now = self.clock()
        self._finish_diagnostic()
        driver_snapshot = self._driver_snapshot()
        alive = (driver_namespace_match(driver_snapshot) is not False
                 and process_alive(self.driver_pid, self.driver_birth))
        progress = self._last_progress
        progress_delta = self._last_progress_delta
        budget_extension = self._last_budget_extension
        stalled_seconds = (0 if self._stalled_since is None
                           else max(0, now - self._stalled_since))
        requested_diagnostic: str | None = None
        try:
            report = self.inspect(self.root, self.run_id)
            rows = report.to_dict()
            # Stage markers were introduced with v22. Older Runs retain their
            # frozen execution contract and must not be labelled unconfirmed
            # merely because they predate this telemetry.
            execution_progress = (
                _execution_progress_projection(self.root, rows, now)
                if self.workflow_version is not None and self.workflow_version >= 22
                else []
            )
            diagnostic_reason = self._diagnostic_reason(
                report, alive=alive, driver_snapshot=driver_snapshot,
                now=now, force=diagnostic,
                execution_progress=execution_progress)
            if diagnostic_reason is not None:
                requested_diagnostic = diagnostic_reason
            status = project_status(report, driver_alive=alive,
                                    coder_seen=self.coder_seen, now=now,
                                    progress=progress, progress_delta=progress_delta,
                                    budget_extension=budget_extension,
                                    stalled_seconds=stalled_seconds,
                                    driver_snapshot=driver_snapshot,
                                    execution_progress=execution_progress)
        except Exception as exc:
            diagnostic_reason = "observation_error"
            if diagnostic_reason != self._last_diagnostic_reason:
                requested_diagnostic = diagnostic_reason
            runner_health = _runner_health(
                driver_alive=alive, run_state="unknown", now=now,
                snapshot=driver_snapshot, worker_activity=None,
                namespace_match=driver_namespace_match(driver_snapshot))
            status = {
                "run_id": self.run_id, "observed_at": now,
                "monitor_pid": os.getpid(), "driver_alive": alive,
                "coder_seen": self.coder_seen, "phase": "observation_error",
                "next_poll_seconds": LIGHTWEIGHT_POLL_SECONDS,
                "error_type": type(exc).__name__,
                "error": str(exc)[:300],
                "progress_evidence": progress,
                "execution_progress": [],
                "progress_delta": progress_delta,
                "budget_extension": budget_extension,
                "stalled_seconds": stalled_seconds,
                "overall_status": ("execution_interrupted"
                                   if runner_health["status"] in {
                                       "interrupted", "unresponsive"}
                                   else "observation_error"),
                "execution": {
                    "state": "unknown", "phase": "observation_error",
                    "complete": False,
                },
                "runner_health": runner_health,
                "acceptance": {
                    "status": "unverified", "source": "monitor_default",
                },
            }
        if requested_diagnostic is not None:
            self._start_diagnostic(requested_diagnostic, now)
        with self._diagnostic_lock:
            diagnostic_pending = (
                self._diagnostic_thread is not None
                and self._diagnostic_thread.is_alive()
            )
            diagnostic_request = self._diagnostic_inflight_reason
        status["last_tangible_progress_at"] = self._last_tangible_progress_at
        status["workflow_version"] = self.workflow_version
        status["stalled_since"] = self._stalled_since
        status["diagnostic_at"] = self._last_diagnostic_at
        status["diagnostic_reason"] = self._last_diagnostic_reason
        status["diagnostic_pending"] = diagnostic_pending
        status["diagnostic_request"] = diagnostic_request
        self.coder_seen = bool(status["coder_seen"])
        atomic_json(self.output_dir / "monitor-status.json", status)
        if isinstance(self._previous_progress, dict):
            atomic_json(self.output_dir / "monitor-progress.json", {
                "schema_version": 1, "run_id": self.run_id,
                "evidence": self._previous_progress,
            })
        signature = _signature(status) if "run_state" in status else json.dumps(
            {"phase": status["phase"], "driver_alive": alive, "error_type": status["error_type"]},
            sort_keys=True)
        # Status is refreshed on every lightweight poll, but the event stream
        # contains only SDK, health, or diagnostic changes.
        if signature != self._previous_signature:
            event = {key: value for key, value in status.items()
                     if key not in {"active_samples", "reason_codes", "error",
                                    "progress_evidence"}}
            event["event"] = "changed"
            self._events = [*self._events[-(MAX_EVENTS - 1):], event]
            atomic_json(self.output_dir / "monitor-events.json", self._events)
            self._last_event_at = now
        self._previous_signature = signature
        return status

    def run(self, *, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if self.output_dir.is_symlink() or not self.output_dir.is_dir():
            raise ValueError("monitor output must be a real directory")
        lock_path = self.output_dir / "monitor.lock"
        if lock_path.is_symlink():
            raise ValueError("monitor lock must not be a symlink")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._restore()
            while True:
                status = self.poll()
                if status.get("run_state") in TERMINAL:
                    return status
                # Keep SDK terminal and driver-loss detection bounded by the
                # lightweight poll interval.  Full artifact scans are only
                # triggered by _diagnostic_reason or an explicit interval.
                sleep(status['next_poll_seconds'])
        finally:
            os.close(descriptor)


def start_monitor(root: Path, run_id: str,
                  *, diagnostic_interval_seconds: float | None = None) -> None:
    """Attach a detached observer to each production execute/resume call."""
    root = Path(root).resolve()
    if diagnostic_interval_seconds is None:
        configured = os.environ.get("MODPORT_MONITOR_DIAGNOSTIC_INTERVAL")
        if configured:
            try:
                diagnostic_interval_seconds = float(configured)
            except ValueError as exc:
                raise ValueError("invalid MODPORT_MONITOR_DIAGNOSTIC_INTERVAL") from exc
    diagnostic_interval_seconds = _validated_diagnostic_interval(diagnostic_interval_seconds)
    output = root / 'artifacts/monitor'
    if output.resolve() != output.absolute():
        raise ValueError('monitor directory must not resolve through a symlink')
    output.mkdir(parents=True, exist_ok=True)
    birth = process_birth(os.getpid())
    # DriverLease owns the complete heartbeat record.  Replacing it with the
    # historical identity-only shape would temporarily erase status and
    # freshness.  Legacy callers without a live lease retain the old publish
    # behavior, including takeover after a killed driver.
    from .runner import DriverHealthError, read_driver_health
    try:
        health = read_driver_health(root)
    except (DriverHealthError, OSError, ValueError):
        health = None
    namespace_match = driver_namespace_match(health)
    heartbeat_recent = bool(
        health and isinstance(health.get('timestamp'), (int, float))
        and not isinstance(health['timestamp'], bool)
        and math.isfinite(health['timestamp'])
        and time.time() - health['timestamp'] <= DRIVER_HEARTBEAT_TIMEOUT_SECONDS)
    live_lease = bool(
        health
        and health.get('schema_version') == 1
        and health.get('status') == 'running'
        and (namespace_match is False or heartbeat_recent
             or process_alive(health.get('pid'), health.get('birth')))
    )
    if not live_lease:
        atomic_json(output / 'monitor-driver.json', {
            'run_id': run_id, 'run_dir': str(root), 'pid': os.getpid(),
            'birth': birth,
        })
    descriptor = os.open(output / 'monitor.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
    finally:
        os.close(descriptor)
    command = [sys.executable, '-m', 'modport.run_monitor',
        '--run-dir', str(root), '--run-id', run_id, '--output-dir', str(output),
        '--driver-pid', str(os.getpid()), '--driver-birth', birth or 'unknown']
    if diagnostic_interval_seconds is not None:
        command += ['--diagnostic-interval', str(diagnostic_interval_seconds)]
    subprocess.Popen(command,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, close_fds=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--driver-pid", type=int)
    parser.add_argument("--driver-birth", type=str)
    parser.add_argument("--coder-seen", action="store_true")
    parser.add_argument(
        "--diagnostic-interval", type=float, default=None,
        help=("optional interval in seconds for full progress diagnostics; disabled by default "
              f"(for example, {OPTIONAL_DIAGNOSTIC_INTERVAL_SECONDS})"),
    )
    arguments = parser.parse_args(argv)
    sdk_release()
    monitor = RunMonitor(
        arguments.run_dir, arguments.run_id, arguments.output_dir,
        driver_pid=arguments.driver_pid, driver_birth=arguments.driver_birth,
        coder_seen=arguments.coder_seen,
        diagnostic_interval_seconds=arguments.diagnostic_interval,
    )
    monitor.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
