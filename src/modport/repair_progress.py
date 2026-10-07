"""Bounded, host-oriented diagnostics for a repair feedback chain.

The values produced here are deliberately not acceptance evidence and do not
carry a scheduling decision.  In particular, a verifier result is current only
when both its execution identity and the candidate it actually checked match
the host's repair record.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from hashlib import sha256
import json
from typing import Any


_UNKNOWN = "unknown"
_MAX_DETAIL = 800
_MAX_REFS = 8
_MAX_ISSUES = 64
_VERIFICATION_STAGES = frozenset({
    "target_build", "contract_verify", "test_execute", "verification",
    "build_and_behavior", "client_smoke", "server_smoke",
})
_SCOPE_KEYS = (
    "scope", "scope_id", "scan_scope", "rule_scope", "ruleset", "ruleset_id",
    "scan_rules", "rules", "paths", "included_paths", "excluded_paths",
    "target", "target_version", "scanner_version", "rules_sha256",
    "rule_ids", "source_scope", "log_scope", "log_sources", "log_source_refs",
    "input_refs",
)


def _text(value: Any, limit: int = 256) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return " ".join(value.split())[:limit]


def _detail(value: Any) -> str:
    return value[:_MAX_DETAIL] if isinstance(value, str) else ""


def _execution_fact(value: Any) -> bool | str:
    return value if type(value) is bool else _UNKNOWN


def _bounded_json(value: Any, limit: int = 500) -> Any:
    """Keep small JSON-like identity values without retaining arbitrary input."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, Mapping):
        result = {}
        for key in sorted(value, key=str)[:16]:
            result[str(key)[:80]] = _bounded_json(value[key], limit)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_bounded_json(item, limit) for item in list(value)[:32]]
    return str(value)[:limit]


def _one_ref(value: Any) -> Any | None:
    if isinstance(value, str):
        return value[:500]
    if not isinstance(value, Mapping):
        return None
    allowed = (
        "path", "sha256", "media_type", "kind", "name", "uri", "execution_id",
        "stream", "offset", "length",
    )
    ref = {key: _bounded_json(value[key]) for key in allowed if key in value}
    return ref or None


def _log_refs(outputs: Mapping[str, Any]) -> list[Any]:
    refs: list[Any] = []
    for key in (
        "log_refs", "raw_log_refs", "build_log_refs", "verification_log_refs",
        "log_ref", "raw_log_ref", "build_log_ref", "verification_log_ref",
        "log", "build_log", "verification_log",
    ):
        value = outputs.get(key)
        values = value if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)) else (value,)
        for item in values:
            ref = _one_ref(item)
            if ref is not None and ref not in refs:
                refs.append(ref)
                if len(refs) == _MAX_REFS:
                    return refs
    artifacts = outputs.get("artifact_refs")
    if isinstance(artifacts, Mapping):
        for name in sorted(artifacts, key=str):
            item = artifacts[name]
            text_ref = (
                isinstance(item, Mapping)
                and isinstance(item.get("media_type"), str)
                and item["media_type"].lower().startswith("text/plain")
            )
            if "log" not in str(name).lower() and not text_ref:
                continue
            ref = _one_ref(item)
            if ref is not None and ref not in refs:
                refs.append(ref)
                if len(refs) == _MAX_REFS:
                    break
    return refs


def _result(update: Any) -> Mapping[str, Any]:
    if not isinstance(update, Mapping):
        return {}
    value = update.get("result")
    return value if isinstance(value, Mapping) else {}


def _outputs(update: Any) -> Mapping[str, Any]:
    value = _result(update).get("outputs")
    return value if isinstance(value, Mapping) else {}


def _summary(update: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if update is None:
        return None
    result = _result(update)
    outputs = _outputs(update)
    diagnostic = outputs.get("diagnostic")
    failure_signature = outputs.get("failure_signature")
    if failure_signature is None and isinstance(diagnostic, Mapping):
        failure_signature = diagnostic.get("failure_signature")
    summary: dict[str, Any] = {
        "stage": _text(update.get("stage")),
        "execution_id": _text(result.get("command_id")),
        "status": _text(result.get("status")),
        "error_code": _text(result.get("error_code")),
        "detail": _detail(result.get("detail")),
        "log_refs": _log_refs(outputs),
        "build_status": _text(outputs.get("build_status")),
        "verification_status": _text(outputs.get("verification_status")),
        "build_error_code": _text(outputs.get("build_error_code")),
        "verification_error_code": _text(outputs.get("verification_error_code")),
        "build_executed": _execution_fact(outputs.get("build_executed")),
        "verification_executed": _execution_fact(outputs.get("verification_executed")),
        "build_detail": _detail(outputs.get("build_detail")),
        "verification_detail": _detail(outputs.get("verification_detail")),
        # Signatures are opaque host values.  Normalizing a digest would erase
        # the very distinction it supplies.
        "failure_signature": _detail(failure_signature) or None,
        "build_failure_signature": _detail(outputs.get("build_failure_signature")) or None,
        "verification_failure_signature": _detail(
            outputs.get("verification_failure_signature")
        ) or None,
        "candidate_workspace": _text(outputs.get("candidate_workspace"), 500),
    }
    return summary


def _candidate(outputs: Mapping[str, Any], key: str) -> str | None:
    return _text(outputs.get(key))


def _run_id(updates: Sequence[Any]) -> str | None:
    values = {_text(_result(update).get("run_id")) for update in updates}
    values.discard(None)
    return next(iter(values)) if len(values) == 1 else None


def _bound_run_id(record: Mapping[str, Any], updates: Sequence[Any]) -> str | None:
    declared = _text(record.get("run_id"))
    observed = _run_id(updates)
    if declared and observed and declared != observed:
        return None
    return declared or observed


def _complete(inventory: Mapping[str, Any]) -> bool:
    coverage = inventory.get("coverage")
    if isinstance(coverage, Mapping):
        return (
            coverage.get("scan_complete") is True
            and coverage.get("truncated") is not True
            and coverage.get("source_complete") is not False
            and coverage.get("logs_complete") is not False
        )
    for key in ("complete", "scan_complete", "inventory_complete", "coverage_complete"):
        if key in inventory:
            return inventory[key] is True
    return False


def _scope(inventory: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = {key: inventory[key] for key in _SCOPE_KEYS if key in inventory}
    coverage = inventory.get("coverage")
    if isinstance(coverage, Mapping):
        # These describe the scanner's comparable range.  Observed counts,
        # skipped paths, and truncation reasons describe one execution instead.
        for key in (
            "limits", "excluded_directory_names", "excluded_directory_prefixes",
            "scanner_version", "rules_sha256", "rule_ids", "source_scope",
            "scoped_files", "log_scope", "log_sources", "log_source_refs",
            "input_refs", "logs_complete",
        ):
            if key in coverage:
                raw["coverage_" + key] = coverage[key]
    if inventory.get("kind") == "repair_inventory":
        raw["inventory_schema_version"] = inventory.get("schema_version")
    if not raw:
        return None
    try:
        encoded = json.dumps(
            raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError, RecursionError):
        return None
    value = {key: _bounded_json(raw[key]) for key in sorted(raw)}
    value["scope_sha256"] = sha256(encoded).hexdigest()
    if inventory.get("kind") == "repair_inventory":
        log_evidence = any(
            isinstance(evidence, Mapping)
            and str(evidence.get("source", "")).startswith("log:")
            for issue in inventory.get("issues", ()) if isinstance(issue, Mapping)
            for evidence in issue.get("evidence", ()) if isinstance(issue.get("evidence"), Sequence)
        ) if isinstance(inventory.get("issues"), Sequence) else False
        has_log_scope = any(
            key in value for key in (
                "log_scope", "log_sources", "log_source_refs", "input_refs",
                "coverage_log_scope", "coverage_log_sources",
                "coverage_log_source_refs", "coverage_input_refs",
            )
        )
        scan_scope = inventory.get("scan_scope")
        if isinstance(scan_scope, Mapping):
            has_log_scope = has_log_scope or any(
                key in scan_scope
                for key in ("log_scope", "log_sources", "log_source_refs", "input_refs")
            )
        if log_evidence and not has_log_scope:
            return None
    return value or None


def _issue_rows(inventory: Mapping[str, Any]) -> list[Any]:
    raw = inventory.get("issue_ids", inventory.get("issues", ()))
    if isinstance(raw, Mapping):
        raw = list(raw)
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return []
    return list(raw)


def _issue_id(item: Any) -> str | None:
    if isinstance(item, Mapping):
        item = next((item.get(key) for key in ("issue_id", "id", "key", "rule_id")
                     if item.get(key) is not None), None)
    return _text(item)


def _evidence_sources(item: Any) -> set[str]:
    if not isinstance(item, Mapping):
        return set()
    evidence = item.get("evidence", ())
    if not isinstance(evidence, Sequence) or isinstance(evidence, (str, bytes, bytearray)):
        return set()
    return {
        str(value.get("source"))
        for value in evidence
        if isinstance(value, Mapping) and isinstance(value.get("source"), str)
    }


def _issue_ids(inventory: Mapping[str, Any], *, bounded: bool = True) -> list[str]:
    values: set[str] = set()
    for item in _issue_rows(inventory):
        value = _issue_id(item)
        if value is not None:
            values.add(value)
    ordered = sorted(values)
    return ordered[:_MAX_ISSUES] if bounded else ordered


def _closable_issue_ids(inventory: Mapping[str, Any]) -> list[str]:
    """Issues whose disappearance may be reported after a complete full scan."""
    values: set[str] = set()
    for item in _issue_rows(inventory):
        issue_id = _issue_id(item)
        if issue_id is None:
            continue
        sources = _evidence_sources(item)
        if isinstance(item, Mapping) and not any(
            source in {"workspace_scan", "workspace_path", "missing_inputs"}
            for source in sources
        ):
            # Compiler output and caller observations are diagnostic snapshots.
            # Their absence on a later failed run does not prove repair.
            continue
        values.add(issue_id)
    return sorted(values)


def _source_issue_ids(inventory: Mapping[str, Any]) -> list[str]:
    values = {
        issue_id
        for item in _issue_rows(inventory)
        if any(source in {"workspace_scan", "workspace_path"}
               for source in _evidence_sources(item))
        for issue_id in (_issue_id(item),)
        if issue_id is not None
    }
    return sorted(values)


def _source_complete(inventory: Mapping[str, Any]) -> bool:
    coverage = inventory.get("coverage")
    scan_scope = inventory.get("scan_scope")
    if isinstance(coverage, Mapping) or isinstance(scan_scope, Mapping):
        return (
            isinstance(coverage, Mapping)
            and coverage.get("source_complete") is True
            and (not isinstance(scan_scope, Mapping)
                 or scan_scope.get("source_complete") is not False)
        )
    return _complete(inventory)


def _scope_summary(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        encoded = json.dumps(
            raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError, RecursionError):
        return None
    value = {key: _bounded_json(raw[key]) for key in sorted(raw)}
    value["scope_sha256"] = sha256(encoded).hexdigest()
    return value


def _source_scope(inventory: Mapping[str, Any]) -> dict[str, Any] | None:
    scan_scope = inventory.get("scan_scope")
    coverage = inventory.get("coverage")
    raw: dict[str, Any] = {}
    if isinstance(scan_scope, Mapping):
        for key in ("ruleset", "rule_ids", "source_files", "build_files", "resource_files"):
            if key in scan_scope:
                raw[key] = scan_scope[key]
    elif "scan_scope" in inventory:
        return None
    if isinstance(coverage, Mapping):
        limits = coverage.get("limits")
        if isinstance(limits, Mapping):
            raw["limits"] = {
                key: value for key, value in limits.items()
                if not str(key).startswith("max_log_")
            }
        for key in ("excluded_directory_names", "excluded_directory_prefixes"):
            if key in coverage:
                raw[key] = coverage[key]
    # Legacy explicit source scopes remain comparable without inventing a v3
    # ruleset.  Native v3 inventories must contain the full source scope.
    if inventory.get("kind") == "repair_inventory" and not all(
        key in raw for key in (
            "ruleset", "rule_ids", "source_files", "build_files",
            "resource_files", "limits", "excluded_directory_names",
            "excluded_directory_prefixes",
        )
    ):
        return None
    return _scope_summary(raw)


def _inventory_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    issue_ids = _issue_ids(value, bounded=False)
    closable_ids = _closable_issue_ids(value)
    source_ids = _source_issue_ids(value)
    encoded = json.dumps(issue_ids, ensure_ascii=False, separators=(",", ":")).encode()
    closable_encoded = json.dumps(
        closable_ids, ensure_ascii=False, separators=(",", ":")
    ).encode()
    source_encoded = json.dumps(
        source_ids, ensure_ascii=False, separators=(",", ":")
    ).encode()
    summary = {
        "complete": _complete(value),
        "scope": _scope(value),
        "source_complete": _source_complete(value),
        "source_scope": _source_scope(value),
        "candidate_id": _text(value.get("candidate_id")),
        "execution_id": _text(value.get("execution_id")),
        "issue_count": len(issue_ids),
        "issue_ids_sha256": sha256(encoded).hexdigest(),
        "closable_issue_count": len(closable_ids),
        "closable_issue_ids_sha256": sha256(closable_encoded).hexdigest(),
        "source_issue_count": len(source_ids),
        "source_issue_ids_sha256": sha256(source_encoded).hexdigest(),
    }
    for key in ("artifact_ref", "inventory_ref", "ref"):
        ref = _one_ref(value.get(key))
        if ref is not None:
            summary["ref"] = ref
            break
    return summary


def _repair_inventory(
    record: Mapping[str, Any], updates: Sequence[Any], *,
    current_candidate: str | None, previous_candidate: str | None,
) -> dict[str, Any] | None:
    before = record.get("before_inventory")
    after = record.get("after_inventory")
    # Only orchestration's verified file references supply inventories. Raw
    # author/verifier output must not override host-collected scan evidence.
    before_summary = _inventory_summary(before)
    after_summary = _inventory_summary(after)
    if before_summary is None and after_summary is None:
        return None
    comparable = bool(
        before_summary and after_summary
        and before_summary["complete"] and after_summary["complete"]
        and before_summary["scope"] is not None
        and before_summary["scope"] == after_summary["scope"]
        and bool(current_candidate) and after_summary["candidate_id"] == current_candidate
        and bool(previous_candidate) and before_summary["candidate_id"] == previous_candidate
    )
    source_comparable = bool(
        before_summary and after_summary
        and before_summary["source_complete"] and after_summary["source_complete"]
        and before_summary["source_scope"] is not None
        and before_summary["source_scope"] == after_summary["source_scope"]
        and bool(current_candidate) and after_summary["candidate_id"] == current_candidate
        and bool(previous_candidate) and before_summary["candidate_id"] == previous_candidate
    )
    before_ids = set(_closable_issue_ids(before)) if isinstance(before, Mapping) else set()
    after_ids = set(_closable_issue_ids(after)) if isinstance(after, Mapping) else set()
    before_source_ids = set(_source_issue_ids(before)) if isinstance(before, Mapping) else set()
    after_source_ids = set(_source_issue_ids(after)) if isinstance(after, Mapping) else set()
    closed_all = sorted(before_ids - after_ids) if comparable else []
    introduced_all = sorted(after_ids - before_ids) if comparable else []
    source_closed_all = (
        sorted(before_source_ids - after_source_ids) if source_comparable else []
    )
    source_introduced_all = (
        sorted(after_source_ids - before_source_ids) if source_comparable else []
    )
    for summary, ref_key in ((before_summary, "before_inventory_ref"),
                             (after_summary, "after_inventory_ref")):
        ref = _one_ref(record.get(ref_key))
        if summary is not None and ref is not None:
            summary["ref"] = ref
    return {
        "before": before_summary,
        "after": after_summary,
        "comparable": comparable,
        "closed_issue_count": len(closed_all),
        "closed_issue_ids": closed_all[:_MAX_ISSUES],
        "introduced_issue_count": len(introduced_all),
        "introduced_issue_ids": introduced_all[:_MAX_ISSUES],
        "source_comparable": source_comparable,
        "source_closed_issue_count": len(source_closed_all),
        "source_closed_issue_ids": source_closed_all[:_MAX_ISSUES],
        "source_introduced_issue_count": len(source_introduced_all),
        "source_introduced_issue_ids": source_introduced_all[:_MAX_ISSUES],
    }


def _status(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.lower()
    if value in {"completed", "passed", "pass", "success", "succeeded", "verified"}:
        return "completed"
    if value in {"failed", "failure", "error", "blocked"}:
        return "failed"
    return None


def _component_state(summary: Mapping[str, Any] | None, component: str) -> str | None:
    if not summary:
        return None
    return _status(summary.get(component + "_status"))


def _component_executed(summary: Mapping[str, Any] | None, component: str) -> bool | str:
    if not summary:
        return _UNKNOWN
    return _execution_fact(summary.get(component + "_executed"))


def _verification_state(summary: Mapping[str, Any] | None) -> str | None:
    """Return the aggregate check state, with a build failure taking priority."""
    if not summary:
        return None
    operation = _status(summary.get("status"))
    build = _component_state(summary, "build")
    verification = _component_state(summary, "verification")
    if build == "failed" or verification == "failed" or operation == "failed":
        return "failed"
    if operation == "completed" and build != "failed" and verification != "failed":
        return "completed"
    return None


def _failure_locus(summary: Mapping[str, Any]) -> str:
    if (_component_executed(summary, "build") is True
            and _component_state(summary, "build") == "failed"):
        return "build"
    if (_component_executed(summary, "verification") is True
            and _component_state(summary, "verification") == "failed"):
        return "verification"
    return "operation"


def _root_code(summary: Mapping[str, Any], locus: str | None = None) -> Any:
    locus = locus or _failure_locus(summary)
    if locus == "build":
        return summary.get("build_error_code")
    if locus == "verification":
        return summary.get("verification_error_code")
    return summary.get("error_code")


def _root_signature(summary: Mapping[str, Any], locus: str) -> str | None:
    if locus == "build":
        return _detail(summary.get("build_failure_signature")) or None
    if locus == "verification":
        return _detail(summary.get("verification_failure_signature")) or None
    return _detail(summary.get("failure_signature")) or None


def _root_detail(summary: Mapping[str, Any], locus: str) -> str:
    if locus == "build":
        return _detail(summary.get("build_detail"))
    if locus == "verification":
        return _detail(summary.get("verification_detail"))
    return _detail(summary.get("detail"))


def _inventory_root(previous: Mapping[str, Any], current: Mapping[str, Any]) -> bool | str:
    previous_inventory = previous.get("repair_inventory")
    current_inventory = current.get("repair_inventory")
    if not isinstance(previous_inventory, Mapping) or not isinstance(current_inventory, Mapping):
        return _UNKNOWN
    previous_after = previous_inventory.get("after")
    current_before = current_inventory.get("before")
    current_after = current_inventory.get("after")
    if not all(isinstance(value, Mapping)
               for value in (previous_after, current_before, current_after)):
        return _UNKNOWN
    if (previous_inventory.get("comparable") is not True
            or current_inventory.get("comparable") is not True):
        return _UNKNOWN
    if (
        previous_after.get("complete") is not True
        or current_before.get("complete") is not True
        or current_after.get("complete") is not True
        or previous_after.get("scope") != current_before.get("scope")
        or previous_after.get("scope") != current_after.get("scope")
        or previous_after.get("closable_issue_ids_sha256")
        != current_before.get("closable_issue_ids_sha256")
        or not previous_after.get("closable_issue_count")
        or not current_after.get("closable_issue_count")
    ):
        return _UNKNOWN
    return (previous_after.get("closable_issue_ids_sha256")
            == current_after.get("closable_issue_ids_sha256"))


def _same_root(
    previous_feedback: Mapping[str, Any], current_feedback: Mapping[str, Any],
    previous: Mapping[str, Any], current: Mapping[str, Any],
) -> bool | str:
    previous_state = _verification_state(previous)
    current_state = _verification_state(current)
    if previous_state is None or current_state is None:
        return _UNKNOWN
    if previous_state != "failed" or current_state != "failed":
        return False
    previous_locus = _failure_locus(previous)
    current_locus = _failure_locus(current)
    if previous_locus != current_locus:
        return False
    previous_code = _root_code(previous, previous_locus)
    current_code = _root_code(current, current_locus)
    if previous_code or current_code:
        if not previous_code or previous_code != current_code:
            return False
    previous_signature = _root_signature(previous, previous_locus)
    current_signature = _root_signature(current, current_locus)
    if previous_signature or current_signature:
        return bool(previous_signature) and previous_signature == current_signature
    # Generic wrapper codes and details are intentionally insufficient.  A
    # complete, candidate-bound inventory can supply a stable issue identity.
    return _inventory_root(previous_feedback, current_feedback)


def _comparison(feedback: Mapping[str, Any], previous: Any) -> dict[str, Any]:
    base = {
        "comparable": _UNKNOWN,
        "same_root_cause": _UNKNOWN,
        "build_advanced": _UNKNOWN,
        "verification_advanced": _UNKNOWN,
        "inventory_advanced": _UNKNOWN,
        "substantive_progress": _UNKNOWN,
        "stalled": _UNKNOWN,
    }
    if not isinstance(previous, Mapping) or previous.get("kind") != "repair_feedback":
        return base
    same_chain = (
        bool(feedback.get("target_agent"))
        and feedback.get("target_agent") == previous.get("target_agent")
        and feedback.get("target_scope") == previous.get("target_scope")
        and bool(feedback.get("run_id"))
        and feedback.get("run_id") == previous.get("run_id")
        and feedback.get("verification_current") is True
        and previous.get("verification_current") is True
    )
    if not same_chain:
        return base
    current_verification = feedback.get("verification")
    previous_verification = previous.get("verification")
    if not isinstance(current_verification, Mapping) or not isinstance(previous_verification, Mapping):
        return base
    previous_state = _verification_state(previous_verification)
    current_state = _verification_state(current_verification)
    previous_build = _component_state(previous_verification, "build")
    current_build = _component_state(current_verification, "build")
    previous_behavior = _component_state(previous_verification, "verification")
    current_behavior = _component_state(current_verification, "verification")
    build_advanced = (
        previous_build == "failed" and current_build == "completed"
        and _component_executed(previous_verification, "build") is True
        and _component_executed(current_verification, "build") is True
    )
    behavior_advanced = (
        previous_behavior == "failed" and current_behavior == "completed"
        and current_build != "failed"
        and _component_executed(previous_verification, "verification") is True
        and _component_executed(current_verification, "verification") is True
    )
    aggregate_advanced = previous_state == "failed" and current_state == "completed"
    verification_advanced = behavior_advanced or aggregate_advanced
    same_root = _same_root(previous, feedback, previous_verification, current_verification)

    inventory = feedback.get("repair_inventory")
    inventory_advanced: bool | str = _UNKNOWN
    if isinstance(inventory, Mapping) and inventory.get("source_comparable") is True:
        inventory_advanced = bool(inventory.get("source_closed_issue_count"))

    substantive = build_advanced or verification_advanced or inventory_advanced is True
    return {
        "comparable": True,
        "same_root_cause": same_root,
        "build_advanced": build_advanced,
        "verification_advanced": verification_advanced,
        "inventory_advanced": inventory_advanced,
        "substantive_progress": substantive,
        "stalled": (not substantive if same_root is True
                    else False if same_root is False else _UNKNOWN),
    }


def build_repair_feedback(record: dict, *, previous: dict | None = None) -> dict:
    """Build deterministic diagnostic feedback from host-collected repair results.

    The function deliberately ignores commits, changed-file lists, and log text
    when deciding progress.  It also does not accept author-declared test IDs as
    progress because no authenticated host source is part of this contract.
    """
    if not isinstance(record, Mapping):
        raise TypeError("repair feedback record must be a mapping")
    raw_updates = record.get("updates", ())
    updates = list(raw_updates) if isinstance(raw_updates, Sequence) and not isinstance(raw_updates, (str, bytes, bytearray)) else []
    updates = [update for update in updates if isinstance(update, Mapping)]
    reviewer_execution_id = _text(record.get("reviewer_execution_id"))
    expected_verification_execution_id = _text(record.get("verification_execution_id"))

    author_update = next((update for update in reversed(updates) if _candidate(_outputs(update), "after_head")), None)
    verification_update = next(
        (update for update in reversed(updates) if expected_verification_execution_id and _text(_result(update).get("command_id")) == expected_verification_execution_id),
        None,
    )
    if verification_update is None:
        verification_update = next(
            (update for update in reversed(updates)
             if _text(update.get("stage")) in _VERIFICATION_STAGES
             and _candidate(_outputs(update), "verification_candidate_id")),
            None,
        )
    if verification_update is None:
        verification_update = next(
            (update for update in reversed(updates)
             if _text(update.get("stage")) in _VERIFICATION_STAGES),
            None,
        )

    author_outputs = _outputs(author_update) if author_update is not None else {}
    candidate_id = (_candidate(author_outputs, "after_candidate_id")
                    if "after_candidate_id" in author_outputs else
                    _candidate(author_outputs, "after_head"))
    verification_candidate_id = (
        _candidate(_outputs(verification_update), "verification_candidate_id")
        if verification_update is not None else None
    )
    verification_execution_id = (
        _text(_result(verification_update).get("command_id"))
        if verification_update is not None else None
    )
    bound_run_id = _bound_run_id(record, updates)
    verification_run_id = (
        _text(_result(verification_update).get("run_id"))
        if verification_update is not None else None
    )
    if (
        candidate_id is None or verification_candidate_id is None
        or verification_execution_id is None or bound_run_id is None
        or verification_run_id != bound_run_id
    ):
        verification_current: bool | str = _UNKNOWN
    else:
        verification_current = (
            candidate_id == verification_candidate_id
            and (
                expected_verification_execution_id is None
                or verification_execution_id == expected_verification_execution_id
            )
        )

    feedback: dict[str, Any] = {
        "schema_version": 1,
        "kind": "repair_feedback",
        "acceptance_evidence": False,
        "request_id": _text(record.get("request_id")),
        "target_agent": _text(record.get("target_agent")),
        "target_scope": _bounded_json(record.get("target_scope")),
        "run_id": bound_run_id,
        "candidate_id": candidate_id,
        "verification_candidate_id": verification_candidate_id,
        "reviewer_execution_id": reviewer_execution_id,
        "expected_verification_execution_id": expected_verification_execution_id,
        "verification_execution_id": verification_execution_id,
        "author": _summary(author_update),
        "verification": _summary(verification_update),
        "verification_current": verification_current,
    }
    baseline = record.get("baseline_verification")
    baseline_outputs = baseline.get("outputs") if isinstance(baseline, Mapping) and isinstance(baseline.get("outputs"), Mapping) else {}
    previous_candidate = (
        _text(previous.get("candidate_id")) if isinstance(previous, Mapping)
        else _candidate(baseline_outputs, "verification_candidate_id")
    )
    inventory = _repair_inventory(
        record, updates, current_candidate=candidate_id,
        previous_candidate=previous_candidate,
    )
    if inventory is not None:
        feedback["repair_inventory"] = inventory
    comparison_base: Any = previous
    if comparison_base is None and isinstance(baseline, Mapping):
        baseline_update = {"stage": "baseline_verification", "result": baseline}
        baseline_candidate = _candidate(baseline_outputs, "verification_candidate_id")
        baseline_execution = _text(baseline.get("command_id"))
        feedback["baseline_verification"] = _summary(baseline_update)
        comparison_base = {
            "kind": "repair_feedback",
            "target_agent": feedback["target_agent"],
            "target_scope": feedback["target_scope"],
            "run_id": feedback["run_id"],
            "candidate_id": baseline_candidate,
            "verification_candidate_id": baseline_candidate,
            "verification_current": bool(baseline_candidate and baseline_execution),
            "verification": feedback["baseline_verification"],
        }
    feedback["progress"] = _comparison(feedback, comparison_base)
    return feedback


def render_repair_feedback(feedback: Mapping[str, Any]) -> str:
    """Render a compact reviewer-facing distinction between failed and absent checks."""
    target = _text(feedback.get("target_agent")) or "unknown target"
    candidate = _text(feedback.get("candidate_id")) or "unknown candidate"
    verification = feedback.get("verification")
    lines = [f"Repair feedback for {target}; candidate {candidate}."]
    if not isinstance(verification, Mapping):
        lines.append("Host verification was not executed or no result was recorded.")
    else:
        execution = _text(verification.get("execution_id")) or "unknown"
        binding = ("current" if feedback.get("verification_current") is True
                   else "stale" if feedback.get("verification_current") is False
                   else _UNKNOWN)
        lines.append(f"Candidate binding: {binding} (verification result {execution}).")
        for label, component in (("Build", "build"), ("Behavior verification", "verification")):
            executed = _component_executed(verification, component)
            status = _text(verification.get(component + "_status")) or _UNKNOWN
            detail = _text(verification.get(component + "_detail"), _MAX_DETAIL)
            line = f"{label}: executed={str(executed).lower()}; status={status}"
            if detail:
                line += f"; detail={detail}"
            lines.append(line + ".")
        state = _verification_state(verification)
        if state == "failed":
            locus = _failure_locus(verification)
            code = _text(_root_code(verification, locus)) or "unspecified_error"
            detail = _text(_root_detail(verification, locus), _MAX_DETAIL) or "no detail"
            lines.append(f"Host verification wrapper recorded failed ({code}): {detail}")
        elif state == "completed":
            lines.append("Host verification wrapper recorded completed.")
        else:
            lines.append("Host verification wrapper outcome is unknown.")
        refs = verification.get("log_refs")
        if isinstance(refs, Sequence) and refs:
            rendered = []
            for ref in list(refs)[:_MAX_REFS]:
                if isinstance(ref, Mapping):
                    rendered.append(str(ref.get("path") or ref.get("uri") or ref.get("name") or "ref"))
                else:
                    rendered.append(str(ref))
            lines.append("Logs: " + ", ".join(rendered))
    progress = feedback.get("progress")
    if isinstance(progress, Mapping):
        lines.append(
            "Diagnostic progress: " + str(progress.get("substantive_progress", _UNKNOWN))
            + "; stalled: " + str(progress.get("stalled", _UNKNOWN)) + "."
        )
    lines.append("This diagnostic does not provide acceptance evidence or control scheduling.")
    return "\n".join(lines)
