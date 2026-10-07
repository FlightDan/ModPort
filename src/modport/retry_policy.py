"""Strict, auditable policy for creating a new Run from a settled parent."""
from dataclasses import fields, replace
from pathlib import Path, PurePosixPath
from typing import Mapping
import re

from .evidence import digest, file_digest, seal_ref
from .models import Budget, MigrationRequest

HARNESS_OUTPUT_DIRS = frozenset({"evidence", "runtime", "cache", ".gradle", "build", "run", "logs", "run-client", "run-server"})
HARNESS_HOST_OUTPUTS = frozenset({"background.md", "preparation.json", "project-init.md",
    "mod-analysis.json", "mod-analysis.md", "gap-research", "contract-review.json", "test-assessment.json",
    "migration-plan.md", "migration-plan.json", "dependency-plan.md", "development-plan.json"})

BUDGET_FIELDS = frozenset(field.name for field in fields(Budget))


def _request(value):
    if isinstance(value, MigrationRequest):
        value.validate()
        return value
    if not isinstance(value, Mapping):
        raise ValueError("retry request must be a MigrationRequest or mapping")
    if set(value) - {field.name for field in fields(MigrationRequest)}:
        raise ValueError("unsupported retry request field")
    request = MigrationRequest.from_mapping(value)
    request.validate()
    if request.to_dict() != dict(value):
        raise ValueError("retry request must use the complete canonical request shape")
    return request


def apply_budget_overrides(parent_request, budget_overrides=None, reason=None):
    """Apply explicitly supplied budget fields without changing any other input."""
    parent = _request(parent_request)
    if not re.fullmatch(r"[0-9a-fA-F]{40}", parent.source_revision or ""):
        raise ValueError("retry requires a resolved immutable source commit")
    if budget_overrides is None:
        budget_overrides = {}
    if not isinstance(budget_overrides, Mapping):
        raise ValueError("budget overrides must be a mapping")
    if set(budget_overrides) - BUDGET_FIELDS:
        raise ValueError("only Budget fields may be overridden")
    if reason is not None and (not isinstance(reason, str) or not reason.strip()):
        raise ValueError("budget reason must be a non-empty string")
    budget = replace(parent.budget, **dict(budget_overrides))
    budget.validate()
    if budget != parent.budget and reason is None:
        raise ValueError("changed retry budget requires a non-empty budget reason")
    return replace(parent, budget=budget)


def validate_retry_request(parent_request, child_request, budget_overrides=None, reason=None, *, dependency_cache=None):
    """Return a frozen-input-ready audit record; reject unrequested differences."""
    parent = _request(parent_request)
    child = _request(child_request)
    expected = apply_budget_overrides(parent, budget_overrides, reason)
    if dependency_cache is not None:
        expected = replace(expected, dependency_cache=str(Path(dependency_cache).absolute()))
        expected.validate()
    if child.to_dict() != expected.to_dict():
        raise ValueError("retry request differs from authorized source-bound parent inputs")
    before, after = parent.budget.to_dict(), child.budget.to_dict()
    return {"overrides": dict(budget_overrides or {}),
            "changes": {name: {"before": before[name], "after": after[name]}
                        for name in sorted(BUDGET_FIELDS) if before[name] != after[name]},
            "reason": reason,
            "dependency_cache_override": None if dependency_cache is None else {
                "before": parent.dependency_cache, "after": child.dependency_cache}}


def is_harness_runtime_output(relative):
    """Identify generated harness directories before a source tree boundary.

    A nested Gradle project may produce build/run/logs at any depth. Once
    inside src, these names can be ordinary source packages. Evidence stays
    separate so callers retain their existing evidence-code fingerprint rules.
    This predicate does not identify host-authored planning documents.
    """
    parts = PurePosixPath(relative).parts
    if len(parts) < 2 or parts[0] != ".modport":
        return False
    if any(part in {".gradle", "__pycache__"} for part in parts[1:]):
        return True
    if parts[1] == "evidence":
        return False
    output_dirs = HARNESS_OUTPUT_DIRS - {"evidence"}
    for part in parts[1:]:
        if part == "src":
            break
        if part in output_dirs:
            return True
    return False


def is_harness_source(relative):
    """Shared producer/consumer exclusion policy for generated harness output."""
    path = PurePosixPath(relative)
    return not (is_harness_runtime_output(relative)
                or len(path.parts) > 1 and path.parts[1] in HARNESS_HOST_OUTPUTS | {"evidence"})


def _harness_path(value):
    if not isinstance(value, str) or "\\" in value:
        raise ValueError("unsafe inherited harness path")
    path = PurePosixPath(value)
    if (path.as_posix() != value or path.is_absolute() or len(path.parts) < 2
            or path.parts[0] != ".modport" or any(p in {"..", ".git"} for p in path.parts)
            or not is_harness_source(value)):
        raise ValueError("unsafe inherited harness path")
    return path


def _contained_reference(root: Path, ref: Mapping) -> Path:
    """Resolve a parent reference by location, without a stale-byte check.

    A retry is created by the same isolated host that produced the parent. The
    current baseline is therefore the input to inherit; a previous artifact
    digest must not veto ordinary work completed after that artifact was
    written. Keep path containment and regular-file checks so a malformed
    reference cannot make the host read outside the parent Run.
    """
    if not isinstance(ref, Mapping):
        raise ValueError("parent evidence reference must be an object")
    relative = Path(str(ref.get("path", "")))
    path = root / relative
    if (relative.is_absolute() or not relative.parts or ".." in relative.parts
            or path.is_symlink() or not path.is_file()
            or not path.resolve().is_relative_to(root.resolve())):
        raise ValueError(f"parent evidence path is not a contained regular file: {relative}")
    return path


def build_harness_snapshot(parent_root, parent_refs, *, parent_run_id, source_commit):
    """Collect the current baseline harness for an explicit child retry.

    The child inherits the files currently present in the parent's isolated
    baseline. It does not compare them with an earlier snapshot: generated
    harness files are expected to change while the Run is being repaired.
    Runtime output remains excluded and paths remain contained. Every file is
    copied into a fresh content-addressed object for the child, and the child
    must still run fresh verification before accepting it.
    """
    root = Path(parent_root).absolute()
    if not re.fullmatch(r"[0-9a-fA-F]{40}", source_commit or ""):
        raise ValueError("harness inheritance requires an immutable source commit")
    base = root / "baseline"
    harness = base / ".modport"
    if harness.is_symlink() or not harness.is_dir() or harness.resolve() != harness.absolute():
        raise ValueError("current baseline harness is unavailable or unsafe")
    files = []
    for path in sorted(harness.rglob("*")):
        if path.is_symlink():
            raise ValueError("inherited harness must not contain symlinks")
        relative = path.relative_to(base).as_posix()
        if ".git" in path.relative_to(base).parts:
            raise ValueError("inherited harness must not contain .git")
        if path.is_dir():
            continue
        if not is_harness_source(relative):
            continue
        _harness_path(relative)
        if not path.is_file():
            raise ValueError("inherited harness must contain only regular files")
        ref = seal_ref(root, {
            "path": path.relative_to(root).as_posix(),
            "sha256": file_digest(path),
            "media_type": "application/octet-stream",
        }, execution_id="retry-harness")
        files.append({"path": relative, "parent_ref_key": "current-baseline:" + relative,
                      "ref": ref})
    by_path = {entry["path"]: entry for entry in files}
    candidate = by_path.get(".modport/functional-contract.json")
    if candidate is None:
        raise ValueError("harness inheritance requires a candidate contract")

    # Preserve a useful provenance artifact when one is available, but refresh
    # its reference from the current parent file. A stale or missing historical
    # report does not prevent a retry; fresh verification is mandatory below.
    provenance_ref = candidate["ref"]
    for key in ("baseline_contract_tests_candidate", "baseline_harness_snapshot"):
        raw_ref = (parent_refs or {}).get(key) if isinstance(parent_refs, Mapping) else None
        if raw_ref is None:
            continue
        try:
            provenance_path = _contained_reference(root, raw_ref)
        except (OSError, TypeError, ValueError):
            continue
        provenance_ref = seal_ref(root, {
            "path": provenance_path.relative_to(root).as_posix(),
            "sha256": file_digest(provenance_path),
            "media_type": raw_ref.get("media_type", "application/json"),
        }, execution_id="retry-harness")
        break
    manifest = {"schema_version": 1, "parent_run_id": parent_run_id,
                "source_commit": source_commit, "candidate_sha256": candidate["ref"]["sha256"],
                "provenance_ref": provenance_ref, "files": files,
                "requires_fresh_verification": True}
    manifest["snapshot_sha256"] = digest(manifest)
    return manifest
