"""Verified host-owned Maven seeds and independent, content-checked snapshots.

The store is host-managed: do not grant migrated projects write access to it.
Digests are caller-supplied trust anchors, never learned from downloaded content.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from email.utils import parsedate_to_datetime
from . import platform_files as fcntl
import hashlib
import json
import math
from .platform_files import file_os as os
from pathlib import Path
import re
import secrets
import shutil
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

MAX_BYTES = 128 * 1024 * 1024
TIMEOUT_SECONDS = 30
MAX_RETRY_WAIT = 10
LOCK_TIMEOUT_SECONDS = 30
MANIFEST = "manifest.json"


def _path(path: Path) -> Path:
    if ".." in Path(path).parts:
        raise ValueError("parent traversal is forbidden")
    path = Path(os.path.abspath(path))
    for entry in reversed((path, *path.parents)):
        try:
            mode = entry.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise ValueError(f"symlinks are forbidden: {entry}")
        if entry != path and not stat.S_ISDIR(mode):
            raise ValueError(f"non-directory ancestor: {entry}")
    return path



@contextmanager
def _parent_fd(path: Path, *, create: bool = False):
    """Walk directories with no-follow handles, including all ancestors."""
    path = _path(path)
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parts[1:-1]:
            if create:
                try:
                    os.mkdir(component, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)

def _read(path: Path) -> bytes:
    path = _path(path)
    with _parent_fd(path) as parent:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError(f"not a bounded regular file: {path}")
        data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("artifact exceeds size limit")
        return data


def _coordinate(coordinate: str) -> tuple[str, str, str]:
    parts = coordinate.split(":")
    if len(parts) != 3 or any(not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", p) or ".." in p for p in parts):
        raise ValueError("expected safe group:artifact:version coordinate")
    if any(not p for p in parts[0].split(".")):
        raise ValueError("invalid Maven group")
    return tuple(parts)


def _digest(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise ValueError("expected SHA-256 hex digest")
    return value.lower()


def _url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.fragment or any(ord(c) <= 32 for c in url):
        raise ValueError("source URL must be HTTPS without credentials or fragment")
    return url


def _json(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def _files(coordinate: str) -> tuple[str, str, bytes]:
    group, artifact, version = _coordinate(coordinate)
    base = f"repository/{group.replace('.', '/')}/{artifact}/{version}/{artifact}-{version}"
    pom = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>'
           f'<groupId>{group}</groupId><artifactId>{artifact}</artifactId><version>{version}</version>'
           '<packaging>jar</packaging></project>\n').encode()
    return base + ".jar", base + ".pom", pom


@contextmanager
def _lock(store: Path, name: str = ".publication.lock", *,
          timeout_seconds: float | None = None):
    if timeout_seconds is None:
        timeout_seconds = LOCK_TIMEOUT_SECONDS
    if not math.isfinite(timeout_seconds) or timeout_seconds < 0:
        raise ValueError("dependency store lock timeout must be nonnegative and finite")
    store = _path(store)
    lock = _path(store / name)
    with _parent_fd(lock, create=True) as parent:
        fd = os.open(lock.name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("lock is not a regular file")
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"dependency store lock timed out: {lock}")
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def _atomic(path: Path, data: bytes):
    path = _path(path)
    with _parent_fd(path, create=True) as parent:
        temporary = ".seed-" + secrets.token_hex(16)
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass


def _manifest(root: Path, *, missing: bool = False) -> dict:
    manifest = _path(root / MANIFEST)
    if missing and not manifest.exists():
        return {"schema_version": 1, "artifacts": {}}
    value = json.loads(_read(manifest))
    if not isinstance(value, dict) or set(value) != {"schema_version", "artifacts"} or value["schema_version"] != 1 or not isinstance(value["artifacts"], dict):
        raise ValueError("invalid dependency manifest")
    for coordinate, record in value["artifacts"].items():
        jar, pom, pom_data = _files(coordinate)
        if not isinstance(record, dict) or set(record) != {"source_url", "jar", "pom"}:
            raise ValueError("invalid artifact record")
        _url(record["source_url"])
        for kind, expected_path in (("jar", jar), ("pom", pom)):
            item = record[kind]
            if not isinstance(item, dict) or set(item) != ({"path", "sha256", "size", "source_url"} if kind == "pom" else {"path", "sha256", "size"}) or item["path"] != expected_path:
                raise ValueError("invalid artifact path")
            data = _read(root / expected_path)
            if type(item["size"]) is not int or len(data) != item["size"] or hashlib.sha256(data).hexdigest() != _digest(item["sha256"]):
                raise ValueError(f"dependency integrity mismatch: {coordinate} {kind}")
            if kind == "pom":
                if item["source_url"] == "generated:no-transitive-dependencies":
                    if data != pom_data:
                        raise ValueError("generated POM differs from original coordinate")
                else:
                    _url(item["source_url"])
    return value


def _record(coordinate: str, data: bytes, expected_sha256: str, source_url: str,
            pom_data: bytes, pom_url: str) -> dict:
    jar, pom, _ = _files(coordinate)
    expected_sha256 = _digest(expected_sha256)
    _url(source_url)
    if len(data) > MAX_BYTES or hashlib.sha256(data).hexdigest() != expected_sha256:
        raise ValueError("download/source SHA-256 mismatch or size limit exceeded")
    return {"source_url": source_url, "jar": {"path": jar, "sha256": expected_sha256, "size": len(data)},
            "pom": {"path": pom, "sha256": hashlib.sha256(pom_data).hexdigest(), "size": len(pom_data), "source_url": pom_url}}


def publish_artifact(store: Path, coordinate: str, source: Path, expected_sha256: str, source_url: str, *,
                     pom_source: Path | None = None, pom_sha256: str | None = None,
                     pom_url: str | None = None, no_transitive_dependencies: bool = False) -> dict:
    """Publish pinned JAR/POM bytes, requiring explicit consent for a synthetic POM.

    Existing coordinates are immutable. An identical publication retains the
    original provenance URL even when requested through a different mirror.
    """
    store = _path(store)
    data = _read(source)
    if no_transitive_dependencies:
        if any(v is not None for v in (pom_source, pom_sha256, pom_url)):
            raise ValueError("choose original POM or explicit no-transitive-dependencies")
        pom_data, provenance = _files(coordinate)[2], "generated:no-transitive-dependencies"
    else:
        if pom_source is None or pom_sha256 is None or pom_url is None:
            raise ValueError("original POM and digest required unless no_transitive_dependencies=True")
        pom_data, provenance = _read(pom_source), _url(pom_url)
        if hashlib.sha256(pom_data).hexdigest() != _digest(pom_sha256):
            raise ValueError("POM SHA-256 mismatch")
    record = _record(coordinate, data, expected_sha256, source_url, pom_data, provenance)
    with _lock(store):
        manifest = _manifest(store, missing=True)
        existing = manifest["artifacts"].get(coordinate)
        if existing is not None:
            if existing["jar"] != record["jar"] or existing["pom"]["sha256"] != record["pom"]["sha256"]:
                raise ValueError(f"immutable coordinate conflict: {coordinate}")
            return existing
        for kind, content in (("jar", data), ("pom", pom_data)):
            path = _path(store / record[kind]["path"])
            if path.exists():
                if _read(path) != content:
                    raise ValueError(f"unindexed coordinate conflict: {coordinate}")
            else:
                _atomic(path, content)
        manifest["artifacts"][coordinate] = record
        _atomic(store / MANIFEST, _json(manifest))
        return record


def validate_snapshot(destination: Path) -> dict:
    """Validate every manifested byte and reject extra files, links and special nodes."""
    root = _path(destination)
    value = _manifest(root)
    expected = {MANIFEST} | {r[k]["path"] for r in value["artifacts"].values() for k in ("jar", "pom")}
    actual = set()
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            mode = path.lstat().st_mode
            if stat.S_ISREG(mode):
                actual.add(path.relative_to(root).as_posix())
            elif not stat.S_ISDIR(mode):
                raise ValueError(f"unsafe snapshot entry: {path}")
    if expected != actual:
        raise ValueError("snapshot contains missing or unmanifested files")
    return value


def snapshot_repository(store: Path, destination: Path) -> dict:
    """Atomically create independent copies; reuse only an identical valid snapshot."""
    store, destination = _path(store), _path(destination)
    if store == destination or store in destination.parents or destination in store.parents:
        raise ValueError("snapshot and store must be separate trees")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with _lock(store) if store.exists() else nullcontext():
        manifest = _manifest(store, missing=True)
        if destination.exists():
            if validate_snapshot(destination) != manifest:
                raise ValueError("existing snapshot differs from current seed store")
            return manifest
        temporary = Path(tempfile.mkdtemp(prefix=".seed-snapshot-", dir=destination.parent))
        try:
            for record in manifest["artifacts"].values():
                for kind in ("jar", "pom"):
                    path = record[kind]["path"]
                    _atomic(temporary / path, _read(store / path))
            _atomic(temporary / MANIFEST, _json(manifest))
            validate_snapshot(temporary)
            os.rename(temporary, destination)
            directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return manifest


class _HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _retry_delay(error: urllib.error.HTTPError, attempt: int) -> float:
    value = error.headers.get("Retry-After") if error.headers else None
    if value is None:
        return float(attempt + 1)
    try:
        delay = float(value) if re.fullmatch(r"[0-9]+", value) else parsedate_to_datetime(value).timestamp() - time.time()
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("invalid Retry-After; refusing early retry") from exc
    if delay > MAX_RETRY_WAIT:
        raise ValueError(f"Retry-After {delay:.0f}s exceeds bounded wait; retry later")
    return max(0.0, delay)


def _download(url: str) -> tuple[bytes, int]:
    _url(url)
    opener = urllib.request.build_opener(_HTTPSRedirect())
    for attempt in range(4):
        try:
            with opener.open(url, timeout=TIMEOUT_SECONDS) as response:
                _url(response.geturl())
                data = response.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise ValueError("download exceeds size limit")
                return data, attempt + 1
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 503) or attempt == 3:
                raise
            time.sleep(_retry_delay(exc, attempt))
        except (urllib.error.URLError, TimeoutError):
            if attempt == 3:
                raise
            time.sleep(attempt + 1)
    raise AssertionError("unreachable")


def fetch_artifact(store: Path, coordinate: str, url: str, sha256: str, *,
                   pom_url: str | None = None, pom_sha256: str | None = None,
                   no_transitive_dependencies: bool = False) -> dict:
    """Fetch pinned HTTPS JAR/POM, with at most three retries and no hit network I/O."""
    _coordinate(coordinate)
    _url(url)
    sha256 = _digest(sha256)
    if no_transitive_dependencies:
        if pom_url is not None or pom_sha256 is not None:
            raise ValueError("choose original POM or no-transitive-dependencies")
        expected_pom = hashlib.sha256(_files(coordinate)[2]).hexdigest()
    else:
        if pom_url is None or pom_sha256 is None:
            raise ValueError("original POM URL and digest required")
        _url(pom_url)
        expected_pom = _digest(pom_sha256)
    store = _path(store)
    key = hashlib.sha256(coordinate.encode()).hexdigest()
    with _lock(store, f".download-{key}.lock"):
        with _lock(store):
            existing = _manifest(store, missing=True)["artifacts"].get(coordinate)
            if existing is not None:
                if existing["jar"]["sha256"] != sha256 or existing["pom"]["sha256"] != expected_pom:
                    raise ValueError(f"immutable coordinate conflict: {coordinate}")
                return {"artifact": existing, "cache_hit": True, "download_attempts": 0}
        data, attempts = _download(url)
        if hashlib.sha256(data).hexdigest() != sha256:
            raise ValueError("download SHA-256 mismatch")
        pom_data = None
        if not no_transitive_dependencies:
            pom_data, pom_attempts = _download(pom_url)
            attempts += pom_attempts
        if pom_data is not None and hashlib.sha256(pom_data).hexdigest() != expected_pom:
            raise ValueError("POM SHA-256 mismatch")
        with tempfile.TemporaryDirectory(prefix=".download-", dir=store) as temporary:
            source = Path(temporary) / "artifact.jar"
            source.write_bytes(data)
            pom_source = None
            if pom_data is not None:
                pom_source = Path(temporary) / "artifact.pom"
                pom_source.write_bytes(pom_data)
            record = publish_artifact(store, coordinate, source, sha256, url,
                                    pom_source=pom_source, pom_sha256=pom_sha256,
                                    pom_url=pom_url, no_transitive_dependencies=no_transitive_dependencies)
            return {"artifact": record, "cache_hit": False, "download_attempts": attempts}


def verify_repository(destination: Path) -> dict:
    """Verify a frozen repository snapshot before a sandbox mount."""
    return validate_snapshot(destination)


def verify_store(store: Path) -> dict:
    """Validate a host store while allowing only its recognized lock/temp entries."""
    store = _path(store)
    with _lock(store) if store.exists() else nullcontext():
        manifest = _manifest(store, missing=True)
        expected = {r[k]["path"] for r in manifest["artifacts"].values() for k in ("jar", "pom")}
        actual = set()
        for directory, dirs, files in os.walk(store, followlinks=False):
            for name in dirs + files:
                path = Path(directory) / name
                relative = path.relative_to(store)
                mode = path.lstat().st_mode
                if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                    raise ValueError(f"unsafe store entry: {path}")
                if stat.S_ISDIR(mode):
                    continue
                first = relative.parts[0]
                if first == "repository":
                    actual.add(relative.as_posix())
                elif len(relative.parts) == 1 and (first == MANIFEST or first == ".publication.lock" or re.fullmatch(r"\.download-[0-9a-f]{64}\.lock", first) or first.startswith(".seed-")):
                    pass
                elif first.startswith(".download-") and len(relative.parts) == 2 and relative.parts[1] in ("artifact.jar", "artifact.pom"):
                    pass
                else:
                    raise ValueError(f"unexplained store file: {path}")
        if actual != expected:
            raise ValueError("store contains unmanifested repository files")
        return manifest
