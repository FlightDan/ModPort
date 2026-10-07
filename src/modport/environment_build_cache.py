"""Host-owned immutable seeds for MDK source and Gradle dependency caches.

The APIs in this module run outside migrated-project sandboxes.  MDK is keyed
by its exact HTTPS repository and Git commit, then published as a verified Git
bundle.  Gradle dependency data is published as a content-addressed
``modules-2`` snapshot without Gradle locks or garbage-collection markers.

Gradle's read-only dependency cache is a directory containing ``modules-2``;
mount the returned snapshot root read-only as ``GRADLE_RO_DEP_CACHE`` and keep
each Run's ``GRADLE_USER_HOME`` separate and writable.  Compatibility follows
the Gradle dependency-cache format table (metadata format 2.107 for Gradle
8.11 through the documented current Gradle 9.8 release). See
https://docs.gradle.org/current/userguide/dependency_caching.html.

This module deliberately does not seed Wrapper distributions or JDKs.  Those
archives need their own pinned download digests and safe extraction policy;
sharing an extracted JDK directory would also violate the private-per-Run
toolchain boundary.
"""
from __future__ import annotations

from contextlib import contextmanager
from . import platform_files as fcntl
import hashlib
import json
from .platform_files import file_os as os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import time
from typing import Any, Iterator
import urllib.parse
from .platform_files import assert_host_owned, metadata_is_host_owned


LOCK_TIMEOUT_SECONDS = 60
MANIFEST = "manifest.json"
_COMMIT_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_GRADLE_VERSION_RE = re.compile(r"([0-9]+)\.([0-9]+)(?:\.([0-9]+))?\Z")
_MAX_MANIFEST_BYTES = 64 * 1024 * 1024
_COPY_CHUNK = 1024 * 1024


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("environment cache operation deadline expired")


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _sha256_file(path: Path, *, max_bytes: int | None = None,
                 deadline: float | None = None) -> tuple[str, int]:
    _check_deadline(deadline)
    digest = hashlib.sha256()
    size = 0
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"expected a regular file: {path.name}")
        if max_bytes is not None and before.st_size > max_bytes:
            raise ValueError(f"file exceeds size limit: {path.name}")
        while True:
            _check_deadline(deadline)
            block = stream.read(_COPY_CHUNK)
            _check_deadline(deadline)
            if not block:
                break
            size += len(block)
            if max_bytes is not None and size > max_bytes:
                raise ValueError(f"file exceeds size limit: {path.name}")
            digest.update(block)
        after = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError(f"file changed while hashing: {path.name}")
    _check_deadline(deadline)
    return digest.hexdigest(), size


def _safe_path(path: Path) -> Path:
    """Return an absolute path after rejecting traversal and symlink ancestors."""
    raw = Path(path)
    if ".." in raw.parts:
        raise ValueError("parent traversal is forbidden")
    absolute = Path(os.path.abspath(raw))
    for entry in reversed((absolute, *absolute.parents)):
        try:
            mode = entry.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise ValueError(f"symlink path is forbidden: {entry.name}")
        if entry != absolute and not stat.S_ISDIR(mode):
            raise ValueError(f"non-directory path ancestor: {entry.name}")
    return absolute


def _ensure_directory(path: Path) -> Path:
    path = _safe_path(path)
    path.mkdir(parents=True, exist_ok=True)
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise ValueError("cache path is not a directory")
    return path


def _assert_cache_ownership(path: Path) -> None:
    """Shared cache entries must be host-owned and not writable by peers."""
    if os.name == "nt":
        assert_host_owned(path.absolute())
        return
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid():
        raise ValueError(f"environment cache entry is not owned by this host user: {path.name}")
    if info.st_mode & 0o022:
        raise ValueError(f"environment cache entry is group/world writable: {path.name}")


def _ensure_cache_directory(path: Path) -> Path:
    path = _ensure_directory(path)
    _assert_cache_ownership(path)
    return path


def _assert_cache_chain(root: Path, path: Path) -> None:
    root, path = _safe_path(root), _safe_path(path)
    if path != root and root not in path.parents:
        raise ValueError("environment cache entry escapes its host-owned root")
    current = path
    while current != root.parent:
        _assert_cache_ownership(current)
        if current == root:
            break
        current = current.parent


@contextmanager
def _store_lock(root: Path, lock_name: str = ".environment-build-cache.lock",
                *, timeout_seconds: float = LOCK_TIMEOUT_SECONDS,
                deadline: float | None = None) -> Iterator[None]:
    _check_deadline(deadline)
    root = _ensure_cache_directory(root)
    lock_name = _path_key(lock_name)
    lock_path = _safe_path(root / lock_name)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("environment cache lock is not a regular file")
        info = os.fstat(fd)
        if not metadata_is_host_owned(info):
            raise ValueError("environment cache lock is not private to the host user")
        lock_deadline = deadline if deadline is not None else time.monotonic() + timeout_seconds
        while True:
            try:
                _check_deadline(lock_deadline)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = lock_deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("environment build cache publication lock timed out")
                time.sleep(min(0.05, remaining))
        _check_deadline(lock_deadline)
        yield
    finally:
        os.close(fd)


def _write_new_file(path: Path, data: bytes, mode: int = 0o644) -> None:
    path = _safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def _write_manifest(path: Path, value: dict[str, Any]) -> None:
    _write_new_file(path, _json_bytes(value), 0o444)


def _read_regular(path: Path, *, max_bytes: int = _MAX_MANIFEST_BYTES,
                  deadline: float | None = None) -> bytes:
    _check_deadline(deadline)
    path = _safe_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            raise ValueError(f"invalid cache file: {path.name}")
        data = source.read(max_bytes + 1)
        _check_deadline(deadline)
        if len(data) > max_bytes:
            raise ValueError("cache metadata exceeds size limit")
        _check_deadline(deadline)
        return data


def _git(cwd: Path | None, *args: str) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"})
    command = ["git"]
    if cwd is not None:
        command.extend(["-C", str(cwd)])
    command.extend(args)
    result = subprocess.run(command, env=env, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=120, check=False)
    if result.returncode:
        raise ValueError(f"git operation failed: {args[0]}")
    return result.stdout.strip()


def _repository_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or any(ord(char) <= 32 for char in value)):
        raise ValueError("MDK repository must be credential-free HTTPS")
    return value


def _commit(value: str) -> str:
    if not isinstance(value, str) or not _COMMIT_RE.fullmatch(value.lower()):
        raise ValueError("MDK revision must be a full Git commit ID")
    return value.lower()


def _mdk_key(repository: str, commit: str) -> str:
    identity = {"kind": "mdk-git-bundle-v1", "repository": repository, "commit": commit}
    return hashlib.sha256(_json_bytes(identity)).hexdigest()


def _validate_bundle_directory(directory: Path, repository: str, commit: str,
                               store_root: Path | None = None) -> dict[str, Any]:
    directory = _safe_path(directory)
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError("MDK bundle seed is not a directory")
    _assert_cache_ownership(directory)
    if store_root is not None:
        _assert_cache_chain(store_root, directory)
    manifest_path = directory / MANIFEST
    bundle_path = directory / "mdk.bundle"
    manifest = json.loads(_read_regular(manifest_path))
    expected_keys = {"schema_version", "kind", "repository", "commit", "bundle_sha256", "bundle_size"}
    if (not isinstance(manifest, dict) or set(manifest) != expected_keys
            or manifest["schema_version"] != 1 or manifest["kind"] != "mdk-git-bundle-v1"
            or manifest["repository"] != repository or manifest["commit"] != commit):
        raise ValueError("MDK bundle manifest identity mismatch")
    entries = sorted(item.name for item in directory.iterdir())
    if entries != [MANIFEST, "mdk.bundle"]:
        raise ValueError("MDK bundle seed contains unmanifested files")
    digest, size = _sha256_file(bundle_path)
    if digest != manifest["bundle_sha256"] or size != manifest["bundle_size"]:
        raise ValueError("MDK bundle SHA-256 or size mismatch")
    _assert_cache_ownership(bundle_path)
    _assert_cache_ownership(manifest_path)
    heads = _git(None, "bundle", "list-heads", str(bundle_path)).splitlines()
    if not any(line.split(maxsplit=1)[0].lower() == commit for line in heads if line.strip()):
        raise ValueError("MDK bundle does not advertise the requested commit")
    return manifest


def _path_key(value: str) -> str:
    if (not isinstance(value, str) or not value or "\\" in value or value.startswith("/")
            or any(part in ("", ".", "..") for part in value.split("/"))):
        raise ValueError("unsafe cache-relative path")
    return value


def _tree_inventory(root: Path, *, omit_gradle_ephemera: bool = False) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """Hash regular files and reject links and special nodes in a cache tree."""
    root = _safe_path(root)
    if not root.is_dir() or root.is_symlink():
        raise ValueError("cache tree root is not a regular directory")
    files: dict[str, dict[str, Any]] = {}
    directories: set[str] = set()
    for current, dirs, names in os.walk(root, followlinks=False):
        parent = Path(current)
        relative_parent = parent.relative_to(root).as_posix()
        for name in list(dirs):
            path = parent / name
            mode = path.lstat().st_mode
            if not stat.S_ISDIR(mode):
                raise ValueError(f"symlink or special directory entry: {path.name}")
            relative = name if relative_parent == "." else f"{relative_parent}/{name}"
            directories.add(_path_key(relative))
        dirs.sort()
        for name in sorted(names):
            path = parent / name
            mode = path.lstat().st_mode
            if not stat.S_ISREG(mode):
                raise ValueError(f"symlink or special cache entry: {path.name}")
            relative = name if relative_parent == "." else f"{relative_parent}/{name}"
            relative = _path_key(relative)
            if omit_gradle_ephemera and (name == "gc.properties" or name.endswith(".lock")):
                continue
            digest, size = _sha256_file(path)
            files[relative] = {"sha256": digest, "size": size,
                               "executable": bool(mode & 0o111)}
    needed_directories = set()
    for relative in files:
        parts = relative.split("/")[:-1]
        for end in range(1, len(parts) + 1):
            needed_directories.add("/".join(parts[:end]))
    return files, needed_directories


def gradle_cache_compatibility(gradle_version: str) -> str:
    """Return the documented ``modules-2`` metadata compatibility family.

    Unknown, prerelease, and unlisted Gradle versions fail closed.  Gradle
    Gradle 8.11 through 9.8 share metadata format 2.107 in the current table.
    """
    match = _GRADLE_VERSION_RE.fullmatch(gradle_version or "")
    if not match:
        raise ValueError("Gradle version must be a stable numeric version")
    major, minor, patch = (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))
    if major == 6 and 1 <= minor <= 3:
        return "modules-2-metadata-2.95"
    if major == 6 and 4 <= minor <= 7:
        return "modules-2-metadata-2.96"
    if (major == 6 and minor >= 8) or (major == 7 and minor <= 4):
        return "modules-2-metadata-2.97"
    if major == 7 and minor == 5:
        return "modules-2-metadata-2.99"
    if major == 7 and minor == 6 and patch <= 1:
        return "modules-2-metadata-2.99"
    if major == 7 and minor == 6 and patch == 2:
        return "modules-2-metadata-2.101"
    if major == 8 and minor == 0:
        return "modules-2-metadata-2.100"
    if major == 8 and minor == 1:
        return "modules-2-metadata-2.105"
    if major == 8 and (2 <= minor <= 9 or (minor == 10 and patch <= 2)):
        return "modules-2-metadata-2.106"
    if (major == 8 and minor >= 11) or (major == 9 and minor <= 8):
        return "modules-2-metadata-2.107"
    raise ValueError(f"Gradle {gradle_version} is outside the verified cache compatibility table")


def _manifest_tree_digest(compatibility: str, files: dict[str, dict[str, Any]]) -> str:
    identity = {"compatibility": compatibility, "files": files}
    return hashlib.sha256(_json_bytes(identity)).hexdigest()


def _set_read_only_tree(root: Path) -> None:
    for current, dirs, files in os.walk(root, topdown=False, followlinks=False):
        parent = Path(current)
        for name in files:
            path = parent / name
            mode = path.lstat().st_mode
            if not stat.S_ISREG(mode):
                raise ValueError("non-regular entry in published environment cache")
            os.chmod(path, 0o555 if mode & 0o111 else 0o444)
        for name in dirs:
            path = parent / name
            if not stat.S_ISDIR(path.lstat().st_mode):
                raise ValueError("unsafe directory in published environment cache")
            os.chmod(path, 0o555)
    os.chmod(root, 0o555)


def _copy_tree_files(source: Path, destination: Path, files: dict[str, dict[str, Any]]) -> None:
    for relative, record in sorted(files.items()):
        relative = _path_key(relative)
        src = source / relative
        dst = destination / relative
        dst.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as input_stream:
            before = os.fstat(input_stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError(f"cache source is not a regular file: {relative}")
            out_fd = os.open(dst, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                             0o755 if record["executable"] else 0o644)
            digest = hashlib.sha256()
            size = 0
            with os.fdopen(out_fd, "wb") as output:
                while True:
                    block = input_stream.read(_COPY_CHUNK)
                    if not block:
                        break
                    output.write(block)
                    digest.update(block)
                    size += len(block)
                output.flush()
                os.fsync(output.fileno())
            after = os.fstat(input_stream.fileno())
            if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                    or size != record["size"] or digest.hexdigest() != record["sha256"]):
                raise ValueError(f"cache source changed or failed integrity check: {relative}")


class EnvironmentBuildCache:
    """Host-owned content-addressed cache for exact MDK and Gradle inputs."""

    def __init__(self, root: Path):
        self.root = _safe_path(Path(root))

    def publish_mdk_bundle(self, checkout: Path, repository: str, commit: str) -> dict[str, Any]:
        """Bundle a clean checkout only when origin and HEAD match the lock."""
        repository = _repository_url(repository)
        commit = _commit(commit)
        checkout = _safe_path(Path(checkout))
        if not checkout.is_dir():
            raise ValueError("MDK source checkout is missing")
        actual_repository = _git(checkout, "remote", "get-url", "origin")
        actual_commit = _git(checkout, "rev-parse", "HEAD^{commit}").lower()
        dirty = _git(checkout, "status", "--porcelain", "--untracked-files=all")
        if actual_repository != repository:
            raise ValueError("MDK origin does not match the exact repository URL")
        if actual_commit != commit:
            raise ValueError("MDK checkout does not match the exact Git commit")
        if dirty:
            raise ValueError("MDK checkout is not clean")

        key = _mdk_key(repository, commit)
        destination = self.root / "mdk-bundles" / key
        with _store_lock(self.root):
            if destination.exists():
                manifest = _validate_bundle_directory(destination, repository, commit, self.root)
                return {**manifest, "cache_key": key}
            parent = _ensure_cache_directory(destination.parent)
            temporary = Path(tempfile.mkdtemp(prefix=".pending-mdk-", dir=parent))
            try:
                bundle = temporary / "mdk.bundle"
                _git(checkout, "bundle", "create", str(bundle), "HEAD")
                digest, size = _sha256_file(bundle)
                heads = _git(None, "bundle", "list-heads", str(bundle)).splitlines()
                if not any(line.split(maxsplit=1)[0].lower() == commit for line in heads if line.strip()):
                    raise ValueError("MDK checkout changed before its bundle was created")
                manifest = {"schema_version": 1, "kind": "mdk-git-bundle-v1",
                            "repository": repository, "commit": commit,
                            "bundle_sha256": digest, "bundle_size": size}
                _write_manifest(temporary / MANIFEST, manifest)
                _validate_bundle_directory(temporary, repository, commit, self.root)
                _set_read_only_tree(temporary)
                os.rename(temporary, destination)
                _fsync_directory(parent)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        return {**manifest, "cache_key": key}

    def materialize_mdk(self, repository: str, commit: str, destination: Path) -> dict[str, Any]:
        """Create a detached, independent checkout from the verified local bundle."""
        repository = _repository_url(repository)
        commit = _commit(commit)
        key = _mdk_key(repository, commit)
        seed = self.root / "mdk-bundles" / key
        destination = _safe_path(Path(destination))
        if destination.exists() or destination.is_symlink():
            if self._verify_mdk_checkout(destination, repository, commit):
                manifest = _validate_bundle_directory(seed, repository, commit, self.root)
                return {**manifest, "cache_key": key, "materialized_path": str(destination)}
            raise ValueError("existing MDK destination does not match the requested immutable source")
        manifest = _validate_bundle_directory(seed, repository, commit, self.root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_root = Path(tempfile.mkdtemp(prefix=".mdk-materialize-", dir=destination.parent))
        try:
            checkout = temporary_root / "checkout"
            bundle = seed / "mdk.bundle"
            # A source checkout can be shallow.  Cloning directly from its
            # bundle with depth=1 keeps the exact tree self-contained without
            # requiring the missing history or ever linking to Run objects.
            _git(None, "clone", "--depth=1", "--no-hardlinks", "--no-local",
                 str(bundle), str(checkout))
            resolved = _git(checkout, "rev-parse", "HEAD^{commit}").lower()
            if resolved != commit:
                raise ValueError("materialized Git bundle resolved to a different commit")
            _git(checkout, "remote", "set-url", "origin", repository)
            if not self._verify_mdk_checkout(checkout, repository, commit):
                raise ValueError("materialized MDK checkout failed source or commit verification")
            if (checkout / ".git" / "objects" / "info" / "alternates").exists():
                raise ValueError("materialized MDK checkout unexpectedly uses Git alternates")
            if _has_shared_object_inode(seed, checkout / ".git" / "objects"):
                raise ValueError("materialized MDK checkout shares cache object hardlinks")
            os.rename(checkout, destination)
            _fsync_directory(destination.parent)
        finally:
            if temporary_root.exists():
                shutil.rmtree(temporary_root)
        return {**manifest, "cache_key": key, "materialized_path": str(destination)}

    def try_materialize_mdk(
        self, repository: str, commit: str, destination: Path,
    ) -> dict[str, Any] | None:
        """Return ``None`` only for an absent seed; corruption still raises.

        This supports a safe cold-cache fallback without catching broad
        ``ValueError`` exceptions that could hide tampering or provenance
        mismatches.
        """
        repository = _repository_url(repository)
        commit = _commit(commit)
        seed = _safe_path(self.root / "mdk-bundles" / _mdk_key(repository, commit))
        if not seed.exists() and not seed.is_symlink():
            return None
        return self.materialize_mdk(repository, commit, destination)

    @staticmethod
    def _verify_mdk_checkout(path: Path, repository: str, commit: str) -> bool:
        try:
            path = _safe_path(path)
            if not path.is_dir() or path.is_symlink():
                return False
            return (_git(path, "remote", "get-url", "origin") == repository
                    and _git(path, "rev-parse", "HEAD^{commit}").lower() == commit
                    and not _git(path, "status", "--porcelain", "--untracked-files=all"))
        except (OSError, ValueError, subprocess.SubprocessError):
            return False

    def publish_gradle_modules(
        self,
        modules_cache: Path,
        gradle_version: str,
        *,
        mdk_repository: str,
        mdk_commit: str,
        neoforge_version: str,
        mdk_bootstrap_succeeded: bool,
    ) -> dict[str, Any]:
        """Publish an immutable copy of a completed Gradle ``modules-2`` tree.

        Call only after the exact MDK's ``compileJava`` bootstrap has succeeded
        and before any migrated-project Gradle tasks touch this user home.
        ``modules_cache`` must be that bootstrap's Gradle user-home
        ``caches/modules-2`` directory. Lock files and ``gc.properties`` are
        omitted; symlinks, special entries, and empty snapshots are rejected.
        """
        compatibility = gradle_cache_compatibility(gradle_version)
        if mdk_bootstrap_succeeded is not True:
            raise ValueError("only a successful clean MDK bootstrap can seed Gradle cache")
        if not isinstance(neoforge_version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", neoforge_version):
            raise ValueError("invalid target NeoForge version for Gradle cache scope")
        mdk_repository = _repository_url(mdk_repository)
        mdk_commit = _commit(mdk_commit)
        modules_cache = _safe_path(Path(modules_cache))
        if modules_cache.name != "modules-2":
            raise ValueError("source must be the Gradle caches/modules-2 directory")
        files, _ = _tree_inventory(modules_cache, omit_gradle_ephemera=True)
        if not files:
            raise ValueError("Gradle modules-2 cache is empty after excluding lock and GC files")
        provenance = {"repository": mdk_repository, "commit": mdk_commit,
                      "neoforge_version": neoforge_version}
        key = _manifest_tree_digest(compatibility, {"scope": provenance, "files": files})
        scope_key = hashlib.sha256(_json_bytes({"compatibility": compatibility,
                                                "scope": provenance})).hexdigest()
        destination = self.root / "gradle-ro" / compatibility / scope_key / key
        manifest = {"schema_version": 1, "kind": "gradle-modules-2-v1",
                    "gradle_version": gradle_version, "compatibility": compatibility,
                    "content_sha256": key, "files": files, "mdk_source": provenance,
                    "scope_sha256": scope_key}
        publication_lock = f".environment-build-cache-gradle-{scope_key}-{key}.lock"
        with _store_lock(self.root, publication_lock):
            if destination.exists() or destination.is_symlink():
                if destination.is_symlink():
                    raise ValueError("Gradle cache snapshot destination is a symlink")
                existing = _validate_gradle_snapshot(destination, compatibility, key)
                if existing["files"] != files:
                    raise ValueError("immutable Gradle snapshot content conflict")
                return {**existing, "snapshot_key": key, "snapshot_path": str(destination)}
            destination.parent.mkdir(parents=True, exist_ok=True)
            _assert_cache_chain(self.root, destination.parent)
            temporary = Path(tempfile.mkdtemp(prefix=".pending-gradle-", dir=destination.parent))
            try:
                modules_destination = temporary / "modules-2"
                modules_destination.mkdir()
                _copy_tree_files(modules_cache, modules_destination, files)
                copied_files, _ = _tree_inventory(modules_cache, omit_gradle_ephemera=True)
                if copied_files != files:
                    raise ValueError("Gradle modules-2 source changed during snapshot publication")
                _write_manifest(temporary / MANIFEST, manifest)
                _set_read_only_tree(temporary)
                _validate_gradle_snapshot(temporary, compatibility, key, self.root)
                os.rename(temporary, destination)
                _fsync_directory(destination.parent)
            finally:
                if temporary.exists():
                    _make_writable_for_cleanup(temporary)
                    shutil.rmtree(temporary)
        return {**manifest, "snapshot_key": key, "snapshot_path": str(destination)}

    def compatible_gradle_snapshots(
        self, gradle_version: str, *, mdk_repository: str, mdk_commit: str,
        neoforge_version: str,
    ) -> list[dict[str, Any]]:
        """List verified snapshots for the consumer's cache-format family."""
        compatibility = gradle_cache_compatibility(gradle_version)
        provenance = {"repository": _repository_url(mdk_repository),
                      "commit": _commit(mdk_commit), "neoforge_version": neoforge_version}
        if not isinstance(neoforge_version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", neoforge_version):
            raise ValueError("invalid target NeoForge version for Gradle cache scope")
        scope_key = hashlib.sha256(_json_bytes({"compatibility": compatibility,
                                                "scope": provenance})).hexdigest()
        parent = _safe_path(self.root / "gradle-ro" / compatibility / scope_key)
        results = []
        if not parent.exists():
            return []
        if not parent.is_dir() or parent.is_symlink():
            raise ValueError("Gradle snapshot family path is unsafe")
        _assert_cache_chain(self.root, parent)
        for path in sorted(parent.iterdir(), key=lambda item: item.name):
            # Publishers build in this directory and expose only the final
            # content-addressed name with one atomic rename. A pending tree is
            # not a cache entry and must not block readers or look corrupt.
            if path.name.startswith(".pending-gradle-"):
                if path.is_symlink() or not path.is_dir():
                    raise ValueError("unsafe pending entry in Gradle snapshot catalog")
                _assert_cache_ownership(path)
                continue
            if path.is_symlink() or not path.is_dir():
                raise ValueError("unsafe entry in Gradle snapshot catalog")
            key = path.name
            manifest = _validate_gradle_snapshot(path, compatibility, key, self.root)
            if manifest["mdk_source"] != provenance:
                raise ValueError("Gradle cache snapshot belongs to another MDK bootstrap scope")
            total = sum(item["size"] for item in manifest["files"].values())
            results.append({**manifest, "snapshot_key": key,
                            "snapshot_path": str(path), "total_bytes": total})
        return sorted(results, key=lambda item: (-item["total_bytes"], item["snapshot_key"]))

    def read_only_gradle_cache(
        self, snapshot_key: str, gradle_version: str, *, mdk_repository: str,
        mdk_commit: str, neoforge_version: str,
    ) -> Path:
        """Return the verified snapshot root for a read-only sandbox mount."""
        compatibility = gradle_cache_compatibility(gradle_version)
        if not re.fullmatch(r"[0-9a-f]{64}", snapshot_key or ""):
            raise ValueError("invalid Gradle snapshot key")
        provenance = {"repository": _repository_url(mdk_repository),
                      "commit": _commit(mdk_commit), "neoforge_version": neoforge_version}
        if not isinstance(neoforge_version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", neoforge_version):
            raise ValueError("invalid target NeoForge version for Gradle cache scope")
        scope_key = hashlib.sha256(_json_bytes({"compatibility": compatibility,
                                                "scope": provenance})).hexdigest()
        path = _safe_path(self.root / "gradle-ro" / compatibility / scope_key / snapshot_key)
        manifest = _validate_gradle_snapshot(path, compatibility, snapshot_key, self.root)
        if manifest["mdk_source"] != provenance:
            raise ValueError("Gradle snapshot belongs to another MDK bootstrap scope")
        return path

    def materialize_gradle_modules(
        self, snapshot_key: str, destination: Path, gradle_version: str, *,
        mdk_repository: str, mdk_commit: str, neoforge_version: str,
    ) -> dict[str, Any]:
        """Copy a verified snapshot to an independent writable Gradle cache."""
        snapshot = self.read_only_gradle_cache(
            snapshot_key, gradle_version, mdk_repository=mdk_repository,
            mdk_commit=mdk_commit, neoforge_version=neoforge_version)
        destination = _safe_path(Path(destination))
        if destination.exists() or destination.is_symlink():
            raise ValueError("Gradle materialization destination already exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        manifest = json.loads(_read_regular(snapshot / MANIFEST))
        temporary = Path(tempfile.mkdtemp(prefix=".gradle-materialize-", dir=destination.parent))
        try:
            modules_source = snapshot / "modules-2"
            modules_destination = temporary / "modules-2"
            modules_destination.mkdir()
            _copy_tree_files(modules_source, modules_destination, manifest["files"])
            files, _ = _tree_inventory(modules_destination)
            if files != manifest["files"]:
                raise ValueError("materialized Gradle cache differs from immutable snapshot")
            os.rename(modules_destination, destination)
            _fsync_directory(destination.parent)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return {"snapshot_key": snapshot_key, "gradle_version": gradle_version,
                "materialized_path": str(destination)}


def _validate_gradle_snapshot(path: Path, compatibility: str, key: str,
                              store_root: Path | None = None) -> dict[str, Any]:
    path = _safe_path(path)
    if not path.is_dir() or path.is_symlink():
        raise ValueError("Gradle cache snapshot is not a directory")
    manifest = json.loads(_read_regular(path / MANIFEST))
    _assert_cache_ownership(path)
    if store_root is not None:
        _assert_cache_chain(store_root, path)
    required = {"schema_version", "kind", "gradle_version", "compatibility",
                "content_sha256", "files", "mdk_source", "scope_sha256"}
    if (not isinstance(manifest, dict) or set(manifest) != required
            or manifest["schema_version"] != 1 or manifest["kind"] != "gradle-modules-2-v1"
            or manifest["compatibility"] != compatibility or manifest["content_sha256"] != key
            or not isinstance(manifest["files"], dict)
            or gradle_cache_compatibility(manifest["gradle_version"]) != compatibility):
        raise ValueError("Gradle cache manifest identity mismatch")
    _assert_cache_ownership(path / MANIFEST)
    if (not isinstance(manifest["mdk_source"], dict)
            or set(manifest["mdk_source"]) != {"repository", "commit", "neoforge_version"}
            or _repository_url(manifest["mdk_source"]["repository"]) != manifest["mdk_source"]["repository"]
            or _commit(manifest["mdk_source"]["commit"]) != manifest["mdk_source"]["commit"]
            or not isinstance(manifest["mdk_source"]["neoforge_version"], str)):
        raise ValueError("Gradle cache source scope is invalid")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", manifest["mdk_source"]["neoforge_version"]):
        raise ValueError("Gradle cache target version is invalid")
    expected_scope = hashlib.sha256(_json_bytes({"compatibility": compatibility,
                                                 "scope": manifest["mdk_source"]})).hexdigest()
    if manifest["scope_sha256"] != expected_scope:
        raise ValueError("Gradle cache source scope hash mismatch")
    _validate_files_manifest(manifest["files"])
    if _manifest_tree_digest(compatibility, {"scope": manifest["mdk_source"],
                                             "files": manifest["files"]}) != key:
        raise ValueError("Gradle cache manifest hash mismatch")
    modules = path / "modules-2"
    files, directories = _tree_inventory(modules)
    if files != manifest["files"]:
        raise ValueError("Gradle cache snapshot bytes differ from the hash manifest")
    expected_dirs = set()
    for relative in manifest["files"]:
        parts = _path_key(relative).split("/")[:-1]
        for end in range(1, len(parts) + 1):
            expected_dirs.add("/".join(parts[:end]))
    if directories != expected_dirs:
        raise ValueError("Gradle cache snapshot contains missing or unmanifested directories")
    actual_root_entries = sorted(entry.name for entry in path.iterdir())
    if actual_root_entries != [MANIFEST, "modules-2"]:
        raise ValueError("Gradle cache snapshot root contains unmanifested entries")
    _assert_cache_ownership(modules)
    for current, dirs, names in os.walk(path, followlinks=False):
        current_path = Path(current)
        _assert_cache_ownership(current_path)
        for name in dirs + names:
            _assert_cache_ownership(current_path / name)
    return manifest


def _validate_files_manifest(files: Any) -> None:
    if not isinstance(files, dict) or not files:
        raise ValueError("Gradle cache file manifest must be a non-empty object")
    for relative, record in files.items():
        _path_key(relative)
        if (not isinstance(record, dict) or set(record) != {"sha256", "size", "executable"}
                or not isinstance(record["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])
                or type(record["size"]) is not int or record["size"] < 0
                or type(record["executable"]) is not bool):
            raise ValueError("Gradle cache file manifest entry is invalid")


def _has_shared_object_inode(left: Path, right: Path) -> bool:
    def inodes(root: Path) -> set[tuple[int, int]]:
        found = set()
        if not root.exists():
            return found
        for current, dirs, names in os.walk(root, followlinks=False):
            parent = Path(current)
            for name in names:
                path = parent / name
                mode = path.lstat().st_mode
                if not stat.S_ISREG(mode):
                    raise ValueError("unexpected Git object entry")
                info = path.stat()
                found.add((info.st_dev, info.st_ino))
        return found
    return bool(inodes(left) & inodes(right))


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _make_writable_for_cleanup(root: Path) -> None:
    if not root.exists():
        return
    for current, dirs, files in os.walk(root, topdown=False, followlinks=False):
        parent = Path(current)
        for name in files:
            path = parent / name
            if not path.is_symlink():
                path.chmod(0o600)
        for name in dirs:
            path = parent / name
            if not path.is_symlink():
                path.chmod(0o700)
    root.chmod(0o700)
