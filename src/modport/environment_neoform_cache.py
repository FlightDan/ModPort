"""Verified, Run-private NeoFormRuntime seeds from a successful official MDK build."""
from __future__ import annotations

from hashlib import sha1, sha256
import json
from .platform_files import file_os as os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any
from urllib.parse import urlparse
from urllib.request import urlopen

from .environment_build_cache import (
    MANIFEST, _assert_cache_chain, _assert_cache_ownership, _commit,
    _copy_tree_files, _ensure_cache_directory, _fsync_directory, _json_bytes,
    _make_writable_for_cleanup, _path_key, _read_regular, _repository_url,
    _safe_path, _set_read_only_tree, _sha256_file, _store_lock,
    _tree_inventory, _validate_files_manifest, _write_manifest,
)


_MAX_FILES = 512
_MAX_BYTES = 2 * 1024 * 1024 * 1024
_MAX_JSON_BYTES = 2 * 1024 * 1024
_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*\Z")
OFFICIAL_LAUNCHER_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
LAUNCHER_MANIFEST_URI = (
    "file:///gradle-cache/caches/neoformruntime/artifacts/"
    "minecraft_launcher_manifest.json"
)
LAUNCHER_MANIFEST_PROPERTY = (
    "-PneoForge.neoFormRuntime.launcherManifestUrl=" + LAUNCHER_MANIFEST_URI
)


def _identity(repository: str, commit: str, minecraft: str, neoforge: str,
              gradle: str) -> dict[str, str]:
    from .environment_build_cache import gradle_cache_compatibility

    result = {"repository": _repository_url(repository), "commit": _commit(commit),
              "minecraft_version": minecraft, "neoforge_version": neoforge,
              "gradle_version": gradle}
    if not all(_VERSION_RE.fullmatch(result[key] or "") for key in
               ("minecraft_version", "neoforge_version", "gradle_version")):
        raise ValueError("invalid NeoForm cache version scope")
    gradle_cache_compatibility(gradle)
    return result


def _sha1_file(path: Path) -> str:
    import stat

    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_BYTES:
            raise ValueError("invalid NeoForm artifact")
        digest = sha1()
        size = 0
        while block := os.read(descriptor, min(1024 * 1024, _MAX_BYTES + 1 - size)):
            size += len(block)
            if size > _MAX_BYTES:
                raise ValueError("NeoForm artifact exceeds limit")
            digest.update(block)
        after = os.fstat(descriptor)
        current = path.lstat()
        key = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        if size != before.st_size or key(before) != key(after) or key(before) != key(current):
            raise ValueError("NeoForm artifact changed during verification")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _json_file(path: Path) -> dict[str, Any]:
    value = json.loads(_read_regular(path, max_bytes=_MAX_JSON_BYTES))
    if not isinstance(value, dict):
        raise ValueError("NeoForm artifact metadata is not an object")
    return value


def _fetch_official_launcher_manifest() -> bytes:
    """Anchor the cache to Mojang HTTPS outside the build sandbox, once per cold seed."""
    with urlopen(OFFICIAL_LAUNCHER_URL, timeout=30) as response:
        final_url = urlparse(response.geturl())
        if (response.status != 200 or final_url.scheme != "https"
                or final_url.netloc != "piston-meta.mojang.com"):
            raise ValueError("official launcher manifest redirected outside Mojang HTTPS")
        data = response.read(_MAX_JSON_BYTES + 1)
    if len(data) > _MAX_JSON_BYTES:
        raise ValueError("official launcher manifest exceeds size limit")
    try:
        parsed = json.loads(data)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("official launcher manifest is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("official launcher manifest is not an object")
    return data


def _verify_mojang_chain(artifacts: Path, minecraft: str) -> None:
    launcher = _json_file(artifacts / "minecraft_launcher_manifest.json")
    versions = launcher.get("versions")
    if not isinstance(versions, list):
        raise ValueError("launcher manifest has no version list")
    matching = [entry for entry in versions
                if isinstance(entry, dict) and entry.get("id") == minecraft]
    version_sha1 = matching[0].get("sha1") if len(matching) == 1 else None
    if (not isinstance(version_sha1, str)
            or not re.fullmatch(r"[0-9a-f]{40}", version_sha1)):
        raise ValueError("launcher manifest lacks one pinned Minecraft version")
    version_path = artifacts / f"minecraft_{minecraft}_version_manifest.json"
    if _sha1_file(version_path) != version_sha1:
        raise ValueError("Minecraft version manifest failed launcher SHA-1")
    version = _json_file(version_path)
    downloads = version.get("downloads")
    if not isinstance(downloads, dict):
        raise ValueError("Minecraft version manifest has no downloads object")
    for side in ("client", "server"):
        download = downloads.get(side, {})
        expected = download.get("sha1", "") if isinstance(download, dict) else ""
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{40}", expected):
            raise ValueError(f"Minecraft {side} lacks a pinned SHA-1")
        if _sha1_file(artifacts / f"minecraft_{minecraft}_{side}.jar") != expected:
            raise ValueError(f"Minecraft {side} failed version manifest SHA-1")


def _inventory(runtime: Path, minecraft: str) -> dict[str, dict[str, Any]]:
    artifacts = runtime / "artifacts"
    intermediates = runtime / "intermediate_results"
    artifact_files, _ = _tree_inventory(artifacts)
    expected = {"minecraft_launcher_manifest.json",
                f"minecraft_{minecraft}_version_manifest.json",
                f"minecraft_{minecraft}_client.jar",
                f"minecraft_{minecraft}_server.jar"}
    if set(artifact_files) != expected:
        raise ValueError("NeoForm artifacts do not match the pinned Minecraft inputs")
    _verify_mojang_chain(artifacts, minecraft)
    intermediate_files, _ = _tree_inventory(intermediates)
    if not intermediate_files:
        raise ValueError("NeoForm intermediate cache is empty")
    files = {f"artifacts/{key}": value for key, value in artifact_files.items()}
    files.update({f"intermediate_results/{key}": value
                  for key, value in intermediate_files.items()})
    if len(files) > _MAX_FILES or sum(item["size"] for item in files.values()) > _MAX_BYTES:
        raise ValueError("NeoForm cache exceeds bounded publication size")
    return files


def _validate_snapshot(path: Path, store: Path, identity: dict[str, str],
                       scope_key: str, key: str) -> dict[str, Any]:
    path = _safe_path(path)
    _assert_cache_chain(store, path)
    if not path.is_dir() or sorted(entry.name for entry in path.iterdir()) != [MANIFEST, "payload"]:
        raise ValueError("NeoForm snapshot has unsafe root entries")
    manifest = json.loads(_read_regular(path / MANIFEST))
    if (not isinstance(manifest, dict) or set(manifest) !=
            {"schema_version", "kind", "identity", "scope_sha256", "content_sha256",
             "launcher_sha256", "launcher_source", "files"}
            or manifest["schema_version"] != 2 or manifest["kind"] != "neoform-runtime-v2"
            or manifest["identity"] != identity or manifest["scope_sha256"] != scope_key
            or manifest["content_sha256"] != key
            or manifest["launcher_source"] != OFFICIAL_LAUNCHER_URL):
        raise ValueError("NeoForm snapshot identity mismatch")
    _validate_files_manifest(manifest["files"])
    if manifest["launcher_sha256"] != manifest["files"].get(
            "artifacts/minecraft_launcher_manifest.json", {}).get("sha256"):
        raise ValueError("NeoForm official launcher digest mismatch")
    expected_key = sha256(_json_bytes({"identity": identity, "files": manifest["files"]})).hexdigest()
    payload = _safe_path(path / "payload")
    if sorted(entry.name for entry in payload.iterdir()) != ["artifacts", "intermediate_results"]:
        raise ValueError("NeoForm snapshot payload has unmanifested roots")
    if expected_key != key or _inventory(payload, identity["minecraft_version"]) != manifest["files"]:
        raise ValueError("NeoForm snapshot contents differ from manifest")
    for current, dirs, names in os.walk(path, followlinks=False):
        _assert_cache_ownership(Path(current))
        for name in dirs + names:
            _assert_cache_ownership(Path(current) / name)
    return manifest


class EnvironmentNeoFormCache:
    def __init__(self, root: Path):
        self.root = _safe_path(Path(root))

    def verified_launcher_manifest(self, *, mdk_repository: str,
                                   mdk_commit: str, minecraft_version: str,
                                   neoforge_version: str,
                                   gradle_version: str) -> bytes | None:
        """Read an official launcher manifest from an authenticated snapshot."""
        identity, scope_key, parent = self._scope(
            mdk_repository=mdk_repository, mdk_commit=mdk_commit,
            minecraft_version=minecraft_version,
            neoforge_version=neoforge_version,
            gradle_version=gradle_version)
        if not parent.exists() and not parent.is_symlink():
            return None
        _assert_cache_chain(self.root, parent)
        snapshots = sorted((entry for entry in parent.iterdir()
                            if not entry.name.startswith(".pending-")),
                           key=lambda entry: entry.name)
        if not snapshots:
            return None
        snapshot = snapshots[0]
        key = snapshot.name
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            raise ValueError("unsafe NeoForm snapshot key")
        manifest = _validate_snapshot(snapshot, self.root, identity, scope_key, key)
        data = _read_regular(
            snapshot / "payload" / "artifacts" /
            "minecraft_launcher_manifest.json", max_bytes=_MAX_JSON_BYTES)
        if sha256(data).hexdigest() != manifest["launcher_sha256"]:
            raise ValueError("official launcher manifest changed after validation")
        return data

    def _scope(self, *, mdk_repository: str, mdk_commit: str,
               minecraft_version: str, neoforge_version: str,
               gradle_version: str) -> tuple[dict[str, str], str, Path]:
        identity = _identity(mdk_repository, mdk_commit, minecraft_version,
                             neoforge_version, gradle_version)
        scope_key = sha256(_json_bytes(identity)).hexdigest()
        return identity, scope_key, self.root / "neoform-runtime-v2" / scope_key

    def publish_bootstrap(self, runtime: Path, *, mdk_repository: str,
                          mdk_commit: str, minecraft_version: str,
                          neoforge_version: str, gradle_version: str,
                          mdk_bootstrap_succeeded: bool) -> dict[str, Any]:
        if mdk_bootstrap_succeeded is not True:
            raise ValueError("only a successful official MDK build may seed NeoForm")
        identity, scope_key, parent = self._scope(
            mdk_repository=mdk_repository, mdk_commit=mdk_commit,
            minecraft_version=minecraft_version, neoforge_version=neoforge_version,
            gradle_version=gradle_version)
        runtime = _safe_path(Path(runtime))
        if runtime.name != "neoformruntime" or runtime.parent.name != "caches":
            raise ValueError("NeoForm source is not a Gradle runtime cache")
        source_files = _inventory(runtime, minecraft_version)
        official_launcher = _fetch_official_launcher_manifest()
        official_sha256 = sha256(official_launcher).hexdigest()
        with _store_lock(self.root, f".neoform-{scope_key}.lock"):
            _ensure_cache_directory(parent)
            temporary = Path(tempfile.mkdtemp(prefix=".pending-neoform-", dir=parent))
            try:
                payload = temporary / "payload"
                payload.mkdir()
                _copy_tree_files(runtime, payload, source_files)
                launcher_path = payload / "artifacts/minecraft_launcher_manifest.json"
                launcher_path.write_bytes(official_launcher)
                files = _inventory(payload, minecraft_version)
                key = sha256(_json_bytes({"identity": identity, "files": files})).hexdigest()
                manifest = {"schema_version": 2, "kind": "neoform-runtime-v2",
                            "identity": identity, "scope_sha256": scope_key,
                            "content_sha256": key, "launcher_sha256": official_sha256,
                            "launcher_source": OFFICIAL_LAUNCHER_URL, "files": files}
                destination = parent / key
                if destination.exists() or destination.is_symlink():
                    existing = _validate_snapshot(destination, self.root, identity, scope_key, key)
                    if existing != manifest:
                        raise ValueError("immutable NeoForm snapshot conflict")
                    return {**manifest, "snapshot_key": key}
                _write_manifest(temporary / MANIFEST, manifest)
                _set_read_only_tree(temporary)
                _validate_snapshot(temporary, self.root, identity, scope_key, key)
                os.rename(temporary, destination)
                _fsync_directory(parent)
            finally:
                if temporary.exists():
                    _make_writable_for_cleanup(temporary)
                    shutil.rmtree(temporary)
        return {**manifest, "snapshot_key": key}

    def try_materialize(self, gradle_home: Path, *, mdk_repository: str,
                        mdk_commit: str, minecraft_version: str,
                        neoforge_version: str, gradle_version: str) -> dict[str, Any] | None:
        identity, scope_key, parent = self._scope(
            mdk_repository=mdk_repository, mdk_commit=mdk_commit,
            minecraft_version=minecraft_version, neoforge_version=neoforge_version,
            gradle_version=gradle_version)
        if not parent.exists() and not parent.is_symlink():
            return None
        _assert_cache_chain(self.root, parent)
        snapshots = sorted((entry for entry in parent.iterdir()
                            if not entry.name.startswith(".pending-")), key=lambda entry: entry.name)
        if not snapshots:
            return None
        # Repeated successful cold builds may publish distinct cache contents
        # under the same fixed toolchain scope. Any verified seed is reusable.
        seed = snapshots[0]
        key = seed.name
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            raise ValueError("unsafe NeoForm snapshot key")
        manifest = _validate_snapshot(seed, self.root, identity, scope_key, key)
        gradle_home = _safe_path(Path(gradle_home))
        target = _safe_path(gradle_home / "caches" / "neoformruntime")
        if target.exists() or target.is_symlink():
            raise ValueError("NeoForm destination already exists")
        _ensure_cache_directory(target.parent)
        temporary = Path(tempfile.mkdtemp(prefix=".neoform-materialize-", dir=target.parent))
        try:
            _copy_tree_files(seed / "payload", temporary, manifest["files"])
            if _inventory(temporary, minecraft_version) != manifest["files"]:
                raise ValueError("materialized NeoForm input failed verification")
            os.rename(temporary, target)
            _fsync_directory(target.parent)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return {"snapshot_key": key, "content_sha256": key,
                "materialized_path": str(target)}
