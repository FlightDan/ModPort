"""Host-owned, SHA-1 verified Minecraft asset cache.

The caller supplies launcher-manifest bytes already authenticated by the
host's locked environment cache. The private version manifest is checked
against that launcher manifest, and the private asset index is checked against
the version manifest before any object is seeded or harvested. ``assets_root``
is explicit because ForgeGradle and NeoFormRuntime use different Gradle paths.

Filesystem operations walk directories with openat-style descriptors and
``O_NOFOLLOW``. Reads and atomic writes stay anchored to the opened parent even
if a pathname ancestor is concurrently replaced with a symlink.

The host seeds after the sandboxed entry probe exits and before the full build,
then harvests after that build exits. The sandbox can write only its private
Gradle mount, and the shared store is host-owned. Directory FDs alone do not
protect against another host writer moving an opened directory outside the
private Run root; callers must serialize such host-side moves.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import errno
from . import platform_files as fcntl
from hashlib import sha1
import json
from .platform_files import file_os as os
from pathlib import Path
import re
import secrets
import stat
import time
from typing import Any, Iterator
from .platform_files import metadata_is_host_owned


_MAX_LAUNCHER_BYTES = 16 * 1024 * 1024
_MAX_VERSION_BYTES = 32 * 1024 * 1024
_MAX_INDEX_BYTES = 64 * 1024 * 1024
_MAX_OBJECT_BYTES = 2 * 1024 * 1024 * 1024
_MAX_TOTAL_OBJECT_BYTES = 64 * 1024 * 1024 * 1024
_MAX_OBJECTS = 100_000
_CHUNK_SIZE = 1024 * 1024
_LOCK_TIMEOUT_SECONDS = 180
_SHA1_RE = re.compile(r"[0-9a-f]{40}\Z")
_INDEX_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*\Z")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


@dataclass(frozen=True)
class _AssetIndexReference:
    index_id: str
    sha1: str
    size: int


class EnvironmentAssetCache:
    """Share official Minecraft asset indexes and objects across Runs.

    ``root`` should be the host-owned environment cache root. The shared store
    is SHA-1 addressed under ``minecraft-assets-v1``; each Run keeps its own
    private, writable Gradle assets directory.

    ``authenticated_launcher_manifest`` must be bytes returned by a host path
    that has already authenticated the official launcher manifest (for
    example, a verified ``EnvironmentNeoFormCache`` snapshot). Never pass the
    copy found only in a Run-private Gradle cache as the trust anchor.
    """

    def __init__(self, root: Path):
        self.root = _absolute_path(Path(root))
        self.store = _absolute_path(self.root / "minecraft-assets-v1")

    def seed(self, assets_root: Path, *, minecraft_version: str,
             authenticated_launcher_manifest: bytes,
             version_manifest_path: Path,
             asset_index_path: Path | None = None) -> dict[str, Any]:
        """Copy verified shared assets into one Run-private Gradle assets root.

        Existing private files are rehashed, including same-size files. A bad
        regular file is removed or replaced so Gradle cannot mistake it for a
        completed download. Symlinks and special files fail closed.
        """
        reference = _authenticated_index_reference(
            minecraft_version, authenticated_launcher_manifest, version_manifest_path)
        assets_root, index_relative = _private_index_location(
            assets_root, asset_index_path, reference.index_id)
        result = _result(reference)

        # Pin the private assets root for the whole seed operation. It may not
        # be a symlink, and all descendants are accessed relative to this fd.
        with _open_directory(assets_root, create=True) as private_root_fd:
            with _asset_store_lock(self.store) as store_fd:
                local_index, local_bad = _read_verified_index_at(
                    private_root_fd, index_relative, reference)
                if local_bad:
                    _unlink_regular_at(private_root_fd, index_relative)

                shared_index_relative = _shared_index_location(reference)
                shared_index, shared_bad = _read_verified_index_at(
                    store_fd, shared_index_relative, reference, shared=True)
                if shared_bad:
                    result["shared_index_rejected"] = True

                # Either index is authenticated by the locked version
                # manifest. A private index enables reuse of blobs harvested
                # by an earlier partial Run even if its index publication was
                # interrupted.
                index_bytes = shared_index if shared_index is not None else local_index
                if index_bytes is None:
                    return result
                objects = _parse_asset_index(index_bytes)

                # Reject all link and special-file paths before modifying the
                # private tree. Content digests are checked during transfer.
                for object_hash, _ in objects:
                    private_object = _private_object_location(object_hash)
                    shared_object = _shared_object_location(object_hash)
                    _require_regular_if_present_at(private_root_fd, private_object)
                    if _require_regular_if_present_at(store_fd, shared_object, shared=True):
                        pass
                result["index_verified"] = True

                if shared_index is not None and local_index is None:
                    result["index_seeded"] = _write_verified_bytes_at(
                        private_root_fd, index_relative, shared_index,
                        reference.sha1, reference.size, mode=0o644)

                for object_hash, object_size in objects:
                    private_object = _private_object_location(object_hash)
                    shared_object = _shared_object_location(object_hash)
                    local_present = _relative_present(private_root_fd, private_object)
                    local_valid = local_present and _matches_at(
                        private_root_fd, private_object, object_hash, object_size,
                        max_bytes=_MAX_OBJECT_BYTES)
                    if local_present and not local_valid:
                        _unlink_regular_at(private_root_fd, private_object)

                    shared_present = _relative_present(store_fd, shared_object, shared=True)
                    if shared_present:
                        if not local_valid:
                            copied = _copy_verified_file_at(
                                store_fd, shared_object, private_root_fd, private_object,
                                object_hash, object_size, mode=0o644,
                                source_shared=True, destination_shared=False)
                            if copied:
                                result["objects_seeded"] += 1
                            else:
                                result["objects_rejected"] += 1
                        elif not _matches_at(
                                store_fd, shared_object, object_hash, object_size,
                                max_bytes=_MAX_OBJECT_BYTES, shared=True):
                            # Do not use a same-size corrupt shared object.
                            result["objects_rejected"] += 1

                    if _relative_present(private_root_fd, private_object) and _matches_at(
                            private_root_fd, private_object, object_hash, object_size,
                            max_bytes=_MAX_OBJECT_BYTES):
                        result["objects_available"] += 1
                    else:
                        result["objects_missing"] += 1
        return result

    def harvest(self, assets_root: Path, *, minecraft_version: str,
                authenticated_launcher_manifest: bytes,
                version_manifest_path: Path,
                asset_index_path: Path | None = None) -> dict[str, Any]:
        """Publish every verified asset present after a build, even on failure.

        Missing and corrupt object files are skipped, so a failed download can
        still contribute its completed objects. An absent or unauthenticated
        private index publishes nothing.
        """
        reference = _authenticated_index_reference(
            minecraft_version, authenticated_launcher_manifest, version_manifest_path)
        assets_root, index_relative = _private_index_location(
            assets_root, asset_index_path, reference.index_id)
        result = _result(reference)
        try:
            private_root_fd = _open_directory_path(assets_root)
        except FileNotFoundError:
            return result
        try:
            index_bytes, index_bad = _read_verified_index_at(
                private_root_fd, index_relative, reference)
            if index_bytes is None:
                result["index_rejected"] = index_bad
                return result

            objects = _parse_asset_index(index_bytes)
            # Validate every relevant node type before publishing anything.
            # Present regular files are SHA-1 and size checked during copy.
            for object_hash, _ in objects:
                _require_regular_if_present_at(
                    private_root_fd, _private_object_location(object_hash))
            result["index_verified"] = True
            with _asset_store_lock(self.store) as store_fd:
                shared_index = _shared_index_location(reference)
                result["index_harvested"] = _write_verified_bytes_at(
                    store_fd, shared_index, index_bytes, reference.sha1,
                    reference.size, mode=0o444, shared=True)

                for object_hash, object_size in objects:
                    private_object = _private_object_location(object_hash)
                    shared_object = _shared_object_location(object_hash)
                    shared_present = _relative_present(store_fd, shared_object, shared=True)
                    if shared_present:
                        if _matches_at(
                                store_fd, shared_object, object_hash, object_size,
                                max_bytes=_MAX_OBJECT_BYTES, shared=True):
                            result["objects_already_cached"] += 1
                            continue
                        # Replace only a regular corrupt cache file. Links and
                        # special nodes were rejected by the no-follow stat.
                        _unlink_regular_at(store_fd, shared_object, shared=True)

                    private_present = _relative_present(private_root_fd, private_object)
                    if not private_present:
                        result["objects_missing"] += 1
                        continue
                    copied = _copy_verified_file_at(
                        private_root_fd, private_object, store_fd, shared_object,
                        object_hash, object_size, mode=0o444,
                        source_shared=False, destination_shared=True)
                    if copied:
                        result["objects_harvested"] += 1
                    else:
                        result["objects_rejected"] += 1
            return result
        finally:
            os.close(private_root_fd)


def _result(reference: _AssetIndexReference) -> dict[str, Any]:
    return {
        "asset_index_id": reference.index_id,
        "asset_index_sha1": reference.sha1,
        "index_verified": False,
        "index_seeded": False,
        "index_harvested": False,
        "index_rejected": False,
        "shared_index_rejected": False,
        "objects_seeded": 0,
        "objects_harvested": 0,
        "objects_available": 0,
        "objects_missing": 0,
        "objects_rejected": 0,
        "objects_already_cached": 0,
    }


def _authenticated_index_reference(minecraft_version: str,
                                   authenticated_launcher_manifest: bytes,
                                   version_manifest_path: Path
                                   ) -> _AssetIndexReference:
    if (not isinstance(minecraft_version, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", minecraft_version)):
        raise ValueError("invalid Minecraft asset version")
    if (not isinstance(authenticated_launcher_manifest, bytes)
            or len(authenticated_launcher_manifest) > _MAX_LAUNCHER_BYTES):
        raise ValueError("authenticated launcher manifest bytes are invalid")
    launcher = _json_object(authenticated_launcher_manifest, "launcher manifest")
    versions = launcher.get("versions")
    if not isinstance(versions, list):
        raise ValueError("authenticated launcher manifest has no version list")
    matching = [item for item in versions
                if isinstance(item, dict) and item.get("id") == minecraft_version]
    version_sha1 = matching[0].get("sha1") if len(matching) == 1 else None
    if not isinstance(version_sha1, str) or not _SHA1_RE.fullmatch(version_sha1):
        raise ValueError("authenticated launcher manifest lacks one pinned Minecraft version")

    version_bytes = _read_regular_path(Path(version_manifest_path), _MAX_VERSION_BYTES)
    if sha1(version_bytes).hexdigest() != version_sha1:
        raise ValueError("private Minecraft version manifest failed launcher SHA-1")
    version = _json_object(version_bytes, "Minecraft version manifest")
    if version.get("id") != minecraft_version:
        raise ValueError("Minecraft version manifest identity mismatch")
    asset_index = version.get("assetIndex")
    if not isinstance(asset_index, dict):
        raise ValueError("Minecraft version manifest has no asset index")
    index_id = asset_index.get("id")
    index_sha1 = asset_index.get("sha1")
    index_size = asset_index.get("size")
    if (not isinstance(index_id, str) or not _INDEX_ID_RE.fullmatch(index_id)
            or not isinstance(index_sha1, str) or not _SHA1_RE.fullmatch(index_sha1)
            or isinstance(index_size, bool) or not isinstance(index_size, int)
            or index_size < 0 or index_size > _MAX_INDEX_BYTES):
        raise ValueError("Minecraft asset index reference is invalid")
    return _AssetIndexReference(index_id, index_sha1, index_size)


def _json_object(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} is not a JSON object")
    return value


def _parse_asset_index(data: bytes) -> tuple[tuple[str, int], ...]:
    index = _json_object(data, "Minecraft asset index")
    entries = index.get("objects")
    if not isinstance(entries, dict) or len(entries) > _MAX_OBJECTS:
        raise ValueError("Minecraft asset index has an invalid objects map")
    objects: dict[str, int] = {}
    total_size = 0
    for name, entry in entries.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            raise ValueError("Minecraft asset index contains an invalid object")
        object_hash = entry.get("hash")
        object_size = entry.get("size")
        if (not isinstance(object_hash, str) or not _SHA1_RE.fullmatch(object_hash)
                or isinstance(object_size, bool) or not isinstance(object_size, int)
                or object_size < 0 or object_size > _MAX_OBJECT_BYTES):
            raise ValueError("Minecraft asset index contains an invalid object reference")
        previous_size = objects.get(object_hash)
        if previous_size is not None and previous_size != object_size:
            raise ValueError("Minecraft asset index assigns conflicting sizes to one SHA-1")
        if previous_size is None:
            objects[object_hash] = object_size
            total_size += object_size
    if total_size > _MAX_TOTAL_OBJECT_BYTES:
        raise ValueError("Minecraft asset index exceeds the shared cache size bound")
    return tuple(sorted(objects.items()))


def _absolute_path(path: Path) -> Path:
    raw = Path(path)
    if ".." in raw.parts:
        raise ValueError("parent traversal is forbidden")
    return Path(os.path.abspath(raw))


@contextmanager
def _open_directory(path: Path, *, create: bool = False,
                    owned_root: Path | None = None) -> Iterator[int]:
    fd = _open_directory_path(path, create=create, owned_root=owned_root)
    try:
        yield fd
    finally:
        os.close(fd)


def _open_directory_path(path: Path, *, create: bool = False,
                         owned_root: Path | None = None) -> int:
    """Open each absolute path component with openat and O_NOFOLLOW."""
    absolute = _absolute_path(path)
    components = absolute.parts[1:]
    owned_components = _absolute_path(owned_root).parts[1:] if owned_root else ()
    if owned_components and components[:len(owned_components)] != owned_components:
        raise ValueError("owned directory root is not an ancestor of the path")
    descriptor = os.open(absolute.anchor, _DIRECTORY_FLAGS)
    opened: list[str] = []
    try:
        for component in components:
            try:
                child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o755, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise ValueError(f"symlink or non-directory path component: {component}") from exc
                raise
            opened.append(component)
            try:
                info = os.fstat(child)
                if owned_components and len(opened) >= len(owned_components):
                    _assert_owned_directory(info, component)
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _assert_owned_directory(info: os.stat_result, name: str) -> None:
    if not metadata_is_host_owned(info):
        raise ValueError(f"shared asset cache directory is not host-owned and private: {name}")


def _assert_owned_file(info: os.stat_result, name: str) -> None:
    if not metadata_is_host_owned(info):
        raise ValueError(f"shared asset cache file is not host-owned and private: {name}")


@contextmanager
def _asset_store_lock(store: Path) -> Iterator[int]:
    store = _absolute_path(store)
    with _open_directory(store, create=True, owned_root=store) as store_fd:
        lock_fd = os.open(
            ".minecraft-assets.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600, dir_fd=store_fd)
        try:
            info = os.fstat(lock_fd)
            if (not stat.S_ISREG(info.st_mode) or not metadata_is_host_owned(info)
                    or info.st_nlink != 1):
                raise ValueError("shared Minecraft asset cache lock is unsafe")
            deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Minecraft asset cache lock timed out")
                    time.sleep(min(0.05, remaining))
            yield store_fd
        finally:
            os.close(lock_fd)


def _validate_relative(relative: tuple[str, ...]) -> tuple[str, ...]:
    if (not relative or any(not isinstance(part, str) or not part
                            or part in (".", "..") or "/" in part or "\\" in part
                            for part in relative)):
        raise ValueError("unsafe relative asset cache path")
    return relative


def _open_relative_parent(root_fd: int, relative: tuple[str, ...], *,
                          create: bool = False, shared: bool = False) -> tuple[int, str]:
    relative = _validate_relative(relative)
    descriptor = os.dup(root_fd)
    try:
        for component in relative[:-1]:
            try:
                child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o755, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise ValueError(f"symlink or non-directory asset ancestor: {component}") from exc
                raise
            try:
                if shared:
                    _assert_owned_directory(os.fstat(child), component)
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor, relative[-1]
    except BaseException:
        os.close(descriptor)
        raise


def _stat_at(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _relative_stat(root_fd: int, relative: tuple[str, ...], *,
                   shared: bool = False) -> os.stat_result | None:
    try:
        parent_fd, name = _open_relative_parent(root_fd, relative, shared=shared)
    except FileNotFoundError:
        return None
    try:
        info = _stat_at(parent_fd, name)
        if info is not None and stat.S_ISLNK(info.st_mode):
            raise ValueError(f"symlink asset entry is forbidden: {name}")
        if info is not None and shared:
            _assert_owned_file(info, name)
        return info
    finally:
        os.close(parent_fd)


def _relative_present(root_fd: int, relative: tuple[str, ...], *,
                      shared: bool = False) -> bool:
    return _relative_stat(root_fd, relative, shared=shared) is not None


def _require_regular_if_present_at(root_fd: int, relative: tuple[str, ...], *,
                                   shared: bool = False) -> bool:
    info = _relative_stat(root_fd, relative, shared=shared)
    if info is None:
        return False
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"asset path is not a regular file: {relative[-1]}")
    return True


def _read_regular_path(path: Path, max_bytes: int) -> bytes:
    absolute = _absolute_path(path)
    with _open_directory(absolute.parent) as parent_fd:
        return _read_regular_bytes_at(parent_fd, (absolute.name,), max_bytes)


def _read_regular_bytes_at(root_fd: int, relative: tuple[str, ...],
                           max_bytes: int, *, shared: bool = False) -> bytes:
    parent_fd, name = _open_relative_parent(root_fd, relative, shared=shared)
    file_fd = -1
    try:
        path_info = _stat_at(parent_fd, name)
        if path_info is None:
            raise FileNotFoundError(name)
        if not stat.S_ISREG(path_info.st_mode) or path_info.st_size > max_bytes:
            raise ValueError(f"invalid regular asset file: {name}")
        if shared:
            _assert_owned_file(path_info, name)
        file_fd = _open_regular_fd(parent_fd, name)
        before = os.fstat(file_fd)
        if _stat_key(before) != _stat_key(path_info):
            raise ValueError(f"asset file changed before reading: {name}")
        chunks: list[bytes] = []
        total = 0
        while block := os.read(file_fd, min(_CHUNK_SIZE, max_bytes + 1 - total)):
            total += len(block)
            if total > max_bytes:
                raise ValueError(f"asset file exceeds size limit: {name}")
            chunks.append(block)
        after = os.fstat(file_fd)
        current = _stat_at(parent_fd, name)
        if current is None or _stat_key(before) != _stat_key(after) \
                or _stat_key(before) != _stat_key(current):
            raise ValueError(f"asset file changed while reading: {name}")
        return b"".join(chunks)
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        os.close(parent_fd)


def _open_regular_fd(parent_fd: int, name: str) -> int:
    try:
        descriptor = os.open(name, _FILE_READ_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ValueError(f"symlink or non-regular asset file: {name}") from exc
        raise
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ValueError(f"asset file is not regular: {name}")
    return descriptor


def _stat_key(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _read_verified_index_at(root_fd: int, relative: tuple[str, ...],
                            reference: _AssetIndexReference, *,
                            shared: bool = False) -> tuple[bytes | None, bool]:
    info = _relative_stat(root_fd, relative, shared=shared)
    if info is None:
        return None, False
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"asset index is not a regular file: {relative[-1]}")
    if info.st_size > _MAX_INDEX_BYTES:
        return None, True
    data = _read_regular_bytes_at(root_fd, relative, _MAX_INDEX_BYTES, shared=shared)
    valid = len(data) == reference.size and sha1(data).hexdigest() == reference.sha1
    return (data if valid else None), not valid


def _hash_regular_file_at(root_fd: int, relative: tuple[str, ...], *,
                          max_bytes: int, shared: bool = False) -> tuple[str, int]:
    parent_fd, name = _open_relative_parent(root_fd, relative, shared=shared)
    file_fd = -1
    try:
        path_info = _stat_at(parent_fd, name)
        if path_info is None:
            raise FileNotFoundError(name)
        if not stat.S_ISREG(path_info.st_mode) or path_info.st_size > max_bytes:
            raise ValueError(f"invalid regular asset file: {name}")
        if shared:
            _assert_owned_file(path_info, name)
        file_fd = _open_regular_fd(parent_fd, name)
        before = os.fstat(file_fd)
        if _stat_key(before) != _stat_key(path_info):
            raise ValueError(f"asset file changed before hashing: {name}")
        digest = sha1()
        total = 0
        while block := os.read(file_fd, min(_CHUNK_SIZE, max_bytes + 1 - total)):
            total += len(block)
            if total > max_bytes:
                raise ValueError(f"asset file exceeds size limit: {name}")
            digest.update(block)
        after = os.fstat(file_fd)
        current = _stat_at(parent_fd, name)
        if current is None or _stat_key(before) != _stat_key(after) \
                or _stat_key(before) != _stat_key(current):
            raise ValueError(f"asset file changed while hashing: {name}")
        return digest.hexdigest(), total
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        os.close(parent_fd)


def _matches_at(root_fd: int, relative: tuple[str, ...], expected_sha1: str,
                expected_size: int, *, max_bytes: int, shared: bool = False) -> bool:
    info = _relative_stat(root_fd, relative, shared=shared)
    if info is None:
        return False
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"asset is not a regular file: {relative[-1]}")
    if info.st_size > max_bytes:
        return False
    digest, size = _hash_regular_file_at(
        root_fd, relative, max_bytes=max_bytes, shared=shared)
    return size == expected_size and digest == expected_sha1


def _unlink_regular_at(root_fd: int, relative: tuple[str, ...], *,
                       shared: bool = False) -> None:
    parent_fd, name = _open_relative_parent(root_fd, relative, shared=shared)
    try:
        info = _stat_at(parent_fd, name)
        if info is None:
            return
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"refusing to remove non-regular asset entry: {name}")
        if shared:
            _assert_owned_file(info, name)
        os.unlink(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _copy_verified_file_at(source_root_fd: int, source_relative: tuple[str, ...],
                           destination_root_fd: int,
                           destination_relative: tuple[str, ...],
                           expected_sha1: str, expected_size: int, *, mode: int,
                           source_shared: bool, destination_shared: bool) -> bool:
    source_parent_fd, source_name = _open_relative_parent(
        source_root_fd, source_relative, shared=source_shared)
    destination_parent_fd = -1
    source_fd = -1
    temporary_fd = -1
    temporary_name: str | None = None
    try:
        source_info = _stat_at(source_parent_fd, source_name)
        if source_info is None:
            return False
        if not stat.S_ISREG(source_info.st_mode):
            raise ValueError(f"asset source is not a regular file: {source_name}")
        if source_shared:
            _assert_owned_file(source_info, source_name)
        if source_info.st_size > _MAX_OBJECT_BYTES:
            return False

        destination_parent_fd, destination_name = _open_relative_parent(
            destination_root_fd, destination_relative, create=True,
            shared=destination_shared)
        destination_info = _stat_at(destination_parent_fd, destination_name)
        if destination_info is not None:
            if not stat.S_ISREG(destination_info.st_mode):
                raise ValueError(f"asset destination is not a regular file: {destination_name}")
            if destination_shared:
                _assert_owned_file(destination_info, destination_name)

        source_fd = _open_regular_fd(source_parent_fd, source_name)
        before = os.fstat(source_fd)
        if _stat_key(before) != _stat_key(source_info):
            raise ValueError(f"asset source changed before copying: {source_name}")
        temporary_name, temporary_fd = _create_temporary(destination_parent_fd)
        digest = sha1()
        total = 0
        with os.fdopen(source_fd, "rb") as input_stream, \
                os.fdopen(temporary_fd, "wb") as output_stream:
            source_fd = temporary_fd = -1
            while block := input_stream.read(_CHUNK_SIZE):
                total += len(block)
                if total > _MAX_OBJECT_BYTES:
                    return False
                digest.update(block)
                output_stream.write(block)
            after = os.fstat(input_stream.fileno())
            current = _stat_at(source_parent_fd, source_name)
            if current is None or _stat_key(before) != _stat_key(after) \
                    or _stat_key(before) != _stat_key(current):
                raise ValueError(f"asset source changed while copying: {source_name}")
            if total != expected_size or digest.hexdigest() != expected_sha1:
                return False
            output_stream.flush()
            os.fsync(output_stream.fileno())
            os.fchmod(output_stream.fileno(), mode)

        os.replace(temporary_name, destination_name,
                   src_dir_fd=destination_parent_fd,
                   dst_dir_fd=destination_parent_fd)
        temporary_name = None
        os.fsync(destination_parent_fd)
        result_info = _stat_at(destination_parent_fd, destination_name)
        if result_info is None or not stat.S_ISREG(result_info.st_mode):
            raise ValueError("copied asset did not remain a regular file")
        if destination_shared:
            _assert_owned_file(result_info, destination_name)
        return True
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        if temporary_fd >= 0:
            os.close(temporary_fd)
        if temporary_name is not None and destination_parent_fd >= 0:
            try:
                os.unlink(temporary_name, dir_fd=destination_parent_fd)
            except FileNotFoundError:
                pass
        if destination_parent_fd >= 0:
            os.close(destination_parent_fd)
        os.close(source_parent_fd)


def _write_verified_bytes_at(root_fd: int, relative: tuple[str, ...], data: bytes,
                             expected_sha1: str, expected_size: int, *,
                             mode: int, shared: bool = False) -> bool:
    if len(data) != expected_size or sha1(data).hexdigest() != expected_sha1:
        raise ValueError("refusing to write unauthenticated Minecraft asset metadata")
    parent_fd, name = _open_relative_parent(
        root_fd, relative, create=True, shared=shared)
    temporary_fd = -1
    temporary_name: str | None = None
    try:
        info = _stat_at(parent_fd, name)
        if info is not None:
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"asset metadata destination is not regular: {name}")
            if shared:
                _assert_owned_file(info, name)
            if info.st_size <= _MAX_INDEX_BYTES:
                existing = _read_regular_bytes_at(root_fd, relative, _MAX_INDEX_BYTES,
                                                  shared=shared)
                if len(existing) == expected_size and sha1(existing).hexdigest() == expected_sha1:
                    return False

        temporary_name, temporary_fd = _create_temporary(parent_fd, prefix=".pending-minecraft-index-")
        with os.fdopen(temporary_fd, "wb") as output:
            temporary_fd = -1
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), mode)
        os.replace(temporary_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        temporary_name = None
        os.fsync(parent_fd)
        result_info = _stat_at(parent_fd, name)
        if result_info is None or not stat.S_ISREG(result_info.st_mode):
            raise ValueError("copied asset index did not remain a regular file")
        if shared:
            _assert_owned_file(result_info, name)
        return True
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)


def _create_temporary(parent_fd: int, *,
                      prefix: str = ".pending-minecraft-asset-") -> tuple[str, int]:
    for _ in range(10):
        name = prefix + secrets.token_hex(12)
        try:
            descriptor = os.open(
                name,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600, dir_fd=parent_fd)
            return name, descriptor
        except FileExistsError:
            continue
    raise FileExistsError("could not allocate a temporary asset file")


def _private_index_location(assets_root: Path, asset_index_path: Path | None,
                            index_id: str) -> tuple[Path, tuple[str, ...]]:
    root = _absolute_path(Path(assets_root))
    relative = ("indexes", f"{index_id}.json")
    if asset_index_path is not None:
        supplied = _absolute_path(Path(asset_index_path))
        expected = _absolute_path(root / relative[0] / relative[1])
        if supplied != expected:
            raise ValueError("private asset index path does not match the locked index ID")
    return root, relative


def _shared_index_location(reference: _AssetIndexReference) -> tuple[str, ...]:
    return "indexes", f"{reference.sha1}.json"


def _private_object_location(object_hash: str) -> tuple[str, ...]:
    return "objects", object_hash[:2], object_hash


def _shared_object_location(object_hash: str) -> tuple[str, ...]:
    return "objects", object_hash[:2], object_hash
