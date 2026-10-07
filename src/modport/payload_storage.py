"""Bounded, deduplicated storage for ModPort operation payloads and results.

The Dispatcher SDK persists commands and results in several durable projections.
Keeping large business values directly in those projections multiplies their
storage cost.  This module leaves the small routing identity in the SDK value
and moves large JSON subtrees into ModPort's authenticated, content-addressed
audit blob store.

Packing is bottom-up.  A large child is replaced before its parent is measured,
so two commands that contain the same large child reuse one blob instead of
compressing two slightly different copies of the complete command.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .application_state_storage import (
    ApplicationStateStorageError,
    _encoded_size,
)
from .audit_storage import (
    AuditStorageError,
    INLINE_LIMIT,
    MAX_RAW_BYTES,
    inspect_data_reference,
    load_data,
    store_data,
)
from .contracts import OperationInput, OperationResult, json_copy


_WIRE_MARKER = "__modport_operation_payload__"
_NODE_MARKER = "__modport_payload_node__"
_INPUT_SCHEMA = "modport.operation-input-wire"
_RESULT_SCHEMA = "modport.operation-result-wire"
_NODE_REFERENCE_SCHEMA = "modport.payload-node-reference"
_NODE_DOCUMENT_SCHEMA = "modport.payload-node"
_VERSION = 1

_INPUT_IDENTITY_FIELDS = (
    "run_id", "task_id", "stage_id", "command_id", "run_dir", "attempt",
    "schema_version",
)
_INPUT_BODY_FIELDS = (
    "payload", "options", "upstream_results", "artifact_refs", "prior_findings",
)
_RESULT_IDENTITY_FIELDS = (
    "status", "run_id", "task_id", "stage_id", "command_id",
)
_RESULT_BODY_FIELDS = ("outputs", "detail", "error_code")
_WIRE_FIELDS = frozenset({"schema", "version", "kind", "identity", "body"})
_NODE_REFERENCE_FIELDS = frozenset({"schema", "version", "blob"})
_RESERVED_FIELDS = frozenset({_WIRE_MARKER, _NODE_MARKER})

# These limits bound malicious reference graphs independently of the raw byte
# limit.  Legitimate packed values normally use only a handful of blob nodes.
MAX_REFERENCE_COUNT = 4096
MAX_REFERENCE_DEPTH = 128


class PayloadStorageError(ValueError):
    """An operation payload/result cannot be stored or restored safely."""


def _serialized(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


def _bounded_size(value: Any, *, limit: int | None = None) -> int:
    try:
        return _encoded_size(value, limit=MAX_RAW_BYTES if limit is None else limit)
    except ApplicationStateStorageError as exc:
        detail = str(exc).replace("application state", "operation payload")
        raise PayloadStorageError(detail) from exc


def _copy_json(value: Any) -> Any:
    try:
        return json_copy(value)
    except (OverflowError, RecursionError, TypeError, ValueError) as exc:
        raise PayloadStorageError("operation payload is not canonical JSON") from exc


def _audit_reference(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PayloadStorageError("payload blob reference must be an object")
    try:
        reference = inspect_data_reference(_serialized(value))
    except (AuditStorageError, OverflowError, RecursionError, TypeError, ValueError) as exc:
        raise PayloadStorageError(str(exc)) from exc
    if reference is None:
        raise PayloadStorageError("payload blob reference is not external")
    return reference


def _store_node(root: Path, value: Any) -> dict[str, Any]:
    document = {
        "schema": _NODE_DOCUMENT_SCHEMA,
        "version": _VERSION,
        "value": value,
    }
    try:
        serialized = store_data(root, document)
        reference = inspect_data_reference(serialized)
    except (AuditStorageError, OSError, OverflowError, RecursionError,
            TypeError, ValueError) as exc:
        raise PayloadStorageError(str(exc)) from exc
    if reference is None:
        raise PayloadStorageError("large operation payload node remained inline")
    blob = json.loads(serialized)
    _audit_reference(blob)
    return {
        _NODE_MARKER: {
            "schema": _NODE_REFERENCE_SCHEMA,
            "version": _VERSION,
            "blob": blob,
        }
    }


def _pack_node(root: Path, value: Any, *, depth: int,
               references: list[int]) -> Any:
    if depth > MAX_REFERENCE_DEPTH:
        raise PayloadStorageError("operation payload nesting exceeds the limit")
    if isinstance(value, dict):
        if _RESERVED_FIELDS & set(value):
            raise PayloadStorageError("operation payload uses a reserved storage field")
        packed = {
            name: _pack_node(root, child, depth=depth + 1, references=references)
            for name, child in value.items()
        }
    elif isinstance(value, list):
        packed = [
            _pack_node(root, child, depth=depth + 1, references=references)
            for child in value
        ]
    else:
        packed = value

    if _bounded_size(packed) < INLINE_LIMIT:
        return packed
    references[0] += 1
    if references[0] > MAX_REFERENCE_COUNT:
        raise PayloadStorageError("operation payload has too many external references")
    return _store_node(root, packed)


def _validate_wire(marker: Any, *, schema: str, kind: str,
                   identity: dict[str, Any]) -> Any:
    if (not isinstance(marker, dict) or set(marker) != _WIRE_FIELDS
            or marker.get("schema") != schema
            or type(marker.get("version")) is not int
            or marker["version"] != _VERSION
            or marker.get("kind") != kind
            or not isinstance(marker.get("identity"), dict)
            or marker["identity"] != identity):
        raise PayloadStorageError(f"malformed operation {kind} wire marker")
    return marker["body"]


class _LoadBudget:
    def __init__(self, initial_bytes: int):
        self.raw_bytes = initial_bytes
        self.references = 0
        self.active: set[str] = set()

    def add(self, reference: dict[str, Any]) -> None:
        self.references += 1
        if self.references > MAX_REFERENCE_COUNT:
            raise PayloadStorageError("operation payload has too many external references")
        self.raw_bytes += reference["raw_size"]
        if self.raw_bytes > MAX_RAW_BYTES:
            raise PayloadStorageError("operation payload references exceed the raw size limit")


def _load_node(root: Path, value: Any, *, depth: int,
               budget: _LoadBudget) -> Any:
    if depth > MAX_REFERENCE_DEPTH:
        raise PayloadStorageError("operation payload reference nesting exceeds the limit")
    if isinstance(value, dict) and set(value) == {_NODE_MARKER}:
        marker = value[_NODE_MARKER]
        if (not isinstance(marker, dict) or set(marker) != _NODE_REFERENCE_FIELDS
                or marker.get("schema") != _NODE_REFERENCE_SCHEMA
                or type(marker.get("version")) is not int
                or marker["version"] != _VERSION):
            raise PayloadStorageError("malformed operation payload node reference")
        reference = _audit_reference(marker["blob"])
        budget.add(reference)
        checksum = reference["sha256"]
        if checksum in budget.active:
            raise PayloadStorageError("cyclic operation payload reference")
        budget.active.add(checksum)
        try:
            try:
                document = load_data(root, _serialized(marker["blob"]))
            except (AuditStorageError, OSError, OverflowError, RecursionError,
                    TypeError, ValueError) as exc:
                raise PayloadStorageError(str(exc)) from exc
            if (set(document) != {"schema", "version", "value"}
                    or document.get("schema") != _NODE_DOCUMENT_SCHEMA
                    or type(document.get("version")) is not int
                    or document["version"] != _VERSION):
                raise PayloadStorageError("malformed operation payload node document")
            return _load_node(
                root, document["value"], depth=depth + 1, budget=budget
            )
        finally:
            budget.active.remove(checksum)

    if isinstance(value, dict):
        if _RESERVED_FIELDS & set(value):
            raise PayloadStorageError("malformed reserved operation payload marker")
        return {
            name: _load_node(root, child, depth=depth + 1, budget=budget)
            for name, child in value.items()
        }
    if isinstance(value, list):
        return [
            _load_node(root, child, depth=depth + 1, budget=budget)
            for child in value
        ]
    return value


def _pack(root: str | Path, value: dict[str, Any], *, kind: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"operation {kind} must be an object")
    # Prove the size bound before json_copy or contract normalization can make a
    # second full in-memory representation.
    _bounded_size(value)
    try:
        if kind == "input":
            normalized = OperationInput.from_dict(value).to_dict()
            identity_fields = _INPUT_IDENTITY_FIELDS
            body_fields = _INPUT_BODY_FIELDS
            schema = _INPUT_SCHEMA
        else:
            normalized = OperationResult.from_dict(value).to_dict()
            identity_fields = _RESULT_IDENTITY_FIELDS
            body_fields = _RESULT_BODY_FIELDS
            schema = _RESULT_SCHEMA
    except (TypeError, ValueError) as exc:
        raise PayloadStorageError(f"invalid operation {kind}: {exc}") from exc

    # Defaults added by contract normalization remain subject to the same cap.
    # Small values retain the historical pure contract shape. This keeps the
    # common SDK path transparent and reserves the storage envelope for values
    # whose repeated inline persistence is material.
    if _bounded_size(normalized) < INLINE_LIMIT:
        return normalized
    identity = {name: normalized[name] for name in identity_fields}
    body = {name: normalized[name] for name in body_fields}
    packed_body = _pack_node(Path(root), body, depth=0, references=[0])
    return {
        **identity,
        _WIRE_MARKER: {
            "schema": schema,
            "version": _VERSION,
            "kind": kind,
            "identity": _copy_json(identity),
            "body": packed_body,
        },
    }


def pack_input(root: str | Path, value: dict[str, Any]) -> dict[str, Any]:
    """Pack one complete :class:`OperationInput` dictionary for SDK storage."""
    return _pack(root, value, kind="input")


def pack_result(root: str | Path, value: dict[str, Any]) -> dict[str, Any]:
    """Pack one complete :class:`OperationResult` dictionary for SDK storage."""
    return _pack(root, value, kind="result")


def _is_packed(value: Any, *, schema: str, kind: str,
               identity_fields: tuple[str, ...]) -> bool:
    if not isinstance(value, dict) or set(value) != set(identity_fields) | {_WIRE_MARKER}:
        return False
    marker = value.get(_WIRE_MARKER)
    identity = {name: value[name] for name in identity_fields}
    return (
        isinstance(marker, dict)
        and set(marker) == _WIRE_FIELDS
        and marker.get("schema") == schema
        and type(marker.get("version")) is int
        and marker["version"] == _VERSION
        and marker.get("kind") == kind
        and marker.get("identity") == identity
    )


def is_packed_input(value: Any) -> bool:
    """Return whether ``value`` is a well-shaped input storage envelope."""
    return _is_packed(
        value, schema=_INPUT_SCHEMA, kind="input",
        identity_fields=_INPUT_IDENTITY_FIELDS,
    )


def is_packed_result(value: Any) -> bool:
    """Return whether ``value`` is a well-shaped result storage envelope."""
    return _is_packed(
        value, schema=_RESULT_SCHEMA, kind="result",
        identity_fields=_RESULT_IDENTITY_FIELDS,
    )


def _unpack(root: str | Path, value: dict[str, Any], *, kind: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"operation {kind} must be an object")
    wire_size = _bounded_size(value)
    if _WIRE_MARKER not in value:
        # Historical SDK rows contain the pure contract dictionary.
        return _copy_json(value)

    if kind == "input":
        identity_fields = _INPUT_IDENTITY_FIELDS
        body_fields = _INPUT_BODY_FIELDS
        schema = _INPUT_SCHEMA
        contract = OperationInput
    else:
        identity_fields = _RESULT_IDENTITY_FIELDS
        body_fields = _RESULT_BODY_FIELDS
        schema = _RESULT_SCHEMA
        contract = OperationResult

    expected_fields = set(identity_fields) | {_WIRE_MARKER}
    if set(value) != expected_fields:
        raise PayloadStorageError(f"malformed operation {kind} wire envelope")
    identity = {name: value[name] for name in identity_fields}
    packed_body = _validate_wire(
        value[_WIRE_MARKER], schema=schema, kind=kind, identity=identity
    )
    body = _load_node(
        Path(root), packed_body, depth=0, budget=_LoadBudget(wire_size)
    )
    if not isinstance(body, dict) or set(body) != set(body_fields):
        raise PayloadStorageError(f"malformed operation {kind} wire body")
    restored = {**identity, **body}
    _bounded_size(restored)
    try:
        normalized = contract.from_dict(restored).to_dict()
    except (TypeError, ValueError) as exc:
        raise PayloadStorageError(f"invalid restored operation {kind}: {exc}") from exc
    if any(normalized[name] != identity[name] for name in identity_fields):
        raise PayloadStorageError(f"restored operation {kind} identity changed")
    return normalized


def unpack_input(root: str | Path, value: dict[str, Any]) -> dict[str, Any]:
    """Restore a packed input, or copy one historical inline input unchanged."""
    return _unpack(root, value, kind="input")


def unpack_result(root: str | Path, value: dict[str, Any]) -> dict[str, Any]:
    """Restore a packed result, or copy one historical inline result unchanged."""
    return _unpack(root, value, kind="result")


def verify_operations(root: str | Path, operations: list[dict[str, Any]]) -> None:
    """Verify stored payload references in SDK task-scheduling operations.

    The operation list and its commands are never modified.  Historical inline
    payloads remain valid, but are still checked against ``OperationInput``.
    """
    if not isinstance(operations, list):
        raise TypeError("SDK operations must be a list")
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            raise PayloadStorageError(f"SDK operation {index} must be an object")
        if operation.get("kind") not in {"add_task", "new_attempt", "schedule"}:
            continue
        command = operation.get("command")
        if not isinstance(command, dict) or not isinstance(command.get("payload"), dict):
            raise PayloadStorageError(f"SDK operation {index} has no command payload")
        restored = unpack_input(root, command["payload"])
        try:
            validated = OperationInput.from_dict(restored)
        except (TypeError, ValueError) as exc:
            raise PayloadStorageError(
                f"SDK operation {index} has an invalid command payload: {exc}"
            ) from exc
        if operation.get("task_id") != validated.task_id:
            raise PayloadStorageError(f"SDK operation {index} task identity does not match its payload")
        execution_id = command.get("execution_id")
        if execution_id != validated.command_id:
            raise PayloadStorageError(
                f"SDK operation {index} execution identity does not match its payload"
            )


def _stored_value(container: Any, name: str) -> dict[str, Any] | None:
    if not isinstance(container, dict):
        return None
    value = container.get(name)
    return value if isinstance(value, dict) and _WIRE_MARKER in value else None


def hydrate_transport_snapshot(root: str | Path,
                               snapshot: dict[str, Any]) -> dict[str, Any]:
    """Copy-on-write an SDK snapshot and restore packed transport payloads.

    Both the Orchestrator attempt projection and its nested Kernel snapshot are
    hydrated. Application state, headers and attempts containing only historical
    inline values retain their original object identity. This avoids copying a
    complete logical history on every observation.
    """
    if not isinstance(snapshot, dict):
        raise TypeError("SDK transport snapshot must be an object")
    hydrated = dict(snapshot)
    tasks = snapshot.get("tasks", {})
    if not isinstance(tasks, dict):
        raise PayloadStorageError("SDK transport snapshot tasks must be an object")
    changed_tasks = None
    for task_id, task in tasks.items():
        if not isinstance(task, dict) or not isinstance(task.get("attempts", []), list):
            raise PayloadStorageError("SDK transport task has invalid attempts")
        changed_attempts = None
        for index, attempt in enumerate(task.get("attempts", [])):
            if not isinstance(attempt, dict):
                raise PayloadStorageError("SDK transport attempt must be an object")
            command = attempt.get("command")
            result = attempt.get("result")
            kernel = attempt.get("kernel_snapshot")
            if kernel is not None and not isinstance(kernel, dict):
                raise PayloadStorageError("Kernel snapshot must be an object")

            input_value = _stored_value(command, "payload")
            result_value = _stored_value(result, "value")
            kernel_command = kernel.get("command") if isinstance(kernel, dict) else None
            kernel_result = kernel.get("result") if isinstance(kernel, dict) else None
            kernel_input_value = _stored_value(kernel_command, "payload")
            kernel_result_value = _stored_value(kernel_result, "value")
            if all(value is None for value in (
                    input_value, result_value, kernel_input_value,
                    kernel_result_value)):
                continue

            changed_attempt = dict(attempt)
            if input_value is not None:
                if not isinstance(command, dict):
                    raise PayloadStorageError("SDK command must be an object")
                changed_command = dict(command)
                changed_command["payload"] = unpack_input(root, input_value)
                changed_attempt["command"] = changed_command
            if result_value is not None:
                if not isinstance(result, dict):
                    raise PayloadStorageError("SDK execution result must be an object")
                changed_result = dict(result)
                changed_result["value"] = unpack_result(root, result_value)
                changed_attempt["result"] = changed_result
            if kernel_input_value is not None or kernel_result_value is not None:
                changed_kernel = dict(kernel)
                if kernel_input_value is not None:
                    if not isinstance(kernel_command, dict):
                        raise PayloadStorageError("Kernel command must be an object")
                    changed_kernel_command = dict(kernel_command)
                    changed_kernel_command["payload"] = unpack_input(
                        root, kernel_input_value
                    )
                    changed_kernel["command"] = changed_kernel_command
                if kernel_result_value is not None:
                    if not isinstance(kernel_result, dict):
                        raise PayloadStorageError("Kernel result must be an object")
                    changed_kernel_result = dict(kernel_result)
                    changed_kernel_result["value"] = unpack_result(
                        root, kernel_result_value
                    )
                    changed_kernel["result"] = changed_kernel_result
                changed_attempt["kernel_snapshot"] = changed_kernel

            if changed_attempts is None:
                changed_attempts = list(task["attempts"])
            changed_attempts[index] = changed_attempt
        if changed_attempts is not None:
            changed_task = dict(task)
            changed_task["attempts"] = changed_attempts
            if changed_tasks is None:
                changed_tasks = dict(tasks)
            changed_tasks[task_id] = changed_task
    if changed_tasks is not None:
        hydrated["tasks"] = changed_tasks
    return hydrated


__all__ = [
    "PayloadStorageError",
    "hydrate_transport_snapshot",
    "is_packed_input",
    "is_packed_result",
    "pack_input",
    "pack_result",
    "unpack_input",
    "unpack_result",
    "verify_operations",
    "read_prepared_json",
]


def read_prepared_json(root: str | Path, relative: str) -> dict:
    """Read a bounded host replay packet before any SDK writer is opened."""
    from .evidence import read_json, verified_path
    path = verified_path(Path(root), {"path": relative})
    if path.stat().st_size > MAX_RAW_BYTES:
        raise PayloadStorageError("prepared payload exceeds 128 MiB storage limit")
    value = read_json(path)
    if not isinstance(value, dict):
        raise PayloadStorageError("prepared payload must be an object")
    return value
