"""Isolated supervisor goal documents and immutable revision evidence.

The supervisor edits only ``goals/<target-key>.md`` in a dedicated workspace.
All identities and published records stay in the Run artifact area, outside
that workspace, so the supervisor cannot rewrite its own authorization data.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from .platform_files import file_os as os
from pathlib import Path
import stat
from typing import Any, Mapping

from .contracts import OperationInput, OperationResult
from .evidence import digest, file_digest, seal_ref, verified_path
from .manifest import canonical_json
from .local_workspace_sandbox import is_sensitive_name
from .workspace import is_project_workspace, project_path, project_relative


MAX_GOAL_BYTES = 128 * 1024
MAX_SOURCE_FILE_BYTES = 1024 * 1024
MAX_SOURCE_TOTAL_BYTES = 16 * 1024 * 1024
MAX_SOURCE_FILES = 2000
MAX_SOURCE_DIRECTORIES = 10000
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_EXCLUDED_DIRECTORIES = frozenset({
    ".git", ".gradle", ".venv", ".cache", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "build", "dist",
    "node_modules", "runs", "workspaces", "artifacts", "logs", "target",
    "out",
})
_GENERATED_MODPORT_DIRECTORIES = frozenset({
    "run-client", "run-server", "evidence", "goal-reports", "runtime-cache",
})


def target_key(task: Mapping[str, Any], plan_ref: Mapping[str, Any]) -> str:
    """Bind a document to one normalized task and one exact plan reference."""
    if not isinstance(task, Mapping) or not isinstance(plan_ref, Mapping):
        raise TypeError("supervised goal identity needs a task and plan reference")
    task_id = task.get("id")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("supervised goal task needs a non-empty id")
    return digest({"task_id": task_id, "plan_ref": dict(plan_ref)})


def prepare(command: OperationInput) -> OperationInput:
    """Build or resume the supervisor's isolated source and goal workspace."""
    root = _run_root(command)
    targets = _normalize_targets(command.payload.get("supervised_goal_targets"))
    identity = digest({"run_id": command.run_id, "task_id": command.task_id,
                       "stage_id": command.stage_id, "command_id": command.command_id})
    workspace_relative = f"workspaces/supervisor/{identity}"
    manifest_relative = f"artifacts/supervised-goals/manifests/{identity}.json"
    manifest_path = _safe_run_path(root, manifest_relative)

    if manifest_path.exists() or manifest_path.is_symlink():
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise ValueError("supervised goal manifest is not a regular file")
        manifest_ref = _reference_for_existing(root, manifest_relative,
                                               "application/json", command.command_id)
        manifest = _read_json_ref(root, manifest_ref, MAX_MANIFEST_BYTES)
        _validate_manifest_identity(manifest, command, targets, workspace_relative)
        workspace = _safe_run_path(root, workspace_relative)
        if workspace.is_symlink() or not workspace.is_dir():
            raise ValueError("supervisor workspace is missing or unsafe; refusing to reset it")
        _validate_prepared_documents(root, workspace, manifest)
    else:
        _ensure_directory(root, workspace_relative)
        workspace = _safe_run_path(root, workspace_relative)
        source_snapshot = _snapshot_sources(root, workspace, command, targets)
        documents = []
        for target in targets:
            task = target["task"]
            task_objective = _goal_text(task.get("objective"), "task objective")
            initial_objective = task_objective
            goal_ref = target.get("goal_ref")
            previous_revision_ref = target.get("previous_revision_ref")
            original_ref = None
            previous = None
            if previous_revision_ref is not None:
                previous = _read_json_ref(root, previous_revision_ref, MAX_MANIFEST_BYTES)
                _validate_previous_revision(root, previous, previous_revision_ref, target)
                initial_objective = _decode_goal(
                    _read_ref_bytes(root, previous.get("after_ref"), MAX_GOAL_BYTES),
                    "previous supervisor objective",
                )
                original_ref = previous["original_objective_ref"]
            elif goal_ref is not None:
                source_goal = _read_json_ref(root, goal_ref, MAX_MANIFEST_BYTES)
                if "objective" in source_goal:
                    initial_objective = _goal_text(source_goal["objective"], "goal objective")

            before_bytes = initial_objective.encode("utf-8")
            before_ref = (previous["after_ref"] if previous is not None else
                          _publish_object(root, before_bytes, "text/markdown",
                                          command.command_id))
            if original_ref is None:
                original_ref = _publish_json_object(root, {
                    "schema_version": 1,
                    "task_id": task["id"],
                    "plan_ref": target["plan_ref"],
                    "objective": task_objective,
                }, command.command_id)
            relative = f"goals/{target['key']}.md"
            _ensure_directory(root, f"{workspace_relative}/goals")
            destination = _safe_workspace_path(workspace, relative)
            _write_workspace_file(destination, before_bytes, preserve_existing=True)
            documents.append({
                "key": target["key"],
                "task_id": task["id"],
                "task": task,
                "plan_ref": target["plan_ref"],
                "source_execution_id": target["source_execution_id"],
                "source_workspace": target.get("source_workspace"),
                "goal_ref": goal_ref,
                "previous_revision_ref": previous_revision_ref,
                "original_objective": task_objective,
                "original_objective_ref": original_ref,
                "initial_goal_objective": initial_objective,
                "before_ref": before_ref,
                "path": relative,
            })

        manifest = {
            "schema_version": 1,
            "identity": identity,
            "run_id": command.run_id,
            "task_id": command.task_id,
            "stage_id": command.stage_id,
            "command_id": command.command_id,
            "workspace": workspace_relative,
            "snapshot_consistency": "per_file_best_effort_not_atomic",
            "source_snapshot": source_snapshot,
            "documents": documents,
        }
        manifest_bytes = _json_bytes(manifest)
        if len(manifest_bytes) > MAX_MANIFEST_BYTES:
            raise ValueError("supervised goal manifest exceeds the size limit")
        _write_immutable_file(root, manifest_relative, manifest_bytes)
        manifest_ref = _reference_for_existing(root, manifest_relative,
                                               "application/json", command.command_id)

    payload = dict(command.payload)
    payload["supervised_goal_manifest_ref"] = manifest_ref
    payload["supervised_goal_documents"] = [
        {key: document[key] for key in (
            "key", "task_id", "plan_ref", "source_execution_id", "path",
            "original_objective", "original_objective_ref", "before_ref", "goal_ref",
        ) if key in document}
        for document in manifest["documents"]
    ]
    payload["supervised_goal_source_snapshot"] = {
        "consistency": manifest["snapshot_consistency"],
        "root_count": len(manifest["source_snapshot"]["roots"]),
        "copied_file_count": sum(len(item["files"])
                                  for item in manifest["source_snapshot"]["roots"]),
        "skipped_entry_count": sum(len(item["skipped"])
                                    for item in manifest["source_snapshot"]["roots"]),
    }
    options = dict(command.options)
    options["workspace"] = workspace_relative
    artifact_refs = dict(command.artifact_refs)
    artifact_refs["supervised_goal_manifest"] = manifest_ref
    return replace(command, options=options, payload=payload,
                   artifact_refs=artifact_refs)


def collect(command: OperationInput, result: OperationResult) -> OperationResult:
    """Publish changed supervisor documents and retain raw execution evidence."""
    root = _run_root(command)
    outcome = (result if isinstance(result, OperationResult)
               else OperationResult.from_dict(result))
    outcome.validate_for(command)
    manifest_ref = command.payload.get("supervised_goal_manifest_ref")
    manifest = _read_json_ref(root, manifest_ref, MAX_MANIFEST_BYTES)
    workspace_relative = command.options.get("workspace")
    if workspace_relative != manifest.get("workspace"):
        raise ValueError("supervisor workspace differs from its host manifest")
    workspace = _safe_run_path(root, workspace_relative)
    if workspace.is_symlink() or not workspace.is_dir():
        raise ValueError("supervisor workspace is missing or unsafe")
    _validate_manifest_identity(
        manifest, command, _normalize_targets(command.payload.get("supervised_goal_targets")),
        workspace_relative,
    )

    revisions = []
    diagnostics = []
    for document in manifest["documents"]:
        try:
            before_bytes = _read_ref_bytes(root, document["before_ref"], MAX_GOAL_BYTES)
            path = _safe_workspace_path(workspace, document["path"])
            if path.is_symlink():
                raise ValueError("goal document is a symlink")
            if not path.exists() or not path.is_file():
                raise ValueError("goal document was deleted")
            after_bytes = _read_workspace_goal(workspace, document["path"])
            after_text = _decode_goal(after_bytes, "edited goal document")
            if not after_text.strip():
                raise ValueError("goal document is empty")
            if after_bytes == before_bytes:
                continue

            after_ref = _publish_object(root, after_bytes, "text/markdown",
                                        command.command_id)
            completed = outcome.status == "completed"
            revision = {
                "schema_version": 1,
                "key": document["key"],
                "task_id": document["task_id"],
                "plan_ref": document["plan_ref"],
                "original_objective": document["original_objective"],
                "original_objective_ref": document["original_objective_ref"],
                "before_ref": document["before_ref"],
                "after_ref": after_ref,
                "source_execution_id": document["source_execution_id"],
                "supervisor_execution_id": command.command_id,
                "previous_revision_ref": document.get("previous_revision_ref"),
                "applicable": completed,
                "source_snapshot_consistency": manifest["snapshot_consistency"],
            }
            revision_path = (
                "artifacts/supervised-goals/revisions/"
                + digest(revision) + ".json"
            )
            revision_ref = _publish_json_at(root, revision_path, revision,
                                            command.command_id)
            revisions.append({"key": document["key"], "revision_ref": revision_ref})
        except (OSError, ValueError, TypeError, KeyError) as exc:
            diagnostics.append({"key": document.get("key"), "detail": str(exc)})

    outputs = dict(outcome.outputs)
    outputs["supervised_goal_revisions"] = revisions
    artifact_refs = outputs.get("artifact_refs")
    if not isinstance(artifact_refs, Mapping):
        artifact_refs = {}
    else:
        artifact_refs = dict(artifact_refs)
    artifact_refs["supervised_goal_manifest"] = manifest_ref
    for revision in revisions:
        artifact_refs[f"supervised_goal_revision:{revision['key']}"] = revision["revision_ref"]
    outputs["artifact_refs"] = artifact_refs
    if diagnostics:
        outputs["supervised_goal_diagnostics"] = diagnostics
    status = outcome.status
    error_code = outcome.error_code
    detail = outcome.detail
    if diagnostics and status == "completed":
        status = "failed"
        error_code = "supervised_goal_invalid"
        detail = (detail + "; " if detail else "") + "one or more supervised goal documents were invalid"
    elif diagnostics:
        detail = (detail + "; " if detail else "") + "partial supervised goal edits were retained as diagnostic evidence"
    return OperationResult(status=status, run_id=outcome.run_id, task_id=outcome.task_id,
                          stage_id=outcome.stage_id, command_id=outcome.command_id,
                          outputs=outputs, detail=detail, error_code=error_code)


def apply_to_goal(command: OperationInput, goal: Mapping[str, Any],
                  task: Mapping[str, Any]) -> dict:
    """Apply one host-bound v26 revision to only the goal objective."""
    if not isinstance(goal, Mapping) or not isinstance(task, Mapping):
        raise TypeError("goal revision needs goal and task objects")
    result = json.loads(json.dumps(dict(goal), ensure_ascii=False, allow_nan=False))
    revision_ref = command.artifact_refs.get("supervised_goal_revision")
    if revision_ref is None:
        return result
    workflow_version = command.options.get("workflow_version", 0)
    if (isinstance(workflow_version, bool) or not isinstance(workflow_version, int)
            or workflow_version < 26):
        raise ValueError("supervised goal revisions require workflow v26")

    task_id = task.get("id")
    objective = task.get("objective")
    plan_ref = command.artifact_refs.get("development_plan")
    if not isinstance(task_id, str) or not task_id or not isinstance(objective, str):
        raise ValueError("goal task identity or original objective is invalid")
    if not isinstance(plan_ref, Mapping):
        raise ValueError("goal revision needs the authenticated development plan reference")
    expected_key = target_key(task, plan_ref)
    revision = _read_json_ref(_run_root(command), revision_ref, MAX_MANIFEST_BYTES)
    if (revision.get("schema_version") != 1
            or revision.get("key") != expected_key
            or revision.get("task_id") != task_id
            or digest(revision.get("plan_ref")) != digest(dict(plan_ref))
            or revision.get("original_objective") != objective
            or revision.get("applicable") is not True):
        raise ValueError("supervised goal revision identity mismatch")
    supervisor_execution_id = revision.get("supervisor_execution_id")
    if (not isinstance(supervisor_execution_id, str) or not supervisor_execution_id
            or revision_ref.get("metadata", {}).get("execution_id") != supervisor_execution_id):
        raise ValueError("supervised goal revision execution identity mismatch")

    original_ref = revision.get("original_objective_ref")
    original_document = _read_json_ref(_run_root(command), original_ref, MAX_MANIFEST_BYTES)
    if (original_document.get("schema_version") != 1
            or original_document.get("task_id") != task_id
            or digest(original_document.get("plan_ref")) != digest(dict(plan_ref))
            or original_document.get("objective") != objective):
        raise ValueError("sealed original goal objective identity mismatch")

    # before_ref is provenance for the supervisor's starting context. It can
    # differ from the normalized task objective and is intentionally not used
    # to reject a fresh goal preparation or coder attempt.
    before_bytes = _read_ref_bytes(_run_root(command), revision.get("before_ref"), MAX_GOAL_BYTES)
    _decode_goal(before_bytes, "supervisor before objective")
    after_bytes = _read_ref_bytes(_run_root(command), revision.get("after_ref"), MAX_GOAL_BYTES)
    after_text = _decode_goal(after_bytes, "supervisor revised objective")
    current_objective = result.get("objective")
    if not isinstance(current_objective, str):
        raise ValueError("goal objective must be text")
    context_separator = "\n\nCoder context:\n"
    if current_objective == after_text or current_objective.startswith(
            after_text + context_separator):
        applied_objective = current_objective
    else:
        # A supervisor can finish between goal_prepare and coder dispatch.
        # Retain the authenticated preparer's context when rebasing its goal
        # onto the newer revision, rather than silently discarding that work.
        bases = [objective]
        previous = result.get("supervised_goal_revision")
        if isinstance(previous, Mapping):
            previous_revision_ref = previous.get("revision_ref")
            previous_revision = _read_json_ref(
                _run_root(command), previous_revision_ref, MAX_MANIFEST_BYTES)
            _validate_previous_revision(_run_root(command), previous_revision,
                previous_revision_ref,
                {"key": expected_key, "task": task, "plan_ref": plan_ref})
            previous_ref = previous_revision.get("after_ref")
            if previous.get("after_ref") != previous_ref:
                raise ValueError("prepared goal previous revision reference mismatch")
            previous_bytes = _read_ref_bytes(
                _run_root(command), previous_ref, MAX_GOAL_BYTES)
            bases.append(_decode_goal(previous_bytes, "previous supervised objective"))
        context = None
        for base in bases:
            if current_objective == base:
                context = ""
                break
            prefix = base + context_separator
            if current_objective.startswith(prefix):
                context = current_objective[len(base):]
                break
        if context is None:
            raise ValueError("prepared coder objective does not match its authenticated base")
        applied_objective = after_text + context
    result["objective"] = applied_objective

    receipt = {
        "schema_version": 1,
        "consumer_execution_id": command.command_id,
        "task_id": task_id,
        "key": expected_key,
        "plan_ref": dict(plan_ref),
        "revision_ref": dict(revision_ref),
        "before_ref": revision["before_ref"],
        "after_ref": revision["after_ref"],
        "original_objective_ref": original_ref,
    }
    receipt_path = f"artifacts/executions/{command.command_id}/supervised-goal-application.json"
    receipt_ref = _publish_json_at(_run_root(command), receipt_path, receipt,
                                  command.command_id)
    result["supervised_goal_revision"] = {
        "revision_ref": dict(revision_ref),
        "before_ref": revision["before_ref"],
        "after_ref": revision["after_ref"],
        "original_objective_ref": original_ref,
        "consumer_execution_id": command.command_id,
        "receipt_ref": receipt_ref,
    }
    return result


def _run_root(command: OperationInput) -> Path:
    root = Path(command.run_dir)
    if not root.is_absolute():
        raise ValueError("run directory must be absolute")
    root.mkdir(parents=True, exist_ok=True)
    resolved = root.resolve()
    if root.absolute() != resolved:
        raise ValueError("run directory must not traverse a symlink")
    return resolved


def _normalize_targets(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("supervised goal targets must be a list")
    targets = []
    seen = set()
    for raw in value:
        if not isinstance(raw, Mapping):
            raise ValueError("supervised goal target must be an object")
        task = raw.get("task")
        plan_ref = raw.get("plan_ref")
        if not isinstance(task, Mapping) or not isinstance(plan_ref, Mapping):
            raise ValueError("supervised goal target needs a task and plan reference")
        task_value = json.loads(json.dumps(dict(task), ensure_ascii=False, allow_nan=False))
        plan_value = json.loads(json.dumps(dict(plan_ref), ensure_ascii=False, allow_nan=False))
        key = target_key(task_value, plan_value)
        if raw.get("key") != key:
            raise ValueError("supervised goal target key does not match task and plan")
        if key in seen:
            raise ValueError("duplicate supervised goal target")
        seen.add(key)
        objective = _goal_text(task_value.get("objective"), "task objective")
        source_execution_id = raw.get("source_execution_id")
        if source_execution_id is not None and not isinstance(source_execution_id, str):
            raise ValueError("source execution id must be text or null")
        goal_ref = raw.get("goal_ref")
        if goal_ref is not None and not isinstance(goal_ref, Mapping):
            raise ValueError("goal reference must be an artifact reference")
        source_workspace = raw.get("source_workspace")
        if source_workspace is not None and not isinstance(source_workspace, str):
            raise ValueError("source workspace must be text or null")
        previous_revision_ref = raw.get("previous_revision_ref")
        if previous_revision_ref is not None and not isinstance(previous_revision_ref, Mapping):
            raise ValueError("previous revision must be an artifact reference")
        targets.append({
            "key": key,
            "task": task_value,
            "plan_ref": plan_value,
            "source_execution_id": source_execution_id,
            "goal_ref": (None if goal_ref is None else
                         json.loads(json.dumps(dict(goal_ref), ensure_ascii=False,
                                               allow_nan=False))),
            "previous_revision_ref": (None if previous_revision_ref is None else
                                      json.loads(json.dumps(dict(previous_revision_ref),
                                                            ensure_ascii=False, allow_nan=False))),
            "source_workspace": source_workspace,
        })
    return targets


def _goal_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    encoded = value.encode("utf-8")
    if len(encoded) > MAX_GOAL_BYTES:
        raise ValueError(f"{label} exceeds 128 KiB")
    return value


def _decode_goal(value: bytes, label: str) -> str:
    if not value:
        raise ValueError(f"{label} is empty")
    if len(value) > MAX_GOAL_BYTES:
        raise ValueError(f"{label} exceeds 128 KiB")
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} is not UTF-8 text") from exc
    if not text.strip():
        raise ValueError(f"{label} is empty")
    return text


def _safe_run_path(root: Path, relative: str) -> Path:
    if (not isinstance(relative, str) or not relative or "\\" in relative
            or "\x00" in relative):
        raise ValueError("artifact path must be a relative path")
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValueError("artifact path must be relative and contained")
    candidate = root
    for part in path.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ValueError(f"artifact path traverses a symlink: {relative}")
    if not candidate.absolute().is_relative_to(root):
        raise ValueError("artifact path escapes the Run directory")
    return candidate


def _safe_project_source(root: Path, relative: str) -> Path:
    if (not isinstance(relative, str) or not relative or "\\" in relative
            or "\x00" in relative):
        raise ValueError("source workspace must be a logical project path")
    logical = Path(relative)
    if logical.is_absolute() or "." in logical.parts or ".." in logical.parts:
        raise ValueError("source workspace must be a logical project path")
    source = project_path(root, logical)
    if (source.resolve() != source.absolute() or not is_project_workspace(root, source)
            or project_relative(root, source).as_posix() != logical.as_posix()):
        raise ValueError("source workspace is outside the registered project")
    return source


def _sensitive_source_name(name: str) -> bool:
    return is_sensitive_name(name)


def _safe_workspace_path(workspace: Path, relative: str) -> Path:
    return _safe_run_path(workspace, relative)


def _ensure_directory(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("directory path must be relative and contained")
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"directory path traverses a symlink: {relative}")
        if current.exists():
            if not current.is_dir():
                raise ValueError(f"directory path component is not a directory: {relative}")
        else:
            current.mkdir()
    return current


def _write_workspace_file(path: Path, content: bytes, *, preserve_existing: bool) -> None:
    if path.is_symlink():
        raise ValueError("supervisor workspace target is a symlink")
    if path.exists():
        if not path.is_file():
            raise ValueError("supervisor workspace target is not a regular file")
        if preserve_existing:
            return
        if path.read_bytes() != content:
            raise ValueError("supervisor workspace source snapshot changed during preparation")
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        if path.is_symlink() or not path.is_file():
            raise ValueError("supervisor workspace target changed during preparation")
        if not preserve_existing and path.read_bytes() != content:
            raise ValueError("supervisor workspace source snapshot changed during preparation")
        return
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _write_immutable_file(root: Path, relative: str, content: bytes) -> None:
    parent_relative = Path(relative).parent.as_posix()
    _ensure_directory(root, parent_relative)
    path = _safe_run_path(root, relative)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
            raise ValueError("immutable supervised goal artifact already differs")
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o444)
    except FileExistsError:
        if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
            raise ValueError("immutable supervised goal artifact changed during publication")
        return
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
        os.fchmod(stream.fileno(), 0o444)


def _publish_object(root: Path, content: bytes, media_type: str,
                    execution_id: str) -> dict[str, Any]:
    checksum = hashlib.sha256(content).hexdigest()
    suffix = ".json" if media_type == "application/json" else ".md"
    relative = f"artifacts/supervised-goals/objects/{checksum}{suffix}"
    _write_immutable_file(root, relative, content)
    return _reference_for_existing(root, relative, media_type, execution_id)


def _publish_json_object(root: Path, value: Mapping[str, Any], execution_id: str) -> dict[str, Any]:
    return _publish_object(root, _json_bytes(value), "application/json", execution_id)


def _publish_json_at(root: Path, relative: str, value: Mapping[str, Any],
                     execution_id: str) -> dict[str, Any]:
    content = _json_bytes(value)
    _write_immutable_file(root, relative, content)
    return _reference_for_existing(root, relative, "application/json", execution_id)


def _reference_for_existing(root: Path, relative: str, media_type: str,
                            execution_id: str) -> dict[str, Any]:
    path = _safe_run_path(root, relative)
    if path.is_symlink() or not path.is_file():
        raise ValueError("supervised goal artifact is not a contained regular file")
    return seal_ref(root, {"path": relative, "sha256": file_digest(path),
                           "media_type": media_type}, execution_id=execution_id)


def _read_ref_bytes(root: Path, ref: Any, max_bytes: int) -> bytes:
    if not isinstance(ref, Mapping):
        raise ValueError("supervised goal artifact reference is missing")
    path = verified_path(root, ref)
    stat_before = path.stat()
    if not stat.S_ISREG(stat_before.st_mode) or stat_before.st_size > max_bytes:
        raise ValueError("supervised goal artifact has an invalid file shape or size")
    content = path.read_bytes()
    stat_after = path.stat()
    if (stat_before.st_ino != stat_after.st_ino or stat_before.st_size != stat_after.st_size
            or stat_before.st_mtime_ns != stat_after.st_mtime_ns
            or len(content) != stat_after.st_size):
        raise ValueError("supervised goal artifact changed while being read")
    expected = ref.get("sha256")
    actual = hashlib.sha256(content).hexdigest()
    if not isinstance(expected, str) or expected != actual:
        raise ValueError("supervised goal artifact digest mismatch")
    return content


def _read_json_ref(root: Path, ref: Any, max_bytes: int) -> dict[str, Any]:
    content = _read_ref_bytes(root, ref, max_bytes)
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("supervised goal artifact is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("supervised goal JSON artifact must be an object")
    return value


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (canonical_json(dict(value)) + "\n").encode("utf-8")


def _validate_manifest_identity(manifest: Mapping[str, Any], command: OperationInput,
                                targets: list[dict[str, Any]], workspace: str) -> None:
    identity = digest({"run_id": command.run_id, "task_id": command.task_id,
                       "stage_id": command.stage_id, "command_id": command.command_id})
    if (manifest.get("schema_version") != 1
            or manifest.get("identity") != identity
            or manifest.get("run_id") != command.run_id
            or manifest.get("task_id") != command.task_id
            or manifest.get("stage_id") != command.stage_id
            or manifest.get("command_id") != command.command_id
            or manifest.get("workspace") != workspace
            or manifest.get("snapshot_consistency") != "per_file_best_effort_not_atomic"
            or not isinstance(manifest.get("documents"), list)):
        raise ValueError("supervised goal manifest identity mismatch")
    by_key = {item.get("key"): item for item in manifest["documents"]
              if isinstance(item, Mapping)}
    if len(by_key) != len(manifest["documents"]) or set(by_key) != {item["key"] for item in targets}:
        raise ValueError("supervised goal manifest targets differ from host targets")
    for target in targets:
        document = by_key[target["key"]]
        if (document.get("task_id") != target["task"]["id"]
                or digest(document.get("plan_ref")) != digest(target["plan_ref"])
                or document.get("original_objective") != target["task"]["objective"]
                or document.get("source_execution_id") != target["source_execution_id"]
                or document.get("previous_revision_ref") != target.get("previous_revision_ref")):
            raise ValueError("supervised goal manifest target identity mismatch")


def _validate_previous_revision(root: Path, revision: Mapping[str, Any],
                                revision_ref: Mapping[str, Any],
                                target: Mapping[str, Any]) -> None:
    task = target["task"]
    plan_ref = target["plan_ref"]
    if (revision.get("schema_version") != 1
            or revision.get("applicable") is not True
            or revision.get("key") != target["key"]
            or revision.get("task_id") != task["id"]
            or digest(revision.get("plan_ref")) != digest(plan_ref)
            or revision.get("original_objective") != task["objective"]
            or not isinstance(revision.get("supervisor_execution_id"), str)
            or revision_ref.get("metadata", {}).get("execution_id")
            != revision.get("supervisor_execution_id")):
        raise ValueError("previous supervised goal revision identity mismatch")
    original = _read_json_ref(root, revision.get("original_objective_ref"),
                              MAX_MANIFEST_BYTES)
    if (original.get("task_id") != task["id"]
            or digest(original.get("plan_ref")) != digest(plan_ref)
            or original.get("objective") != task["objective"]):
        raise ValueError("previous sealed original objective identity mismatch")
    _read_ref_bytes(root, revision.get("before_ref"), MAX_GOAL_BYTES)
    _read_ref_bytes(root, revision.get("after_ref"), MAX_GOAL_BYTES)


def _validate_prepared_documents(root: Path, workspace: Path,
                                 manifest: Mapping[str, Any]) -> None:
    for document in manifest.get("documents", []):
        if not isinstance(document, Mapping):
            raise ValueError("supervised goal manifest document is invalid")
        path = _safe_workspace_path(workspace, document.get("path"))
        if path.is_symlink() or not path.is_file():
            raise ValueError("supervisor goal document is missing or unsafe")
        _read_ref_bytes(root, document.get("before_ref"), MAX_GOAL_BYTES)
        _read_json_ref(root, document.get("original_objective_ref"), MAX_MANIFEST_BYTES)


def _read_workspace_goal(workspace: Path, relative: str) -> bytes:
    from .goal_validation import contained_file

    path = contained_file(workspace, relative)
    absolute = path.absolute()
    if path.resolve() != absolute or workspace.resolve() != workspace.absolute():
        raise ValueError("goal document parent traverses a symlink")
    before_stat = path.stat()
    if not stat.S_ISREG(before_stat.st_mode) or before_stat.st_size > MAX_GOAL_BYTES:
        raise ValueError("goal document exceeds 128 KiB or is not a regular file")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        opened_stat = os.fstat(stream.fileno())
        content = stream.read(MAX_GOAL_BYTES + 1)
        after_stat = os.fstat(stream.fileno())
    if (opened_stat.st_ino != before_stat.st_ino
            or opened_stat.st_size != before_stat.st_size
            or opened_stat.st_mtime_ns != before_stat.st_mtime_ns
            or opened_stat.st_ino != after_stat.st_ino
            or opened_stat.st_size != after_stat.st_size
            or opened_stat.st_mtime_ns != after_stat.st_mtime_ns
            or len(content) != after_stat.st_size
            or path.resolve() != absolute
            or workspace.resolve() != workspace.absolute()):
        raise ValueError("goal document changed or escaped while being read")
    if len(content) > MAX_GOAL_BYTES:
        raise ValueError("goal document exceeds 128 KiB")
    return content


def _snapshot_sources(root: Path, workspace: Path, command: OperationInput,
                      targets: list[dict[str, Any]]) -> dict[str, Any]:
    default_source = (command.options.get("source_workspace")
                      or command.options.get("workspace"))
    if default_source is None:
        default_source = "worktree"
    source_values = []
    seen = set()
    for target in targets:
        relative = target.get("source_workspace") or default_source
        if relative is None:
            continue
        if relative not in seen:
            seen.add(relative)
            source_values.append(relative)
    if not source_values and default_source:
        source_values.append(default_source)
    roots = []
    copied_total = 0
    copied_count = 0
    visited_directories = 0
    for relative in source_values:
        root_record = {"source_workspace": relative,
                       "snapshot_directory": f"source/{digest(relative)[:16]}",
                       "consistency": "per_file_best_effort_not_atomic",
                       "files": [], "skipped": []}
        try:
            source = _safe_project_source(root, relative)
        except (OSError, ValueError) as exc:
            root_record["skipped"].append({"path": relative,
                                           "reason": "unsafe_source_workspace",
                                           "detail": str(exc)[:200]})
            roots.append(root_record)
            continue
        if source.is_symlink():
            root_record["skipped"].append({"path": relative, "reason": "symlink_root"})
            roots.append(root_record)
            continue
        if not source.exists() or not source.is_dir():
            root_record["skipped"].append({"path": relative, "reason": "source_workspace_missing"})
            roots.append(root_record)
            continue
        snapshot_relative = f"{Path(workspace).relative_to(root).as_posix()}/{root_record['snapshot_directory']}"
        _ensure_directory(root, snapshot_relative)

        def walk_error(error):
            raw_path = getattr(error, "filename", relative)
            try:
                display_path = Path(raw_path).relative_to(source).as_posix()
            except (TypeError, ValueError):
                display_path = relative
            root_record["skipped"].append({
                "path": display_path,
                "reason": "directory_read_error",
                "detail": str(error),
            })

        stop = False
        for current, directories, filenames in os.walk(source, topdown=True,
                                                       followlinks=False, onerror=walk_error):
            visited_directories += 1
            if visited_directories > MAX_SOURCE_DIRECTORIES:
                root_record["skipped"].append({
                    "path": Path(current).relative_to(source).as_posix(),
                    "reason": "directory_limit"})
                break
            current_path = Path(current)
            allowed_directories = []
            in_modport = ".modport" in current_path.relative_to(source).parts
            for name in sorted(directories):
                child = current_path / name
                relative_child = child.relative_to(source).as_posix()
                generated_modport = (in_modport and
                                     name.lower() in _GENERATED_MODPORT_DIRECTORIES)
                if (_sensitive_source_name(name)
                        or name.lower() in _EXCLUDED_DIRECTORIES or generated_modport):
                    root_record["skipped"].append({"path": relative_child,
                                                   "reason": "generated_directory"})
                elif child.is_symlink():
                    root_record["skipped"].append({"path": relative_child,
                                                   "reason": "symlink_directory"})
                else:
                    allowed_directories.append(name)
            directories[:] = allowed_directories
            for filename in sorted(filenames):
                if _sensitive_source_name(filename):
                    root_record["skipped"].append({
                        "path": (current_path / filename).relative_to(source).as_posix(),
                        "reason": "sensitive_entry"})
                    continue
                if copied_count >= MAX_SOURCE_FILES:
                    root_record["skipped"].append({"path": current_path.relative_to(source).as_posix(),
                                                   "reason": "file_count_limit"})
                    stop = True
                    break
                candidate = current_path / filename
                relative_file = candidate.relative_to(source).as_posix()
                try:
                    before_stat = candidate.lstat()
                    if stat.S_ISLNK(before_stat.st_mode):
                        raise ValueError("symlink")
                    if not stat.S_ISREG(before_stat.st_mode):
                        raise ValueError("special_file")
                    if before_stat.st_size > MAX_SOURCE_FILE_BYTES:
                        raise ValueError("file_size_limit")
                    if copied_total + before_stat.st_size > MAX_SOURCE_TOTAL_BYTES:
                        raise ValueError("total_size_limit")
                    absolute = candidate.absolute()
                    if candidate.resolve() != absolute or source.resolve() != source.absolute():
                        raise ValueError("parent_symlink")
                    flags = os.O_RDONLY
                    if hasattr(os, "O_NOFOLLOW"):
                        flags |= os.O_NOFOLLOW
                    descriptor = os.open(candidate, flags)
                    with os.fdopen(descriptor, "rb") as stream:
                        opened_stat = os.fstat(stream.fileno())
                        content = stream.read(MAX_SOURCE_FILE_BYTES + 1)
                        after_stat = os.fstat(stream.fileno())
                    if (opened_stat.st_ino != before_stat.st_ino
                            or opened_stat.st_size != before_stat.st_size
                            or opened_stat.st_mtime_ns != before_stat.st_mtime_ns
                            or opened_stat.st_ino != after_stat.st_ino
                            or opened_stat.st_size != after_stat.st_size
                            or opened_stat.st_mtime_ns != after_stat.st_mtime_ns
                            or len(content) != after_stat.st_size
                            or candidate.resolve() != absolute
                            or source.resolve() != source.absolute()):
                        raise ValueError("changed_during_snapshot")
                    if b"\x00" in content:
                        raise ValueError("binary_file")
                    content.decode("utf-8")
                    source_ref = _publish_object(root, content, "text/plain",
                                                 command.command_id)
                    destination_relative = (
                        snapshot_relative + "/" + relative_file
                    )
                    _ensure_directory(root, str(Path(destination_relative).parent))
                    destination = _safe_run_path(root, destination_relative)
                    _write_workspace_file(destination, content, preserve_existing=False)
                    root_record["files"].append({
                        "path": relative_file,
                        "snapshot_path": Path(destination_relative).relative_to(
                            Path(workspace).relative_to(root)).as_posix(),
                        "size": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "source_ref": source_ref,
                    })
                    copied_count += 1
                    copied_total += len(content)
                except (OSError, ValueError, UnicodeDecodeError) as exc:
                    reason = str(exc) if isinstance(exc, ValueError) else "unreadable"
                    root_record["skipped"].append({"path": relative_file,
                                                   "reason": reason[:200]})
            if stop:
                break
        roots.append(root_record)
    return {"consistency": "per_file_best_effort_not_atomic", "roots": roots}
