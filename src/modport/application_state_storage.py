"""Bounded, authenticated storage for ModPort-owned application state.

The Dispatcher SDK treats ``application_state`` as one opaque value.  Large
values are therefore stored in every changed Run revision and in the matching
decision event.  This module keeps that SDK value small while retaining the
complete JSON state in durable, content-addressed Run blobs.

Large top-level fields are stored independently before the compact root is
considered.  Unchanged fields consequently resolve to the same blob even when
another part of the application state changes.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import re
from typing import Any

from .audit_storage import (
    AuditStorageError,
    INLINE_LIMIT,
    MAX_RAW_BYTES,
    inspect_data_reference,
    load_data,
    store_data,
)
from .contracts import json_copy


_AUDIT_MARKER = "__modport_audit_blob__"
_ROOT_MARKER = "__modport_application_state__"
_FIELD_INDEX = "__modport_application_state_fields__"
_ROOT_SCHEMA = "modport.application-state-root"
_FIELD_INDEX_SCHEMA = "modport.application-state-fields"
_FIELD_SCHEMA = "modport.application-state-field"
_VERSION = 1
_SIZE_TEXT_CHARS = 64 * 1024
_JSON_ESCAPE = re.compile(r'["\\\x00-\x1f]')
_SHORT_ESCAPES = frozenset("\b\f\n\r\t")
_RESERVED_FIELDS = frozenset({_AUDIT_MARKER, _ROOT_MARKER, _FIELD_INDEX})
# A strict upper approximation of log2(10). It is used only to prove that a
# decimal integer can fit inside the remaining byte budget before calling str().
_LOG2_10_NUMERATOR = 332193
_LOG2_10_DENOMINATOR = 100000


class ApplicationStateStorageError(ValueError):
    """Application state cannot be stored or restored safely."""


def _serialized(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


def _encoded_size(value: Any, *, limit: int | None = None) -> int:
    """Measure strict canonical JSON without materializing encoded scalars.

    CPython's ``JSONEncoder.iterencode`` may emit one whole string value as a
    single chunk, so it is not a memory bound. This walker counts JSON escaping
    and UTF-8 bytes character by character and stops at the configured ceiling.
    """
    if limit is None:
        limit = MAX_RAW_BYTES
    total = 0
    active: set[int] = set()

    def add(amount: int) -> None:
        nonlocal total
        total += amount
        if total > limit:
            raise ApplicationStateStorageError(
                "application state exceeds the raw size limit"
            )

    def string(text: str) -> None:
        add(2)  # quotes
        for offset in range(0, len(text), _SIZE_TEXT_CHARS):
            # Encoding a fixed-size slice bounds the temporary allocation even
            # when the original scalar is arbitrarily large. Regex matching
            # counts only the extra bytes JSON adds beyond UTF-8.
            chunk = text[offset:offset + _SIZE_TEXT_CHARS]
            try:
                encoded_size = len(chunk.encode("utf-8"))
            except UnicodeEncodeError as exc:
                raise ApplicationStateStorageError(
                    "application state is not canonical JSON"
                ) from exc
            escaped_extra = 0
            for match in _JSON_ESCAPE.finditer(chunk):
                character = match.group(0)
                escaped_extra += 1 if (
                    character in {'"', "\\"} or character in _SHORT_ESCAPES
                ) else 5
            add(encoded_size + escaped_extra)

    def integer_text(number: int, *, reserve: int = 0) -> str:
        room = limit - total - reserve
        sign = 1 if number < 0 else 0
        digit_room = room - sign
        if digit_room < 1:
            raise ApplicationStateStorageError(
                "application state exceeds the raw size limit"
            )
        maximum_bits = (
            digit_room * _LOG2_10_NUMERATOR
            + _LOG2_10_DENOMINATOR - 1
        ) // _LOG2_10_DENOMINATOR
        if number.bit_length() > maximum_bits:
            raise ApplicationStateStorageError(
                "application state exceeds the raw size limit"
            )
        # The bit proof limits this allocation to the remaining budget plus at
        # most one rounding digit. Exact JSON validation still comes from the
        # built-in conversion and the final add() check.
        return str(number)

    def key_text(key: Any) -> str:
        if type(key) is str:
            return key
        if key is True:
            return "true"
        if key is False:
            return "false"
        if key is None:
            return "null"
        if type(key) is int:
            return integer_text(key, reserve=2)
        if type(key) is float and math.isfinite(key):
            return json.dumps(key, allow_nan=False, separators=(",", ":"))
        raise ApplicationStateStorageError("application state is not canonical JSON")

    def walk(item: Any) -> None:
        if item is None:
            add(4)
        elif item is True:
            add(4)
        elif item is False:
            add(5)
        elif type(item) is str:
            string(item)
        elif type(item) is int:
            add(len(integer_text(item)))
        elif type(item) is float:
            if not math.isfinite(item):
                raise ApplicationStateStorageError(
                    "application state is not canonical JSON"
                )
            add(len(json.dumps(item, allow_nan=False, separators=(",", ":"))))
        elif type(item) in {list, tuple}:
            identity = id(item)
            if identity in active:
                raise ApplicationStateStorageError(
                    "application state is not canonical JSON"
                )
            active.add(identity)
            try:
                add(1)
                for index, child in enumerate(item):
                    if index:
                        add(1)
                    walk(child)
                add(1)
            finally:
                active.remove(identity)
        elif type(item) is dict:
            identity = id(item)
            if identity in active:
                raise ApplicationStateStorageError(
                    "application state is not canonical JSON"
                )
            active.add(identity)
            try:
                if any(type(key) not in {str, int, float, bool, type(None)}
                       for key in item):
                    raise ApplicationStateStorageError(
                        "application state is not canonical JSON"
                    )
                try:
                    entries = sorted(item.items())
                except TypeError as exc:
                    raise ApplicationStateStorageError(
                        "application state is not canonical JSON"
                    ) from exc
                add(1)
                for index, (key, child) in enumerate(entries):
                    if index:
                        add(1)
                    string(key_text(key))
                    add(1)
                    walk(child)
                add(1)
            finally:
                active.remove(identity)
        else:
            raise ApplicationStateStorageError(
                "application state is not canonical JSON"
            )

    try:
        walk(value)
    except ApplicationStateStorageError:
        raise
    except (OverflowError, RecursionError, TypeError, ValueError) as exc:
        raise ApplicationStateStorageError(
            "application state is not canonical JSON"
        ) from exc
    return total


def is_packed_application_state(value: Any) -> bool:
    """Return whether a stored SDK value uses a ModPort blob marker."""
    if not isinstance(value, dict):
        return False
    if set(value) == {_ROOT_MARKER}:
        marker = value.get(_ROOT_MARKER)
        return (isinstance(marker, dict)
                and marker.get("schema") == _ROOT_SCHEMA
                and marker.get("version") == _VERSION
                and "blob" in marker)
    marker = value.get(_FIELD_INDEX)
    return (isinstance(marker, dict)
            and marker.get("schema") == _FIELD_INDEX_SCHEMA
            and marker.get("version") == _VERSION
            and isinstance(marker.get("refs"), dict))


def _blob_reference(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ApplicationStateStorageError("application state blob reference must be an object")
    try:
        reference = inspect_data_reference(_serialized(value))
    except (AuditStorageError, TypeError, ValueError) as exc:
        raise ApplicationStateStorageError(str(exc)) from exc
    if reference is None:
        raise ApplicationStateStorageError("application state blob reference is not external")
    return reference


def _store_document(root: Path, document: dict[str, Any]) -> dict[str, Any]:
    try:
        serialized = store_data(root, document)
        reference = inspect_data_reference(serialized)
    except (AuditStorageError, OSError, TypeError, ValueError) as exc:
        raise ApplicationStateStorageError(str(exc)) from exc
    if reference is None:
        raise ApplicationStateStorageError("large application state document remained inline")
    value = json.loads(serialized)
    _blob_reference(value)
    return value


def _load_document(root: Path, reference: Any) -> dict[str, Any]:
    _blob_reference(reference)
    try:
        return load_data(root, _serialized(reference))
    except (AuditStorageError, OSError, TypeError, ValueError) as exc:
        raise ApplicationStateStorageError(str(exc)) from exc


def _root_payload(root: Path, stored: dict[str, Any]) -> dict[str, Any]:
    if set(stored) != {_ROOT_MARKER}:
        if set(stored) == {_AUDIT_MARKER}:
            raise ApplicationStateStorageError(
                "bare application state blob reference is unsupported"
            )
        return json_copy(stored)

    marker = stored[_ROOT_MARKER]
    expected = {"schema", "version", "blob"}
    if (not isinstance(marker, dict) or set(marker) != expected
            or marker.get("schema") != _ROOT_SCHEMA
            or type(marker.get("version")) is not int
            or marker["version"] != _VERSION):
        raise ApplicationStateStorageError("malformed application state root marker")
    document = _load_document(root, marker["blob"])
    if (set(document) != {"schema", "version", "state"}
            or document.get("schema") != _ROOT_SCHEMA
            or type(document.get("version")) is not int
            or document["version"] != _VERSION
            or not isinstance(document.get("state"), dict)):
        raise ApplicationStateStorageError("malformed application state root document")
    return document["state"]


def pack_application_state(root: str | Path, application_state: dict[str, Any]) -> dict[str, Any]:
    """Return a bounded SDK representation of one complete application state.

    Existing JSON is never mutated.  Values whose canonical JSON representation
    is at least 64 KiB are stored as independent field blobs.  If the resulting
    root still reaches that threshold, it is stored as one additional blob.
    """
    if not isinstance(application_state, dict):
        raise TypeError("application state must be an object")
    _encoded_size(application_state)
    try:
        state = json_copy(application_state)
    except (TypeError, ValueError) as exc:
        raise ApplicationStateStorageError("application state is not canonical JSON") from exc
    reserved = _RESERVED_FIELDS & set(state)
    if reserved:
        raise ApplicationStateStorageError("application state uses a reserved storage field")

    root_path = Path(root)
    packed: dict[str, Any] = {}
    field_refs: dict[str, Any] = {}
    for name, value in state.items():
        if _encoded_size(value) < INLINE_LIMIT:
            packed[name] = value
            continue
        document = {
            "schema": _FIELD_SCHEMA,
            "version": _VERSION,
            "field": name,
            "value": value,
        }
        field_refs[name] = _store_document(root_path, document)
    if field_refs:
        packed[_FIELD_INDEX] = {
            "schema": _FIELD_INDEX_SCHEMA,
            "version": _VERSION,
            "refs": field_refs,
        }

    root_document = {"schema": _ROOT_SCHEMA, "version": _VERSION, "state": packed}
    try:
        serialized = store_data(root_path, root_document)
        reference = inspect_data_reference(serialized)
    except (AuditStorageError, OSError, TypeError, ValueError) as exc:
        raise ApplicationStateStorageError(str(exc)) from exc
    if reference is None:
        return json_copy(packed)
    blob = json.loads(serialized)
    _blob_reference(blob)
    return {
        _ROOT_MARKER: {
            "schema": _ROOT_SCHEMA,
            "version": _VERSION,
            "blob": blob,
        }
    }


def unpack_application_state(root: str | Path, stored: dict[str, Any]) -> dict[str, Any]:
    """Verify and restore an inline or packed application-state object."""
    if not isinstance(stored, dict):
        raise TypeError("stored application state must be an object")
    root_path = Path(root)
    try:
        # Bound legacy inline values before _root_payload makes a recursive
        # copy. Packed root markers are tiny; their referenced blob is bounded
        # independently by audit_storage while it is decompressed.
        _encoded_size(stored)
        packed = _root_payload(root_path, stored)
        marker = packed.pop(_FIELD_INDEX, None)
        if marker is None:
            _encoded_size(packed)
            return json_copy(packed)
        if (not isinstance(marker, dict)
                or set(marker) != {"schema", "version", "refs"}
                or marker.get("schema") != _FIELD_INDEX_SCHEMA
                or type(marker.get("version")) is not int
                or marker["version"] != _VERSION
                or not isinstance(marker.get("refs"), dict)
                or not marker["refs"]):
            raise ApplicationStateStorageError("malformed application state field index")

        references: dict[str, tuple[Any, dict[str, Any]]] = {}
        declared_size = _encoded_size(packed)
        for name, blob in marker["refs"].items():
            if (not isinstance(name, str) or name in packed
                    or name in _RESERVED_FIELDS):
                raise ApplicationStateStorageError("invalid or duplicate application state field")
            reference = _blob_reference(blob)
            declared_size += reference["raw_size"]
            if declared_size > MAX_RAW_BYTES:
                raise ApplicationStateStorageError("application state fields exceed the raw size limit")
            references[name] = (blob, reference)

        for name, (blob, _) in references.items():
            document = _load_document(root_path, blob)
            if (set(document) != {"schema", "version", "field", "value"}
                    or document.get("schema") != _FIELD_SCHEMA
                    or type(document.get("version")) is not int
                    or document["version"] != _VERSION
                    or document.get("field") != name):
                raise ApplicationStateStorageError("malformed application state field document")
            packed[name] = document["value"]
        _encoded_size(packed)
        return json_copy(packed)
    except ApplicationStateStorageError:
        raise
    except (AuditStorageError, OSError, TypeError, ValueError) as exc:
        raise ApplicationStateStorageError(str(exc)) from exc


def hydrate_run_snapshot(root: str | Path, snapshot: dict[str, Any]) -> dict[str, Any]:
    """Restore application state and operation transport values for policy code."""
    if not isinstance(snapshot, dict):
        raise TypeError("Run snapshot must be an object")
    from .payload_storage import hydrate_transport_snapshot
    hydrated = hydrate_transport_snapshot(root, snapshot)
    stored = snapshot.get("application_state")
    if stored is not None:
        hydrated["application_state"] = unpack_application_state(root, stored)
    return hydrated
