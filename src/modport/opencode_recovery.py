"""Cleanup-only reconciliation for a settled OpenCode coder attempt.

This module never changes SDK state and never starts a model.  It records the
settled SDK attempt, proves or refuses cleanup for the exact saved process
identity, and publishes an immutable host receipt.  Candidate collection is a
separate later step in :mod:`modport.partial_recovery`.
"""
from __future__ import annotations

from .workspace import git_probe

from hashlib import sha256
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
from typing import Any, Mapping

from .application_state_storage import hydrate_run_snapshot
from .contracts import OperationInput, OperationResult
from .evidence import atomic_json, digest, file_digest, verified_path, workspace_lock


_SETTLED = frozenset({"succeeded", "failed", "cancelled", "timed_out", "dead"})
_SCHEMA = "modport.opencode-cleanup-reconciliation.v1"


def _contained_root(root: Path) -> Path:
    candidate = Path(root).absolute()
    if candidate.is_symlink():
        raise ValueError("cleanup reconciliation requires a real Run directory")
    root = candidate.resolve()
    if not root.is_dir():
        raise ValueError("cleanup reconciliation requires a real Run directory")
    return root


def _attempt_directories(root: Path) -> list[Path]:
    """Read at most 64 receipt entries and reject every malformed entry."""
    if not root.exists() and not root.is_symlink():
        return []
    if root.is_symlink() or not root.is_dir() or root.resolve() != root.absolute():
        raise ValueError("cleanup receipt history directory is unsafe")
    entries: list[Path] = []
    with os.scandir(root) as scan:
        for entry in scan:
            if len(entries) >= 64:
                raise ValueError("cleanup receipt history exceeds the 64 attempt limit")
            path = Path(entry.path)
            if (not re.fullmatch(r"attempt-[0-9]{4}", entry.name)
                    or entry.is_symlink() or not entry.is_dir(follow_symlinks=False)):
                raise ValueError("prior cleanup receipt history contains a malformed entry")
            entries.append(path)
    return sorted(entries)


def _proc_mount_matches_namespace(namespace: str) -> bool:
    """Refuse to infer absence from a procfs mounted for another PID namespace."""
    try:
        return (os.readlink("/proc/self/ns/pid") == namespace
                and os.readlink("/proc/1/ns/pid") == namespace)
    except OSError:
        return False


def _read_stat(pid: int) -> dict[str, Any] | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return {"pid": pid, "state": fields[0], "ppid": int(fields[1]),
                "pgid": int(fields[2]), "sid": int(fields[3]),
                "start_ticks": int(fields[19]), "birth": f"{boot}:{fields[19]}"}
    except FileNotFoundError:
        return None
    except (OSError, ValueError, IndexError):
        raise ValueError("the saved OpenCode PID identity cannot be read in this namespace")


def _proc_identity(pid: int) -> dict[str, Any] | None:
    stat = _read_stat(pid)
    if stat is None:
        return None
    try:
        argv = [part.decode("utf-8", errors="strict") for part in
                Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if part]
        exe_path = f"/proc/{pid}/exe"
        executable_sha = sha256()
        executable_size = 0
        with open(exe_path, "rb") as executable:
            while True:
                chunk = executable.read(1024 * 1024)
                if not chunk:
                    break
                executable_size += len(chunk)
                if executable_size > 512 * 1024 * 1024:
                    raise ValueError("OpenCode executable exceeds the bounded identity check")
                executable_sha.update(chunk)
        return {**stat, "pid_namespace": os.readlink(f"/proc/{pid}/ns/pid"),
                "cwd": os.readlink(f"/proc/{pid}/cwd"),
                "argv": argv,
                "executable": os.readlink(exe_path).removesuffix(" (deleted)"),
                "executable_sha256": executable_sha.hexdigest()}
    except (FileNotFoundError, OSError, UnicodeError):
        # A zombie has no cwd or executable link; its saved birth, PGID and
        # session identity are still useful while its pidfd pins that PID.
        if stat["state"] == "Z":
            try:
                return {**stat, "pid_namespace": os.readlink(f"/proc/{pid}/ns/pid")}
            except OSError as exc:
                raise ValueError("the saved OpenCode PID namespace cannot be observed") from exc
        raise ValueError("the saved OpenCode process command or workspace cannot be observed")


def _group_observation(pgid: int, pid_namespace: str, *, maximum: int = 32768) -> dict[str, Any]:
    members: list[dict[str, Any]] = []
    errors: list[str] = []
    scanned = 0
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        entries = (item for item in Path("/proc").iterdir() if item.name.isdecimal())
        for entry in entries:
            scanned += 1
            if scanned > maximum:
                return {"members": members, "scan_complete": False,
                        "scanned_entries": maximum, "scan_errors": ["scan_limit"]}
            try:
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                member_pgid = int(fields[2])
                if member_pgid != pgid:
                    continue
                member_namespace = os.readlink(entry / "ns/pid")
                members.append({"pid": int(entry.name), "state": fields[0],
                    "ppid": int(fields[1]), "pgid": member_pgid,
                    "sid": int(fields[3]), "start_ticks": int(fields[19]),
                    "birth": f"{boot}:{fields[19]}",
                    "pid_namespace": member_namespace})
            except FileNotFoundError:
                # Process exit during a full /proc walk is a normal race.
                continue
            except (OSError, ValueError, IndexError) as exc:
                errors.append(type(exc).__name__)
                if len(errors) >= 8:
                    break
    except OSError as exc:
        errors.append(type(exc).__name__)
    members.sort(key=lambda item: item["pid"])
    return {"members": members, "scan_complete": not errors,
            "namespace_mismatch": any(item["pid_namespace"] != pid_namespace
                                      for item in members),
            "scanned_entries": scanned, "scan_errors": errors}


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError as exc:
        raise ValueError("the saved OpenCode process group cannot be inspected") from exc


def _same_launch(observed: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    if any(observed.get(key) != expected.get(source) for key, source in (
            ("pid", "pid"), ("birth", "birth"), ("pgid", "process_group_id"),
            ("sid", "session_id"), ("pid_namespace", "pid_namespace"))):
        return False
    if observed.get("state") == "Z":
        return True
    argv = observed.get("argv")
    encoded = json.dumps(argv, ensure_ascii=False, separators=(",", ":")).encode()
    return bool(
        isinstance(argv, list)
        and argv == expected.get("argv")
        and sha256(encoded).hexdigest() == expected.get("argv_sha256")
        and observed.get("cwd") == expected.get("cwd")
        and observed.get("executable") == expected.get("executable")
        and observed.get("executable_sha256") == expected.get("executable_sha256")
    )


def _wait_for_cleanup(expected: Mapping[str, Any], *, timeout: float = 5.0) -> dict[str, Any]:
    pid = expected.get("pid")
    pgid = expected.get("process_group_id")
    if (type(pid) is not int or pid <= 0 or pgid != pid
            or expected.get("session_id") != pid
            or not isinstance(expected.get("pid_namespace"), str)
            or not _proc_mount_matches_namespace(expected.get("pid_namespace"))):
        return {"cleanup_confirmed": False, "reason": "pid_namespace_or_group_identity_mismatch"}

    try:
        leader = _proc_identity(pid)
    except ValueError as exc:
        return {"cleanup_confirmed": False, "reason": str(exc)}

    if leader is None:
        # A missing leader is not authority to signal a numeric PGID.  It only
        # proves cleanup when the group is also absent and /proc was completely
        # scanned in the launch namespace.
        try:
            exists = _group_exists(pgid)
            observation = _group_observation(pgid, expected["pid_namespace"])
        except ValueError as exc:
            return {"cleanup_confirmed": False, "reason": str(exc)}
        gone = (not exists and observation["scan_complete"]
                and not observation["members"])
        return {"cleanup_confirmed": gone, "process_group_gone": gone,
                "group_observation": observation,
                "reason": "group_gone" if gone else "leader_birth_unavailable_group_present"}

    if not _same_launch(leader, expected):
        return {"cleanup_confirmed": False, "reason": "saved_pid_birth_or_launch_identity_mismatch",
                "observed_leader": {key: leader.get(key) for key in
                    ("pid", "birth", "state", "pgid", "sid", "pid_namespace")}}

    pidfd_open = getattr(os, "pidfd_open", None)
    if not callable(pidfd_open):
        return {"cleanup_confirmed": False, "reason": "pidfd_unavailable_for_race_safe_signal"}
    try:
        pidfd = pidfd_open(pid, 0)
    except OSError as exc:
        return {"cleanup_confirmed": False, "reason": "pidfd_open_failed",
                "error_type": type(exc).__name__}
    try:
        # Recheck the birth and launch immediately before signaling. Holding
        # pidfd prevents the numeric PID/PGID from being recycled in this
        # critical section.
        leader = _proc_identity(pid)
        if leader is None or not _same_launch(leader, expected):
            return {"cleanup_confirmed": False,
                    "reason": "saved_pid_identity_changed_before_signal"}
        try:
            before_signal = _group_observation(pgid, expected["pid_namespace"])
        except OSError as exc:
            return {"cleanup_confirmed": False,
                    "reason": "process_group_identity_scan_failed",
                    "error_type": type(exc).__name__}
        if (not before_signal["scan_complete"]
                or before_signal["namespace_mismatch"]
                or any(item["sid"] != pgid for item in before_signal["members"])):
            return {"cleanup_confirmed": False,
                    "reason": "process_group_identity_scan_incomplete",
                    "group_observation_before_signal": before_signal}
        if not any(item["pid"] == pid and item["birth"] == expected["birth"]
                   and item["pgid"] == pgid
                   and item["pid_namespace"] == expected["pid_namespace"]
                   for item in before_signal["members"]):
            return {"cleanup_confirmed": False,
                    "reason": "saved_process_missing_from_group_observation",
                    "group_observation_before_signal": before_signal}
        try:
            os.killpg(pgid, signal.SIGKILL)
            signal_sent = True
        except ProcessLookupError:
            signal_sent = False
        except OSError as exc:
            return {"cleanup_confirmed": False, "reason": "process_group_signal_failed",
                    "error_type": type(exc).__name__}

        deadline = time.monotonic() + max(0.0, timeout)
        previous_zombies = None
        zombie_observation = None
        while True:
            try:
                exists = _group_exists(pgid)
                observation = _group_observation(pgid, expected["pid_namespace"])
            except ValueError as exc:
                return {"cleanup_confirmed": False, "signal_sent": signal_sent,
                        "reason": str(exc)}
            members = observation["members"]
            if (not observation["scan_complete"]
                    or observation["namespace_mismatch"]
                    or any(item["sid"] != pgid for item in members)):
                return {"cleanup_confirmed": False, "signal_sent": signal_sent,
                        "reason": "process_group_scan_incomplete_or_cross_namespace",
                        "group_observation": observation}
            if not exists and not members:
                return {"cleanup_confirmed": True, "process_group_gone": True,
                        "signal_sent": signal_sent, "group_observation": observation,
                        "wait_seconds": round(timeout - max(0.0, deadline - time.monotonic()), 3)}
            # A fully observed zombie-only group cannot write.  Require the
            # exact PID/birth set twice with a grace interval, mirroring the
            # in-process cleanup proof while avoiding a false empty snapshot.
            zombies = (bool(members) and all(item["state"] == "Z" for item in members)
                       and all(item["sid"] == pgid for item in members))
            identities = tuple((item["pid"], item["birth"], item["pid_namespace"])
                               for item in members)
            if zombies and previous_zombies == identities:
                return {"cleanup_confirmed": True, "process_group_gone": False,
                        "group_quiescence_reason": "zombie_only",
                        "signal_sent": signal_sent, "group_observation": observation,
                        "wait_seconds": round(timeout - max(0.0, deadline - time.monotonic()), 3)}
            previous_zombies = identities if zombies else None
            if time.monotonic() >= deadline:
                return {"cleanup_confirmed": False, "process_group_gone": False,
                        "signal_sent": signal_sent, "group_observation": observation,
                        "reason": "process_group_survived_bounded_wait",
                        "wait_seconds": timeout}
            time.sleep(min(0.05 if zombies else 0.1,
                           max(0.0, deadline - time.monotonic())))
    finally:
        os.close(pidfd)


def _load_attempt(root: Path, sdk, run_id: str, command_id: str):
    raw = sdk.get_run(run_id)
    if not isinstance(raw, dict) or raw.get("run_id") != run_id:
        raise ValueError("public SDK get_run returned a different Run identity")
    state = hydrate_run_snapshot(root, raw)
    matches = []
    for task_key, task in state.get("tasks", {}).items():
        for index, attempt in enumerate(task.get("attempts", [])):
            command_envelope = attempt.get("command")
            payload = (command_envelope.get("payload")
                       if isinstance(command_envelope, Mapping) else None)
            if isinstance(payload, Mapping) and payload.get("command_id") == command_id:
                matches.append((task_key, index, attempt, payload))
    if len(matches) != 1:
        raise ValueError("SDK must contain exactly one attempt with the requested command ID")
    task_key, index, attempt, payload = matches[0]
    if attempt.get("state") not in _SETTLED:
        raise ValueError("OpenCode cleanup requires an already settled SDK attempt")
    raw_result = (attempt.get("result") or {}).get("value")
    if not isinstance(raw_result, Mapping):
        raise ValueError("settled SDK attempt has no immutable business result")
    command = OperationInput.from_dict(payload)
    result = OperationResult.from_dict(raw_result)
    result.validate_for(command)
    logical_run = state.get("input", {}).get("logical_run_id", run_id)
    if (state.get("run_id") != run_id or command.run_id != logical_run
            or command.command_id != command_id or command.task_id != task_key
            or command.stage_id != "coder" or not task_key.startswith("coder.g")):
        raise ValueError("settled SDK attempt command identity is not an OpenCode coder")
    if result.status == "completed":
        raise ValueError("cleanup reconciliation cannot reinterpret a completed SDK result")
    native = result.outputs.get("native_goal")
    if (not isinstance(native, Mapping)
            or native.get("cleanup_unconfirmed") is not True
            or native.get("producer_stopped") is True):
        raise ValueError("SDK result does not record an unconfirmed OpenCode cleanup")
    if command.options.get("workflow_version", 0) < 17:
        raise ValueError("OpenCode cleanup recovery is only defined for current workflow attempts")
    return state, task_key, index, attempt, command, result, raw_result


def _read_json_ref(root: Path, ref: Mapping[str, Any], label: str) -> tuple[dict[str, Any], Path]:
    path = verified_path(root, ref)
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError(f"{label} artifact exceeds the 16 MiB recovery limit")
    data = path.read_bytes()
    expected = ref.get("sha256")
    if not isinstance(expected, str) or sha256(data).hexdigest() != expected:
        raise ValueError(f"{label} artifact digest differs from the settled SDK result")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError(f"{label} artifact must be an object")
    return value, path


def validate_cleanup_source_evidence(root: Path, *, result: OperationResult,
                                     command: OperationInput,
                                     native: Mapping[str, Any],
                                     setup: Mapping[str, Any]) -> dict[str, Any]:
    """Require response and explicit no-capture lanes before cleanup recovery."""
    refs = result.outputs.get("artifact_refs", {})
    if not isinstance(refs, Mapping):
        raise ValueError("settled source result has no artifact reference map")
    model_ref = refs.get("native_goal_model_result")
    if not isinstance(model_ref, Mapping):
        raise ValueError("settled source result has no structured model-result artifact")
    model, _ = _read_json_ref(root, model_ref, "model result")
    native_model_ref = native.get("model_result_ref")
    if (not isinstance(native_model_ref, Mapping)
            or native_model_ref.get("path") != model_ref.get("path")
            or native_model_ref.get("sha256") != model_ref.get("sha256")
            or model.get("schema") != "modport.native-goal-model-result.v1"
            or model.get("command_id") != command.command_id
            or model.get("model_terminal_status") not in {
                "pending", "completed", "interrupted", "provider_error", "not_started"}
            or type(model.get("response_observed")) is not bool):
        raise ValueError("model-result artifact differs from the settled native goal state")
    capture_ref = refs.get("candidate_capture")
    if not isinstance(capture_ref, Mapping):
        raise ValueError("settled source result has no candidate-capture outcome artifact")
    capture, _ = _read_json_ref(root, capture_ref, "candidate capture")
    capture_record = capture.get("record")
    if (not isinstance(capture_record, Mapping)
            or capture.get("sha256") != digest(capture_record)
            or capture_record.get("schema") != "modport.candidate-capture.v1"
            or capture_record.get("status") != "not_captured"
            or capture_record.get("reason") != "opencode_process_tree_cleanup_unconfirmed"
            or capture_record.get("source_command_id") != command.command_id
            or capture_record.get("workspace") != setup.get("workspace")
            or capture_record.get("base_commit") != setup.get("base_commit")
            or capture_record.get("generation") != setup.get("generation")
            or capture_record.get("start_commit") != setup.get("start_commit")
            or capture_record.get("start_tree") != setup.get("start_tree")):
        raise ValueError("candidate-capture receipt does not record cleanup-blocked capture")
    return {"model_result_ref": dict(model_ref), "candidate_capture_ref": dict(capture_ref)}


def _setup_record(root: Path, command: OperationInput) -> tuple[dict[str, Any], Path, str]:
    path = root / "artifacts" / "executions" / command.command_id / "coder-setup.json"
    if path.is_symlink() or not path.is_file() or path.resolve() != path.absolute():
        raise ValueError("source coder setup record is missing or unsafe")
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("source coder setup record exceeds the 2 MiB recovery limit")
    raw = json.loads(path.read_text(encoding="utf-8"))
    record = raw.get("record") if isinstance(raw, dict) else None
    record_bytes = (json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
                    if isinstance(record, dict) else b"")
    if not isinstance(record, dict) or raw.get("sha256") != sha256(record_bytes).hexdigest():
        raise ValueError("source coder setup record digest mismatch")
    workspace_rel = command.options.get("workspace")
    workspace = root / workspace_rel if isinstance(workspace_rel, str) else None
    if (workspace is None or workspace.is_symlink() or not workspace.is_dir()
            or workspace.resolve() != workspace.absolute()
            or not workspace.is_relative_to(root)):
        raise ValueError("source coder workspace is missing, symlinked, or outside the Run")
    start = record.get("start_commit")
    tree = record.get("start_tree")
    if (record.get("command_id") != command.command_id
            or record.get("run_id") != command.run_id
            or record.get("task_id") != command.task_id
            or record.get("workspace") != workspace_rel
            or not isinstance(start, str) or len(start) != 40
            or not isinstance(tree, str) or len(tree) != 40):
        raise ValueError("source coder setup identity differs from the SDK command")
    probe = git_probe(["git", "-C", str(workspace), "rev-parse", start + "^{tree}"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=10, check=False,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent",
             "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
             "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1"})
    if probe.returncode or probe.stdout.strip() != tree:
        raise ValueError("source coder start tree is not present with its recorded identity")
    return record, workspace, file_digest(path)


def _write_envelope(path: Path, record: Mapping[str, Any]) -> dict[str, Any]:
    envelope = {"record": dict(record), "sha256": digest(record)}
    if path.exists() or path.is_symlink():
        if (path.is_symlink() or not path.is_file()
                or path.stat().st_size > 8 * 1024 * 1024
                or json.loads(path.read_text()) != envelope):
            raise ValueError("an existing cleanup receipt differs from this reconciliation")
    else:
        atomic_json(path, envelope)
    return envelope


def _receipt_ref(root: Path, path: Path, metadata: Mapping[str, Any]) -> dict[str, Any]:
    return {"path": path.relative_to(root).as_posix(), "sha256": file_digest(path),
            "media_type": "application/json", "metadata": dict(metadata)}


def _validate_binding(state, command, result, native, setup):
    workspace_rel = command.options.get("workspace")
    workspace = Path(command.run_dir) / workspace_rel
    task = command.payload.get("development_task")
    task_name = task.get("id") if isinstance(task, Mapping) else None
    group = command.payload.get("development_generation")
    if type(group) is not int:
        group = command.payload.get("development_task_generation")
    binding = native.get("opencode_cleanup_binding")
    if not isinstance(binding, Mapping):
        raise ValueError("native goal has no host-bound OpenCode cleanup identity")
    expected = {
        "run_id": command.run_id, "command_id": command.command_id,
        "task_id": command.task_id, "development_task_id": task_name,
        "workspace": workspace_rel, "workspace_path": str(workspace),
        "base_commit": setup["base_commit"], "generation": setup["generation"],
        "start_commit": setup["start_commit"], "start_tree": setup["start_tree"],
    }
    if dict(binding) != expected:
        raise ValueError("native goal cleanup binding differs from the coder setup record")
    if (native.get("command_id") != command.command_id
            or native.get("worktree") != str(workspace)
            or native.get("host_accepted") is True
            or result.status == "completed"):
        raise ValueError("native goal identity or result contradicts cleanup-only recovery")
    return expected


def _next_attempt(directory: Path, expected: Mapping[str, Any]) -> int:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("cleanup attempt directory is unsafe")
    paths = _attempt_directories(directory)
    for number, attempt_dir in enumerate(paths, start=1):
        if (attempt_dir.name != f"attempt-{number:04d}"
                or attempt_dir.is_symlink() or not attempt_dir.is_dir()
                or attempt_dir.resolve() != attempt_dir.absolute()):
            raise ValueError("prior cleanup receipt history is malformed")
        receipt = attempt_dir / "process-cleanup.json"
        if not receipt.exists() and not receipt.is_symlink() and number == len(paths):
            # A crash after creating the final attempt directory (or during
            # atomic_json's temporary write) has not published a receipt.
            # Reuse only that final number, leaving incomplete bytes intact.
            incomplete = []
            with os.scandir(attempt_dir) as scan:
                for entry in scan:
                    incomplete.append(entry)
                    if len(incomplete) > 64:
                        break
            if (len(incomplete) <= 64
                    and sum(entry.stat(follow_symlinks=False).st_size
                            for entry in incomplete) <= 16 * 1024 * 1024
                    and all(
                    entry.name.startswith(".modport-")
                    and not entry.is_symlink()
                    and entry.is_file(follow_symlinks=False)
                    and entry.stat(follow_symlinks=False).st_size <= 8 * 1024 * 1024
                    for entry in incomplete)):
                return number
            raise ValueError("last cleanup attempt has unsafe incomplete files")
        if (receipt.is_symlink() or not receipt.is_file()
                or receipt.resolve() != receipt.absolute()
                or receipt.stat().st_size > 8 * 1024 * 1024):
            raise ValueError("prior cleanup receipt is missing or oversized")
        envelope = json.loads(receipt.read_text(encoding="utf-8"))
        record = envelope.get("record") if isinstance(envelope, dict) else None
        if not isinstance(record, dict) or envelope.get("sha256") != digest(record):
            raise ValueError("prior cleanup receipt digest is invalid")
        for key in ("schema", "source_run_id", "source_run_revision", "source_task_id",
                    "source_attempt_index", "source_command_id", "source_result_sha256",
                    "process_identity", "binding", "coder_setup_sha256"):
            if record.get(key) != expected.get(key):
                raise ValueError("prior cleanup receipt identity differs from this SDK attempt")
        if record.get("attempt_number") != number:
            raise ValueError("prior cleanup receipt attempt number is inconsistent")
        if type(record.get("cleanup_confirmed")) is not bool:
            raise ValueError("prior cleanup receipt has no boolean cleanup outcome")
    return len(paths) + 1


def _settlement_record(path: Path, *, run_id: str, state: Mapping[str, Any],
                       task_key: str, attempt_index: int, attempt: Mapping[str, Any],
                       command: OperationInput, result: OperationResult,
                       raw_result: Mapping[str, Any]) -> dict[str, Any]:
    """Preserve the first settlement snapshot for this immutable SDK result."""
    source_revision = state.get("revision")
    if type(source_revision) is not int:
        raise ValueError("public SDK snapshot has no integer Run revision")
    expected = {
        "schema": "modport.opencode-sdk-settlement.v1",
        "run_id": run_id, "sdk_task_id": task_key,
        "attempt_index": attempt_index, "attempt_state": attempt.get("state"),
        "command_id": command.command_id,
        "command_sha256": digest(command.to_dict()),
        "result_status": result.status, "result_error_code": result.error_code,
        "result_sha256": digest(raw_result),
        "result_cleanup_unconfirmed": True,
    }
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("existing SDK settlement receipt is unsafe or oversized")
        envelope = json.loads(path.read_text(encoding="utf-8"))
        record = envelope.get("record") if isinstance(envelope, dict) else None
        if (not isinstance(record, dict) or envelope.get("sha256") != digest(record)
                or any(record.get(key) != value for key, value in expected.items())
                or type(record.get("run_revision")) is not int
                or record["run_revision"] > source_revision):
            raise ValueError("existing SDK settlement receipt differs from the settled source attempt")
        return record
    return {**expected, "run_revision": source_revision}


def _ensure_host_directory(root: Path, path: Path) -> None:
    if not path.is_relative_to(root):
        raise ValueError("cleanup receipt path escaped the Run directory")
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            mode = current.lstat().st_mode
        if current.is_symlink() or not current.is_dir():
            raise ValueError("cleanup receipt directory contains a symlink or non-directory")


def _reconcile_opencode_cleanup_locked(root: Path, *, sdk, run_id: str,
                                       command_id: str) -> dict[str, Any]:
    """Reconcile one settled OpenCode cleanup without model or SDK writes.

    ``sdk`` must be an already identity-checked public SDK reader (for example
    a disposable Orchestrator opened by the CLI after ``sdk-inspect``).  The
    function calls only ``sdk.get_run``.  It writes host-owned receipts under
    ``artifacts/opencode-recovery`` and returns an unconfirmed receipt as data
    so callers can display the exact blocker without mutating the failed SDK
    result.
    """
    root = _contained_root(root)
    if not isinstance(run_id, str) or not run_id or not isinstance(command_id, str) or not command_id:
        raise ValueError("run_id and command_id are required")
    state, task_key, attempt_index, attempt, command, result, raw_result = _load_attempt(
        root, sdk, run_id, command_id)
    if Path(command.run_dir).resolve() != root:
        raise ValueError("source SDK command points at a different Run directory")
    refs = result.outputs.get("artifact_refs", {})
    state_ref = refs.get("native_goal_state") if isinstance(refs, Mapping) else None
    if not isinstance(state_ref, Mapping):
        raise ValueError("settled source result has no native goal state artifact reference")
    native, native_path = _read_json_ref(root, state_ref, "native goal state")
    setup, workspace, setup_sha = _setup_record(root, command)
    binding = _validate_binding(state, command, result, native, setup)
    validate_cleanup_source_evidence(root, result=result, command=command,
                                     native=native, setup=setup)
    expected = native.get("owned_process_identity")
    if not isinstance(expected, Mapping):
        raise ValueError("native goal has no saved process identity for cleanup")
    required_identity = ("pid", "birth", "pid_namespace", "process_group_id", "session_id",
                         "cwd", "argv", "argv_sha256", "executable", "executable_sha256")
    if any(key not in expected for key in required_identity):
        raise ValueError("saved OpenCode process identity is incomplete")
    if (expected.get("cwd") != str(workspace)
            or expected.get("process_group_id") != expected.get("pid")
            or expected.get("session_id") != expected.get("pid")):
        raise ValueError("saved OpenCode process identity differs from its coder workspace")

    source_key = sha256((run_id + "\0" + command_id).encode()).hexdigest()[:24]
    base = root / "artifacts" / "opencode-recovery" / source_key
    _ensure_host_directory(root, base)
    settlement_path = base / "sdk-settlement.json"
    settlement_record = _settlement_record(
        settlement_path, run_id=run_id, state=state, task_key=task_key,
        attempt_index=attempt_index, attempt=attempt, command=command,
        result=result, raw_result=raw_result)
    settlement_envelope = {"record": settlement_record, "sha256": digest(settlement_record)}
    if not (settlement_path.exists() or settlement_path.is_symlink()):
        atomic_json(settlement_path, settlement_envelope)
    settlement_ref = _receipt_ref(root, settlement_path, {
        "run_id": run_id, "command_id": command_id,
        "attempt_index": attempt_index, "result_sha256": settlement_record["result_sha256"]})

    diagnostics = native.get("process_diagnostics", [])
    diagnostic = diagnostics[-1] if isinstance(diagnostics, list) and diagnostics else {}
    diagnostic_digest = digest(diagnostic if isinstance(diagnostic, Mapping) else {})
    setup_record_digest = sha256(json.dumps(
        setup, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    attempt_identity = {
        "schema": _SCHEMA, "source_run_id": run_id,
        "source_run_revision": settlement_record["run_revision"], "source_task_id": task_key,
        "source_attempt_index": attempt_index, "source_command_id": command_id,
        "source_result_sha256": settlement_record["result_sha256"],
        "process_identity": dict(expected), "binding": binding,
        "coder_setup_sha256": setup_record_digest,
    }
    attempt_number = _next_attempt(base / "attempts", attempt_identity)
    cleanup = _wait_for_cleanup(expected)
    cleanup_path = base / "attempts" / f"attempt-{attempt_number:04d}" / "process-cleanup.json"
    _ensure_host_directory(root, cleanup_path.parent)
    cleanup_record = {
        "schema": _SCHEMA, "source_run_id": run_id,
        "source_run_revision": settlement_record["run_revision"], "source_task_id": task_key,
        "source_attempt_index": attempt_index, "source_command_id": command_id,
        "source_result_sha256": settlement_record["result_sha256"],
        "sdk_settlement_ref": settlement_ref,
        "native_goal_state_ref": dict(state_ref),
        "native_goal_state_sha256": file_digest(native_path),
        "coder_setup_file_sha256": setup_sha,
        "coder_setup_sha256": setup_record_digest,
        "binding": binding,
        "process_identity": dict(expected),
        "original_cleanup_diagnostic_sha256": diagnostic_digest,
        "attempt_number": attempt_number,
        "receipt_path": cleanup_path.relative_to(root).as_posix(),
        "reconciled_at": time.time(),
        **cleanup,
    }
    _write_envelope(cleanup_path, cleanup_record)
    cleanup_ref = _receipt_ref(root, cleanup_path, {
        "run_id": run_id, "command_id": command_id,
        "attempt_number": attempt_number,
        "cleanup_confirmed": cleanup_record["cleanup_confirmed"],
        "result_sha256": settlement_record["result_sha256"]})
    return {"status": "confirmed" if cleanup_record["cleanup_confirmed"] else "unconfirmed",
            "run_id": run_id, "task_id": task_key, "command_id": command_id,
            "sdk_settlement": settlement_ref, "process_cleanup": cleanup_ref,
            "cleanup_confirmed": cleanup_record["cleanup_confirmed"],
            "reason": cleanup_record.get("reason")}


def reconcile_opencode_cleanup(root: Path, *, sdk, run_id: str,
                               command_id: str) -> dict[str, Any]:
    """Reconcile one settled OpenCode cleanup without model or SDK writes.

    The SDK object must be an already identity-checked public SDK reader.  The
    function calls only ``sdk.get_run`` and acquires the Run workspace lock
    nonblocking so it cannot race a live host writer.
    """
    root = _contained_root(root)
    with workspace_lock(root, blocking=False):
        return _reconcile_opencode_cleanup_locked(
            root, sdk=sdk, run_id=run_id, command_id=command_id)


def confirmed_cleanup_receipt(root: Path, *, state: Mapping[str, Any],
                              command: OperationInput, attempt_index: int,
                              raw_result: Mapping[str, Any], native: Mapping[str, Any],
                              setup: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return a matching process-cleanup record only after all receipt bindings pass."""
    root = _contained_root(root)
    source_key = sha256((str(state.get("run_id")) + "\0" + command.command_id).encode()).hexdigest()[:24]
    base = root / "artifacts" / "opencode-recovery" / source_key
    settlement_path = base / "sdk-settlement.json"
    if (settlement_path.is_symlink() or not settlement_path.is_file()
            or settlement_path.resolve() != settlement_path.absolute()):
        return None
    try:
        if settlement_path.stat().st_size > 2 * 1024 * 1024:
            return None
        settlement_envelope = json.loads(settlement_path.read_text(encoding="utf-8"))
        settlement = settlement_envelope.get("record")
        if (not isinstance(settlement, dict)
                or settlement_envelope.get("sha256") != digest(settlement)):
            return None
        expected_result_digest = digest(raw_result)
        if (settlement.get("run_id") != state.get("run_id")
                or type(settlement.get("run_revision")) is not int
                or settlement.get("run_revision") > state.get("revision", -1)
                or settlement.get("command_id") != command.command_id
                or settlement.get("sdk_task_id") != command.task_id
                or settlement.get("attempt_index") != attempt_index
                or settlement.get("result_sha256") != expected_result_digest
                or settlement.get("command_sha256") != digest(command.to_dict())
                or settlement.get("attempt_state") not in _SETTLED
                or settlement.get("result_cleanup_unconfirmed") is not True):
            return None
        attempts_root = base / "attempts"
        if attempts_root.is_symlink():
            return None
        attempt_dirs = _attempt_directories(attempts_root)
        matches = []
        for expected_number, directory in enumerate(attempt_dirs, start=1):
            if (not re.fullmatch(r"attempt-[0-9]{4}", directory.name)
                    or directory.name != f"attempt-{expected_number:04d}"
                    or directory.is_symlink() or not directory.is_dir()
                    or directory.resolve() != directory.absolute()):
                return None
            path = directory / "process-cleanup.json"
            if (path.is_symlink() or not path.is_file()
                    or path.resolve() != path.absolute()
                    or path.stat().st_size > 8 * 1024 * 1024):
                return None
            envelope = json.loads(path.read_text(encoding="utf-8"))
            record = envelope.get("record") if isinstance(envelope, dict) else None
            if not isinstance(record, dict) or envelope.get("sha256") != digest(record):
                return None
            if (record.get("schema") != _SCHEMA
                    or record.get("source_run_id") != state.get("run_id")
                    or record.get("source_run_revision") != settlement.get("run_revision")
                    or record.get("source_task_id") != command.task_id
                    or record.get("source_attempt_index") != attempt_index
                    or record.get("source_command_id") != command.command_id
                    or record.get("source_result_sha256") != expected_result_digest
                    or record.get("process_identity") != native.get("owned_process_identity")
                    or record.get("binding") != native.get("opencode_cleanup_binding")
                    or record.get("coder_setup_sha256") != sha256(json.dumps(
                        setup, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                    or record.get("attempt_number") != expected_number
                    or record.get("receipt_path") != path.relative_to(root).as_posix()
                    or type(record.get("cleanup_confirmed")) is not bool):
                return None
            if record.get("cleanup_confirmed") is True:
                matches.append(record)
        if not matches:
            return None
        selected = max(matches, key=lambda row: row.get("attempt_number", 0))
        selected = dict(selected)
        receipt_path = root / selected["receipt_path"]
        selected["receipt_ref"] = _receipt_ref(root, receipt_path, {
            "run_id": state.get("run_id"), "command_id": command.command_id,
            "attempt_number": selected["attempt_number"],
            "cleanup_confirmed": True,
            "result_sha256": expected_result_digest,
        })
        return selected
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None
