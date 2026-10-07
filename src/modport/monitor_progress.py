"""Bounded, host-observed progress evidence for the detached Run monitor.

This module deliberately has no SDK writer and no scheduling side effects.  It
reads only the small, host-produced evidence files under ``artifacts`` and
turns them into a bounded comparison that a host may use when deciding whether
to continue a Run after a wall-clock budget stop.

Revision numbers, commits, unverified changed files, agent reports, and SDK
completion are intentionally excluded from the progress decision.  A byte
change is considered only when a host-created candidate snapshot proves it and
the path is a product source, build configuration, or runtime resource.
"""

from __future__ import annotations

from hashlib import sha256
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping


INITIAL_BUDGET_SECONDS = 8 * 60 * 60
MAX_BUDGET_SECONDS = 12 * 60 * 60
EXTENSION_QUANTUM_SECONDS = 4 * 60 * 60

MAX_ARTIFACT_FILES = 4096
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_TEXT_BYTES = 256 * 1024
MAX_TEXT_TOTAL_BYTES = 2 * 1024 * 1024
MAX_INVENTORIES = 512
MAX_VERIFICATION_RUNS = 256
MAX_TEST_IDS = 4096
MAX_FAILURES = 256
MAX_ISSUE_IDS = 20_000
MAX_CANDIDATE_FILES = 20_000

_INVENTORY_NAMES = frozenset({"inventory.json", "review-inventory.json"})
_VERIFICATION_NAMES = frozenset({
    "baseline-contract-tests.json", "characterization-diagnostic.json",
    "verification.json", "verification-report.json", "test-results.json",
})
_DEPENDENCY_MARKERS = (
    "dependency_resolution_failed", "dependency_rate_limited",
    "could not resolve", "could not get resource", "unknownhostexception",
    "temporary failure in name resolution", "received status code 429",
    "received status code 503", "connection timed out", "connect timed out",
    "network is unreachable", "connection refused",
)
_STATUS_FAILURES = frozenset({"failed", "failure", "error", "blocked"})
_STATUS_SUCCESS = frozenset({"passed", "succeeded", "success", "completed"})


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value: Any) -> str:
    try:
        return sha256(_canonical(value)).hexdigest()
    except (TypeError, ValueError, OverflowError):
        return sha256(repr(value).encode("utf-8", "replace")).hexdigest()


def _text(value: Any, limit: int = 512) -> str | None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        return None
    return " ".join(value.split())[:limit]


def _bounded_strings(value: Any, *, limit: int) -> list[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    result = []
    for item in value:
        text = _text(item, 256)
        if text is not None and text not in result:
            result.append(text)
            if len(result) >= limit:
                break
    return sorted(result)


def _contained(root: Path, path: Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError):
        return False


def _artifact_files(root: Path, *, suffixes: frozenset[str] | None = None,
                    names: frozenset[str] | None = None,
                    max_files: int = MAX_ARTIFACT_FILES) -> list[Path]:
    """Return bounded regular files without following artifact symlinks."""
    artifacts = root / "artifacts"
    if artifacts.is_symlink() or not artifacts.is_dir():
        return []
    found: list[Path] = []
    visited_dirs = 0
    for directory, dirnames, filenames in __import__("os").walk(artifacts, followlinks=False):
        visited_dirs += 1
        if visited_dirs > MAX_ARTIFACT_FILES:
            break
        dirnames[:] = [name for name in dirnames
                       if name not in {".git", "__pycache__"}
                       and not (Path(directory) / name).is_symlink()]
        for name in sorted(filenames):
            path = Path(directory) / name
            if suffixes is not None and path.suffix.lower() not in suffixes:
                continue
            if names is not None and path.name.lower() not in names:
                continue
            if _contained(root, path):
                found.append(path)
                if len(found) >= max_files:
                    return found
    return found


def _read_json(path: Path) -> Any | None:
    try:
        info = path.stat()
        if info.st_size > MAX_JSON_BYTES:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        return None


def _issue_rows(inventory: Mapping[str, Any]) -> list[Any]:
    rows = inventory.get("issues", inventory.get("issue_ids", ()))
    if isinstance(rows, Mapping):
        rows = list(rows.values())
    if not isinstance(rows, (list, tuple)):
        return []
    return list(rows[:MAX_ISSUE_IDS])


def _issue_id(row: Any) -> str | None:
    if isinstance(row, Mapping):
        for key in ("issue_id", "id", "key"):
            value = _text(row.get(key), 256)
            if value is not None:
                return value
        # Rule IDs are only a fallback for legacy inventories.  A repeated
        # rule can represent multiple locations, so current inventories should
        # provide issue_id and will not hit this branch.
        return _text(row.get("rule_id"), 256)
    return _text(row, 256)


def _issue_is_open(row: Any) -> bool:
    if not isinstance(row, Mapping):
        return True
    status = _text(row.get("status"), 64)
    return status is None or status.lower() not in {"closed", "resolved", "fixed", "verified"}


def _scope_digest(inventory: Mapping[str, Any]) -> str:
    coverage = inventory.get("coverage")
    scope = inventory.get("scan_scope")
    selected: dict[str, Any] = {}
    if isinstance(scope, Mapping):
        for key in ("ruleset", "rule_ids", "source_files", "build_files", "resource_files",
                    "scope_sha256"):
            if key in scope:
                selected[key] = scope[key]
    if isinstance(coverage, Mapping):
        for key in ("excluded_directory_names", "excluded_directory_prefixes",
                    "limits", "source_complete", "scan_complete", "complete"):
            if key in coverage:
                selected[key] = coverage[key]
    explicit = _text(inventory.get("scope_sha256"), 128)
    if explicit is not None:
        selected["explicit_scope_sha256"] = explicit
    return _digest(selected)


def _inventory_complete(inventory: Mapping[str, Any]) -> bool:
    coverage = inventory.get("coverage")
    if not isinstance(coverage, Mapping):
        return inventory.get("scan_complete") is True
    if coverage.get("complete") is not True and coverage.get("scan_complete") is not True:
        return False
    return coverage.get("source_complete", True) is not False and coverage.get("truncated", False) is not True


def _inventory_summary(path: Path, root: Path, inventory: Mapping[str, Any]) -> dict[str, Any]:
    issue_ids = sorted({_issue_id(row) for row in _issue_rows(inventory)
                        if _issue_is_open(row) and _issue_id(row) is not None})
    issue_ids = issue_ids[:MAX_ISSUE_IDS]
    try:
        observed_ns = path.stat().st_mtime_ns
    except OSError:
        observed_ns = 0
    return {
        "path": path.relative_to(root).as_posix(),
        "observed_ns": observed_ns,
        "execution_id": _text(inventory.get("execution_id"), 300),
        "candidate_id": _text(inventory.get("candidate_id"), 128),
        "scope_sha256": _scope_digest(inventory),
        "complete": _inventory_complete(inventory),
        "issue_count": len(issue_ids),
        "issue_ids": issue_ids,
        "issue_ids_sha256": _digest(issue_ids),
    }


def _inventory_map(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Select the newest inventory for each scan scope deterministically."""
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        scope = item["scope_sha256"]
        current = result.get(scope)
        if current is None or (item.get("observed_ns", 0), item.get("path", "")) > (
                current.get("observed_ns", 0), current.get("path", "")):
            result[scope] = item
    return result


def _collect_inventories(root: Path) -> dict[str, Any]:
    paths = _artifact_files(root, names=_INVENTORY_NAMES, max_files=MAX_INVENTORIES)
    items = []
    for path in paths:
        data = _read_json(path)
        if isinstance(data, Mapping):
            items.append(_inventory_summary(path, root, data))
    selected = _inventory_map(items)
    public = []
    for item in sorted(selected.values(), key=lambda row: row["path"]):
        public.append({key: value for key, value in item.items() if key != "issue_ids"})
    return {
        "snapshot_count": len(items),
        "scopes": public,
        "by_scope": selected,
    }


def _candidate_file_is_product(relative: str) -> tuple[str, str | None]:
    """Classify a host-collected candidate path for real patch evidence.

    Documentation, generated reports, and ``.modport`` bookkeeping are
    intentionally excluded.  The second value says whether the path belongs
    to source, build configuration, or runtime resources and is used only for
    bounded reporting.
    """
    relative = relative.replace("\\", "/").lstrip("/")
    if relative.startswith((".modport/", ".git/")):
        return "", None
    if relative.startswith(("src/main/java/", "src/main/kotlin/", "src/main/groovy/")):
        return relative, "source"
    if relative in {"build.gradle", "settings.gradle", "gradle.properties"} or relative.startswith("gradle/"):
        return relative, "build"
    if relative.startswith(("src/main/resources/", "src/generated/resources/")):
        return relative, "resource"
    return "", None


def _candidate_summary(path: Path, root: Path, value: Mapping[str, Any]) -> dict[str, Any] | None:
    # A candidate-files report is a host-created mapping from relative paths
    # to observed bytes.  Do not accept a model's list of changed files as a
    # substitute for this shape.
    if not value or not all(isinstance(key, str) and isinstance(item, Mapping)
                            and isinstance(item.get("sha256"), str)
                            for key, item in list(value.items())[:64]):
        return None
    files: dict[str, str] = {}
    kinds = {"source": 0, "build": 0, "resource": 0}
    for key, item in list(value.items())[:MAX_CANDIDATE_FILES]:
        relative, kind = _candidate_file_is_product(key)
        if kind is None:
            continue
        digest = _text(item.get("sha256"), 128)
        if digest is None:
            continue
        files[relative] = digest
        kinds[kind] += 1
    if not files:
        return None
    try:
        observed_ns = path.stat().st_mtime_ns
    except OSError:
        observed_ns = 0
    return {
        "path": path.relative_to(root).as_posix(),
        "observed_ns": observed_ns,
        "files": files,
        "file_count": len(files),
        "source_files": kinds["source"],
        "build_files": kinds["build"],
        "resource_files": kinds["resource"],
    }


def _collect_candidates(root: Path) -> dict[str, Any]:
    paths = [path for path in _artifact_files(root, names=frozenset({"candidate-files.json"}),
                                               max_files=MAX_CANDIDATE_FILES)
             if "/handoff/" not in path.as_posix()]
    rows: list[dict[str, Any]] = []
    for path in paths:
        data = _read_json(path)
        if isinstance(data, Mapping):
            row = _candidate_summary(path, root, data)
            if row is not None:
                rows.append(row)
    rows.sort(key=lambda row: (row["observed_ns"], row["path"]))
    latest = rows[-1] if rows else None
    prior = rows[-2] if len(rows) > 1 else None
    changed: set[str] = set()
    if latest is not None and prior is not None:
        before = prior["files"]
        after = latest["files"]
        changed = {key for key in set(before) | set(after) if before.get(key) != after.get(key)}
    public_latest = None
    if latest is not None:
        public_latest = {key: value for key, value in latest.items() if key != "files"}
    return {
        "snapshot_count": len(rows),
        "latest": public_latest,
        "changed_files_since_previous_snapshot": len(changed),
        "changed_source_files": sum(1 for path in changed if path.startswith(("src/main/java/", "src/main/kotlin/", "src/main/groovy/"))),
        "changed_build_files": sum(1 for path in changed if path in {"build.gradle", "settings.gradle", "gradle.properties"} or path.startswith("gradle/")),
        "changed_resource_files": sum(1 for path in changed if path.startswith(("src/main/resources/", "src/generated/resources/"))),
        "changed_paths": sorted(changed)[:MAX_CANDIDATE_FILES],
        "latest_private": latest,
    }


def _verification_candidate(data: Mapping[str, Any], path: Path) -> bool:
    name = path.name.lower()
    diagnostics = data.get("diagnostics") if isinstance(data.get("diagnostics"), Mapping) else data
    authenticated = diagnostics.get("authenticated_test_ids")
    records = data.get("evidence_records")
    host_evidence = (isinstance(authenticated, list) and bool(authenticated)
                     or isinstance(records, Mapping) and bool(records)
                     or isinstance(records, list) and bool(records)
                     or data.get("host_verified") is True)
    if name in _VERIFICATION_NAMES:
        return host_evidence
    # Only host diagnostic shapes qualify.  A model report or ordinary receipt
    # may claim a test was run, but cannot make it verification evidence.
    if data.get("kind") == "characterization_diagnostic":
        return host_evidence
    return False


def _verification_summary(path: Path, root: Path, data: Mapping[str, Any]) -> dict[str, Any] | None:
    if not _verification_candidate(data, path):
        return None
    diagnostics = data.get("diagnostics") if isinstance(data.get("diagnostics"), Mapping) else data
    ids = _bounded_strings(diagnostics.get("authenticated_test_ids"), limit=MAX_TEST_IDS)
    records = data.get("evidence_records")
    if not ids and isinstance(records, Mapping):
        ids = _bounded_strings(list(records.keys()), limit=MAX_TEST_IDS)
    if not ids and isinstance(records, list):
        ids = _bounded_strings(
            [row.get("test_id") for row in records if isinstance(row, Mapping)],
            limit=MAX_TEST_IDS)
    execution = (_text(data.get("execution_id"), 300)
                 or _text(diagnostics.get("execution_id"), 300)
                 or path.parent.name[:300])
    status = (_text(data.get("verification_status"), 64)
              or _text(data.get("status"), 64)
              or _text(diagnostics.get("verification_status"), 64))
    code = (_text(data.get("error_code"), 128)
            or _text(diagnostics.get("error_code"), 128))
    failure_signature = (_text(data.get("failure_signature"), 800)
                         or _text(diagnostics.get("failure_signature"), 800))
    if status is None:
        status = "failed" if code or diagnostics.get("category") in _STATUS_FAILURES else "observed"
    return {
        "path": path.relative_to(root).as_posix(),
        "execution_id": execution,
        "status": status,
        "error_code": code,
        "failure_signature": failure_signature,
        "authenticated_test_ids": ids,
        "authenticated_test_count": len(ids),
        "evidence": bool(ids or records or data.get("host_verified") is True),
    }


def _collect_verification(root: Path) -> dict[str, Any]:
    paths = _artifact_files(root, suffixes=frozenset({".json"}), max_files=MAX_ARTIFACT_FILES)
    rows: list[dict[str, Any]] = []
    for path in paths:
        if "/handoff/" in path.as_posix():
            continue
        data = _read_json(path)
        if isinstance(data, Mapping):
            row = _verification_summary(path, root, data)
            if row is not None:
                rows.append(row)
                if len(rows) >= MAX_VERIFICATION_RUNS:
                    break
    rows.sort(key=lambda row: (row["execution_id"] or "", row["path"]))
    run_ids = sorted({row["execution_id"] for row in rows if row.get("execution_id")})
    test_ids = sorted({test_id for row in rows for test_id in row.get("authenticated_test_ids", ())})[:MAX_TEST_IDS]
    successful = sum(1 for row in rows if str(row.get("status", "")).lower() in _STATUS_SUCCESS)
    failed = sum(1 for row in rows if str(row.get("status", "")).lower() in _STATUS_FAILURES
                 or row.get("error_code"))
    public_rows = [{key: value for key, value in row.items() if key != "authenticated_test_ids"}
                   for row in rows]
    return {
        "run_count": len(run_ids),
        "execution_ids": run_ids,
        "authenticated_test_count": len(test_ids),
        "authenticated_test_ids": test_ids,
        "successful_runs": successful,
        "failed_runs": failed,
        "rows": public_rows,
        "evidence_sha256": _digest({"runs": run_ids, "tests": test_ids, "rows": public_rows}),
    }


def _dependency_failure(value: Any) -> str | None:
    if isinstance(value, str):
        lower = value.lower()
        for marker in _DEPENDENCY_MARKERS:
            if marker == "could not resolve":
                if any(marker in line and "could not resolve keysym " not in line
                       for line in lower.splitlines()):
                    return marker
            elif marker in lower:
                return marker
        return None
    if isinstance(value, Mapping):
        for key in ("error_code", "code", "detail", "error", "reason", "message"):
            found = _dependency_failure(value.get(key))
            if found is not None:
                return found
    elif isinstance(value, (list, tuple)):
        for item in value[:64]:
            found = _dependency_failure(item)
            if found is not None:
                return found
    return None


def _collect_dependency_failures(root: Path) -> dict[str, Any]:
    failures: set[str] = set()
    paths = _artifact_files(root, suffixes=frozenset({".json", ".txt", ".log", ".jsonl"}),
                            max_files=MAX_ARTIFACT_FILES)
    text_budget = MAX_TEXT_TOTAL_BYTES
    for path in paths:
        if "/handoff/" in path.as_posix():
            continue
        try:
            size = path.stat().st_size
            if size > (MAX_JSON_BYTES if path.suffix == ".json" else MAX_TEXT_BYTES):
                continue
            if path.suffix == ".json":
                value = _read_json(path)
                marker = _dependency_failure(value)
            else:
                if text_budget <= 0:
                    break
                raw = path.read_bytes()[:min(size, text_budget)]
                text_budget -= len(raw)
                marker = _dependency_failure(raw.decode("utf-8", "replace"))
        except (OSError, UnicodeError):
            continue
        if marker is not None:
            failures.add(f"{path.relative_to(root).as_posix()}:{marker}")
            if len(failures) >= MAX_FAILURES:
                break
    values = sorted(failures)
    return {"count": len(values), "items": values,
            "items_sha256": _digest(values)}


def collect_progress_evidence(root: str | Path) -> dict[str, Any]:
    """Collect bounded, host-visible evidence without interpreting agent claims."""
    root = Path(root).resolve()
    inventories = _collect_inventories(root)
    candidates = _collect_candidates(root)
    verification = _collect_verification(root)
    dependencies = _collect_dependency_failures(root)
    # The issue IDs are retained only in the private comparison map.  Public
    # status remains small even when a scanner reports thousands of findings.
    private = {scope: {key: value for key, value in item.items()
                       if key in {"complete", "issue_count", "issue_ids", "issue_ids_sha256"}}
               for scope, item in inventories["by_scope"].items()}
    public_inventories = {key: value for key, value in inventories.items() if key != "by_scope"}
    public_candidates = {key: value for key, value in candidates.items()
                         if key not in {"latest_private", "changed_paths"}}
    public = {
        "inventories": public_inventories,
        "candidates": public_candidates,
        "verification": verification,
        "dependency_failures": dependencies,
    }
    private["candidates"] = {
        "latest": candidates.get("latest_private"),
        "changed_paths": candidates.get("changed_paths", []),
    }
    return {"public": public, "private": private, "signature": _digest(public)}


def _as_private(evidence: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(evidence, Mapping):
        return {}
    value = evidence.get("private")
    return value if isinstance(value, Mapping) else {}


def _as_public(evidence: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(evidence, Mapping):
        return {}
    value = evidence.get("public")
    return value if isinstance(value, Mapping) else evidence


def _inventory_delta(previous: Mapping[str, Any] | None,
                     current: Mapping[str, Any]) -> dict[str, Any]:
    before = _as_private(previous)
    after = _as_private(current)
    closed: set[str] = set()
    introduced: set[str] = set()
    comparable = False
    for scope, current_row in after.items():
        prior_row = before.get(scope)
        if not isinstance(current_row, Mapping) or not isinstance(prior_row, Mapping):
            continue
        if current_row.get("complete") is not True or prior_row.get("complete") is not True:
            continue
        old = set(_bounded_strings(prior_row.get("issue_ids"), limit=MAX_ISSUE_IDS))
        new = set(_bounded_strings(current_row.get("issue_ids"), limit=MAX_ISSUE_IDS))
        comparable = True
        closed.update(old - new)
        introduced.update(new - old)
    return {
        "comparable": comparable,
        "closed": len(closed),
        "introduced": len(introduced),
        "closed_ids_sha256": _digest(sorted(closed)),
        "introduced_ids_sha256": _digest(sorted(introduced)),
    }


def _verification_delta(previous: Mapping[str, Any] | None,
                        current: Mapping[str, Any]) -> dict[str, Any]:
    before = _as_public(previous).get("verification", {}) if isinstance(_as_public(previous), Mapping) else {}
    after = _as_public(current).get("verification", {}) if isinstance(_as_public(current), Mapping) else {}
    old_runs = set(_bounded_strings(before.get("execution_ids"), limit=MAX_VERIFICATION_RUNS)) if isinstance(before, Mapping) else set()
    new_runs = set(_bounded_strings(after.get("execution_ids"), limit=MAX_VERIFICATION_RUNS)) if isinstance(after, Mapping) else set()
    old_tests = set(_bounded_strings(before.get("authenticated_test_ids"), limit=MAX_TEST_IDS)) if isinstance(before, Mapping) else set()
    new_tests = set(_bounded_strings(after.get("authenticated_test_ids"), limit=MAX_TEST_IDS)) if isinstance(after, Mapping) else set()
    old_rows = {row.get("execution_id"): row for row in before.get("rows", ())
                if isinstance(row, Mapping) and row.get("execution_id")} if isinstance(before, Mapping) else {}
    new_rows = {row.get("execution_id"): row for row in after.get("rows", ())
                if isinstance(row, Mapping) and row.get("execution_id")} if isinstance(after, Mapping) else {}
    new_run_ids = new_runs - old_runs
    successful_new = 0
    for execution_id in new_run_ids:
        row = new_rows.get(execution_id, {})
        status = str(row.get("status", "")).lower()
        if (status in _STATUS_SUCCESS and row.get("evidence") is True
                and not row.get("error_code")):
            successful_new += 1
    successful_transition = 0
    for execution_id, row in new_rows.items():
        previous_row = old_rows.get(execution_id)
        if previous_row is None:
            continue
        if (str(row.get("status", "")).lower() in _STATUS_SUCCESS
                and row.get("evidence") is True
                and not row.get("error_code")
                and str(previous_row.get("status", "")).lower() not in _STATUS_SUCCESS):
            successful_transition += 1
    return {
        "new_runs": len(new_run_ids),
        "new_tests": len(new_tests - old_tests),
        "successful_new_runs": successful_new,
        "successful_transitions": successful_transition,
        "tangible": bool(new_tests - old_tests or successful_new or successful_transition),
        "runs": len(new_runs), "tests": len(new_tests),
        "new_run_ids_sha256": _digest(sorted(new_run_ids)),
        "new_test_ids_sha256": _digest(sorted(new_tests - old_tests)),
    }


def compare_progress_evidence(previous: Mapping[str, Any] | None,
                              current: Mapping[str, Any]) -> dict[str, Any]:
    """Return actual deltas; repeated reports and source churn do not count."""
    inventory = _inventory_delta(previous, current)
    verification = _verification_delta(previous, current)
    public = _as_public(current)
    previous_public = _as_public(previous)
    current_candidates = public.get("candidates", {}) if isinstance(public, Mapping) else {}
    current_private = _as_private(current).get("candidates", {})
    previous_private = _as_private(previous).get("candidates", {})
    current_latest = current_private.get("latest") if isinstance(current_private, Mapping) else None
    previous_latest = previous_private.get("latest") if isinstance(previous_private, Mapping) else None
    candidate_changed: set[str] = set()
    if isinstance(current_latest, Mapping) and isinstance(previous_latest, Mapping):
        before_files = previous_latest.get("files", {})
        after_files = current_latest.get("files", {})
        if isinstance(before_files, Mapping) and isinstance(after_files, Mapping):
            candidate_changed = {key for key in set(before_files) | set(after_files)
                                 if before_files.get(key) != after_files.get(key)}
    # A current collection can already contain two host-verified candidate
    # snapshots when the monitor starts after a worker finishes.  Preserve that
    # observed delta so a continuation can be justified without counting a
    # commit or a revision.
    if previous is None and not candidate_changed and isinstance(current_candidates, Mapping):
        changed_paths = _as_private(current).get("candidates", {})
        changed_paths = changed_paths.get("changed_paths", []) if isinstance(changed_paths, Mapping) else []
        candidate_changed = set(_bounded_strings(changed_paths, limit=MAX_CANDIDATE_FILES))
        if not candidate_changed and _count(current_candidates.get("changed_files_since_previous_snapshot", 0)) > 0:
            # The host found a delta but did not retain its paths.  Keep it
            # diagnostic only; it must not grant an extension.
            candidate_changed = {"<unattributed-candidate-delta>"}
    source_changed = 0
    build_changed = 0
    resource_changed = 0
    for path in candidate_changed:
        _, kind = _candidate_file_is_product(path)
        if kind == "source":
            source_changed += 1
        elif kind == "build":
            build_changed += 1
        elif kind == "resource":
            resource_changed += 1
    patch = {
        "candidate_changed_files": len(candidate_changed),
        "candidate_changed_ids_sha256": _digest(sorted(candidate_changed)),
        "source_changed_files": source_changed,
        "build_changed_files": build_changed,
        "resource_changed_files": resource_changed,
    }
    dependencies = public.get("dependency_failures", {})
    previous_dependencies = previous_public.get("dependency_failures", {}) if isinstance(previous_public, Mapping) else {}
    current_dependency_items = set(_bounded_strings(dependencies.get("items"), limit=MAX_FAILURES)) if isinstance(dependencies, Mapping) else set()
    old_dependency_items = set(_bounded_strings(previous_dependencies.get("items"), limit=MAX_FAILURES)) if isinstance(previous_dependencies, Mapping) else set()
    new_dependencies = sorted(current_dependency_items - old_dependency_items)
    tangible = bool(inventory["closed"] or verification["tangible"]
                    or patch["source_changed_files"]
                    or patch["build_changed_files"]
                    or patch["resource_changed_files"])
    return {
        "tangible_progress": tangible,
        "patch_bytes": patch,
        "inventory": inventory,
        "verification": verification,
        "dependency_failures": {
            "count": len(current_dependency_items),
            "new": len(new_dependencies),
            "new_items_sha256": _digest(new_dependencies),
        },
        "signature": _digest({"inventory": inventory, "verification": verification,
                              "patch_bytes": patch,
                              "verification_observation": public.get("verification", {}).get("evidence_sha256")
                              if isinstance(public.get("verification"), Mapping) else None,
                              "dependencies": sorted(current_dependency_items)}),
    }


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _count(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


def budget_extension_decision(progress_delta: Mapping[str, Any], *, now: float,
                              started_at: float, current_deadline: float | None,
                              initial_seconds: int = INITIAL_BUDGET_SECONDS,
                              maximum_seconds: int = MAX_BUDGET_SECONDS,
                              quantum_seconds: int = EXTENSION_QUANTUM_SECONDS) -> dict[str, Any]:
    """Authorize a bounded continuation recommendation, never a state mutation.

    The absolute cap is measured from the Run start.  A caller should pass the
    returned ``additional_seconds`` to the public continuation/reopen API and
    persist that API's authenticated result.  This function returns zero when
    evidence is missing, the initial budget is not exhausted, the cap is
    reached, or progress is only diagnostic.
    """
    values = (now, started_at, current_deadline)
    if any(_finite_number(value) is None for value in values[:2]):
        raise ValueError("now and started_at must be finite numbers")
    if current_deadline is not None and _finite_number(current_deadline) is None:
        raise ValueError("current_deadline must be finite or None")
    if any(type(value) is not int or value <= 0 for value in
           (initial_seconds, maximum_seconds, quantum_seconds)):
        raise ValueError("budget limits must be positive integers")
    if maximum_seconds < initial_seconds or quantum_seconds > maximum_seconds:
        raise ValueError("budget limits are inconsistent")
    now = float(now)
    started_at = float(started_at)
    absolute_cap = started_at + maximum_seconds
    initial_deadline = started_at + initial_seconds
    deadline = absolute_cap if current_deadline is None else float(current_deadline)
    tangible = progress_delta.get("tangible_progress") is True
    remaining = max(0.0, absolute_cap - max(now, deadline))
    amount = min(float(quantum_seconds), remaining)
    if now < initial_deadline:
        reason = "initial_budget_not_exhausted"
    elif not tangible:
        reason = "no_authenticated_progress"
    elif amount <= 0:
        reason = "absolute_budget_cap_reached"
    elif current_deadline is not None and now < float(current_deadline):
        reason = "current_budget_not_exhausted"
    else:
        reason = "authenticated_progress"
    eligible = reason == "authenticated_progress"
    return {
        "eligible": eligible,
        "reason": reason,
        "initial_budget_seconds": initial_seconds,
        "maximum_budget_seconds": maximum_seconds,
        "quantum_seconds": quantum_seconds,
        "started_at": started_at,
        "initial_deadline": initial_deadline,
        "absolute_cap": absolute_cap,
        "current_deadline": current_deadline,
        "additional_seconds": int(amount) if eligible else 0,
        "progress_basis": {
            "tangible_progress": tangible,
            "inventory_closed": _count(progress_delta.get("inventory", {}).get("closed", 0)),
            "verification_runs": _count(progress_delta.get("verification", {}).get("new_runs", 0)),
            "verification_tests": _count(progress_delta.get("verification", {}).get("new_tests", 0)),
            "successful_verifications": _count(progress_delta.get("verification", {}).get("successful_new_runs", 0)) + _count(progress_delta.get("verification", {}).get("successful_transitions", 0)),
            "source_changed_files": _count(progress_delta.get("patch_bytes", {}).get("source_changed_files", 0)),
            "build_changed_files": _count(progress_delta.get("patch_bytes", {}).get("build_changed_files", 0)),
            "resource_changed_files": _count(progress_delta.get("patch_bytes", {}).get("resource_changed_files", 0)),
        },
    }
