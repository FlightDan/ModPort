"""Bounded, content-addressed storage for large audit event data.

Callers must redact data before passing it here.  Small JSON objects remain
inline.  Large objects are written durably before a compact reference is
returned for insertion into the audit database.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import gzip
import hashlib
import json
from .platform_files import file_os as os
from pathlib import Path
import stat
from typing import Any
import uuid


INLINE_LIMIT = 64 * 1024
MAX_RAW_BYTES = 128 * 1024 * 1024
MAX_COMPRESSED_BYTES = MAX_RAW_BYTES + 1024 * 1024

_BLOB_DIRECTORY = "audit-blobs"
_MARKER = "__modport_audit_blob__"
_SCHEMA = "modport.audit-blob"
_VERSION = 1
_ENCODING = "json+gzip"
_CHUNK_SIZE = 1024 * 1024
_TEXT_SIZE_CHUNK = 256 * 1024
_MAX_IDENTITY_TEXT_BYTES = 1024
_IDENTITY_FIELDS = frozenset({
    "run_id", "task_id", "stage_id", "command_id", "attempt",
    "invocation_id", "operation_invocation_id", "agent_id",
    "requested_model", "reported_model", "provider", "api",
    "reasoning_effort", "status", "timestamp",
})
_REFERENCE_FIELDS = frozenset({
    "schema", "version", "encoding", "sha256", "raw_size",
    "gzip_size", "path", "identity",
})


class AuditStorageError(ValueError):
    """An audit value or external reference is unsafe or invalid."""


def _json_encoder() -> json.JSONEncoder:
    # Match telemetry's historical json.dumps options so inline data keeps the
    # same representation and non-JSON diagnostic values still become strings.
    return json.JSONEncoder(ensure_ascii=False, default=str)


def _identity(data: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in _IDENTITY_FIELDS:
        value = data.get(name)
        if value is None or isinstance(value, bool):
            if name in data:
                result[name] = value
        elif isinstance(value, int):
            result[name] = value
        elif isinstance(value, str) and len(value) <= _MAX_IDENTITY_TEXT_BYTES:
            if len(value.encode("utf-8")) <= _MAX_IDENTITY_TEXT_BYTES:
                result[name] = value
    return result


def _checked_root(root: str | os.PathLike[str], *, create: bool) -> Path:
    path = Path(root).absolute()
    if create:
        path.mkdir(parents=True, exist_ok=True)
    if (not path.is_dir() or path.is_symlink()
            or path.resolve() != path):
        raise AuditStorageError("audit root must be a real directory")
    return path


def _directory_descriptors(root: Path, *, create: bool) -> tuple[int, int]:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    root_fd = os.open(root, os.O_RDONLY | directory | nofollow)
    try:
        if create:
            try:
                os.mkdir(_BLOB_DIRECTORY, mode=0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
            else:
                os.fsync(root_fd)
        blob_fd = os.open(
            _BLOB_DIRECTORY, os.O_RDONLY | directory | nofollow, dir_fd=root_fd
        )
    except BaseException:
        os.close(root_fd)
        raise
    return root_fd, blob_fd


def _reference(checksum: str, raw_size: int, gzip_size: int,
               identity: Mapping[str, Any]) -> str:
    document = {
        _MARKER: {
            "schema": _SCHEMA,
            "version": _VERSION,
            "encoding": _ENCODING,
            "sha256": checksum,
            "raw_size": raw_size,
            "gzip_size": gzip_size,
            "path": f"{_BLOB_DIRECTORY}/{checksum}.json.gz",
            "identity": dict(identity),
        }
    }
    return json.dumps(document, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _validate_identity(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not set(value).issubset(_IDENTITY_FIELDS):
        raise AuditStorageError("invalid audit blob identity")
    for name, item in value.items():
        if item is None or isinstance(item, bool):
            continue
        if isinstance(item, int):
            continue
        if isinstance(item, str) and len(item) <= _MAX_IDENTITY_TEXT_BYTES:
            if len(item.encode("utf-8")) <= _MAX_IDENTITY_TEXT_BYTES:
                continue
        raise AuditStorageError(f"invalid audit blob identity field: {name}")
    return dict(value)


def _inspect_document(document: Any) -> dict[str, Any] | None:
    # A reserved-looking key in a normal event is harmless.  Only the exact
    # single-key envelope is interpreted as an external reference.
    if not isinstance(document, dict) or set(document) != {_MARKER}:
        return None
    reference = document[_MARKER]
    if not isinstance(reference, dict) or set(reference) != _REFERENCE_FIELDS:
        raise AuditStorageError("malformed audit blob reference")
    if (reference.get("schema") != _SCHEMA
            or type(reference.get("version")) is not int
            or reference["version"] != _VERSION
            or reference.get("encoding") != _ENCODING):
        raise AuditStorageError("unsupported audit blob reference")
    checksum = reference.get("sha256")
    if (not isinstance(checksum, str) or len(checksum) != 64
            or any(character not in "0123456789abcdef" for character in checksum)):
        raise AuditStorageError("invalid audit blob digest")
    raw_size = reference.get("raw_size")
    gzip_size = reference.get("gzip_size")
    if (type(raw_size) is not int or not 2 <= raw_size <= MAX_RAW_BYTES):
        raise AuditStorageError("invalid audit blob raw size")
    if (type(gzip_size) is not int or not 1 <= gzip_size <= MAX_COMPRESSED_BYTES):
        raise AuditStorageError("invalid audit blob compressed size")
    expected_path = f"{_BLOB_DIRECTORY}/{checksum}.json.gz"
    if reference.get("path") != expected_path:
        raise AuditStorageError("audit blob path does not match its digest")
    return {
        "schema": _SCHEMA,
        "version": _VERSION,
        "encoding": _ENCODING,
        "sha256": checksum,
        "raw_size": raw_size,
        "gzip_size": gzip_size,
        "path": expected_path,
        "identity": _validate_identity(reference.get("identity")),
    }


def _parse_serialized(serialized: str) -> Any:
    if not isinstance(serialized, str):
        raise TypeError("serialized audit data must be text")
    if len(serialized) > MAX_RAW_BYTES:
        raise AuditStorageError("serialized audit data exceeds the size limit")
    if serialized.isascii():
        encoded_size = len(serialized)
    else:
        encoded_size = 0
        try:
            for offset in range(0, len(serialized), _TEXT_SIZE_CHUNK):
                encoded_size += len(
                    serialized[offset:offset + _TEXT_SIZE_CHUNK].encode("utf-8")
                )
                if encoded_size > MAX_RAW_BYTES:
                    raise AuditStorageError("serialized audit data exceeds the size limit")
        except UnicodeEncodeError as exc:
            raise AuditStorageError("serialized audit data is not valid UTF-8") from exc
    if encoded_size > MAX_RAW_BYTES:
        raise AuditStorageError("serialized audit data exceeds the size limit")
    try:
        return json.loads(serialized)
    except (TypeError, ValueError) as exc:
        raise AuditStorageError("serialized audit data is not valid JSON") from exc


def inspect_data_reference(serialized: str) -> dict[str, Any] | None:
    """Return validated reference metadata, or ``None`` for inline JSON."""
    return _inspect_document(_parse_serialized(serialized))


def _verify_blob(blob_fd: int, name: str, *, checksum: str, raw_size: int,
                 gzip_size: int | None, collect: bool) -> tuple[bytearray | None, int]:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, os.O_RDONLY | nofollow, dir_fd=blob_fd,
                             allow_readonly_hardlinks=True)
    except OSError as exc:
        raise AuditStorageError("audit blob is missing or inaccessible") from exc
    output = bytearray() if collect else None
    observed_size = 0
    digest = hashlib.sha256()
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise AuditStorageError("audit blob is not a regular file")
        if info.st_size > MAX_COMPRESSED_BYTES:
            raise AuditStorageError("audit blob compressed size exceeds the limit")
        if gzip_size is not None and info.st_size != gzip_size:
            raise AuditStorageError("audit blob compressed size mismatch")
        try:
            with os.fdopen(descriptor, "rb") as compressed:
                descriptor = -1
                with gzip.GzipFile(fileobj=compressed, mode="rb") as stream:
                    while True:
                        chunk = stream.read(_CHUNK_SIZE)
                        if not chunk:
                            break
                        observed_size += len(chunk)
                        if observed_size > MAX_RAW_BYTES or observed_size > raw_size:
                            raise AuditStorageError("audit blob raw size exceeds its reference")
                        digest.update(chunk)
                        if output is not None:
                            output.extend(chunk)
        except (OSError, EOFError) as exc:
            raise AuditStorageError("audit blob is not valid gzip data") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if observed_size != raw_size:
        raise AuditStorageError("audit blob raw size mismatch")
    if digest.hexdigest() != checksum:
        raise AuditStorageError("audit blob digest mismatch")
    return output, info.st_size


def _write_blob(root: Path, chunks: Iterable[bytes], *,
                identity: Mapping[str, Any]) -> str:
    try:
        root_fd, blob_fd = _directory_descriptors(root, create=True)
    except OSError as exc:
        raise AuditStorageError("audit blob directory is missing or unsafe") from exc
    temporary = f".audit-blob-{os.getpid()}-{uuid.uuid4().hex}.tmp"
    descriptor = -1
    raw_size = 0
    digest = hashlib.sha256()
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600, dir_fd=blob_fd)
        with os.fdopen(descriptor, "wb") as compressed:
            descriptor = -1
            with gzip.GzipFile(filename="", mode="wb", fileobj=compressed, mtime=0) as stream:
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise TypeError("audit JSON chunks must be bytes")
                    raw_size += len(chunk)
                    if raw_size > MAX_RAW_BYTES:
                        raise AuditStorageError("audit event exceeds the raw size limit")
                    digest.update(chunk)
                    stream.write(chunk)
            compressed.flush()
            os.fsync(compressed.fileno())

        temporary_info = os.stat(temporary, dir_fd=blob_fd, follow_symlinks=False)
        if temporary_info.st_size > MAX_COMPRESSED_BYTES:
            raise AuditStorageError("audit event exceeds the compressed size limit")
        checksum = digest.hexdigest()
        final_name = f"{checksum}.json.gz"
        try:
            os.link(temporary, final_name, src_dir_fd=blob_fd,
                    dst_dir_fd=blob_fd, follow_symlinks=False)
        except FileExistsError:
            # A concurrent writer may have won.  Accept it only after verifying
            # that the existing object really has the requested content.
            _, gzip_size = _verify_blob(
                blob_fd, final_name, checksum=checksum, raw_size=raw_size,
                gzip_size=None, collect=False,
            )
        else:
            gzip_size = temporary_info.st_size
            os.fsync(blob_fd)
        return _reference(checksum, raw_size, gzip_size, identity)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=blob_fd)
        except FileNotFoundError:
            pass
        os.close(blob_fd)
        os.close(root_fd)


def store_data(root: str | os.PathLike[str], data: Mapping[str, Any]) -> str:
    """Serialize audit data inline or return a durable external blob reference."""
    if not isinstance(data, Mapping):
        raise TypeError("audit data must be a mapping")
    value = dict(data)
    encoded_parts: list[bytes] = []
    encoded_size = 0
    iterator = iter(_json_encoder().iterencode(value))
    for text in iterator:
        chunk = text.encode("utf-8")
        encoded_parts.append(chunk)
        encoded_size += len(chunk)
        if encoded_size >= INLINE_LIMIT:
            root_path = _checked_root(root, create=True)

            def remaining() -> Iterable[bytes]:
                yield from encoded_parts
                for later_text in iterator:
                    yield later_text.encode("utf-8")

            return _write_blob(root_path, remaining(), identity=_identity(value))
    return b"".join(encoded_parts).decode("utf-8")


def store_raw(root: str | os.PathLike[str], raw_bytes: bytes) -> str:
    """Externalize one historical JSON event while preserving its exact bytes."""
    if not isinstance(raw_bytes, bytes):
        raise TypeError("raw audit data must be bytes")
    if len(raw_bytes) > MAX_RAW_BYTES:
        raise AuditStorageError("audit event exceeds the raw size limit")
    try:
        document = json.loads(raw_bytes)
    except (UnicodeDecodeError, ValueError) as exc:
        raise AuditStorageError("raw audit data is not a valid JSON object") from exc
    if not isinstance(document, dict):
        raise AuditStorageError("raw audit data must contain a JSON object")
    if _inspect_document(document) is not None:
        raise AuditStorageError("nested audit blob references are unsupported")
    root_path = _checked_root(root, create=True)
    chunks = (raw_bytes[offset:offset + _CHUNK_SIZE]
              for offset in range(0, len(raw_bytes), _CHUNK_SIZE))
    return _write_blob(root_path, chunks, identity=_identity(document))


def load_data(root: str | os.PathLike[str], serialized: str) -> dict[str, Any]:
    """Load inline audit data or verify and restore an external blob reference."""
    document = _parse_serialized(serialized)
    reference = _inspect_document(document)
    if reference is None:
        if not isinstance(document, dict):
            raise AuditStorageError("audit data must contain a JSON object")
        return document

    root_path = _checked_root(root, create=False)
    try:
        root_fd, blob_fd = _directory_descriptors(root_path, create=False)
    except OSError as exc:
        raise AuditStorageError("audit blob directory is missing or unsafe") from exc
    try:
        raw, _ = _verify_blob(
            blob_fd, f"{reference['sha256']}.json.gz",
            checksum=reference["sha256"], raw_size=reference["raw_size"],
            gzip_size=reference["gzip_size"], collect=True,
        )
    finally:
        os.close(blob_fd)
        os.close(root_fd)
    try:
        restored = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise AuditStorageError("audit blob does not contain valid JSON") from exc
    if not isinstance(restored, dict):
        raise AuditStorageError("audit blob does not contain a JSON object")
    return restored
