"""Archive old ModPort-owned artifacts without touching SDK storage.

Retention is deliberately split into a read-only plan and a locked apply.
Callers obtain ``settled_segments`` from an authenticated SDK snapshot; this
module never opens or modifies an SDK database.
"""
from __future__ import annotations

from contextlib import contextmanager
from . import platform_files as fcntl
import gzip
from hashlib import sha256
import json
from .platform_files import file_os as os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
from typing import Any, Iterable, Mapping

from .evidence import atomic_json, digest, read_json, workspace_lock


INDEX_PATH = Path("artifacts/storage/retention-index.json")
MAX_RESTORE_BYTES = 8 * 1024 * 1024 * 1024
RESTORE_SPACE_RESERVE_BYTES = 2 * 1024 * 1024 * 1024
_CHUNK_SIZE = 1024 * 1024
_SEGMENT_NAME = re.compile(r"[A-Za-z0-9_.:-]+")
_GENERATION_NAME = re.compile(r"generation-[0-9a-f]{32}")
_AUDIT_FILES = frozenset({"audit.html", "audit.json", "audit.csv", "audit.md"})


class ArtifactRetentionError(ValueError):
    """The retention request or retained artifact is unsafe or invalid."""


def _relative(value: str | Path) -> str:
    text = str(value).replace("\\", "/")
    path = PurePosixPath(text)
    if path.is_absolute() or text in {"", "."} or ".." in path.parts:
        raise ArtifactRetentionError("artifact path must be relative and contained")
    return path.as_posix()


def _normal_strings(values: Iterable[str] | None, *, paths: bool = False) -> list[str]:
    result = set()
    for value in values or ():
        if not isinstance(value, str) or not value:
            raise ArtifactRetentionError("retention evidence must contain non-empty strings")
        result.add(_relative(value) if paths else value)
    return sorted(result)


def _sha_stream(stream) -> tuple[str, int]:
    result = sha256()
    size = 0
    for block in iter(lambda: stream.read(_CHUNK_SIZE), b""):
        result.update(block)
        size += len(block)
    return result.hexdigest(), size


def _safe_regular(root: Path, relative: str, *, single_link: bool = True) -> tuple[Path, os.stat_result]:
    relative = _relative(relative)
    root = root.resolve()
    current = root
    parts = PurePosixPath(relative).parts
    for part in parts[:-1]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError as error:
            raise ArtifactRetentionError(f"artifact parent is missing: {relative}") from error
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ArtifactRetentionError(f"artifact parent is unsafe: {relative}")
    path = root.joinpath(*parts)
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise ArtifactRetentionError(f"artifact is missing: {relative}") from error
    if not stat.S_ISREG(info.st_mode):
        raise ArtifactRetentionError(f"artifact is not a regular file: {relative}")
    if single_link and info.st_nlink != 1:
        raise ArtifactRetentionError(f"artifact has multiple hard links: {relative}")
    return path, info


def _fingerprint(root: Path, relative: str, *, single_link: bool = True) -> dict[str, Any]:
    path, before = _safe_regular(root, relative, single_link=single_link)
    with path.open("rb") as source:
        checksum, size = _sha_stream(source)
        after = os.fstat(source.fileno())
    if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or size != after.st_size):
        raise ArtifactRetentionError(f"artifact changed while it was inspected: {relative}")
    return {"device": after.st_dev, "inode": after.st_ino, "mtime_ns": after.st_mtime_ns,
            "size": after.st_size, "sha256": checksum}


def _shape(root: Path, relative: str) -> dict[str, Any]:
    """Inspect a blocked candidate without reading its potentially large body."""
    _, info = _safe_regular(root, relative)
    return {"device": info.st_dev, "inode": info.st_ino,
            "mtime_ns": info.st_mtime_ns, "size": info.st_size}


def _same_fingerprint(root: Path, relative: str, expected: Mapping[str, Any]) -> bool:
    try:
        return _fingerprint(root, relative) == dict(expected)
    except (ArtifactRetentionError, OSError):
        return False


def _header(root: Path, relative: str, expected_run_id: str | None = None) -> tuple[dict, dict]:
    fingerprint = _fingerprint(root, relative, single_link=False)
    value = read_json(root / relative)
    if not isinstance(value, dict) or not isinstance(value.get("run_id"), str):
        raise ArtifactRetentionError(f"invalid historical header: {relative}")
    if expected_run_id is not None and value["run_id"] != expected_run_id:
        raise ArtifactRetentionError(f"historical header identity differs: {relative}")
    return value, fingerprint


def _previous(header: Mapping[str, Any]) -> str | None:
    continuation = header.get("continuation")
    if continuation is None:
        return None
    if not isinstance(continuation, dict):
        raise ArtifactRetentionError("continuation metadata is invalid")
    previous = continuation.get("previous_run_id")
    if previous is None:
        return None
    if not isinstance(previous, str) or _SEGMENT_NAME.fullmatch(previous) is None:
        raise ArtifactRetentionError("previous segment identity is unsafe")
    return previous


def _segment_chain(root: Path) -> tuple[list[str], dict, list[dict]]:
    blocked: list[dict] = []
    current, anchor = _header(root, "run.json")
    current_id = current["run_id"]
    if _SEGMENT_NAME.fullmatch(current_id) is None:
        raise ArtifactRetentionError("current segment identity is unsafe")
    chain = [current_id]
    seen = {current_id}
    header = current
    while True:
        try:
            previous = _previous(header)
        except ArtifactRetentionError as error:
            blocked.append({"scope": "segment_chain", "reason": str(error)})
            break
        if previous is None:
            break
        if previous in seen:
            blocked.append({"scope": "segment_chain", "reason": "continuation chain contains a cycle"})
            break
        relative = f"artifacts/continuations/{previous}/run.json"
        try:
            header, _ = _header(root, relative, previous)
        except (ArtifactRetentionError, OSError, ValueError, json.JSONDecodeError) as error:
            blocked.append({"scope": "segment_chain", "segment_id": previous,
                            "reason": f"continuation chain cannot be authenticated: {error}"})
            break
        chain.append(previous)
        seen.add(previous)
    return chain, anchor, blocked


def _valid_audit_current(root: Path) -> tuple[str | None, dict | None, str | None]:
    pointer = root / "audit-report/.audit-current"
    generations = root / "audit-report/.audit-generations"
    if not generations.exists():
        return None, None, None
    if generations.is_symlink() or not generations.is_dir():
        return None, None, "audit generation store is unsafe"
    try:
        info = pointer.lstat()
    except FileNotFoundError:
        return None, None, "audit current pointer is missing"
    if not stat.S_ISLNK(info.st_mode):
        return None, None, "audit current pointer is not a symlink"
    target = os.readlink(pointer)
    match = re.fullmatch(r"\.audit-generations/(generation-[0-9a-f]{32})", target)
    if match is None:
        return None, {"target": target, "inode": info.st_ino, "mtime_ns": info.st_mtime_ns}, \
            "audit current pointer is invalid"
    name = match.group(1)
    directory = generations / name
    manifest_path = directory / "manifest.json"
    try:
        _safe_regular(root,
            f"audit-report/.audit-generations/{name}/manifest.json")
        manifest = read_json(manifest_path)
    except (ArtifactRetentionError, OSError, ValueError, json.JSONDecodeError):
        return None, {"target": target, "inode": info.st_ino, "mtime_ns": info.st_mtime_ns}, \
            "audit current manifest is unreadable"
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1
            or manifest.get("generation") != name or not isinstance(manifest.get("artifacts"), dict)):
        return None, {"target": target, "inode": info.st_ino, "mtime_ns": info.st_mtime_ns}, \
            "audit current manifest is invalid"
    if set(manifest["artifacts"]) != _AUDIT_FILES:
        return None, {"target": target, "inode": info.st_ino, "mtime_ns": info.st_mtime_ns}, \
            "audit current manifest is incomplete"
    for filename, record in manifest["artifacts"].items():
        relative = f"audit-report/.audit-generations/{name}/{filename}"
        try:
            observed = _fingerprint(root, relative)
        except (ArtifactRetentionError, OSError):
            return None, {"target": target, "inode": info.st_ino, "mtime_ns": info.st_mtime_ns}, \
                "audit current generation is incomplete"
        if (not isinstance(record, dict) or record.get("sha256") != observed["sha256"]
                or record.get("size") != observed["size"]):
            return None, {"target": target, "inode": info.st_ino, "mtime_ns": info.st_mtime_ns}, \
                "audit current manifest does not authenticate its files"
    return name, {"target": target, "inode": info.st_ino, "mtime_ns": info.st_mtime_ns}, None


def _audit_groups(root: Path, current: str | None, current_error: str | None,
                  protected: set[str]) -> tuple[list[dict], list[dict]]:
    generations = root / "audit-report/.audit-generations"
    if not generations.is_dir() or generations.is_symlink():
        return [], []
    groups = []
    blocked = []
    for directory in sorted(generations.iterdir(), key=lambda value: value.name):
        if not _GENERATION_NAME.fullmatch(directory.name):
            continue
        scope = f"audit_generation:{directory.name}"
        if directory.is_symlink() or not directory.is_dir():
            blocked.append({"scope": scope, "reason": "audit generation is unsafe"})
            continue
        if directory.name == current:
            continue
        members = sorted(directory.iterdir(), key=lambda value: value.name)
        files = []
        reasons = []
        if current_error:
            group = {"id": scope, "kind": "audit_generation",
                     "generation": directory.name, "files": [], "status": "blocked",
                     "blocked_reasons": [
                         current_error + "; all generations are protected for fallback"]}
            groups.append(group)
            blocked.append({"scope": scope, "reason": group["blocked_reasons"][0]})
            continue
        expected_names = _AUDIT_FILES | {"manifest.json"}
        if ({value.name for value in members} != expected_names
                or any(value.is_symlink() or not value.is_file() for value in members)):
            reasons.append("audit generation is not a complete flat report set")
        manifest = None
        if not reasons:
            try:
                manifest = read_json(directory / "manifest.json")
            except (OSError, ValueError, json.JSONDecodeError):
                reasons.append("audit generation manifest is unreadable")
        if (manifest is not None
                and (not isinstance(manifest, dict) or manifest.get("schema_version") != 1
                     or manifest.get("generation") != directory.name
                     or not isinstance(manifest.get("artifacts"), dict)
                     or set(manifest["artifacts"]) != _AUDIT_FILES)):
            reasons.append("audit generation manifest is invalid")
        for member in members:
            relative = member.relative_to(root).as_posix()
            if relative in protected:
                reasons.append("path is explicitly protected")
            try:
                fingerprint = _fingerprint(root, relative)
            except (ArtifactRetentionError, OSError) as error:
                reasons.append(str(error))
                continue
            files.append({"relative_path": relative, "fingerprint": fingerprint})
            if member.name in _AUDIT_FILES and isinstance(manifest, dict):
                record = manifest.get("artifacts", {}).get(member.name)
                if (not isinstance(record, dict)
                        or record.get("sha256") != fingerprint["sha256"]
                        or record.get("size") != fingerprint["size"]):
                    reasons.append("audit generation manifest does not authenticate its files")
        group = {"id": scope, "kind": "audit_generation", "generation": directory.name,
                 "files": files, "status": "blocked" if reasons else "eligible",
                 "blocked_reasons": sorted(set(reasons))}
        groups.append(group)
        if reasons:
            blocked.append({"scope": scope, "reason": "; ".join(sorted(set(reasons)))})
    return groups, blocked


def plan_retention(root, *, keep_rounds=3, settled_segments=None,
                   protected_paths=None) -> dict[str, Any]:
    """Return a deterministic, read-only archive plan for one ModPort Run.

    ``settled_segments`` must come from a trusted, read-only SDK snapshot.
    Without it matching candidates remain visible but blocked.
    """
    if isinstance(keep_rounds, bool) or not isinstance(keep_rounds, int) or keep_rounds < 1:
        raise ArtifactRetentionError("keep_rounds must be a positive integer")
    root = Path(root).resolve()
    settled_list = _normal_strings(settled_segments)
    settled = set(settled_list)
    protected_list = _normal_strings(protected_paths, paths=True)
    protected = set(protected_list)
    chain, chain_anchor, blocked = _segment_chain(root)
    groups: list[dict] = []
    for segment in chain[keep_rounds:]:
        for filename in ("rework-sources.json", "prepared.json"):
            relative = f"artifacts/continuations/{segment}/{filename}"
            path = root / relative
            if not path.exists() and not path.is_symlink():
                continue
            reasons = []
            files = []
            if relative in protected:
                reasons.append("path is explicitly protected")
            if segment not in settled:
                reasons.append("segment has no authenticated settled-state evidence")
            try:
                identity = (_shape(root, relative) if reasons
                            else _fingerprint(root, relative))
                files.append({"relative_path": relative, "fingerprint": identity})
            except (ArtifactRetentionError, OSError) as error:
                reasons.append(str(error))
            scope = f"segment_artifact:{segment}:{filename}"
            group = {"id": scope, "kind": "segment_artifact", "segment_id": segment,
                     "files": files, "status": "blocked" if reasons else "eligible",
                     "blocked_reasons": sorted(set(reasons))}
            groups.append(group)
            if reasons:
                blocked.append({"scope": scope, "reason": "; ".join(sorted(set(reasons)))})
    audit_current, audit_anchor, audit_error = _valid_audit_current(root)
    audit_groups, audit_blocked = _audit_groups(root, audit_current, audit_error, protected)
    groups.extend(audit_groups)
    blocked.extend(audit_blocked)
    plan = {"schema_version": 1, "root": str(root), "keep_rounds": keep_rounds,
            "segment_chain": chain, "retained_segments": chain[:keep_rounds],
            "settled_segments": settled_list, "protected_paths": protected_list,
            "chain_anchor": chain_anchor, "audit_current": audit_current,
            "audit_anchor": audit_anchor, "candidates": groups, "blocked": blocked}
    plan["plan_sha256"] = digest(plan)
    return plan


def _validate_plan(root: Path, plan: Mapping[str, Any]) -> dict:
    if not isinstance(plan, Mapping) or plan.get("schema_version") != 1:
        raise ArtifactRetentionError("unsupported retention plan")
    supplied = dict(plan)
    checksum = supplied.pop("plan_sha256", None)
    if not isinstance(checksum, str) or checksum != digest(supplied):
        raise ArtifactRetentionError("retention plan digest differs")
    if supplied.get("root") != str(root):
        raise ArtifactRetentionError("retention plan belongs to another root")
    fresh = plan_retention(root, keep_rounds=supplied.get("keep_rounds"),
        settled_segments=supplied.get("settled_segments"),
        protected_paths=supplied.get("protected_paths"))
    if fresh != dict(plan):
        raise ArtifactRetentionError("retention plan is stale")
    return fresh


def _real_directory(path: Path, *, create: bool = False) -> Path:
    path = path.absolute()
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if not create:
                raise ArtifactRetentionError(f"directory is missing: {path}")
            current.mkdir()
            info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ArtifactRetentionError(f"directory is unsafe: {path}")
    return path


def _archive_location(archive_root: Path, checksum: str) -> tuple[Path, str]:
    relative = f"objects/sha256/{checksum[:2]}/{checksum}.gz"
    parent = archive_root / "objects" / "sha256" / checksum[:2]
    _real_directory(parent, create=True)
    return archive_root / relative, relative


def _device(path: Path) -> int:
    return path.stat().st_dev


def _verify_archive(path: Path, *, checksum: str, size: int,
                    gzip_size: int | None = None, gzip_sha256: str | None = None) -> dict:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ArtifactRetentionError("archive object is not a single-link regular file")
    if gzip_size is not None and info.st_size != gzip_size:
        raise ArtifactRetentionError("archive compressed size differs")
    with path.open("rb") as raw:
        compressed_sha, compressed_size = _sha_stream(raw)
    if gzip_sha256 is not None and compressed_sha != gzip_sha256:
        raise ArtifactRetentionError("archive compressed digest differs")
    result = sha256()
    restored_size = 0
    try:
        with gzip.open(path, "rb") as source:
            for block in iter(lambda: source.read(_CHUNK_SIZE), b""):
                restored_size += len(block)
                if restored_size > size:
                    raise ArtifactRetentionError("archive expands beyond its recorded size")
                result.update(block)
    except (EOFError, OSError) as error:
        raise ArtifactRetentionError("archive gzip stream is invalid") from error
    if restored_size != size or result.hexdigest() != checksum:
        raise ArtifactRetentionError("archive content differs")
    return {"gzip_size": compressed_size, "gzip_sha256": compressed_sha}


def _store_archive(source: Path, archive_root: Path, fingerprint: Mapping[str, Any]) -> dict:
    destination, locator = _archive_location(archive_root, fingerprint["sha256"])
    if destination.exists() or destination.is_symlink():
        archive = _verify_archive(destination, checksum=fingerprint["sha256"],
                                  size=fingerprint["size"])
    else:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=destination.parent, prefix=".modport-retention-")
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w+b") as raw:
                with gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as output:
                    with source.open("rb") as input_file:
                        before = os.fstat(input_file.fileno())
                        copied_sha = sha256()
                        copied_size = 0
                        for block in iter(lambda: input_file.read(_CHUNK_SIZE), b""):
                            output.write(block)
                            copied_sha.update(block)
                            copied_size += len(block)
                        after = os.fstat(input_file.fileno())
                    if ((before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size)
                            != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size)
                            or copied_size != fingerprint["size"]
                            or copied_sha.hexdigest() != fingerprint["sha256"]):
                        raise ArtifactRetentionError("artifact changed while it was archived")
                raw.flush()
                os.fsync(raw.fileno())
            archive = _verify_archive(temporary, checksum=fingerprint["sha256"],
                                      size=fingerprint["size"])
            try:
                os.link(temporary, destination, follow_symlinks=False)
            except FileExistsError:
                archive = _verify_archive(destination, checksum=fingerprint["sha256"],
                                          size=fingerprint["size"])
            directory = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    return {"archive_root": str(archive_root), "locator": locator,
            "sha256": fingerprint["sha256"], "size": fingerprint["size"],
            "gzip_size": archive["gzip_size"], "gzip_sha256": archive["gzip_sha256"]}


def _empty_index() -> dict:
    return {"schema_version": 1, "entries": {}}


def _load_index(root: Path) -> dict:
    path = root / INDEX_PATH
    if not path.exists() and not path.is_symlink():
        return _empty_index()
    _safe_regular(root, INDEX_PATH.as_posix(), single_link=False)
    document = read_json(path)
    if (not isinstance(document, dict) or document.get("schema_version") != 1
            or not isinstance(document.get("entries"), dict)):
        raise ArtifactRetentionError("retention index is invalid")
    for relative, record in document["entries"].items():
        _relative(relative)
        if not isinstance(record, dict):
            raise ArtifactRetentionError("retention index entry is invalid")
        required = {"archive_root", "locator", "sha256", "size", "gzip_size", "gzip_sha256"}
        if not required.issubset(record):
            raise ArtifactRetentionError("retention index entry is incomplete")
        _relative(record["locator"])
        if (not isinstance(record["archive_root"], str)
                or not Path(record["archive_root"]).is_absolute()
                or not isinstance(record["size"], int) or record["size"] < 0
                or not isinstance(record["gzip_size"], int) or record["gzip_size"] < 0
                or not re.fullmatch(r"[0-9a-f]{64}", str(record["sha256"]))
                or not re.fullmatch(r"[0-9a-f]{64}", str(record["gzip_sha256"]))):
            raise ArtifactRetentionError("retention index entry has invalid metadata")
    return document


def _ensure_index_parent(root: Path) -> Path:
    current = root
    for part in INDEX_PATH.parts[:-1]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            current.mkdir()
            info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ArtifactRetentionError("retention index parent is unsafe")
    return current


def inspect_retention_registry(root, *, verify_archives=False) -> dict:
    """Read and structurally validate the local path-to-archive registry."""
    root = Path(root).resolve()
    document = _load_index(root)
    if verify_archives:
        for record in document["entries"].values():
            archive = _archive_from_record(record)
            _verify_archive(archive, checksum=record["sha256"], size=record["size"],
                            gzip_size=record["gzip_size"],
                            gzip_sha256=record["gzip_sha256"])
    return document


def inspect_archived_artifact(root, relative) -> dict | None:
    """Return verified cold-locator metadata without restoring or inflating it.

    This authenticates the compressed object against the durable local index.
    Full uncompressed-content verification is performed by archive publication
    and again before an explicit restore.
    """
    root = Path(root).resolve()
    relative = _relative(relative)
    record = _load_index(root)["entries"].get(relative)
    if record is None:
        return None
    archive = _archive_from_record(record)
    info = archive.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ArtifactRetentionError("archive object is not a single-link regular file")
    if info.st_size != record["gzip_size"]:
        raise ArtifactRetentionError("archive compressed size differs")
    with archive.open("rb") as source:
        checksum, size = _sha_stream(source)
    if size != record["gzip_size"] or checksum != record["gzip_sha256"]:
        raise ArtifactRetentionError("archive compressed digest differs")
    return {**record, "relative_path": relative, "archive_path": str(archive)}


@contextmanager
def _audit_lock(root: Path, needed: bool):
    if not needed:
        yield
        return
    path = root / "audit-report/.audit-publish.lock"
    flags = os.O_RDWR | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def _release_verified(root: Path, item: Mapping[str, Any]) -> None:
    """Move one planned inode aside, verify it, then release it."""
    relative = item["relative_path"]
    source, _ = _safe_regular(root, relative)
    quarantine = source.with_name(f".modport-retention-{os.getpid()}-{source.name}")
    if quarantine.exists() or quarantine.is_symlink():
        raise ArtifactRetentionError(f"retention quarantine already exists: {relative}")
    os.rename(source, quarantine)
    quarantine_relative = quarantine.relative_to(root).as_posix()
    try:
        if not _same_fingerprint(root, quarantine_relative, item["fingerprint"]):
            try:
                os.link(quarantine, source, follow_symlinks=False)
            except FileExistsError:
                raise ArtifactRetentionError(
                    f"changed artifact retained at quarantine path: {quarantine_relative}")
            quarantine.unlink()
            raise ArtifactRetentionError(f"artifact changed during release: {relative}")
        quarantine.unlink()
    except BaseException:
        # If verification itself failed, restore the pathname when it is still
        # free. Never unlink an unverified quarantine.
        if quarantine.exists() and not source.exists() and not source.is_symlink():
            try:
                os.rename(quarantine, source)
            except OSError:
                pass
        raise


def apply_retention(root, plan, archive_root) -> dict[str, Any]:
    """Archive and unlink eligible plan entries after locked stale-plan checks."""
    root = Path(root).resolve()
    archive_input = Path(archive_root)
    if archive_input.exists() and archive_input.is_symlink():
        raise ArtifactRetentionError("archive root must not be a symlink")
    archive_root = _real_directory(archive_input, create=True)
    if (archive_root == root or archive_root.is_relative_to(root)
            or root.is_relative_to(archive_root)):
        raise ArtifactRetentionError("archive root must be independent of the Run root")
    if _device(archive_root) == _device(root):
        raise ArtifactRetentionError("archive root must be on an independent filesystem")
    report = {"schema_version": 1, "archived": [], "skipped": [], "errors": [],
              "archived_bytes": 0, "released_bytes": 0}
    try:
        with workspace_lock(root, blocking=False):
            candidates = plan.get("candidates", []) if isinstance(plan, Mapping) else []
            needs_audit = any(isinstance(group, Mapping)
                              and group.get("kind") == "audit_generation"
                              and group.get("status") == "eligible"
                              for group in candidates)
            with _audit_lock(root, needs_audit):
                # The audit lock must precede the fresh plan. Otherwise an
                # exporter could move .audit-current between validation and
                # lock acquisition.
                fresh = _validate_plan(root, plan)
                for group in fresh["candidates"]:
                    if group["status"] != "eligible":
                        report["skipped"].append({"id": group["id"],
                                                  "reasons": group["blocked_reasons"]})
                        continue
                    try:
                        archived = []
                        for item in group["files"]:
                            if not _same_fingerprint(root, item["relative_path"], item["fingerprint"]):
                                raise ArtifactRetentionError(
                                    f"stale artifact: {item['relative_path']}")
                            source, _ = _safe_regular(root, item["relative_path"])
                            record = _store_archive(source, archive_root, item["fingerprint"])
                            archived.append((item, record))
                        index = _load_index(root)
                        for item, record in archived:
                            index["entries"][item["relative_path"]] = record
                        _ensure_index_parent(root)
                        atomic_json(root / INDEX_PATH, index)
                        for item, _ in archived:
                            if not _same_fingerprint(root, item["relative_path"], item["fingerprint"]):
                                raise ArtifactRetentionError(
                                    f"artifact changed before release: {item['relative_path']}")
                        parents = set()
                        for item, record in archived:
                            source = root / item["relative_path"]
                            _release_verified(root, item)
                            parents.add(source.parent)
                            report["archived"].append({"id": group["id"],
                                "relative_path": item["relative_path"], **record})
                            report["archived_bytes"] += record["gzip_size"]
                            report["released_bytes"] += record["size"]
                        for parent in parents:
                            descriptor = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                            try:
                                os.fsync(descriptor)
                            finally:
                                os.close(descriptor)
                        if group["kind"] == "audit_generation":
                            directory = root / "audit-report/.audit-generations" / group["generation"]
                            try:
                                directory.rmdir()
                            except OSError:
                                pass
                    except (ArtifactRetentionError, OSError, ValueError) as error:
                        report["errors"].append({"id": group["id"], "reason": str(error)})
    except (BlockingIOError, OSError) as error:
        report["errors"].append({"id": "retention", "reason": f"active writer or lock unavailable: {error}"})
    return report


def _restore_parent(root: Path, relative: str) -> Path:
    current = root
    for part in PurePosixPath(_relative(relative)).parts[:-1]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            current.mkdir()
            info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ArtifactRetentionError("restore parent is unsafe")
    return current


def _archive_from_record(record: Mapping[str, Any]) -> Path:
    archive_root_path = Path(record["archive_root"])
    if archive_root_path.is_symlink():
        raise ArtifactRetentionError("archive root is unsafe")
    archive_root = _real_directory(archive_root_path)
    locator = _relative(record["locator"])
    archive = archive_root / locator
    current = archive_root
    for part in PurePosixPath(locator).parts[:-1]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError as error:
            raise ArtifactRetentionError("archive locator is missing") from error
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ArtifactRetentionError("archive locator is unsafe")
    return archive


def restore_archived_artifact(root, relative) -> Path | None:
    """Restore one indexed artifact atomically, or return ``None`` if unknown."""
    root = Path(root).resolve()
    relative = _relative(relative)
    try:
        with workspace_lock(root, blocking=False):
            audit_restore = relative.startswith("audit-report/.audit-generations/")
            with _audit_lock(root, audit_restore):
                index = _load_index(root)
                record = index["entries"].get(relative)
                if record is None:
                    return None
                if record["size"] > MAX_RESTORE_BYTES:
                    raise ArtifactRetentionError("artifact exceeds the finite restore size limit")
                archive = _archive_from_record(record)
                _verify_archive(archive, checksum=record["sha256"], size=record["size"],
                                gzip_size=record["gzip_size"],
                                gzip_sha256=record["gzip_sha256"])
                parent = _restore_parent(root, relative)
                target = root / relative
                if target.exists() or target.is_symlink():
                    observed = _fingerprint(root, relative)
                    if (observed["sha256"] == record["sha256"]
                            and observed["size"] == record["size"]):
                        return target
                    raise ArtifactRetentionError("restore target already contains different data")
                if (shutil.disk_usage(parent).free
                        < record["size"] + RESTORE_SPACE_RESERVE_BYTES):
                    raise ArtifactRetentionError("insufficient free space to restore artifact")
                descriptor, temporary_name = tempfile.mkstemp(
                    dir=parent, prefix=".modport-restore-")
                temporary = Path(temporary_name)
                try:
                    restored_sha = sha256()
                    restored_size = 0
                    with os.fdopen(descriptor, "wb") as output, gzip.open(archive, "rb") as source:
                        for block in iter(lambda: source.read(_CHUNK_SIZE), b""):
                            restored_size += len(block)
                            if restored_size > record["size"] or restored_size > MAX_RESTORE_BYTES:
                                raise ArtifactRetentionError("archive expands beyond its restore limit")
                            output.write(block)
                            restored_sha.update(block)
                        output.flush()
                        os.fsync(output.fileno())
                    if (restored_size != record["size"]
                            or restored_sha.hexdigest() != record["sha256"]):
                        raise ArtifactRetentionError("restored artifact digest differs")
                    try:
                        os.link(temporary, target, follow_symlinks=False)
                    except FileExistsError:
                        observed = _fingerprint(root, relative)
                        if (observed["sha256"] != record["sha256"]
                                or observed["size"] != record["size"]):
                            raise ArtifactRetentionError(
                                "restore target appeared with different data")
                    directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                    return target
                finally:
                    try:
                        temporary.unlink()
                    except FileNotFoundError:
                        pass
    except BlockingIOError as error:
        raise ArtifactRetentionError("active workspace writer prevents restore") from error
