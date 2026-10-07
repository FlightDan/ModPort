"""Prepare and apply reversible compaction of ModPort's own audit database.

Preparation works from a consistent read-only SQLite backup, publishes a full
compressed backup and an authenticated update plan, and never changes the live
database.  Application verifies every input before updating the existing
database inode in one immediate transaction.
"""

from __future__ import annotations

from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
from typing import Any, Iterable
import uuid

from .audit_storage import (
    INLINE_LIMIT,
    MAX_RAW_BYTES,
    inspect_data_reference,
    load_data,
    store_raw,
)
from .evidence import atomic_json, verified_path


_SCHEMA = "modport.audit-compaction"
_VERSION = 1
_DATABASE_NAME = "audit.sqlite3"
_BACKUP_NAME = "audit-backup.sqlite3.gz"
_UPDATES_NAME = "updates.jsonl"
_MANIFEST_NAME = "manifest.json"
_IO_CHUNK = 1024 * 1024
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_BACKUP_BYTES = 1024 * 1024 * 1024 * 1024
_MAX_UPDATES_BYTES = 16 * 1024 * 1024 * 1024
_MAX_UPDATE_LINE_BYTES = MAX_RAW_BYTES + 64 * 1024
_UPDATE_FIELDS = frozenset({
    "event_id", "timestamp", "kind", "old_data_sha256", "new_data",
})
_MANIFEST_FIELDS = frozenset({
    "schema", "version", "prepared_at", "root", "database", "source",
    "result", "backup", "updates", "stats", "vacuum_after_apply",
})


class AuditCompactionError(ValueError):
    """A compaction plan or its source database failed verification."""


def _real_root(root: str | os.PathLike[str]) -> Path:
    path = Path(root).absolute()
    if (not path.is_dir() or path.is_symlink() or path.resolve() != path):
        raise AuditCompactionError("audit root must be a real directory")
    return path


def _contained_path(root: Path, path: str | os.PathLike[str], *,
                    regular: bool) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.absolute()
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise AuditCompactionError("compaction path must be contained by the audit root") from exc
    if not relative.parts or ".." in relative.parts:
        raise AuditCompactionError("compaction path must be contained by the audit root")
    if regular:
        try:
            return verified_path(root, {"path": relative.as_posix()})
        except ValueError as exc:
            raise AuditCompactionError("compaction artifact is missing or unsafe") from exc
    if (candidate.is_symlink() or candidate.resolve() != candidate
            or (candidate.exists() and not candidate.is_dir())):
        raise AuditCompactionError("compaction output directory is unsafe")
    return candidate


def _database_path(root: Path) -> Path:
    database = root / _DATABASE_NAME
    if (not database.is_file() or database.is_symlink()
            or database.resolve() != database):
        raise AuditCompactionError("audit.sqlite3 must be a real regular file")
    return database


def _database_identity(path: Path) -> dict[str, int | str]:
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise AuditCompactionError("audit.sqlite3 must be a regular file")
    return {
        "path": _DATABASE_NAME,
        "device": info.st_dev,
        "inode": info.st_ino,
    }


def _same_database(path: Path, identity: dict[str, Any]) -> bool:
    try:
        current = _database_identity(path)
    except (OSError, AuditCompactionError):
        return False
    return (current["device"], current["inode"]) == (
        identity.get("device"), identity.get("inode")
    )


def _readonly_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _validate_events_table(connection: sqlite3.Connection) -> None:
    table = connection.execute(
        "SELECT type,sql FROM sqlite_master WHERE name='events'"
    ).fetchone()
    if (table is None or table[0] != "table" or not isinstance(table[1], str)
            or "CREATE VIRTUAL TABLE" in table[1].upper()):
        raise AuditCompactionError("audit events must be a regular SQLite table")
    columns = connection.execute("PRAGMA table_info(events)").fetchall()
    if [row[1] for row in columns] != ["event_id", "timestamp", "kind", "data"]:
        raise AuditCompactionError("audit database has an unsupported events schema")
    triggers = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='events'"
    ).fetchone()
    if triggers is not None:
        raise AuditCompactionError("audit events table must not have write triggers")


def _rows(connection: sqlite3.Connection) -> Iterable[tuple[str, str, str, str]]:
    cursor = connection.execute(
        "SELECT event_id,timestamp,kind,data FROM events "
        "ORDER BY event_id COLLATE BINARY"
    )
    for row in cursor:
        if len(row) != 4 or any(not isinstance(value, str) for value in row):
            raise AuditCompactionError("audit event columns must contain text")
        yield row


def _digest_row(digest: Any, row: tuple[str, str, str, str]) -> None:
    digest.update(b"modport-audit-row-v1\0")
    for value in row:
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)


def _logical_state(connection: sqlite3.Connection) -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    for row in _rows(connection):
        _digest_row(digest, row)
        count += 1
    return digest.hexdigest(), count


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_created_path(root: Path, path: Path) -> None:
    parent = path.parent
    while True:
        _fsync_directory(parent)
        if parent == root:
            break
        parent = parent.parent


def _publish_temporary(temporary: Path, destination: Path) -> None:
    os.replace(temporary, destination)
    _fsync_directory(destination.parent)


def _file_sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_IO_CHUNK), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _compress_backup(snapshot: Path, destination: Path) -> dict[str, Any]:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent, prefix=".audit-backup-"
    )
    temporary = Path(temporary_name)
    raw_digest = hashlib.sha256()
    raw_size = 0
    try:
        with snapshot.open("rb") as source, os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as archive:
                for chunk in iter(lambda: source.read(_IO_CHUNK), b""):
                    raw_digest.update(chunk)
                    raw_size += len(chunk)
                    if raw_size > _MAX_BACKUP_BYTES:
                        raise AuditCompactionError("audit backup exceeds the size limit")
                    archive.write(chunk)
            output.flush()
            os.fsync(output.fileno())

        verified_digest = hashlib.sha256()
        verified_size = 0
        with gzip.open(temporary, "rb") as archive:
            for chunk in iter(lambda: archive.read(_IO_CHUNK), b""):
                verified_digest.update(chunk)
                verified_size += len(chunk)
        if (verified_digest.hexdigest() != raw_digest.hexdigest()
                or verified_size != raw_size):
            raise AuditCompactionError("compressed audit backup failed verification")
        archive_digest, archive_size = _file_sha256(temporary)
        if archive_size > _MAX_BACKUP_BYTES:
            raise AuditCompactionError("compressed audit backup exceeds the size limit")
        _publish_temporary(temporary, destination)
        return {
            "path": "",  # Filled with a root-relative trusted path by the caller.
            "format": "sqlite3+gzip",
            "sha256": archive_digest,
            "size": archive_size,
            "raw_sha256": raw_digest.hexdigest(),
            "raw_size": raw_size,
        }
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def _snapshot_database(database: Path, output_directory: Path) -> Path:
    descriptor, snapshot_name = tempfile.mkstemp(
        dir=output_directory, prefix=".audit-snapshot-", suffix=".sqlite3"
    )
    os.close(descriptor)
    snapshot = Path(snapshot_name)
    try:
        source = _readonly_connection(database)
        try:
            destination = sqlite3.connect(snapshot)
            try:
                _validate_events_table(source)
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()
        with snapshot.open("rb") as stream:
            os.fsync(stream.fileno())
        return snapshot
    except BaseException:
        snapshot.unlink(missing_ok=True)
        _fsync_directory(output_directory)
        raise


def _write_updates(snapshot: Path, root: Path, destination: Path) -> dict[str, Any]:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent, prefix=".audit-updates-"
    )
    temporary = Path(temporary_name)
    updates_digest = hashlib.sha256()
    source_digest = hashlib.sha256()
    result_digest = hashlib.sha256()
    source_count = 0
    selected_count = 0
    original_bytes = 0
    new_inline_bytes = 0
    stats_name: str | None = None
    blob_stats: sqlite3.Connection | None = None
    connection: sqlite3.Connection | None = None
    try:
        stats_descriptor, stats_name = tempfile.mkstemp(
            dir=destination.parent, prefix=".audit-blob-stats-", suffix=".sqlite3"
        )
        os.close(stats_descriptor)
        blob_stats = sqlite3.connect(stats_name)
        blob_stats.execute("CREATE TABLE blobs (sha256 TEXT PRIMARY KEY, size INTEGER NOT NULL)")
        connection = _readonly_connection(snapshot)
        _validate_events_table(connection)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            for event_id, timestamp, kind, data in _rows(connection):
                source_count += 1
                source_row = (event_id, timestamp, kind, data)
                _digest_row(source_digest, source_row)
                raw = data.encode("utf-8")
                if kind != "sdk.event" or len(raw) < INLINE_LIMIT:
                    _digest_row(result_digest, source_row)
                    continue
                if inspect_data_reference(data) is not None:
                    _digest_row(result_digest, source_row)
                    continue

                envelope = store_raw(root, raw)
                reference = inspect_data_reference(envelope)
                if reference is None:
                    raise AuditCompactionError("audit storage did not return an external reference")
                old_sha256 = hashlib.sha256(raw).hexdigest()
                if reference["sha256"] != old_sha256:
                    raise AuditCompactionError("audit blob digest does not match source data")
                entry = {
                    "event_id": event_id,
                    "timestamp": timestamp,
                    "kind": kind,
                    "old_data_sha256": old_sha256,
                    "new_data": envelope,
                }
                line = (json.dumps(entry, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")) + "\n").encode("utf-8")
                if len(line) > _MAX_UPDATE_LINE_BYTES:
                    raise AuditCompactionError("generated update entry exceeds the size limit")
                updates_digest.update(line)
                output.write(line)
                selected_count += 1
                original_bytes += len(raw)
                new_inline_bytes += len(envelope.encode("utf-8"))
                blob_stats.execute(
                    "INSERT OR IGNORE INTO blobs VALUES (?,?)",
                    (reference["sha256"], reference["gzip_size"]),
                )
                _digest_row(result_digest, (event_id, timestamp, kind, envelope))
            output.flush()
            os.fsync(output.fileno())
        blob_physical_bytes = blob_stats.execute(
            "SELECT COALESCE(SUM(size),0) FROM blobs"
        ).fetchone()[0]
        actual_digest, updates_size = _file_sha256(temporary)
        if actual_digest != updates_digest.hexdigest():
            raise AuditCompactionError("updates artifact failed verification")
        if updates_size > _MAX_UPDATES_BYTES:
            raise AuditCompactionError("updates artifact exceeds the size limit")
        result = {
            "updates": {
                "path": "",
                "format": "jsonl",
                "sha256": actual_digest,
                "size": updates_size,
                "count": selected_count,
            },
            "source": {
                "logical_sha256": source_digest.hexdigest(),
                "row_count": source_count,
            },
            "result": {
                "logical_sha256": result_digest.hexdigest(),
                "row_count": source_count,
            },
            "stats": {
                "source_count": source_count,
                "selected_count": selected_count,
                "original_bytes": original_bytes,
                "new_inline_bytes": new_inline_bytes,
                "blob_physical_bytes": blob_physical_bytes,
            },
        }
        _publish_temporary(temporary, destination)
        return result
    finally:
        if connection is not None:
            connection.close()
        if blob_stats is not None:
            blob_stats.close()
        if descriptor >= 0:
            os.close(descriptor)
        if stats_name is not None:
            Path(stats_name).unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)


def prepare_compaction(root: str | os.PathLike[str],
                       outdir: str | os.PathLike[str] | None = None) -> Path:
    """Publish a verified backup and plan without changing the live database."""
    root_path = _real_root(root)
    database = _database_path(root_path)
    database_identity = _database_identity(database)
    if outdir is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output_directory = root_path / "audit-maintenance" / (
            f"compaction-{stamp}-{uuid.uuid4().hex[:12]}"
        )
    else:
        output_directory = _contained_path(root_path, outdir, regular=False)
    if output_directory.exists() and any(output_directory.iterdir()):
        raise AuditCompactionError("compaction output directory must be empty")
    output_directory.mkdir(parents=True, exist_ok=True)
    output_directory = _contained_path(root_path, output_directory, regular=False)
    _fsync_created_path(root_path, output_directory)

    snapshot: Path | None = None
    try:
        snapshot = _snapshot_database(database, output_directory)
        if not _same_database(database, database_identity):
            raise AuditCompactionError("audit database inode changed during preparation")

        backup_path = output_directory / _BACKUP_NAME
        backup = _compress_backup(snapshot, backup_path)
        backup["path"] = backup_path.relative_to(root_path).as_posix()

        updates_path = output_directory / _UPDATES_NAME
        prepared = _write_updates(snapshot, root_path, updates_path)
        prepared["updates"]["path"] = updates_path.relative_to(root_path).as_posix()
        prepared["stats"]["archive_bytes"] = backup["size"]
        prepared["stats"]["database_bytes"] = database.stat().st_size

        # The plaintext working backup is no longer needed.  Remove and sync it
        # before publishing the manifest, so the manifest is the final durable
        # signal that every plan artifact is complete.
        snapshot.unlink()
        snapshot = None
        _fsync_directory(output_directory)

        manifest = {
            "schema": _SCHEMA,
            "version": _VERSION,
            "prepared_at": datetime.now(timezone.utc).isoformat(),
            "root": str(root_path),
            "database": database_identity,
            "source": prepared["source"],
            "result": prepared["result"],
            "backup": backup,
            "updates": prepared["updates"],
            "stats": prepared["stats"],
            "vacuum_after_apply": True,
        }
        manifest_path = output_directory / _MANIFEST_NAME
        atomic_json(manifest_path, manifest)
        return manifest_path
    finally:
        if snapshot is not None and snapshot.exists():
            snapshot.unlink()
            _fsync_directory(output_directory)


def _read_manifest(root: Path, manifest_path: str | os.PathLike[str]) -> tuple[Path, dict[str, Any]]:
    path = _contained_path(root, manifest_path, regular=True)
    if path.stat().st_size > _MAX_MANIFEST_BYTES:
        raise AuditCompactionError("compaction manifest exceeds the size limit")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise AuditCompactionError("compaction manifest is not valid JSON") from exc
    if not isinstance(document, dict) or set(document) != _MANIFEST_FIELDS:
        raise AuditCompactionError("compaction manifest has an unsupported shape")
    if (document.get("schema") != _SCHEMA
            or type(document.get("version")) is not int
            or document["version"] != _VERSION
            or document.get("root") != str(root)
            or document.get("vacuum_after_apply") is not True):
        raise AuditCompactionError("compaction manifest identity is invalid")
    return path, document


def _integer(value: Any, name: str, *, maximum: int | None = None) -> int:
    if type(value) is not int or value < 0 or (maximum is not None and value > maximum):
        raise AuditCompactionError(f"invalid {name}")
    return value


def _digest_string(value: Any, name: str) -> str:
    if (not isinstance(value, str) or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)):
        raise AuditCompactionError(f"invalid {name}")
    return value


def _artifact(root: Path, value: Any, *, format_name: str,
              maximum_size: int) -> tuple[Path, dict[str, Any]]:
    fields = {"path", "format", "sha256", "size"}
    if format_name == "sqlite3+gzip":
        fields |= {"raw_sha256", "raw_size"}
    else:
        fields |= {"count"}
    if not isinstance(value, dict) or set(value) != fields or value.get("format") != format_name:
        raise AuditCompactionError(f"invalid {format_name} artifact reference")
    size = _integer(value.get("size"), f"{format_name} size", maximum=maximum_size)
    checksum = _digest_string(value.get("sha256"), f"{format_name} digest")
    try:
        path = verified_path(root, value)
    except ValueError as exc:
        raise AuditCompactionError(f"{format_name} artifact is missing or unsafe") from exc
    if path.stat().st_size != size:
        raise AuditCompactionError(f"{format_name} artifact size mismatch")
    actual_checksum, actual_size = _file_sha256(path)
    if actual_size != size or actual_checksum != checksum:
        raise AuditCompactionError(f"{format_name} artifact digest mismatch")
    return path, value


def _verify_backup(root: Path, value: Any) -> Path:
    path, reference = _artifact(
        root, value, format_name="sqlite3+gzip", maximum_size=_MAX_BACKUP_BYTES
    )
    expected_size = _integer(
        reference.get("raw_size"), "backup raw size", maximum=_MAX_BACKUP_BYTES
    )
    expected_digest = _digest_string(reference.get("raw_sha256"), "backup raw digest")
    digest = hashlib.sha256()
    size = 0
    try:
        with gzip.open(path, "rb") as archive:
            for chunk in iter(lambda: archive.read(_IO_CHUNK), b""):
                size += len(chunk)
                if size > expected_size:
                    raise AuditCompactionError("backup expands beyond its declared size")
                digest.update(chunk)
    except (OSError, EOFError) as exc:
        raise AuditCompactionError("backup is not valid gzip data") from exc
    if size != expected_size or digest.hexdigest() != expected_digest:
        raise AuditCompactionError("backup decompressed content mismatch")
    return path


def _updates(root: Path, value: Any) -> tuple[Path, dict[str, Any]]:
    path, reference = _artifact(
        root, value, format_name="jsonl", maximum_size=_MAX_UPDATES_BYTES
    )
    _integer(reference.get("count"), "updates count")
    return path, reference


def _update_entries(path: Path) -> Iterable[dict[str, str]]:
    with path.open("rb") as stream:
        number = 0
        while True:
            raw_line = stream.readline(_MAX_UPDATE_LINE_BYTES + 1)
            if not raw_line:
                break
            number += 1
            if (len(raw_line) > _MAX_UPDATE_LINE_BYTES
                    or not raw_line.endswith(b"\n")):
                raise AuditCompactionError(f"oversized or incomplete update at line {number}")
            try:
                entry = json.loads(raw_line)
            except (UnicodeError, ValueError) as exc:
                raise AuditCompactionError(f"invalid update entry at line {number}") from exc
            if not isinstance(entry, dict) or set(entry) != _UPDATE_FIELDS:
                raise AuditCompactionError(f"invalid update entry at line {number}")
            if any(not isinstance(entry.get(name), str) for name in _UPDATE_FIELDS):
                raise AuditCompactionError(f"invalid update entry at line {number}")
            if entry["kind"] != "sdk.event":
                raise AuditCompactionError(f"non-SDK update entry at line {number}")
            _digest_string(entry["old_data_sha256"], "update source digest")
            yield entry


def _validate_manifest_sections(document: dict[str, Any]) -> None:
    if not isinstance(document.get("prepared_at"), str) or not document["prepared_at"]:
        raise AuditCompactionError("invalid compaction preparation timestamp")
    database = document.get("database")
    if (not isinstance(database, dict)
            or set(database) != {"path", "device", "inode"}
            or database.get("path") != _DATABASE_NAME):
        raise AuditCompactionError("invalid compaction database identity")
    _integer(database.get("device"), "database device")
    _integer(database.get("inode"), "database inode")
    for name in ("source", "result"):
        value = document.get(name)
        if not isinstance(value, dict) or set(value) != {"logical_sha256", "row_count"}:
            raise AuditCompactionError(f"invalid compaction {name} state")
        _digest_string(value.get("logical_sha256"), f"{name} logical digest")
        _integer(value.get("row_count"), f"{name} row count")
    if document["source"]["row_count"] != document["result"]["row_count"]:
        raise AuditCompactionError("compaction row counts do not match")
    stats = document.get("stats")
    expected_stats = {
        "source_count", "selected_count", "original_bytes", "new_inline_bytes",
        "blob_physical_bytes", "archive_bytes", "database_bytes",
    }
    if not isinstance(stats, dict) or set(stats) != expected_stats:
        raise AuditCompactionError("invalid compaction statistics")
    for name in expected_stats:
        _integer(stats.get(name), f"statistic {name}")
    updates = document.get("updates")
    if not isinstance(updates, dict):
        raise AuditCompactionError("invalid compaction updates reference")
    updates_count = _integer(updates.get("count"), "updates count")
    if (stats["source_count"] != document["source"]["row_count"]
            or stats["selected_count"] != updates_count
            or stats["selected_count"] > stats["source_count"]):
        raise AuditCompactionError("compaction statistics do not match the plan")


def _preflight_updates(root: Path, path: Path, expected_count: int) -> None:
    count = 0
    previous_event_id: str | None = None
    for entry in _update_entries(path):
        if previous_event_id is not None and entry["event_id"] <= previous_event_id:
            raise AuditCompactionError("update entries are not in stable event order")
        previous_event_id = entry["event_id"]
        reference = inspect_data_reference(entry["new_data"])
        if reference is None or reference["sha256"] != entry["old_data_sha256"]:
            raise AuditCompactionError("update blob does not match its source digest")
        # Full load verifies containment, symlink safety, compressed/raw sizes,
        # gzip integrity, and the raw content hash while holding one event only.
        load_data(root, entry["new_data"])
        count += 1
    if count != expected_count:
        raise AuditCompactionError("updates count does not match the manifest")


def _apply_updates(connection: sqlite3.Connection, root: Path, path: Path,
                   expected_count: int) -> int:
    applied = 0
    for entry in _update_entries(path):
        # Repeat the content verification under the same write transaction that
        # installs the reference.  Preflight keeps corrupt plans from taking a
        # lock; this check closes the gap between preflight and mutation.
        reference = inspect_data_reference(entry["new_data"])
        if reference is None or reference["sha256"] != entry["old_data_sha256"]:
            raise AuditCompactionError("update blob does not match its source digest")
        load_data(root, entry["new_data"])
        row = connection.execute(
            "SELECT timestamp,kind,data FROM events WHERE event_id=?",
            (entry["event_id"],),
        ).fetchone()
        if row is None or row[0] != entry["timestamp"] or row[1] != entry["kind"]:
            raise AuditCompactionError("planned audit event identity changed")
        if not isinstance(row[2], str):
            raise AuditCompactionError("planned audit event data is not text")
        if hashlib.sha256(row[2].encode("utf-8")).hexdigest() != entry["old_data_sha256"]:
            raise AuditCompactionError("planned audit event data changed")
        cursor = connection.execute(
            "UPDATE events SET data=? WHERE event_id=? AND timestamp=? AND kind=?",
            (entry["new_data"], entry["event_id"], entry["timestamp"], entry["kind"]),
        )
        if cursor.rowcount != 1:
            raise AuditCompactionError("planned audit event could not be updated")
        applied += 1
    if applied != expected_count:
        raise AuditCompactionError("applied update count does not match the manifest")
    return applied


def apply_compaction(root: str | os.PathLike[str],
                     manifest_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Verify and atomically apply a prepared audit compaction plan."""
    root_path = _real_root(root)
    manifest_file, document = _read_manifest(root_path, manifest_path)
    _validate_manifest_sections(document)
    database = _database_path(root_path)
    if not _same_database(database, document["database"]):
        raise AuditCompactionError("audit database inode differs from the prepared source")

    backup_path = _verify_backup(root_path, document["backup"])
    updates_path, updates_reference = _updates(root_path, document["updates"])
    expected_count = updates_reference["count"]
    _preflight_updates(root_path, updates_path, expected_count)

    connection = sqlite3.connect(database, timeout=30)
    status = "not_started"
    applied = 0
    try:
        connection.execute("BEGIN IMMEDIATE")
        _validate_events_table(connection)
        if not _same_database(database, document["database"]):
            raise AuditCompactionError("audit database inode changed before application")
        logical_digest, row_count = _logical_state(connection)
        source = document["source"]
        result = document["result"]
        if (logical_digest, row_count) == (result["logical_sha256"], result["row_count"]):
            connection.rollback()
            status = "already_applied"
        elif (logical_digest, row_count) != (source["logical_sha256"], source["row_count"]):
            raise AuditCompactionError("audit database changed after preparation")
        else:
            applied = _apply_updates(connection, root_path, updates_path, expected_count)
            final_digest, final_count = _logical_state(connection)
            if (final_digest, final_count) != (
                    result["logical_sha256"], result["row_count"]):
                raise AuditCompactionError("compacted audit state does not match the plan")
            connection.commit()
            status = "applied"
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()

    vacuum: dict[str, str]
    if status in {"applied", "already_applied"} and document["vacuum_after_apply"]:
        vacuum_connection = sqlite3.connect(database, timeout=30)
        try:
            vacuum_connection.execute("VACUUM")
        except sqlite3.Error as exc:
            vacuum = {"status": "failed", "error_type": type(exc).__name__}
        else:
            vacuum = {"status": "completed"}
        finally:
            vacuum_connection.close()
    else:
        vacuum = {"status": "skipped"}

    return {
        "status": status,
        "applied_count": applied,
        "source_count": document["source"]["row_count"],
        "selected_count": expected_count,
        "manifest_path": str(manifest_file),
        "database_path": str(database),
        "backup_path": str(backup_path),
        "updates_path": str(updates_path),
        "backup_retained": True,
        "vacuum": vacuum,
        "database_bytes_after": database.stat().st_size,
        "stats": dict(document["stats"]),
    }
