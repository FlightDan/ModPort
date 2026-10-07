"""Explicit operator adjudication of one interrupted baseline client verification.

The caller holds the original operation lock.  This module never changes SDK
state: a separate operator must resolve the original effect through the SDK's
public recovery API using the returned failed response.
"""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Any

# Absolute imports allow an operator to load this one file against a frozen,
# already installed ModPort package without replacing the old deployment.
from modport.contracts import OperationInput, OperationResult
from modport.evidence import atomic_json, digest, file_digest, read_json
from modport.kernel_runtime import (check_storage_budget, reconcile_receipt,
                                    repository_facts, validate_stage_response)
from modport.payload_storage import pack_result, unpack_input


SCHEMA = "modport.interrupted-verification.v1"
MAX_LOG_BYTES = 16 * 1024 * 1024
MAX_INPUT_BYTES = 128 * 1024 * 1024
MAX_PROCESSES = 20000
SCAN_FRESH_SECONDS = 300


def _regular_file(root: Path, relative: str, limit: int) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("unsafe verification evidence path")
    path = root / relative
    if (not path.is_file() or path.is_symlink() or path.resolve() != path.absolute()
            or not path.resolve().is_relative_to(root.resolve()) or path.stat().st_size > limit):
        raise ValueError("verification evidence must be a bounded regular Run file")
    return path


def _bytes(root: Path, relative: str, limit: int) -> bytes:
    path = _regular_file(root, relative, limit)
    value = path.read_bytes()
    if len(value) > limit or not path.is_file() or path.is_symlink():
        raise ValueError("verification evidence changed or exceeds its size limit")
    return value


def _git_head(root: Path) -> str:
    baseline = root / "baseline"
    if not baseline.is_dir() or baseline.is_symlink() or baseline.resolve() != baseline.absolute():
        raise ValueError("baseline checkout is missing or unsafe")
    completed = subprocess.run(["git", "rev-parse", "--verify", "HEAD^{commit}"],
                               cwd=baseline, capture_output=True, text=True, timeout=15,
                               check=False)
    head = completed.stdout.strip()
    if completed.returncode or len(head) != 40 or any(c not in "0123456789abcdef" for c in head):
        raise ValueError("baseline Git commit cannot be established")
    return head


def capture_workspace_facts(root: Path) -> dict[str, str]:
    """Bind the retained original source commit and candidate contract bytes."""
    root = Path(root).resolve()
    source_bytes = _bytes(root, "artifacts/source.json", MAX_INPUT_BYTES)
    source = json.loads(source_bytes)
    head = _git_head(root)
    if not isinstance(source, dict) or source.get("source_commit") != head:
        raise ValueError("baseline HEAD disagrees with original source evidence")
    contract = _bytes(root, "baseline/.modport/functional-contract.json", MAX_INPUT_BYTES)
    if source_bytes != _bytes(root, "artifacts/source.json", MAX_INPUT_BYTES) or head != _git_head(root):
        raise ValueError("baseline source or Git HEAD changed during verification")
    return {"baseline_head": head, "source_sha256": sha256(source_bytes).hexdigest(),
            "functional_contract_sha256": sha256(contract).hexdigest()}


def _ancestors() -> set[int]:
    ancestors = set()
    pid = os.getpid()
    while pid > 0 and pid not in ancestors:
        ancestors.add(pid)
        try:
            # /proc/<pid>/stat's comm may contain spaces or parentheses.
            stat = (Path("/proc") / str(pid) / "stat").read_text()
            pid = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return ancestors


def capture_process_scan(root: Path, execution_id: str) -> dict[str, Any]:
    """Look for live processes tied to this execution without exposing env vars.

    On permission errors the inspection fails closed. An absence of live
    processes is an observed host fact, not proof of what previously ran.
    """
    root = Path(root).resolve()
    if not execution_id or not (Path("/proc") / "sys/kernel/random/boot_id").is_file():
        raise ValueError("Linux host process scan is unavailable")
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    ancestors = _ancestors()
    matching = []
    pids = [entry for entry in Path("/proc").iterdir() if entry.name.isdecimal()]
    if len(pids) > MAX_PROCESSES:
        raise ValueError("too many processes to establish verification quiescence")
    prefix = b"MODPORT_EXECUTION_ID="
    for entry in pids:
        pid = int(entry.name)
        if pid in ancestors:
            continue
        try:
            with (entry / "cmdline").open("rb") as stream:
                cmdline = stream.read(65537)
            if len(cmdline) > 65536:
                raise ValueError("process command line exceeds inspection limit")
            # A process may deliberately omit this environment variable, so
            # inspect root-bound cwd and known workload commands as well.
            cwd = (entry / "cwd").resolve(strict=True)
            with (entry / "environ").open("rb") as stream:
                environment = stream.read(1048577)
            if len(environment) > 1048576:
                raise ValueError("process environment exceeds inspection limit")
        except (FileNotFoundError, ProcessLookupError):
            continue  # Exited process, or kernel thread without a userspace environment.
        except (PermissionError, OSError) as exc:
            raise ValueError("cannot inspect a live process for verification quiescence") from exc
        execution_match = any(item == prefix + execution_id.encode() for item in environment.split(b"\0"))
        command_match = execution_id.encode() in cmdline
        workload_name = any(marker in cmdline.lower() for marker in (b"java", b"gradle", b"bwrap", b"xvfb", b"runclient"))
        workspace_match = cwd == root or cwd.is_relative_to(root)
        if execution_match or command_match or (workload_name and workspace_match):
            matching.append(pid)
    return {"status": "clear" if not matching else "active", "checked_at": time.time(),
            "boot_id": boot_id, "matching_pids": matching}


def _verify_evidence(root: Path, operation: OperationInput, evidence: dict) -> dict:
    if not isinstance(evidence, dict) or evidence.get("schema") != SCHEMA:
        raise ValueError("explicit interruption evidence schema is required")
    if any(not isinstance(evidence.get(key), str) or not evidence[key].strip()
           for key in ("operator_id", "decision_id", "execution_id", "effect_id", "reason", "before_sha256")):
        raise ValueError("interruption evidence has missing operator or effect identity")
    if evidence["execution_id"] != operation.command_id or evidence["reason"] != "workload_timeout":
        raise ValueError("interruption evidence does not describe this timed-out verification")
    log = evidence.get("timeout_log")
    if (not isinstance(log, dict) or not isinstance(log.get("path"), str)
            or not log["path"].startswith("audit-logs/") or not log["path"].endswith("/combined.log")):
        raise ValueError("original combined timeout log is required")
    raw = _bytes(root, log["path"], MAX_LOG_BYTES)
    if sha256(raw).hexdigest() != log.get("sha256"):
        raise ValueError("original timeout log SHA-256 differs")
    found = False
    for line in raw.splitlines():
        prefix = b"MODPORT_CLIENT_PREFLIGHT "
        if not line.startswith(prefix) or len(line) > 1048576:
            continue
        try:
            event = json.loads(line[len(prefix):])
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if (isinstance(event, dict) and event.get("error_code") == "workload_timeout"
                and event.get("execution_id") == operation.command_id):
            found = True
            break
    if not found:
        raise ValueError("original log has no matching workload_timeout event")
    directory = f"artifacts/executions/{operation.command_id}"
    before = _bytes(root, f"{directory}/before.json", MAX_INPUT_BYTES)
    if sha256(before).hexdigest() != evidence["before_sha256"]:
        raise ValueError("original before snapshot SHA-256 differs")
    if json.loads(before) != repository_facts(root, operation):
        raise ValueError("original before snapshot differs from operation scope")
    if json.loads(_bytes(root, f"{directory}/input.json", MAX_INPUT_BYTES)) != operation.to_dict():
        raise ValueError("original input differs from frozen SDK command")
    workspace = capture_workspace_facts(root)
    if evidence.get("workspace") != workspace:
        raise ValueError("current baseline workspace differs from operator observation")
    scan = evidence.get("process_scan")
    if (not isinstance(scan, dict) or scan.get("status") != "clear"
            or scan.get("matching_pids") != [] or not isinstance(scan.get("boot_id"), str)
            or type(scan.get("checked_at")) not in (int, float)
            or not 0 <= time.time() - scan["checked_at"] <= SCAN_FRESH_SECONDS):
        raise ValueError("fresh same-host quiescent process attestation is required")
    observed = capture_process_scan(root, operation.command_id)
    if observed["status"] != "clear" or observed["boot_id"] != scan["boot_id"]:
        raise ValueError("original verification workload is still active or host rebooted")
    return workspace


def _uncertain_diagnostic(value: Any) -> dict:
    """Only SDK-owned uncertainty diagnostics can precede manual adjudication."""
    if not isinstance(value, dict):
        raise ValueError("effect response is not an SDK uncertainty diagnostic")
    if value.get("code") == "effect_call_raised":
        if (set(value) != {"code", "message", "exception_type"}
                or not isinstance(value["message"], str)
                or not isinstance(value["exception_type"], str)):
            raise ValueError("malformed SDK effect-call uncertainty diagnostic")
    elif value.get("code") == "effect_outcome_uncertain":
        details = value.get("details")
        if (set(value) != {"code", "message", "retryable", "details", "schema_version"}
                or not isinstance(value["message"], str) or value["retryable"] is not False
                or not isinstance(details, dict)
                or set(details) != {"trigger", "prior_state", "attempt", "fence"}
                or not isinstance(details["trigger"], str)
                or details["prior_state"] not in {"prepared", "performing"}
                or type(details["attempt"]) is not int or details["attempt"] < 1
                or type(details["fence"]) is not int or details["fence"] < 1):
            raise ValueError("malformed SDK parked-effect uncertainty diagnostic")
        from dispatcher_sdk.execution_kernel import ExecutionError
        ExecutionError.from_dict(value)
    else:
        raise ValueError("effect response is not an SDK uncertainty diagnostic")
    return value


def reconcile_interrupted_verification(root: Path, command: dict, effect, *, evidence: dict) -> dict:
    """Write an authenticated *failed* receipt for an observed timeout only.

    Caller must own the stage lock and subsequently use public SDK
    ``resolve_effect(decision='applied', response=returned_value, ...)``.
    Existing complete receipts always take precedence.
    """
    root = Path(root).resolve()
    operation = OperationInput.from_dict(unpack_input(root, command["payload"]))
    if operation.stage_id != "contract_verify" or root != Path(operation.run_dir).resolve():
        raise ValueError("only the original contract_verify Run may be adjudicated")
    expected = {"input_sha256": digest(operation.to_dict()), "run_dir": str(root), "stage": operation.stage_id}
    if effect.request != expected or effect.execution_id != operation.command_id or effect.name != "modport.stage":
        raise ValueError("frozen SDK effect does not match original operation")
    directory = root / "artifacts" / "executions" / operation.command_id
    receipt = directory / "receipt.json"
    if receipt.exists() or receipt.is_symlink():
        return reconcile_receipt(root, command, effect)
    if (effect.state != "indeterminate" or not isinstance(evidence, dict)
            or effect.effect_id != evidence.get("effect_id") or effect.revision != evidence.get("effect_revision")):
        raise ValueError("frozen SDK effect or revision does not match interruption evidence")
    original_diagnostic = _uncertain_diagnostic(effect.response)
    workspace = _verify_evidence(root, operation, evidence)
    note = directory / "interrupted-verification.json"
    record = {"schema": SCHEMA, "status": "failed", "error_code": "contract_verify_interrupted",
              "execution_id": operation.command_id, "effect_id": effect.effect_id,
              "effect_revision": effect.revision, "effect_request": expected,
              "original_effect_diagnostic": original_diagnostic,
              "original_receipt_present": False, "evidence": evidence, "workspace": workspace}
    if note.exists() or note.is_symlink():
        if note.is_symlink():
            raise ValueError("a different interruption adjudication already exists")
        original = read_json(note)
        old_evidence = original.get("evidence") if isinstance(original, dict) else None
        prior_scan = old_evidence.get("process_scan") if isinstance(old_evidence, dict) else None
        if (not isinstance(prior_scan, dict) or prior_scan.get("status") != "clear"
                or prior_scan.get("matching_pids") != []
                or prior_scan.get("boot_id") != evidence["process_scan"]["boot_id"]):
            raise ValueError("a different interruption adjudication has invalid host attestation")
        # The original attestation remains in the immutable note. A fresh
        # attestation is required on every retry, even after its old check
        # becomes stale; it must not change the operator decision or evidence.
        comparable = {**record, "evidence": {**evidence, "process_scan": prior_scan}}
        if original != comparable:
            raise ValueError("a different interruption adjudication already exists")
    else:
        # Recheck the mutable evidence immediately before writing durable facts.
        _verify_evidence(root, operation, evidence)
        atomic_json(note, record)
    response = OperationResult(
        "failed", operation.run_id, operation.task_id, operation.stage_id, operation.command_id,
        outputs={"artifacts_complete": False, "acceptance_status": "unverified",
                 "operator_adjudicated": True,
                 "artifact_refs": {"interrupted_verification": {
                     "path": note.relative_to(root).as_posix(), "sha256": file_digest(note)},
                     "original_timeout_log": evidence["timeout_log"]}},
        detail="Original client workload timed out; no acceptance result was established",
        error_code="contract_verify_interrupted").to_dict()
    atomic_json(receipt, {"execution_id": operation.command_id, "effect_request": expected,
                          "response": response, "after": repository_facts(root, operation)})
    verified = validate_stage_response(root, operation, response, expected)
    check_storage_budget(root, phase="kernel-recovery:contract_verify:effect-result")
    return pack_result(root, verified)
