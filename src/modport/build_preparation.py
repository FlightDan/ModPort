"""Conservative, deterministic preparation of a locked MDK build skeleton.

The supported subset is intentionally small: a locked MDK file may be added
when the candidate path is missing, or authenticated when it already matches
the locked bytes exactly.  Existing differing build configuration is never
parsed or overwritten; it is reported for manual coordination.

No function in this module executes Gradle or other project code.
"""

from __future__ import annotations

from .workspace import git_probe

from dataclasses import dataclass, field
import difflib
from hashlib import sha256
from .platform_files import file_os as os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile
from typing import Any, Mapping

from .manifest import canonical_json, manifest_sha256
from .models import LockedManifest


MAX_MDK_FILE_BYTES = 2 * 1024 * 1024
MAX_MDK_TOTAL_BYTES = 8 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")
_GIT_COMMIT = re.compile(r"[0-9a-f]{40}")
_SUPPORTED_MDK_PATHS = frozenset({
    "build.gradle",
    "settings.gradle",
    "gradle.properties",
    "gradle/wrapper/gradle-wrapper.properties",
    "gradlew",
    "gradlew.bat",
    "gradle/wrapper/gradle-wrapper.jar",
    "src/main/resources/META-INF/neoforge.mods.toml",
    "src/main/templates/META-INF/neoforge.mods.toml",
})
_REQUIRED_MDK_PATHS = frozenset({
    "build.gradle",
    "settings.gradle",
    "gradle.properties",
    "gradle/wrapper/gradle-wrapper.properties",
    "gradle/wrapper/gradle-wrapper.jar",
    "gradlew",
    "gradlew.bat",
})
_AMBIGUOUS_ROOT_FILES = frozenset({
    "build.gradle.kts",
    "settings.gradle.kts",
    "gradle/libs.versions.toml",
})


class BuildPreparationError(ValueError):
    """Base error for deterministic build preparation."""


class BuildPreparationIntegrityError(BuildPreparationError):
    """Locked MDK content or its declared digest is invalid."""


class UnsupportedBuildLayout(BuildPreparationError):
    """The candidate requires a semantic Gradle merge outside this subset."""

    def __init__(self, diagnostics: tuple[str, ...] | list[str]) -> None:
        self.diagnostics = tuple(diagnostics)
        super().__init__("; ".join(self.diagnostics))


class StaleBuildPreparationPlan(BuildPreparationError):
    """The candidate, manifest, or planned files changed after planning."""


@dataclass(frozen=True, slots=True)
class BuildFileDigest:
    path: str
    sha256: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class TargetConfigMarker:
    candidate_identity: str
    manifest_sha256: str
    mdk_configuration_sha256: str
    files: tuple[BuildFileDigest, ...]
    required_gradle_properties: tuple[tuple[str, str], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "status": "authenticated",
            "candidate_identity": {
                "kind": "git_commit",
                "value": self.candidate_identity,
            },
            "manifest_sha256": self.manifest_sha256,
            "mdk_configuration_sha256": self.mdk_configuration_sha256,
            "files": [item.to_dict() for item in self.files],
            "required_gradle_properties": dict(self.required_gradle_properties),
        }


@dataclass(frozen=True, slots=True)
class BuildPreparationChange:
    path: str
    before_sha256: None
    after_sha256: str
    patch: str
    _after: bytes = field(repr=False, compare=False)
    _mode: int = field(default=0o644, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "add",
            "path": self.path,
            "before_sha256": None,
            "after_sha256": self.after_sha256,
            "patch": self.patch,
            "patch_format": "preview_only; host publishes a Git binary patch when applying",
        }


@dataclass(frozen=True, slots=True)
class BuildPreparationPlan:
    candidate_identity: str
    manifest_sha256: str
    mdk_configuration_sha256: str
    required_gradle_properties: tuple[tuple[str, str], ...]
    supported: bool
    changes: tuple[BuildPreparationChange, ...]
    diagnostics: tuple[str, ...]
    draft: tuple[Mapping[str, Any], ...]

    @property
    def patch(self) -> str:
        return "".join(change.patch for change in self.changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "candidate_identity": {
                "kind": "git_commit",
                "value": self.candidate_identity,
            },
            "manifest_sha256": self.manifest_sha256,
            "mdk_configuration_sha256": self.mdk_configuration_sha256,
            "required_gradle_properties": dict(self.required_gradle_properties),
            "supported": self.supported,
            "changes": [change.to_dict() for change in self.changes],
            "diagnostics": list(self.diagnostics),
            "draft": [dict(item) for item in self.draft],
            "patch": self.patch,
            "acceptance_evidence": False,
            "remaining_scope": [
                "existing customized Gradle configuration requires a semantic manual merge",
                "whole-file replacement is disabled until a locked source-template contract exists",
                "the draft does not authenticate compilation or runtime behavior",
                "target compilation must pass only the published required Gradle properties",
            ],
        }


@dataclass(frozen=True, slots=True)
class BuildPreparationApplyResult:
    candidate_identity: str
    applied_changes: int
    changed_paths: tuple[str, ...]
    state: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_identity": self.candidate_identity,
            "applied_changes": self.applied_changes,
            "changed_paths": list(self.changed_paths),
            "state": self.state,
        }


def _manifest(value: LockedManifest | Mapping[str, Any]) -> LockedManifest:
    manifest = value if isinstance(value, LockedManifest) else LockedManifest.from_mapping(value)
    manifest.validate()
    return manifest


def _safe_relative(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or value.startswith("/"):
        raise BuildPreparationIntegrityError("MDK path must be a portable relative path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise BuildPreparationIntegrityError(f"unsafe MDK path: {value!r}")
    return PurePosixPath(value).as_posix()


def _real_directory(value: str | os.PathLike[str], name: str) -> Path:
    path = Path(os.path.abspath(os.fspath(value)))
    if not path.is_dir() or path.is_symlink() or path.resolve() != path:
        raise BuildPreparationError(f"{name} must be an existing real directory")
    return path


def _checked_path(root: Path, relative: str, *, allow_missing: bool = False) -> Path:
    relative = _safe_relative(relative)
    path = root.joinpath(*PurePosixPath(relative).parts)
    current = root
    for index, part in enumerate(PurePosixPath(relative).parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if allow_missing:
                break
            raise BuildPreparationError(f"required path is missing: {relative}") from None
        if stat.S_ISLNK(info.st_mode):
            raise BuildPreparationError(f"symbolic links are not supported: {relative}")
        if index < len(PurePosixPath(relative).parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise BuildPreparationError(f"path ancestor is not a directory: {relative}")
    if not path.resolve(strict=False).is_relative_to(root):
        raise BuildPreparationError(f"path escapes its root: {relative}")
    return path


def _git(worktree: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    result = git_probe(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "core.quotePath=true",
         "-C", str(worktree), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=30,
        env={**os.environ, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
             'GIT_NO_REPLACE_OBJECTS': '1', 'GIT_TERMINAL_PROMPT': '0'},
    )
    if result.returncode:
        raise BuildPreparationError(f"cannot inspect candidate with git {arguments[0]}")
    return result


def _candidate_identity(worktree: Path, *, require_clean: bool) -> str:
    result = _git(worktree, "rev-parse", "--show-toplevel", "HEAD")
    lines = result.stdout.splitlines()
    if len(lines) != 2 or Path(lines[0]).resolve() != worktree:
        raise BuildPreparationError("worktree must be the Git candidate root")
    identity = lines[1].strip().lower()
    if not _GIT_COMMIT.fullmatch(identity):
        raise BuildPreparationError("candidate identity is not an exact Git commit")
    if require_clean:
        status = _git(worktree, "status", "--porcelain=v1", "--untracked-files=all")
        if status.stdout:
            raise BuildPreparationError("candidate must be clean before build preparation")
    return identity


def _tracked_paths(worktree: Path) -> tuple[str, ...]:
    result = _git(worktree, "ls-files", "-z")
    return tuple(path for path in result.stdout.split("\0") if path)


def _dirty_paths(worktree: Path) -> frozenset[str]:
    paths: set[str] = set()
    for arguments in (
        ("diff", "--name-only", "-z"),
        ("diff", "--cached", "--name-only", "-z"),
        ("ls-files", "--others", "--exclude-standard", "-z"),
    ):
        paths.update(path for path in _git(worktree, *arguments).stdout.split("\0") if path)
    return frozenset(paths)


def _configuration_sha(files: tuple[BuildFileDigest, ...]) -> str:
    return sha256(canonical_json([item.to_dict() for item in files]).encode("utf-8")).hexdigest()


def _required_gradle_properties(manifest: LockedManifest) -> tuple[tuple[str, str], ...]:
    version = manifest.neoforge_version
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", version):
        raise BuildPreparationIntegrityError("neoforge_version is unsafe for a Gradle property")
    return (("neo_version", version),)


def _locked_mdk_files(
    manifest: LockedManifest,
    mdk_root: Path,
) -> tuple[dict[str, tuple[bytes, int]], tuple[BuildFileDigest, ...], tuple[str, ...]]:
    selected: dict[str, tuple[bytes, int]] = {}
    diagnostics: list[str] = []
    total = 0
    for key, expected in sorted(manifest.checksums.items()):
        if not key.startswith("mdk:"):
            continue
        relative = _safe_relative(key.removeprefix("mdk:"))
        supported_path = relative in _SUPPORTED_MDK_PATHS
        if not supported_path:
            diagnostics.append(f"unsupported locked MDK path: {relative}")
        checksum = expected.lower()
        if not _SHA256.fullmatch(checksum):
            raise BuildPreparationIntegrityError(f"invalid locked MDK SHA-256: {relative}")
        path = _checked_path(mdk_root, relative)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MDK_FILE_BYTES:
            raise BuildPreparationIntegrityError(f"locked MDK file is not a bounded regular file: {relative}")
        data = path.read_bytes()
        actual = sha256(data).hexdigest()
        if actual != checksum:
            raise BuildPreparationIntegrityError(f"locked MDK SHA-256 mismatch: {relative}")
        total += len(data)
        if total > MAX_MDK_TOTAL_BYTES:
            raise BuildPreparationIntegrityError("locked MDK files exceed total size limit")
        if not supported_path:
            continue
        if relative != "gradle/wrapper/gradle-wrapper.jar":
            if b"\0" in data:
                raise BuildPreparationIntegrityError(
                    f"binary MDK file is outside the supported subset: {relative}"
                )
            try:
                data.decode("utf-8")
            except UnicodeDecodeError as error:
                raise BuildPreparationIntegrityError(
                    f"non-UTF-8 MDK file is outside the supported subset: {relative}"
                ) from error
        mode = 0o755 if relative == "gradlew" else 0o644
        selected[relative] = (data, mode)
    for relative in sorted(_REQUIRED_MDK_PATHS - set(selected)):
        diagnostics.append(f"locked MDK checksum is unavailable; publication is report-only: {relative}")
    files = tuple(BuildFileDigest(path, sha256(data).hexdigest())
                  for path, (data, _) in sorted(selected.items()))
    return selected, files, tuple(diagnostics)


def locked_probe_files(manifest, mdk_root):
    """Return authenticated MDK bytes for an isolated diagnostic compiler."""
    locked = _manifest(manifest)
    files, _, diagnostics = _locked_mdk_files(locked, _real_directory(mdk_root, 'MDK root'))
    if diagnostics:
        raise UnsupportedBuildLayout(diagnostics)
    return files, _required_gradle_properties(locked)


def _ambiguous_paths(tracked: tuple[str, ...], locked: set[str]) -> tuple[str, ...]:
    ambiguous: list[str] = []
    for relative in tracked:
        path = PurePosixPath(relative)
        if relative in locked:
            continue
        if relative in _AMBIGUOUS_ROOT_FILES:
            ambiguous.append(relative)
        elif len(path.parts) > 1 and path.name in {
            "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"
        }:
            ambiguous.append(relative)
        elif path.parts and path.parts[0] == "buildSrc":
            ambiguous.append(relative)
        elif (path.parts and path.parts[0] == "gradle"
              and path.suffix in {".gradle", ".kts"}):
            ambiguous.append(relative)
    return tuple(sorted(set(ambiguous)))


def _addition_patch(relative: str, data: bytes) -> str:
    if b'\0' in data:
        return f"Binary file addition: {relative} sha256={sha256(data).hexdigest()}\n"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return f"Binary file addition: {relative} sha256={sha256(data).hexdigest()}\n"
    return "".join(difflib.unified_diff(
        (), text.splitlines(keepends=True),
        fromfile="/dev/null", tofile=f"b/{relative}",
    ))


def draft_build_preparation(
    root: str | os.PathLike[str],
    worktree: str | os.PathLike[str],
    manifest: LockedManifest | Mapping[str, Any],
) -> BuildPreparationPlan:
    """Create a path-independent MDK merge draft without modifying files."""
    run_root = _real_directory(root, "run root")
    candidate = _real_directory(worktree, "worktree")
    if not candidate.is_relative_to(run_root):
        raise BuildPreparationError("worktree must be contained by the run root")
    locked = _manifest(manifest)
    manifest_identity = manifest_sha256(locked)
    candidate_identity = _candidate_identity(candidate, require_clean=True)
    mdk_root = _real_directory(run_root / "toolchains" / "mdk", "MDK root")
    mdk, files, source_diagnostics = _locked_mdk_files(locked, mdk_root)
    diagnostics = list(source_diagnostics)
    draft: list[Mapping[str, Any]] = []
    changes: list[BuildPreparationChange] = []

    for relative in _ambiguous_paths(_tracked_paths(candidate), set(mdk)):
        diagnostics.append(f"custom or multi-project Gradle layout is unsupported: {relative}")
        draft.append({"path": relative, "action": "manual_merge_required",
                      "reason": "ambiguous_gradle_layout"})

    for relative, (target, mode) in sorted(mdk.items()):
        path = _checked_path(candidate, relative, allow_missing=True)
        if not path.exists():
            digest = sha256(target).hexdigest()
            changes.append(BuildPreparationChange(
                path=relative,
                before_sha256=None,
                after_sha256=digest,
                patch=_addition_patch(relative, target),
                _after=target,
                _mode=mode,
            ))
            draft.append({"path": relative, "action": "add", "sha256": digest})
            continue
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise BuildPreparationError(f"candidate build path is not a regular file: {relative}")
        actual = sha256(path.read_bytes()).hexdigest()
        expected = sha256(target).hexdigest()
        if actual == expected:
            draft.append({"path": relative, "action": "already_exact", "sha256": expected})
        else:
            diagnostics.append(
                f"existing configuration differs from locked MDK and will not be overwritten: {relative}"
            )
            draft.append({"path": relative, "action": "manual_merge_required",
                          "reason": "locked_source_template_absent",
                          "current_sha256": actual, "target_sha256": expected})

    supported = not diagnostics
    if not supported:
        changes = []
    return BuildPreparationPlan(
        candidate_identity=candidate_identity,
        manifest_sha256=manifest_identity,
        mdk_configuration_sha256=_configuration_sha(files),
        required_gradle_properties=_required_gradle_properties(locked),
        supported=supported,
        changes=tuple(changes),
        diagnostics=tuple(dict.fromkeys(diagnostics)),
        draft=tuple(draft),
    )


plan_build_preparation = draft_build_preparation


def _plan_fingerprint(plan: BuildPreparationPlan) -> str:
    return sha256(canonical_json(plan.to_dict()).encode("utf-8")).hexdigest()


def _validate_plan_sources(
    run_root: Path,
    manifest: LockedManifest,
    plan: BuildPreparationPlan,
) -> None:
    mdk_root = _real_directory(run_root / "toolchains" / "mdk", "MDK root")
    _, files, diagnostics = _locked_mdk_files(manifest, mdk_root)
    if (diagnostics
            or _configuration_sha(files) != plan.mdk_configuration_sha256
            or _required_gradle_properties(manifest) != plan.required_gradle_properties):
        raise StaleBuildPreparationPlan("locked MDK configuration changed after planning")


def _ensure_parent(root: Path, relative: str) -> tuple[Path, ...]:
    path = _checked_path(root, relative, allow_missing=True)
    missing: list[Path] = []
    current = path.parent
    while current != root and not current.exists():
        missing.append(current)
        current = current.parent
    if current.is_symlink() or not current.is_dir():
        raise BuildPreparationError(f"unsafe destination parent: {relative}")
    for directory in reversed(missing):
        directory.mkdir(mode=0o755)
    return tuple(missing)


def _atomic_create(path: Path, data: bytes, mode: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".modport-mdk-", dir=path.parent)
    temporary = Path(temporary_name)
    linked = False
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        if path.exists() or path.is_symlink():
            raise StaleBuildPreparationPlan(f"build path appeared before apply: {path.name}")
        os.link(temporary, path, follow_symlinks=False)
        linked = True
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException as error:
        if linked:
            try:
                source_info = temporary.lstat()
                target_info = path.lstat()
            except FileNotFoundError:
                pass
            else:
                if ((source_info.st_dev, source_info.st_ino)
                        != (target_info.st_dev, target_info.st_ino)):
                    raise StaleBuildPreparationPlan(
                        f"build path changed during failed atomic create: {path.name}"
                    ) from error
                path.unlink()
        raise
    finally:
        temporary.unlink(missing_ok=True)


def apply_build_preparation(
    root: str | os.PathLike[str],
    worktree: str | os.PathLike[str],
    manifest: LockedManifest | Mapping[str, Any],
    plan: BuildPreparationPlan,
) -> BuildPreparationApplyResult:
    """Apply an authenticated addition-only plan, rolling back on failure."""
    if not isinstance(plan, BuildPreparationPlan):
        raise TypeError("plan must be a BuildPreparationPlan")
    if not plan.supported:
        raise UnsupportedBuildLayout(plan.diagnostics)
    run_root = _real_directory(root, "run root")
    candidate = _real_directory(worktree, "worktree")
    if not candidate.is_relative_to(run_root):
        raise BuildPreparationError("worktree must be contained by the run root")
    identity = _candidate_identity(candidate, require_clean=False)
    if identity != plan.candidate_identity:
        raise StaleBuildPreparationPlan("candidate identity changed after planning")
    locked = _manifest(manifest)
    if manifest_sha256(locked) != plan.manifest_sha256:
        raise StaleBuildPreparationPlan("locked manifest changed after planning")
    _validate_plan_sources(run_root, locked, plan)

    states: list[str] = []
    for change in plan.changes:
        path = _checked_path(candidate, change.path, allow_missing=True)
        if not path.exists():
            states.append("before")
        elif path.is_file() and not path.is_symlink() and sha256(path.read_bytes()).hexdigest() == change.after_sha256:
            states.append("after")
        else:
            states.append("conflict")
    if states and all(state == "after" for state in states):
        if _dirty_paths(candidate) != frozenset(change.path for change in plan.changes):
            raise StaleBuildPreparationPlan("candidate has changes outside the applied build plan")
        return BuildPreparationApplyResult(identity, 0, (), "already_applied")
    if any(state != "before" for state in states):
        raise StaleBuildPreparationPlan("planned build paths are in a mixed or conflicting state")

    current = draft_build_preparation(run_root, candidate, locked)
    if _plan_fingerprint(current) != _plan_fingerprint(plan):
        raise StaleBuildPreparationPlan("build preparation draft changed before apply")
    changes = current.changes
    completed: list[BuildPreparationChange] = []
    created_directories: list[Path] = []
    try:
        for change in changes:
            path = _checked_path(candidate, change.path, allow_missing=True)
            created_directories.extend(_ensure_parent(candidate, change.path))
            _atomic_create(path, change._after, change._mode)
            completed.append(change)
    except BaseException:
        for change in reversed(completed):
            path = _checked_path(candidate, change.path)
            if sha256(path.read_bytes()).hexdigest() == change.after_sha256:
                path.unlink()
        for directory in reversed(created_directories):
            try:
                directory.rmdir()
            except OSError:
                pass
        raise
    return BuildPreparationApplyResult(
        identity, len(completed), tuple(change.path for change in completed), "applied"
    )


def revert_build_preparation(
    root: str | os.PathLike[str],
    worktree: str | os.PathLike[str],
    manifest: LockedManifest | Mapping[str, Any],
    plan: BuildPreparationPlan,
) -> BuildPreparationApplyResult:
    """Remove exact files added by ``plan``; never touch changed content."""
    if not isinstance(plan, BuildPreparationPlan):
        raise TypeError("plan must be a BuildPreparationPlan")
    run_root = _real_directory(root, "run root")
    candidate = _real_directory(worktree, "worktree")
    if not candidate.is_relative_to(run_root):
        raise BuildPreparationError("worktree must be contained by the run root")
    identity = _candidate_identity(candidate, require_clean=False)
    locked = _manifest(manifest)
    if identity != plan.candidate_identity or manifest_sha256(locked) != plan.manifest_sha256:
        raise StaleBuildPreparationPlan("candidate or manifest changed before revert")
    _validate_plan_sources(run_root, locked, plan)
    paths: list[Path] = []
    for change in plan.changes:
        path = _checked_path(candidate, change.path)
        if not path.is_file() or sha256(path.read_bytes()).hexdigest() != change.after_sha256:
            raise StaleBuildPreparationPlan(f"applied build file changed before revert: {change.path}")
        paths.append(path)
    for path in reversed(paths):
        path.unlink()
        parent = path.parent
        while parent != candidate:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    return BuildPreparationApplyResult(
        identity, len(paths), tuple(change.path for change in plan.changes), "reverted"
    )


def authenticate_target_config(
    worktree: str | os.PathLike[str],
    manifest: LockedManifest | Mapping[str, Any],
    mdk_root: str | os.PathLike[str],
    expected_marker: TargetConfigMarker | Mapping[str, Any] | None = None,
) -> TargetConfigMarker:
    """Authenticate an exact, clean target configuration without executing it."""
    candidate = _real_directory(worktree, "worktree")
    mdk = _real_directory(mdk_root, "MDK root")
    locked = _manifest(manifest)
    identity = _candidate_identity(candidate, require_clean=True)
    targets, files, source_diagnostics = _locked_mdk_files(locked, mdk)
    diagnostics = list(source_diagnostics)
    for relative in _ambiguous_paths(_tracked_paths(candidate), set(targets)):
        diagnostics.append(f"custom or multi-project Gradle layout is unsupported: {relative}")
    for item in files:
        path = _checked_path(candidate, item.path, allow_missing=True)
        if not path.is_file() or path.is_symlink():
            diagnostics.append(f"locked target configuration is missing: {item.path}")
        elif sha256(path.read_bytes()).hexdigest() != item.sha256:
            diagnostics.append(f"target configuration differs from locked MDK: {item.path}")
    if diagnostics:
        raise UnsupportedBuildLayout(tuple(dict.fromkeys(diagnostics)))
    marker = TargetConfigMarker(
        candidate_identity=identity,
        manifest_sha256=manifest_sha256(locked),
        mdk_configuration_sha256=_configuration_sha(files),
        files=files,
        required_gradle_properties=_required_gradle_properties(locked),
    )
    if expected_marker is not None:
        expected = expected_marker.to_dict() if isinstance(expected_marker, TargetConfigMarker) else dict(expected_marker)
        if expected != marker.to_dict():
            raise StaleBuildPreparationPlan("target configuration marker does not match the candidate")
    return marker
