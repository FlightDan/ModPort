"""Conservative characterization diagnostics; never produces acceptance evidence.

``evidence_records`` must contain only records authenticated by the host verifier.
Client state messages and ordinary logs are untrusted diagnostic observations.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import re
from typing import Any

from .client_harness import CLIENT_STAGES, STATE_PREFIX, parse_client_states


def _normalize(text: str) -> str:
    text = re.sub(r"\b[0-9a-f]{32,64}\b", "<identity>", text, flags=re.I)
    text = re.sub(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", "<identity>", text, flags=re.I)
    text = re.sub(r"\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b", "<time>", text)
    text = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "<date>", text)
    text = re.sub(r"(?<=\])\d{10,}\b", "<address>", text)
    text = re.sub(r"(?<=:)\d+\b", "<line>", text)
    return " ".join(text.split())[:2000]


def _compile_origin(log_line: str, executor_provenance: Any) -> tuple[str, str]:
    match = re.search(r"(?P<path>[^\s:]+\.java):\d+(?::\d+)?:\s*error:", log_line)
    if match is None:
        return "unknown", "independent_diagnosis"
    path = match.group("path").replace("\\", "/")
    # Reject ambiguous traversal rather than assigning ownership by a substring.
    if ".." in path.split("/"):
        return "unknown", "independent_diagnosis"
    relative = path.removeprefix("/workspace/").removeprefix("./")
    if relative.startswith(".modport/") or "/.modport/" in path:
        return "harness_compile", "harness_sources"
    if isinstance(executor_provenance, Mapping):
        for record in executor_provenance.values():
            files = record.get("test_source_files", {}) if isinstance(record, Mapping) else {}
            if isinstance(files, (Mapping, list, tuple)) and relative in files:
                return "harness_compile", "harness_sources"
    if relative.startswith("src/main/java/") or "/src/main/java/" in path:
        return "product_compile", "product_sources"
    return "unknown", "independent_diagnosis"


def _runtime_failure_observations(log_text: str) -> dict[str, Any]:
    """Retain separate harness failures as bounded, untrusted diagnostics."""
    failures = []
    seen = set()
    truncated = False
    for line_number, line in enumerate(log_text.splitlines(), 1):
        if not line.startswith("MODPORT_RUNTIME_FAILURE "):
            continue
        parts = line.split(maxsplit=2)
        if len(parts) != 3:
            continue
        truncated = truncated or len(parts[1]) > 200 or len(parts[2]) > 2000
        test_id, detail = parts[1][:200], parts[2][:2000]
        identity = (test_id, detail)
        if identity in seen:
            continue
        if len(failures) == 64:
            truncated = True
            break
        seen.add(identity)
        failures.append({"test_id": test_id, "detail": detail, "log_line": line_number})
    return ({"runtime_failures": failures, "runtime_failures_truncated": truncated,
             "runtime_failure_trust": "untrusted_process_log"} if failures else {})


def classify_characterization_failure(
    log_text: str, *, exit_code: int | None, timed_out: bool, phase: str,
    record_errors: Sequence[str] = (), evidence_records: Mapping[str, Any] | Sequence[Mapping[str, Any]] = (),
    executor_provenance: Any = None, candidate_sha256: str | None = None,
    execution_id: str | None = None, cancelled: bool = False,
    missing_tests: Sequence[str] = (), raw_log_refs: Any = (), environment: Any = None,
    client_states: Sequence[Mapping[str, Any]] | None = None,
    infrastructure_error: str | None = None,
    workload_budget: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Describe a failed attempt without replacing its original error or exit status.

    Cancellation is an explicit host fact, never inferred from a process exit code.
    Unknown causes (including a generic timeout) remain unknown. Workspace
    changes are normal agent work and are not interpreted as behavioral
    progress or failure. Candidate and environment digests are intentionally
    not emitted; the host owns workspace association and the SDK owns
    execution identity.
    """
    launcher_code = ""
    if (
        isinstance(environment, Mapping)
        and bool(execution_id)
        and environment.get("execution_id") == execution_id
        and type(environment.get("schema_version")) is int
        and environment.get("schema_version") == 1
        and environment.get("kind") == "client_environment_diagnostic"
        and environment.get("acceptance_evidence") is False
        and isinstance(environment.get("error_code"), str)
    ):
        launcher_code = environment["error_code"]
    timed_out = timed_out or launcher_code in {"workload_timeout", "preflight_timeout"}
    cancelled = cancelled or launcher_code == "execution_cancelled"
    state_errors: list[str] = []
    try:
        state_log = "\n".join(STATE_PREFIX + json.dumps(record) for record in client_states) if client_states is not None else log_text
        states = parse_client_states(state_log, execution_id=execution_id)
    except ValueError as exc:
        states = []
        state_errors.append(str(exc))
    last = states[-1] if states else {}
    task_match = re.search(r"(?:> Task (\S+) FAILED|Execution failed for task ['\"]([^'\"]+))", log_text)
    first_task = next((group for group in task_match.groups() if group), None) if task_match else None
    category, code, reason, confidence = "unknown", "unclassified_failure", "", "low"
    from .dependency_build import gradle_failure_kind
    dependency_failure = gradle_failure_kind(log_text) if exit_code else "gradle_failed"
    # Strong and contextual signals only. In particular, OpenAL failure alone
    # is a capability limitation, not evidence of a fatal client startup failure.
    compile_scope = None
    patterns = (
        ("harness_runtime", "client_timeout", r"(?im)^.*Client characterization timed out.*$"),
        ("environment", "display_unavailable", r"(?im)^.*(?:GLFW error 65544.*(?:X11|DISPLAY)|Failed to initialize GLFW|GLFW initialization failed|Unable to open display|X11: The DISPLAY environment variable is missing).*$"),
        ("harness_compile", "compilation_failed", r"(?im)^.*(?:\.java:\d+(?::\d+)?:\s*error:|error: (?:cannot find symbol|package .* does not exist|incompatible types|name clash|.* has private access|.* is not public)|Compilation failed;|Compilation failure).*$"),
        ("behavior_assertion", "assertion_failed", r"(?im)^.*(?:java\.lang\.AssertionError|org\.opentest4j\.AssertionFailedError|org\.junit\.ComparisonFailure|\[minecraft/LogTestReporter\]: characterize failed!).*$"),
        ("harness_runtime", "uncaught_exception", r'(?im)^.*Exception in thread ["\'].*$'),
    )
    if cancelled:
        category, code, reason, confidence = "cancelled", "execution_cancelled", "host cancelled execution", "high"
    elif infrastructure_error:
        category, code, reason, confidence = "infrastructure", "executor_infrastructure_failure", infrastructure_error, "high"
    elif dependency_failure != "gradle_failed":
        category, code, reason, confidence = "infrastructure", dependency_failure, "Gradle dependency retrieval failed; inspect the raw repository error", "high"
    elif launcher_code in {"xvfb_unavailable", "display_start_failed", "display_unavailable"}:
        category, code, reason, confidence = "environment", launcher_code, "sandbox launcher: " + launcher_code, "high"
    elif (launcher_code == "workload_timeout" and workload_budget is not None
          and isinstance(workload_budget.get("launcher_seconds"), (int, float))
          and workload_budget["launcher_seconds"] < 120):
        category, code, reason, confidence = (
            "infrastructure", "insufficient_workload_window",
            "client workload reached a host-assigned window under 120 seconds; underlying slowdown unconfirmed", "high")
    elif launcher_code in {"workload_timeout", "preflight_timeout"}:
        category, code, reason, confidence = "harness_runtime", launcher_code, "sandbox launcher deadline exceeded: " + launcher_code, "high"
    elif launcher_code in {"launch_failed", "preflight_failed"}:
        category, code, reason, confidence = "infrastructure", launcher_code, "sandbox launcher: " + launcher_code, "high"
    else:
        for possible_category, possible_code, pattern in patterns:
            match = re.search(pattern, log_text)
            if match:
                category, code, reason, confidence = possible_category, possible_code, match.group(0), "medium"
                if possible_category == "harness_compile":
                    category, compile_scope = _compile_origin(reason, executor_provenance)
                break
        if category == "unknown" and last.get("error_code") in {"stage_timeout", "overall_timeout", "unknown_screen", "resource_reload_failed", "onboarding_transition_failed"}:
            category, code, reason, confidence = "harness_runtime", str(last["error_code"]), str(last.get("detail", last["error_code"])), "medium"
        if category == "unknown" and timed_out:
            code, reason = "execution_timeout", "host deadline exceeded; root cause unconfirmed"
        elif category == "unknown" and code == "unclassified_failure" and (record_errors or state_errors):
            category, code, reason, confidence = "evidence_protocol", "invalid_or_missing_evidence", " | ".join([*record_errors, *state_errors]), "high"
    if isinstance(evidence_records, Mapping):
        authenticated_ids = sorted(str(key) for key in evidence_records)
    else:
        authenticated_ids = sorted({str(record["test_id"]) for record in evidence_records if "test_id" in record})
    milestone = last.get("stage")
    # Timeout logs are volatile and often contain nonfatal warnings. Bind their
    # signature to the observed stage, not the last incidental warning.
    signature_detail = reason or log_text[-4000:]
    if execution_id:
        signature_detail = signature_detail.replace(execution_id, "<execution>")
    normalized = _normalize(signature_detail)
    # Keep a readable, stable label for same-failure comparison. This is a
    # diagnostic value, not a content-addressed identity or a workflow gate.
    signature = json.dumps(
        {"category": category, "code": code, "task": first_task,
         "stage": milestone, "detail": normalized},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return {
        "schema_version": 1, "kind": "characterization_diagnostic", "category": category,
        "error_code": code, "phase": phase, "execution_id": execution_id,
        "exit_code": exit_code, "timed_out": timed_out,
        "repair_allowed": not cancelled and dependency_failure == "gradle_failed",
        "first_failed_task": first_task, "last_milestone": milestone,
        "milestone_trust": "untrusted_client_diagnostic",
        "last_client_state": dict(last), "state_protocol_errors": state_errors,
        "failure_signature": signature, "normalized_failure": normalized,
        "raw_log_refs": raw_log_refs,
        **_runtime_failure_observations(log_text),
        **({"workload_budget": dict(workload_budget)} if workload_budget is not None else {}),
        "record_errors": list(record_errors), "missing_tests": list(missing_tests),
        "authenticated_test_ids": authenticated_ids,
        "evidence_counts": {"authenticated": len(authenticated_ids), "missing": len(missing_tests)},
        "audio_capability_warning": bool(re.search(
            r"(?i)(?:(?:OpenAL|audio device|sound system).*(?:fail|unavailable|cannot)"
            r"|(?:fail|unavailable|cannot).*?(?:OpenAL|audio device|sound system))", log_text)),
        "suggested_repair_scope": compile_scope or {"environment": "sandbox_environment", "harness_compile": "harness_sources", "product_compile": "product_sources", "harness_runtime": "harness_runtime", "behavior_assertion": "investigate_assertion_and_behavior", "evidence_protocol": "evidence_protocol", "cancelled": "none", "infrastructure": "host_executor"}.get(category, "independent_diagnosis"),
        "confidence": confidence,
    }


def compare_characterization_progress(previous: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, bool]:
    """Compare attempts in the same host-controlled repair chain.

    A changed failure is inconclusive (not stalled). Authenticated new test IDs
    and later observed lifecycle stages count as diagnostic progress. Mutable
    workspace changes and candidate/environment metadata are ignored.
    Lifecycle observations remain untrusted and cannot satisfy behavior obligations.
    The caller must not compare different contracts or unrelated repair chains.
    """
    def rank(record: Mapping[str, Any]) -> int:
        stage = record.get("last_milestone")
        return CLIENT_STAGES.index(stage) if stage in CLIENT_STAGES else -1
    milestone_advanced = rank(current) > rank(previous)
    evidence_advanced = bool(set(current.get("authenticated_test_ids", ())) - set(previous.get("authenticated_test_ids", ())))
    same_failure = bool(current.get("failure_signature")) and current.get("failure_signature") == previous.get("failure_signature") and current.get("phase") == previous.get("phase")
    cancelled = current.get("category") == "cancelled" or previous.get("category") == "cancelled"
    return {
        "same_failure": same_failure, "milestone_advanced": milestone_advanced,
        "evidence_advanced": evidence_advanced,
        "stalled": same_failure and not milestone_advanced and not evidence_advanced and not cancelled,
        "repair_allowed": not cancelled,
    }
