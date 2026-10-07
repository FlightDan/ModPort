"""Immutable host cache for verified Gradle Wrapper distributions and JDKs.

Publish only after the exact, clean MDK checkout completed its Java bootstrap.
The cache contains byte copies of the current Run's Wrapper distribution and
the JDK that the host verified; materialization always creates private copies
under a new Run's ``toolchains/gradle-cache`` directory.
"""
from __future__ import annotations

import hashlib
import json
from .platform_files import file_os as os
from .platform_files import assert_host_owned
from pathlib import Path
import re
import shutil
import stat
import tempfile
from typing import Any

from .environment_build_cache import (
    MANIFEST,
    _assert_cache_chain,
    _assert_cache_ownership,
    _copy_tree_files,
    _fsync_directory,
    _git,
    _has_shared_object_inode,
    _make_writable_for_cleanup,
    _path_key,
    _read_regular,
    _repository_url,
    _safe_path,
    _set_read_only_tree,
    _sha256_file,
    _store_lock,
    _tree_inventory,
    _write_manifest,
    _validate_files_manifest,
)


_NEOFORGE_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*\Z")
_GRADLE_VERSION = re.compile(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?\Z")
_JAVA_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){0,3}\Z")
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SCHEMA = "gradle-wrapper-jdk-v1"


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                        ensure_ascii=False) + "\n").encode("utf-8")


def _identity_scope(
    *, mdk_repository: str, mdk_commit: str, neoforge_version: str,
    gradle_version: str, java_version: str,
) -> dict[str, str]:
    repository = _repository_url(mdk_repository)
    commit = mdk_commit.lower() if isinstance(mdk_commit, str) else ""
    if not _COMMIT.fullmatch(commit):
        raise ValueError("MDK revision must be a full Git commit ID")
    if not isinstance(neoforge_version, str) or not _NEOFORGE_VERSION.fullmatch(neoforge_version):
        raise ValueError("invalid exact NeoForge version")
    if not isinstance(gradle_version, str) or not _GRADLE_VERSION.fullmatch(gradle_version):
        raise ValueError("Gradle version must be a stable numeric version")
    if not isinstance(java_version, str) or not _JAVA_VERSION.fullmatch(java_version):
        raise ValueError("Java version must be a numeric version")
    return {
        "mdk_repository": repository,
        "mdk_commit": commit,
        "neoforge_version": neoforge_version,
        "gradle_version": gradle_version,
        "java_version": java_version,
    }


def _identity(
    *, mdk_repository: str, mdk_commit: str, neoforge_version: str,
    gradle_version: str, java_version: str, java_sha256: str,
) -> dict[str, str]:
    scope = _identity_scope(
        mdk_repository=mdk_repository, mdk_commit=mdk_commit,
        neoforge_version=neoforge_version, gradle_version=gradle_version,
        java_version=java_version,
    )
    digest = java_sha256.lower() if isinstance(java_sha256, str) else ""
    if not _SHA256.fullmatch(digest):
        raise ValueError("verified Java SHA-256 is invalid")
    return {**scope, "java_sha256": digest}


def _cache_key(identity: dict[str, str]) -> str:
    return hashlib.sha256(_json_bytes({"kind": _SCHEMA, "identity": identity})).hexdigest()


def _scope_key(identity: dict[str, str]) -> str:
    scope = {key: value for key, value in identity.items() if key != "java_sha256"}
    return hashlib.sha256(_json_bytes({"kind": _SCHEMA, "scope": scope})).hexdigest()


def _exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _assert_private_tree(root: Path, *, allow_writable_legal_files: bool = False) -> None:
    root = _safe_path(root)
    if not root.is_dir() or root.is_symlink():
        raise ValueError("toolchain cache input is not a directory")
    _assert_cache_ownership(root)
    for current, directories, names in os.walk(root, followlinks=False):
        parent = Path(current)
        _assert_cache_ownership(parent)
        for name in directories:
            _assert_cache_ownership(parent / name)
        for name in names:
            path = parent / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ValueError(f"symlink or special toolchain input: {name}")
            if os.name == "nt":
                assert_host_owned(path.absolute())
                continue
            if info.st_uid != os.geteuid():
                raise ValueError(f"toolchain input is not owned by this host user: {name}")
            if info.st_mode & 0o022:
                relative = path.relative_to(root).parts
                legal_notice = (allow_writable_legal_files and len(relative) >= 3
                                and relative[1] == "legal")
                if not legal_notice:
                    raise ValueError(f"toolchain input is group/world writable: {name}")


def _is_lock_file(name: str) -> bool:
    lowered = name.lower()
    return lowered == "lock" or lowered.endswith((".lock", ".lck"))


def _expected_directories(files: dict[str, dict[str, Any]]) -> set[str]:
    result: set[str] = set()
    for relative in files:
        parts = _path_key(relative).split("/")[:-1]
        for end in range(1, len(parts) + 1):
            result.add("/".join(parts[:end]))
    return result


def _selected_source_inventory(
    gradle_home: Path,
    *,
    gradle_version: str,
    verified_java: Path,
    java_sha256: str,
) -> tuple[dict[str, dict[str, Any]], set[str], str] | None:
    wrapper_root = gradle_home / "wrapper"
    jdks_root = gradle_home / "jdks"
    if not _exists(wrapper_root) or not _exists(jdks_root) or not _exists(verified_java):
        return None
    wrapper_root, jdks_root = _safe_path(wrapper_root), _safe_path(jdks_root)
    verified_java = _safe_path(verified_java)
    if not wrapper_root.is_dir() or not jdks_root.is_dir():
        raise ValueError("Wrapper or JDK cache input is not a directory")
    try:
        java_relative = verified_java.relative_to(jdks_root).as_posix()
    except ValueError as exc:
        raise ValueError("verified Java executable is outside this Run's jdks directory") from exc
    java_parts = java_relative.split("/")
    if len(java_parts) < 2 or java_parts[-2] != "bin" or java_parts[-1] not in {"java", "java.exe"}:
        raise ValueError("verified Java executable must be below a JDK bin/java or bin/java.exe path")

    _assert_private_tree(wrapper_root)
    # Adoptium ships regular legal notice files with mode 0777. Limit this
    # exception to the legal tree; launcher and native library files remain
    # subject to the strict permission check. Ownership, node type, no-follow
    # reads, and copy-time digest/inode stability remain enforced.
    # Cache copies are normalized by _copy_tree_files to 0644/0755 before the
    # host snapshot and per-Run materialization are published.
    _assert_private_tree(jdks_root, allow_writable_legal_files=True)
    wrapper_files, wrapper_directories = _tree_inventory(wrapper_root)
    jdk_files, jdk_directories = _tree_inventory(jdks_root)
    # Gradle leaves distribution/JDK lock markers behind after a successful
    # bootstrap. The source tree is scanned for links and special files above;
    # transient lock markers are deliberately excluded from the published copy.
    wrapper_files = {path: record for path, record in wrapper_files.items()
                     if not _is_lock_file(Path(path).name)}
    jdk_files = {path: record for path, record in jdk_files.items()
                 if not _is_lock_file(Path(path).name)}

    distribution_names = {
        name for name in (f"gradle-{gradle_version}-bin", f"gradle-{gradle_version}-all")
        if f"dists/{name}" in wrapper_directories
    }
    if not distribution_names:
        return None
    wrapper_prefixes = {f"dists/{name}" for name in distribution_names}
    java_root = java_parts[0]
    if java_root not in jdk_directories:
        raise ValueError("verified Java does not belong to a JDK installation directory")
    selected_java_relative = f"jdks/{java_relative}"
    java_record = jdk_files.get(java_relative)
    if (java_record is None or not java_record["executable"]
            or java_record["sha256"] != java_sha256):
        raise ValueError("verified Java executable digest or mode differs from the cache identity")

    selected_wrapper_files = {
        f"wrapper/{path}": record for path, record in wrapper_files.items()
        if any(path == prefix or path.startswith(prefix + "/") for prefix in wrapper_prefixes)
    }
    selected_jdk_files = {
        f"jdks/{path}": record for path, record in jdk_files.items()
        if path == java_root or path.startswith(java_root + "/")
    }
    if not selected_wrapper_files or not selected_jdk_files:
        return None
    files = {**selected_wrapper_files, **selected_jdk_files}
    directories = _expected_directories(files)
    if selected_java_relative not in files:
        raise ValueError("verified Java is missing from the selected cache inventory")
    return files, directories, selected_java_relative


def _validate_entry(
    path: Path,
    identity: dict[str, str],
    key: str,
    store_root: Path,
    *,
    require_key_path: bool = True,
) -> dict[str, Any]:
    path = _safe_path(path)
    if not path.is_dir() or path.is_symlink():
        raise ValueError("Wrapper/JDK cache entry is not a directory")
    if require_key_path and path.name != key:
        raise ValueError("Wrapper/JDK cache directory key mismatch")
    _assert_cache_chain(store_root, path)
    manifest_path = path / MANIFEST
    manifest = json.loads(_read_regular(manifest_path))
    required = {
        "schema_version", "kind", "cache_key", "identity", "verified_java_path",
        "files", "directories", "content_sha256", "manifest_sha256",
    }
    if (not isinstance(manifest, dict) or set(manifest) != required
            or manifest["schema_version"] != 1 or manifest["kind"] != _SCHEMA
            or manifest["cache_key"] != key or manifest["identity"] != identity):
        raise ValueError("Wrapper/JDK cache manifest identity mismatch")
    payload = {name: value for name, value in manifest.items() if name != "manifest_sha256"}
    if manifest["manifest_sha256"] != hashlib.sha256(_json_bytes(payload)).hexdigest():
        raise ValueError("Wrapper/JDK cache manifest checksum mismatch")
    _validate_files_manifest(manifest["files"])
    if any(not (name.startswith("wrapper/") or name.startswith("jdks/"))
           for name in manifest["files"]):
        raise ValueError("Wrapper/JDK manifest contains paths outside cache subtrees")
    if (not isinstance(manifest["directories"], list)
            or any(not isinstance(name, str) for name in manifest["directories"])
            or len(set(manifest["directories"])) != len(manifest["directories"])):
        raise ValueError("Wrapper/JDK directory manifest is invalid")
    expected_content = hashlib.sha256(_json_bytes({
        "identity": identity,
        "files": manifest["files"],
        "directories": manifest["directories"],
    })).hexdigest()
    if manifest["content_sha256"] != expected_content:
        raise ValueError("Wrapper/JDK cache content manifest hash mismatch")
    if set(manifest["directories"]) != _expected_directories(manifest["files"]):
        raise ValueError("Wrapper/JDK directory inventory differs from its files")
    java_path = _path_key(manifest["verified_java_path"])
    java_record = manifest["files"].get(java_path)
    if (not java_path.startswith("jdks/") or java_record is None
            or java_record["sha256"] != identity["java_sha256"]
            or not java_record["executable"]):
        raise ValueError("cached verified Java does not match its bound SHA-256")
    gradle_prefix = f"wrapper/dists/gradle-{identity['gradle_version']}-"
    if not any(name.startswith(gradle_prefix) for name in manifest["files"]):
        raise ValueError("Wrapper cache does not contain the bound Gradle distribution")

    files, directories = _tree_inventory(path)
    files.pop(MANIFEST, None)
    expected_files = manifest["files"]
    if files != expected_files:
        raise ValueError("Wrapper/JDK cache bytes differ from their hash manifest")
    if directories != set(manifest["directories"]):
        raise ValueError("Wrapper/JDK cache directories differ from their manifest")
    if sorted(item.name for item in path.iterdir()) != ["jdks", "manifest.json", "wrapper"]:
        raise ValueError("Wrapper/JDK cache entry has unmanifested root items")
    for current, dirs, names in os.walk(path, followlinks=False):
        parent = Path(current)
        _assert_cache_ownership(parent)
        for name in dirs + names:
            item = parent / name
            _assert_cache_ownership(item)
            if _is_lock_file(name):
                raise ValueError("Wrapper/JDK cache contains a lock file")
    return manifest


def _make_manifest(
    identity: dict[str, str],
    key: str,
    files: dict[str, dict[str, Any]],
    directories: set[str],
    java_path: str,
) -> dict[str, Any]:
    content_sha = hashlib.sha256(_json_bytes({
        "identity": identity,
        "files": files,
        "directories": sorted(directories),
    })).hexdigest()
    payload = {
        "schema_version": 1,
        "kind": _SCHEMA,
        "cache_key": key,
        "identity": identity,
        "verified_java_path": java_path,
        "files": files,
        "directories": sorted(directories),
        "content_sha256": content_sha,
    }
    return {**payload,
            "manifest_sha256": hashlib.sha256(_json_bytes(payload)).hexdigest()}


class EnvironmentToolchainCache:
    """Host-owned cache for exact Gradle Wrapper and Java toolchain inputs."""

    def __init__(self, root: Path):
        self.root = _safe_path(Path(root))

    @staticmethod
    def _run_paths(run_root: Path) -> tuple[Path, Path, Path]:
        run_root = _safe_path(Path(run_root))
        if not _exists(run_root) or not run_root.is_dir():
            raise ValueError("Run root must already exist")
        toolchains = _safe_path(run_root / "toolchains")
        gradle_home = _safe_path(toolchains / "gradle-cache")
        mdk = _safe_path(toolchains / "mdk")
        return run_root, gradle_home, mdk

    @staticmethod
    def _verify_clean_mdk(mdk: Path, repository: str, commit: str) -> None:
        if not mdk.is_dir():
            raise ValueError("verified MDK checkout is not a directory")
        _assert_cache_ownership(mdk)
        actual_repository = _git(mdk, "remote", "get-url", "origin")
        actual_commit = _git(mdk, "rev-parse", "HEAD^{commit}").lower()
        status = _git(mdk, "status", "--porcelain", "--untracked-files=all")
        if actual_repository != repository or actual_commit != commit:
            raise ValueError("MDK checkout does not match the exact repository and commit")
        if status:
            raise ValueError("MDK checkout is not clean after compileJava bootstrap")

    def publish_bootstrap_toolchains(
        self,
        run_root: Path,
        *,
        mdk_bootstrap_succeeded: bool,
        mdk_repository: str,
        mdk_commit: str,
        neoforge_version: str,
        gradle_version: str,
        java_version: str,
        verified_java: Path,
        verified_java_sha256: str,
    ) -> dict[str, Any] | None:
        """Publish Wrapper/JDK bytes from a clean, successful Run bootstrap.

        ``None`` means the Run did not materialize the matching Wrapper/JDK
        inputs. Invalid provenance or filesystem content raises instead.
        """
        if mdk_bootstrap_succeeded is not True:
            raise ValueError("only a successful MDK compileJava bootstrap can seed Wrapper/JDK cache")
        identity = _identity(
            mdk_repository=mdk_repository, mdk_commit=mdk_commit,
            neoforge_version=neoforge_version, gradle_version=gradle_version,
            java_version=java_version, java_sha256=verified_java_sha256,
        )
        key = _cache_key(identity)
        run_root, gradle_home, mdk = self._run_paths(run_root)
        if not _exists(gradle_home):
            return None
        if not gradle_home.is_dir():
            raise ValueError("Run Gradle home is not a directory")
        repository = identity["mdk_repository"]
        commit = identity["mdk_commit"]
        if not _exists(mdk):
            return None
        self._verify_clean_mdk(mdk, repository, commit)

        verified_java = _safe_path(Path(verified_java))
        try:
            verified_java.relative_to(gradle_home / "jdks")
        except ValueError as exc:
            raise ValueError("verified Java must be inside the Run-private gradle-cache/jdks") from exc
        if not _exists(verified_java):
            return None
        actual_java_sha, _ = _sha256_file(verified_java)
        if actual_java_sha != identity["java_sha256"]:
            raise ValueError("verified Java SHA-256 differs from the requested cache identity")

        inventory = _selected_source_inventory(
            gradle_home, gradle_version=identity["gradle_version"],
            verified_java=verified_java, java_sha256=identity["java_sha256"],
        )
        if inventory is None:
            return None
        files, directories, java_path = inventory
        scope_parent = self.root / "wrapper-jdks" / _scope_key(identity)
        destination = scope_parent / key
        publication_lock = f".environment-toolchains-{key}.lock"
        with _store_lock(self.root, publication_lock):
            existing_at_start = _exists(destination)
            scope_parent.mkdir(parents=True, exist_ok=True)
            _assert_cache_chain(self.root, scope_parent)
        if existing_at_start:
            existing = _validate_entry(destination, identity, key, self.root)
            if (existing["files"] != files or existing["directories"] != sorted(directories)
                    or existing["verified_java_path"] != java_path):
                raise ValueError("immutable Wrapper/JDK cache content conflict")
            return {**existing, "cache_key": key, "cache_path": str(destination)}
        temporary = Path(tempfile.mkdtemp(prefix=".pending-toolchains-", dir=scope_parent))
        try:
            for relative in sorted(directories):
                (temporary / _path_key(relative)).mkdir(parents=True, exist_ok=True)
            _copy_tree_files(gradle_home, temporary, files)
            rescanned = _selected_source_inventory(
                gradle_home, gradle_version=identity["gradle_version"],
                verified_java=verified_java, java_sha256=identity["java_sha256"],
            )
            if rescanned != inventory:
                raise ValueError("Run toolchains changed while publishing the shared cache")
            manifest = _make_manifest(identity, key, files, directories, java_path)
            _write_manifest(temporary / MANIFEST, manifest)
            _set_read_only_tree(temporary)
            _validate_entry(temporary, identity, key, self.root, require_key_path=False)
            with _store_lock(self.root, publication_lock):
                if _exists(destination):
                    existing_won = True
                else:
                    os.rename(temporary, destination)
                    _fsync_directory(scope_parent)
                    existing_won = False
            if existing_won:
                existing = _validate_entry(destination, identity, key, self.root)
                if (existing["files"] != files or existing["directories"] != sorted(directories)
                        or existing["verified_java_path"] != java_path):
                    raise ValueError("immutable Wrapper/JDK cache content conflict")
                return {**existing, "cache_key": key, "cache_path": str(destination)}
        finally:
            if temporary.exists():
                _make_writable_for_cleanup(temporary)
                shutil.rmtree(temporary)
        return {**manifest, "cache_key": key, "cache_path": str(destination)}

    def compatible_toolchain_snapshots(
        self,
        *,
        mdk_repository: str,
        mdk_commit: str,
        neoforge_version: str,
        gradle_version: str,
        java_version: str,
    ) -> list[dict[str, Any]]:
        """List fully verified snapshots before the new Run knows its Java SHA.

        Results have stable SHA/key ordering so callers can make deterministic
        selections and then verify the materialized Java version and digest.
        """
        scope = _identity_scope(
            mdk_repository=mdk_repository, mdk_commit=mdk_commit,
            neoforge_version=neoforge_version, gradle_version=gradle_version,
            java_version=java_version,
        )
        parent = self.root / "wrapper-jdks" / _scope_key(scope)
        if not _exists(parent):
            return []
        parent = _safe_path(parent)
        if not parent.is_dir():
            raise ValueError("Wrapper/JDK cache catalog is not a directory")
        _assert_cache_chain(self.root, parent)
        entries = sorted(parent.iterdir(), key=lambda item: item.name)
        results: list[dict[str, Any]] = []
        for entry in entries:
            if entry.name.startswith(".pending-toolchains-"):
                continue
            if entry.is_symlink() or not entry.is_dir():
                raise ValueError("unsafe entry in Wrapper/JDK cache catalog")
            raw_manifest = json.loads(_read_regular(entry / MANIFEST))
            stored = raw_manifest.get("identity") if isinstance(raw_manifest, dict) else None
            if not isinstance(stored, dict):
                raise ValueError("Wrapper/JDK cache identity is missing")
            identity = _identity(
                mdk_repository=stored.get("mdk_repository"),
                mdk_commit=stored.get("mdk_commit"),
                neoforge_version=stored.get("neoforge_version"),
                gradle_version=stored.get("gradle_version"),
                java_version=stored.get("java_version"),
                java_sha256=stored.get("java_sha256"),
            )
            key = _cache_key(identity)
            if entry.name != key:
                raise ValueError("Wrapper/JDK cache directory key mismatch")
            if not all(identity[field] == value for field, value in scope.items()):
                raise ValueError("Wrapper/JDK cache scope directory contains a different identity")
            manifest = _validate_entry(entry, identity, key, self.root)
            results.append({**manifest, "cache_key": key, "cache_path": str(entry)})
        return sorted(results, key=lambda item: (item["identity"]["java_sha256"], item["cache_key"]))

    def try_materialize_toolchains(
        self,
        run_root: Path,
        *,
        mdk_repository: str,
        mdk_commit: str,
        neoforge_version: str,
        gradle_version: str,
        java_version: str,
        verified_java_sha256: str | None = None,
    ) -> dict[str, Any] | None:
        """Copy a verified hit, optionally selecting by scope before Java exists.

        If no SHA is known yet, the first result from
        :meth:`compatible_toolchain_snapshots` is selected deterministically;
        the returned record contains the SHA the caller must verify after
        materialization.
        """
        if verified_java_sha256 is None:
            candidates = self.compatible_toolchain_snapshots(
                mdk_repository=mdk_repository, mdk_commit=mdk_commit,
                neoforge_version=neoforge_version, gradle_version=gradle_version,
                java_version=java_version,
            )
            if not candidates:
                return None
            identity = candidates[0]["identity"]
        else:
            identity = _identity(
                mdk_repository=mdk_repository, mdk_commit=mdk_commit,
                neoforge_version=neoforge_version, gradle_version=gradle_version,
                java_version=java_version, java_sha256=verified_java_sha256,
            )
        key = _cache_key(identity)
        entry = self.root / "wrapper-jdks" / _scope_key(identity) / key
        if not _exists(entry):
            return None
        manifest = _validate_entry(entry, identity, key, self.root)
        run_root, gradle_home, _ = self._run_paths(run_root)
        if (run_root == self.root or run_root in self.root.parents
                or self.root in run_root.parents):
            raise ValueError("host cache and Run destination must be separate trees")
        gradle_home.parent.mkdir(parents=True, exist_ok=True)
        if not _exists(gradle_home):
            gradle_home.mkdir()
        elif not gradle_home.is_dir():
            raise ValueError("Run Gradle home is not a directory")
        _assert_cache_ownership(gradle_home)
        target_wrapper, target_jdks = gradle_home / "wrapper", gradle_home / "jdks"
        if _exists(target_wrapper) or _exists(target_jdks):
            raise ValueError("new Run already contains Wrapper or JDK cache inputs")
        temporary = Path(tempfile.mkdtemp(prefix=".toolchain-copy-", dir=gradle_home))
        moved: list[Path] = []
        try:
            for relative in manifest["directories"]:
                (temporary / _path_key(relative)).mkdir(parents=True, exist_ok=True)
            _copy_tree_files(entry, temporary, manifest["files"])
            copied_files, copied_directories = _tree_inventory(temporary)
            if (copied_files != manifest["files"]
                    or copied_directories != set(manifest["directories"])):
                raise ValueError("private Wrapper/JDK materialization differs from its manifest")
            if _has_shared_object_inode(entry, temporary):
                raise ValueError("private Wrapper/JDK materialization shares cache inodes")
            for child in ("wrapper", "jdks"):
                source = temporary / child
                if not _exists(source):
                    raise ValueError(f"toolchain cache is missing its {child} subtree")
                destination = gradle_home / child
                if _exists(destination):
                    raise ValueError("Run toolchain destination appeared during materialization")
                os.rename(source, destination)
                moved.append(destination)
            _fsync_directory(gradle_home)
            if (_has_shared_object_inode(entry / "wrapper", target_wrapper)
                    or _has_shared_object_inode(entry / "jdks", target_jdks)):
                raise ValueError("materialized Wrapper/JDK shares cache inodes")
        except Exception:
            for path in reversed(moved):
                if _exists(path):
                    shutil.rmtree(path)
            raise
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return {
            **manifest,
            "cache_key": key,
            "cache_path": str(entry),
            "materialized_gradle_home": str(gradle_home),
            "materialized_java": str(gradle_home / manifest["verified_java_path"]),
        }
