"""Authenticated cache for complete, deterministic static source scans only.

This module deliberately does not cache Gradle, game, harness, or other runtime
evidence.  A cache hit reuses a trusted scanner observation; it is never a new
execution or acceptance evidence.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from fnmatch import fnmatchcase
from hashlib import sha256
from . import platform_files as fcntl
import json
from .platform_files import file_os as os
from .platform_files import assert_host_owned, metadata_is_host_owned
from pathlib import Path
import platform
import re
import secrets
import stat
import sys
import time
from typing import Any, Mapping

from .manifest import canonical_json
from .skill_tools.scan import DEFAULT_EXCLUDES, _exclude_directory


CACHE_SCHEMA_VERSION = 1
CACHE_KIND = "static_source_scan"
MAX_RULES_BYTES = 2 * 1024 * 1024
MAX_SCANNER_BYTES = 4 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_REPORT_BYTES = 32 * 1024 * 1024
MAX_TREE_ENTRIES = 100_000
MAX_TREE_BYTES = 256 * 1024 * 1024
LOCK_TIMEOUT_SECONDS = 30
_DIGEST = re.compile(r"[0-9a-f]{64}")
_KIND = re.compile(r"[a-z][a-z0-9_-]{0,63}")


class CacheUnavailable(ValueError):
    """The cache cannot be trusted or bounded; callers should run the check."""

    def __init__(self, code: str):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            raise ValueError("invalid cache diagnostic code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class StaticScanKey:
    fingerprint: str
    kind: str
    rules_sha256: str
    scanner_sha256: str
    descriptor: Mapping[str, Any]


def static_scan_config() -> dict[str, Any]:
    """Return every trusted scanner option used by the v20 cache path."""

    return {
        "excluded_directory_names": sorted(DEFAULT_EXCLUDES),
        "custom_excluded_directory_names": [],
        "include_generated": False,
        "max_file_bytes": 2 * 1024 * 1024,
        "max_findings": 10_000,
        "worker_timeout_seconds": 60,
    }


def _digest_bytes(data: bytes) -> str:
    return sha256(data).hexdigest()


def _digest_json(value: Any) -> str:
    return _digest_bytes(canonical_json(value).encode("utf-8"))


def _absolute_without_links(path: Path, *, directory: bool | None = None) -> Path:
    path = Path(os.path.abspath(path))
    for entry in reversed((path, *path.parents)):
        try:
            mode = entry.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise CacheUnavailable("symlink_rejected")
        if entry != path and not stat.S_ISDIR(mode):
            raise CacheUnavailable("unsafe_ancestor")
    if directory is True and (not path.is_dir() or path.resolve() != path):
        raise CacheUnavailable("directory_unavailable")
    if directory is False and (not path.is_file() or path.resolve() != path):
        raise CacheUnavailable("file_unavailable")
    return path


def _bounded_regular(path: Path, limit: int, *, owned: bool = False) -> bytes:
    path = _absolute_without_links(path, directory=False)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise CacheUnavailable("file_unavailable") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_size > limit:
            raise CacheUnavailable("file_limit_exceeded")
        if owned and not metadata_is_host_owned(details):
            raise CacheUnavailable("cache_permissions_invalid")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            block = os.read(descriptor, min(1024 * 1024, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        data = b"".join(chunks)
        if len(data) > limit:
            raise CacheUnavailable("file_limit_exceeded")
        return data
    finally:
        os.close(descriptor)


def _rules(path: Path, expected_sha256: str) -> tuple[bytes, list[str]]:
    if not isinstance(expected_sha256, str) or not _DIGEST.fullmatch(expected_sha256):
        raise CacheUnavailable("rules_digest_missing")
    raw = _bounded_regular(path, MAX_RULES_BYTES)
    if _digest_bytes(raw) != expected_sha256:
        raise CacheUnavailable("rules_digest_mismatch")
    try:
        value = json.loads(raw)
        rows = value["rules"]
        patterns = [pattern for row in rows for pattern in row["files"]]
    except (KeyError, TypeError, ValueError, UnicodeError, OverflowError,
            RecursionError, json.JSONDecodeError) as exc:
        raise CacheUnavailable("rules_invalid") from exc
    if (not isinstance(rows, list) or not all(isinstance(pattern, str) and pattern
            for pattern in patterns)):
        raise CacheUnavailable("rules_invalid")
    return raw, sorted(set(patterns))


def _validated_config(config: Mapping[str, Any]) -> dict[str, Any]:
    expected = set(static_scan_config())
    if not isinstance(config, Mapping) or set(config) != expected:
        raise CacheUnavailable("configuration_invalid")
    excluded = config["excluded_directory_names"]
    custom = config["custom_excluded_directory_names"]
    if (not isinstance(excluded, list) or not all(isinstance(v, str) and v for v in excluded)
            or not isinstance(custom, list) or not all(isinstance(v, str) and v for v in custom)
            or type(config["include_generated"]) is not bool):
        raise CacheUnavailable("configuration_invalid")
    for name in ("max_file_bytes", "max_findings", "worker_timeout_seconds"):
        value = config[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise CacheUnavailable("configuration_invalid")
    if type(config["max_file_bytes"]) is not int or type(config["max_findings"]) is not int:
        raise CacheUnavailable("configuration_invalid")
    try:
        return json.loads(canonical_json(dict(config)))
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise CacheUnavailable("configuration_invalid") from exc


def _tree_identity(root: Path, *, patterns: list[str], config: Mapping[str, Any]) -> dict[str, Any]:
    root = _absolute_without_links(root, directory=True)
    excluded = set(config["excluded_directory_names"])
    custom = tuple(config["custom_excluded_directory_names"])
    maximum = int(config["max_file_bytes"])
    digest = sha256()
    entries = 0
    content_bytes = 0

    def record(value: Mapping[str, Any]) -> None:
        nonlocal entries
        entries += 1
        if entries > MAX_TREE_ENTRIES:
            raise CacheUnavailable("tree_entry_limit_exceeded")
        digest.update(canonical_json(value).encode("utf-8"))
        digest.update(b"\n")

    def visit(directory: Path, relative_parent: Path) -> None:
        nonlocal content_bytes
        try:
            children = []
            with os.scandir(directory) as iterator:
                for child in iterator:
                    if entries + len(children) >= MAX_TREE_ENTRIES:
                        raise CacheUnavailable("tree_entry_limit_exceeded")
                    children.append(child)
            children.sort(key=lambda item: item.name)
        except OSError as exc:
            raise CacheUnavailable("tree_unreadable") from exc
        for child in children:
            relative_path = relative_parent / child.name
            relative = relative_path.as_posix()
            try:
                if child.is_symlink():
                    raise CacheUnavailable("workspace_symlink_rejected")
                if child.is_dir(follow_symlinks=False):
                    if _exclude_directory(relative, excluded, custom):
                        record({"path": relative, "type": "excluded_directory"})
                    else:
                        record({"path": relative, "type": "directory"})
                        visit(Path(child.path), relative_path)
                    continue
                applicable = any(fnmatchcase(relative, pattern) for pattern in patterns)
                if not child.is_file(follow_symlinks=False):
                    record({"path": relative, "type": "special", "applicable": applicable})
                    if applicable:
                        raise CacheUnavailable("applicable_file_unsafe")
                    continue
                if not applicable:
                    record({"path": relative, "type": "file", "applicable": False})
                    continue
                raw = _bounded_regular(Path(child.path), maximum)
                content_bytes += len(raw)
                if content_bytes > MAX_TREE_BYTES:
                    raise CacheUnavailable("tree_byte_limit_exceeded")
                record({"path": relative, "type": "file", "applicable": True,
                        "size": len(raw), "sha256": _digest_bytes(raw)})
            except OSError as exc:
                raise CacheUnavailable("tree_unreadable") from exc

    visit(root, Path())
    return {"sha256": digest.hexdigest(), "entries": entries,
            "applicable_content_bytes": content_bytes}


def _runtime_identity() -> dict[str, Any]:
    return {
        "implementation": platform.python_implementation(),
        "python": list(sys.version_info[:3]),
        "sys_platform": sys.platform,
        "os_name": os.name,
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "filesystem_encoding": sys.getfilesystemencoding(),
        "utf8_mode": int(sys.flags.utf8_mode),
    }


def prepare_static_scan(
    workspace: Path,
    *,
    kind: str,
    candidate_identity: Mapping[str, Any],
    identities: Mapping[str, Any],
    bundle_sha256: str,
    rules_path: Path,
    expected_rules_sha256: str,
    scanner_path: Path,
    config: Mapping[str, Any],
) -> StaticScanKey:
    """Authenticate and fingerprint every input that can affect a static scan."""

    if not isinstance(kind, str) or not _KIND.fullmatch(kind):
        raise CacheUnavailable("kind_invalid")
    if (not isinstance(candidate_identity, Mapping)
            or candidate_identity.get("kind") != "git_commit"
            or not isinstance(candidate_identity.get("value"), str)
            or not candidate_identity["value"]):
        raise CacheUnavailable("candidate_identity_unavailable")
    if not isinstance(bundle_sha256, str) or not _DIGEST.fullmatch(bundle_sha256):
        raise CacheUnavailable("bundle_digest_missing")
    if (not isinstance(identities, Mapping) or set(identities) != {"source", "target"}
            or not all(isinstance(identities[side], Mapping) for side in ("source", "target"))):
        raise CacheUnavailable("version_identity_invalid")
    normalized_config = _validated_config(config)
    rules_raw, patterns = _rules(rules_path, expected_rules_sha256)
    scanner_raw = _bounded_regular(scanner_path, MAX_SCANNER_BYTES)
    scanner_sha256 = _digest_bytes(scanner_raw)
    descriptor = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "cache_kind": CACHE_KIND,
        "skill_kind": kind,
        "candidate_identity": dict(candidate_identity),
        "version_identity": {side: dict(identities[side]) for side in ("source", "target")},
        "workspace_tree": _tree_identity(workspace, patterns=patterns, config=normalized_config),
        "skill": {"bundle_sha256": bundle_sha256,
                  "rules_sha256": _digest_bytes(rules_raw)},
        "scanner": {"sha256": scanner_sha256},
        "runtime": _runtime_identity(),
        "configuration": normalized_config,
    }
    return StaticScanKey(_digest_json(descriptor), kind, _digest_bytes(rules_raw),
                         scanner_sha256, descriptor)


def _cache_root(store: Path) -> Path:
    store = _absolute_without_links(store)
    store.mkdir(parents=True, exist_ok=True, mode=0o700)
    store = _absolute_without_links(store, directory=True)
    details = os.stat(store)
    if not metadata_is_host_owned(details):
        raise CacheUnavailable("cache_permissions_invalid")
    root = store / "static-scan-v1"
    root.mkdir(mode=0o700, exist_ok=True)
    root = _absolute_without_links(root, directory=True)
    details = os.stat(root)
    if not metadata_is_host_owned(details):
        raise CacheUnavailable("cache_permissions_invalid")
    for name in ("keys", "objects", "locks"):
        path = root / name
        path.mkdir(mode=0o700, exist_ok=True)
        details = os.stat(_absolute_without_links(path, directory=True))
        if not metadata_is_host_owned(details):
            raise CacheUnavailable("cache_permissions_invalid")
    return root


def _atomic(path: Path, data: bytes) -> None:
    parent = _absolute_without_links(path.parent, directory=True)
    temporary = ".check-cache-" + secrets.token_hex(16)
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                             0o600, dir_fd=parent_fd)
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)


@contextmanager
def _lock(root: Path, fingerprint: str):
    path = root / "locks" / (fingerprint + ".lock")
    parent = _absolute_without_links(path.parent, directory=True)
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        descriptor = os.open(path.name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                             0o600, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or not metadata_is_host_owned(details):
            raise CacheUnavailable("cache_permissions_invalid")
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise CacheUnavailable("cache_lock_timeout")
                time.sleep(0.05)
        yield
    finally:
        os.close(descriptor)


def _manifest_path(root: Path, key: StaticScanKey) -> Path:
    return root / "keys" / f"{key.kind}-{key.fingerprint}.json"


def _object_path(root: Path, digest: str) -> Path:
    if not _DIGEST.fullmatch(digest):
        raise CacheUnavailable("cache_manifest_invalid")
    return root / "objects" / (digest + ".json")


def _validate_report(report: Any, key: StaticScanKey, config: Mapping[str, Any], *, cached: bool) -> dict:
    if not isinstance(report, dict) or report.get("schema_version") != 1:
        raise CacheUnavailable("cache_report_invalid" if cached else "scan_report_invalid")
    if (report.get("scan_complete") is not True or report.get("skipped") != []
            or report.get("source") != key.descriptor["version_identity"]["source"]
            or report.get("target") != key.descriptor["version_identity"]["target"]
            or report.get("rules_sha256") != key.rules_sha256
            or report.get("acceptance_evidence") not in (None, False)
            or not isinstance(report.get("findings"), list)
            or not isinstance(report.get("scanned_files"), list)):
        raise CacheUnavailable("cache_report_invalid" if cached else "scan_incomplete")
    scope = report.get("scope")
    if (not isinstance(scope, dict)
            or scope.get("excluded_directory_names") != config["excluded_directory_names"]
            or scope.get("max_file_bytes") != config["max_file_bytes"]
            or scope.get("max_findings") != config["max_findings"]):
        raise CacheUnavailable("cache_report_invalid" if cached else "scan_report_invalid")
    try:
        return json.loads(canonical_json(report))
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise CacheUnavailable("cache_report_invalid" if cached else "scan_report_invalid") from exc


def load_static_scan(store: Path, key: StaticScanKey, *, workspace: Path,
                     config: Mapping[str, Any]) -> tuple[dict, dict] | None:
    """Load and authenticate a complete cached report, or return a clean miss."""

    root = _cache_root(store)
    manifest_path = _manifest_path(root, key)
    if not manifest_path.exists():
        return None
    raw_manifest = _bounded_regular(manifest_path, MAX_MANIFEST_BYTES, owned=True)
    try:
        manifest = json.loads(raw_manifest)
    except (TypeError, ValueError, UnicodeError, RecursionError, json.JSONDecodeError) as exc:
        raise CacheUnavailable("cache_manifest_invalid") from exc
    fields = {"schema_version", "cache_kind", "fingerprint", "skill_kind",
              "input_sha256", "report_sha256", "report_size",
              "scan_complete", "acceptance_evidence"}
    if (not isinstance(manifest, dict) or set(manifest) != fields
            or manifest.get("schema_version") != CACHE_SCHEMA_VERSION
            or manifest.get("cache_kind") != CACHE_KIND
            or manifest.get("fingerprint") != key.fingerprint
            or manifest.get("input_sha256") != _digest_json(key.descriptor)
            or manifest.get("skill_kind") != key.kind
            or manifest.get("scan_complete") is not True
            or manifest.get("acceptance_evidence") is not False
            or type(manifest.get("report_size")) is not int
            or not 0 < manifest["report_size"] <= MAX_REPORT_BYTES):
        raise CacheUnavailable("cache_manifest_invalid")
    report_raw = _bounded_regular(_object_path(root, manifest.get("report_sha256", "")),
                                  MAX_REPORT_BYTES, owned=True)
    if (len(report_raw) != manifest["report_size"]
            or _digest_bytes(report_raw) != manifest["report_sha256"]):
        raise CacheUnavailable("cache_report_digest_mismatch")
    try:
        report = json.loads(report_raw)
    except (TypeError, ValueError, UnicodeError, RecursionError, json.JSONDecodeError) as exc:
        raise CacheUnavailable("cache_report_invalid") from exc
    if not isinstance(report, dict) or report.get("root") != ".":
        raise CacheUnavailable("cache_report_invalid")
    report = _validate_report(report, key, _validated_config(config), cached=True)
    report["root"] = str(_absolute_without_links(workspace, directory=True))
    observation = {
        "schema_version": 1,
        "kind": CACHE_KIND,
        "status": "hit",
        "fingerprint": key.fingerprint,
        "report_sha256": manifest["report_sha256"],
        "reused_result": True,
        "scan_executed": False,
        "counts_as_new_execution": False,
        "acceptance_evidence": False,
    }
    return report, observation


def publish_static_scan(store: Path, key: StaticScanKey, report: Mapping[str, Any], *,
                        workspace: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    """Publish one complete trusted scanner result under its authenticated key."""

    normalized_config = _validated_config(config)
    normalized = _validate_report(dict(report), key, normalized_config, cached=False)
    if Path(str(normalized.get("root", ""))).resolve() != _absolute_without_links(
            workspace, directory=True):
        raise CacheUnavailable("scan_report_invalid")
    normalized["root"] = "."
    report_raw = (canonical_json(normalized) + "\n").encode("utf-8")
    if len(report_raw) > MAX_REPORT_BYTES:
        raise CacheUnavailable("report_limit_exceeded")
    report_sha256 = _digest_bytes(report_raw)
    manifest = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "cache_kind": CACHE_KIND,
        "fingerprint": key.fingerprint,
        "skill_kind": key.kind,
        "input_sha256": _digest_json(key.descriptor),
        "report_sha256": report_sha256,
        "report_size": len(report_raw),
        "scan_complete": True,
        "acceptance_evidence": False,
    }
    root = _cache_root(store)
    with _lock(root, key.fingerprint):
        _atomic(_object_path(root, report_sha256), report_raw)
        _atomic(_manifest_path(root, key), (canonical_json(manifest) + "\n").encode("utf-8"))
    return {
        "schema_version": 1,
        "kind": CACHE_KIND,
        "status": "stored",
        "fingerprint": key.fingerprint,
        "report_sha256": report_sha256,
        "reused_result": False,
        "scan_executed": True,
        "counts_as_new_execution": True,
        "acceptance_evidence": False,
    }


__all__ = [
    "CACHE_KIND",
    "CacheUnavailable",
    "StaticScanKey",
    "load_static_scan",
    "prepare_static_scan",
    "publish_static_scan",
    "static_scan_config",
]
