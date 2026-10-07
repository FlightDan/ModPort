"""Create and authenticate small, artifact-only migration handoffs.

The handoff deliberately contains no Dispatcher SDK state.  It carries a Git
bundle for the immutable source and current target commits, plus only the
files explicitly selected by the caller.  Every installed file is bound by a
digest in the handoff manifest and the manifest is itself returned as a
normal run-local artifact reference.
"""

from __future__ import annotations

from .workspace import git_probe

from .workspace import project_path
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import selectors
import shutil
import stat
import subprocess
import tempfile
import time
from typing import Any, Mapping

from .evidence import atomic_json, file_digest
from .manifest import canonical_json


SCHEMA = "modport-artifact-handoff"
VERSION = 1
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_HANDOFF_BYTES = 512 * 1024 * 1024
MAX_TREE_ENTRIES = 20_000
_MAX_HEADER_BYTES = 16 * 1024 * 1024
_MAX_TREE_RECORD_BYTES = 1024 * 1024
_HEX = frozenset("0123456789abcdef")
_FORBIDDEN_NAMES = frozenset({
    "input.json", "prepared.json", "rework-sources.json", "run.json",
})
_EXPECTED_EVIDENCE = (
    ("baseline/.modport/functional-contract.json", "contract_not_carried"),
    ("baseline/.modport/contract-review.json", "contract_review_not_carried"),
    ("artifacts/baseline-contract-tests.json", "baseline_tests_not_carried"),
    ("worktree/.modport/code-review.json", "code_review_not_carried"),
)
_ENVIRONMENT_HANDOFF_PAIR = frozenset({
    "artifacts/locked-manifest.json",
    "toolchains/neoforge-maven-metadata.xml",
})


def _validate_environment_handoff_pair(selected: set[str]) -> None:
    present = selected & _ENVIRONMENT_HANDOFF_PAIR
    if present and present != _ENVIRONMENT_HANDOFF_PAIR:
        missing = sorted(_ENVIRONMENT_HANDOFF_PAIR - present)
        raise ValueError(
            "environment handoff must select the locked manifest and NeoForge metadata together; "
            "missing: " + ", ".join(missing)
        )


def _read_json_snapshot(path: Path, *, limit: int) -> tuple[Mapping[str, Any], str]:
    _regular_contained(path.parent, path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_size > limit:
            raise ValueError(f"JSON input is too large or unsafe: {path.name}")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            block = os.read(descriptor, min(1024 * 1024, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        raw = b"".join(chunks)
        if len(raw) != details.st_size or len(raw) > limit:
            raise ValueError(f"JSON input changed while being read: {path.name}")
    finally:
        os.close(descriptor)
    value = json.loads(raw)
    if not isinstance(value, Mapping):
        raise ValueError(f"JSON input must be an object: {path.name}")
    return value, hashlib.sha256(raw).hexdigest()


def _read_json(path: Path, *, limit: int) -> Mapping[str, Any]:
    return _read_json_snapshot(path, limit=limit)[0]


def _safe_relative(value: str, *, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty relative path")
    path = Path(value)
    if path.is_absolute() or not path.parts or ".." in path.parts or "." in path.parts:
        raise ValueError(f"{label} must be a contained relative path: {value!r}")
    if path.as_posix() != value:
        raise ValueError(f"{label} must use canonical POSIX separators: {value!r}")
    return path


def _regular_contained(root: Path, path: Path) -> None:
    root_resolved = root.resolve(strict=True)
    try:
        relative = path.absolute().relative_to(root.absolute())
    except ValueError as exc:
        raise ValueError(f"path escapes its root: {path}") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"symlinks are not allowed in handoff paths: {path}")
    if not path.is_file() or path.resolve(strict=True) != path.absolute():
        raise ValueError(f"handoff path is not a contained regular file: {path}")
    if not path.resolve(strict=True).is_relative_to(root_resolved):
        raise ValueError(f"path escapes its root: {path}")


def _safe_directory(path: Path, *, existing: bool = True) -> Path:
    absolute = path.absolute()
    if existing and (not absolute.is_dir() or absolute.is_symlink()):
        raise ValueError(f"directory is missing or unsafe: {path}")
    if existing and absolute.resolve(strict=True) != absolute:
        raise ValueError(f"directory traverses a symlink: {path}")
    return absolute


def _git(worktree: Path, *args: str, timeout: int = 120) -> str:
    result = git_probe(
        ["git", "-C", str(worktree), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
        env={
            **os.environ,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        },
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown Git error"
        raise ValueError(f"Git command failed ({' '.join(args)}): {detail}")
    return result.stdout.strip()


def _worktree_status(worktree: Path) -> str:
    return _git(worktree, "status", "--porcelain=v1", "--untracked-files=all")


def _hidden_index_problem(worktree: Path) -> str | None:
    # Git's assume-unchanged and skip-worktree flags can hide edits from
    # normal status output.  A handoff must not authenticate such a checkout.
    for line in _git(worktree, "ls-files", "-v").splitlines():
        if line and (line[0].islower() or line[0] == "S"):
            return f"hidden index flag on {line[2:]}"
    return None


def _commit(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a Git object id")
    result = value.strip().lower()
    if len(result) not in {40, 64} or any(character not in _HEX for character in result):
        raise ValueError(f"{label} must be a full Git object id")
    return result


def _digest_value(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a SHA-256 digest")
    result = value.strip().lower()
    if len(result) != 64 or any(character not in _HEX for character in result):
        raise ValueError(f"{label} must be a SHA-256 digest")
    return result


def _manifest_digest(manifest: Mapping[str, Any]) -> str:
    payload = dict(manifest)
    payload.pop("manifest_sha256", None)
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _forbidden(relative: Path) -> bool:
    lowered = relative.name.lower()
    return (
        any(part.lower() == ".git" for part in relative.parts)
        or lowered in _FORBIDDEN_NAMES
        or ".sqlite3" in lowered
        or lowered.endswith(("-wal", "-shm", "-journal"))
    )


def _copy_selected(source: Path, destination: Path) -> tuple[str, int]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source, flags)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise ValueError(f"selected path is not a regular file: {source}")
        if details.st_size > MAX_FILE_BYTES:
            raise ValueError(f"selected file exceeds 64 MiB: {source}")
        digest = hashlib.sha256()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with os.fdopen(os.dup(descriptor), "rb") as input_stream, destination.open("xb") as output:
            for block in iter(lambda: input_stream.read(1024 * 1024), b""):
                digest.update(block)
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        if destination.stat().st_size != details.st_size:
            raise OSError(f"short artifact copy: {source}")
        return digest.hexdigest(), details.st_size
    finally:
        os.close(descriptor)


def _validate_request(request: Any) -> dict[str, Any]:
    if not isinstance(request, Mapping):
        raise ValueError("source run has no frozen migration request")
    required = (
        "source_repository", "source_revision", "source_loader",
        "source_minecraft", "target_loader", "target_minecraft",
    )
    for key in required:
        if not isinstance(request.get(key), str) or not request[key].strip():
            raise ValueError(f"source request is missing {key}")
    # Round-trip through canonical JSON to detach nested values from custom
    # mapping implementations and reject unsupported/non-finite values.
    return json.loads(canonical_json(request))


def prepare_handoff(
    source_run_dir: Path | str,
    output_dir: Path | str,
    selected_paths: list[str],
    *,
    committed_head_only: bool = False,
) -> dict[str, Any]:
    from .workspace import workspace_context
    with workspace_context(source_run_dir):
        return _prepare_handoff(source_run_dir, output_dir, selected_paths,
                                committed_head_only=committed_head_only)


def _prepare_handoff(source_run_dir, output_dir, selected_paths, *, committed_head_only=False):
    """Create an authenticated handoff without reading SDK databases.

    ``selected_paths`` are run-relative regular files.  Directories, links,
    scheduler payloads, databases, and implicit age-based selection are all
    rejected. By default the source checkout must be clean. Explicit
    ``committed_head_only`` packages only target ``HEAD`` and excludes all
    uncommitted project files, including selected worktree paths.
    """

    root = _safe_directory(Path(source_run_dir))
    output = Path(output_dir).absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"handoff output already exists: {output}")
    parent = _safe_directory(output.parent)
    if not isinstance(selected_paths, list) or not selected_paths:
        raise ValueError("selected_paths must be a non-empty list")

    worktree = _safe_directory(project_path(root, "worktree"))
    if output.is_relative_to(worktree):
        raise ValueError("handoff output must be outside the source worktree")
    top = Path(_git(worktree, "rev-parse", "--show-toplevel")).resolve(strict=True)
    if top != worktree:
        raise ValueError("source worktree is not the canonical Git top level")
    if type(committed_head_only) is not bool:
        raise ValueError("committed_head_only must be a boolean")
    initial_status = _worktree_status(worktree)
    hidden = _hidden_index_problem(worktree)
    if hidden:
        raise ValueError(f"source target worktree has uncommitted changes: {hidden}")
    if initial_status and not committed_head_only:
        raise ValueError(f"source target worktree has uncommitted changes: {initial_status.splitlines()[0]}")
    target_commit = _commit(_git(worktree, "rev-parse", "HEAD^{commit}"), label="target_commit")

    header_path = root / "run.json"
    source_path = root / "artifacts" / "source.json"
    header, header_sha256 = _read_json_snapshot(header_path, limit=_MAX_HEADER_BYTES)
    source, source_sha256 = _read_json_snapshot(source_path, limit=1024 * 1024)
    request = _validate_request(header.get("request"))
    source_commit = _commit(source.get("source_commit"), label="source_commit")
    if source.get("source_repository") != request["source_repository"]:
        raise ValueError("source evidence repository does not match frozen request")
    if source.get("requested_revision") != request["source_revision"]:
        raise ValueError("source evidence revision does not match frozen request")
    _git(worktree, "cat-file", "-e", f"{source_commit}^{{commit}}")
    try:
        _git(worktree, "merge-base", "--is-ancestor", source_commit, target_commit)
    except ValueError as exc:
        raise ValueError("recorded source commit is not an ancestor of target HEAD") from exc

    normalized: list[Path] = []
    seen: set[str] = set()
    selected_total = 0
    for value in selected_paths:
        relative = _safe_relative(value, label="selected path")
        if committed_head_only and relative.parts[0] == "worktree":
            raise ValueError("committed-head-only handoff cannot select worktree files")
        if relative.as_posix() in seen:
            raise ValueError(f"selected path is duplicated: {value}")
        seen.add(relative.as_posix())
        if _forbidden(relative):
            raise ValueError(f"scheduler/database payload cannot be handed off: {value}")
        candidate = project_path(root, relative)
        _regular_contained(worktree if relative.parts[0] == 'worktree' else root, candidate)
        size = candidate.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ValueError(f"selected file exceeds 64 MiB: {value}")
        selected_total += size
        if selected_total > MAX_HANDOFF_BYTES:
            raise ValueError("selected files exceed the 512 MiB handoff limit")
        normalized.append(relative)

    _validate_environment_handoff_pair(seen)

    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=parent))
    try:
        artifacts: list[dict[str, Any]] = []
        for relative in sorted(normalized, key=lambda item: item.as_posix()):
            installed = Path("files") / relative
            checksum, size = _copy_selected(project_path(root, relative), temporary / installed)
            artifacts.append({
                "source_path": relative.as_posix(),
                "path": installed.as_posix(),
                "sha256": checksum,
                "size": size,
                "media_type": mimetypes.guess_type(relative.name)[0] or "application/octet-stream",
            })

        bundle = temporary / "repository.bundle"
        _git(worktree, "bundle", "create", str(bundle), "HEAD", timeout=900)
        _regular_contained(temporary, bundle)
        bundle_size = bundle.stat().st_size
        if bundle_size > MAX_FILE_BYTES:
            raise ValueError("repository bundle exceeds the 64 MiB per-file limit")

        diagnostics = [
            {
                "code": code,
                "path": expected,
                "detail": "evidence was not selected for the artifact-only handoff",
            }
            for expected, code in _EXPECTED_EVIDENCE
            if expected not in seen
        ]
        if initial_status:
            diagnostics.append({
                "code": "uncommitted_changes_excluded",
                "detail": "target bundle contains committed HEAD only; uncommitted worktree files were excluded",
            })
        run_id = header.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("source run_id is missing")
        logical_run_id = header.get("logical_run_id")
        if logical_run_id is not None and (not isinstance(logical_run_id, str) or not logical_run_id):
            raise ValueError("source logical_run_id is invalid")
        manifest: dict[str, Any] = {
            "schema": SCHEMA,
            "version": VERSION,
            "acceptance_status": "unverified",
            "source": {
                "run_id": run_id,
                "logical_run_id": logical_run_id,
                "run_header_sha256": header_sha256,
                "source_evidence_sha256": source_sha256,
                "request": request,
                "source_commit": source_commit,
                "target_commit": target_commit,
            },
            "repository_bundle": {
                "path": "repository.bundle",
                "sha256": file_digest(bundle),
                "size": bundle_size,
            },
            "artifacts": artifacts,
            "diagnostics": diagnostics,
        }
        if committed_head_only:
            manifest["source"]["worktree_selection"] = {
                "mode": "committed_head_only",
                "excluded_status_sha256": hashlib.sha256(initial_status.encode("utf-8")).hexdigest(),
                "excluded_status_entries": len(initial_status.splitlines()),
            }
        manifest["manifest_sha256"] = _manifest_digest(manifest)
        atomic_json(temporary / "manifest.json", manifest)
        final_head = _commit(_git(worktree, "rev-parse", "HEAD^{commit}"), label="target_commit")
        final_status = _worktree_status(worktree)
        if (final_head != target_commit or final_status != initial_status
                or _hidden_index_problem(worktree)):
            raise ValueError("source target worktree changed while the handoff was prepared")
        _regular_contained(root, header_path)
        _regular_contained(root, source_path)
        if file_digest(header_path) != header_sha256 or file_digest(source_path) != source_sha256:
            raise ValueError("source provenance changed while the handoff was prepared")
        total = sum(path.stat().st_size for path in temporary.rglob("*") if path.is_file())
        if total > MAX_HANDOFF_BYTES:
            raise ValueError("handoff package exceeds the 512 MiB limit")
        validate_handoff(temporary)
        os.replace(temporary, output)
        return validate_handoff(output)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _tree_checkout_totals(
    bare: Path,
    commit: str,
    *,
    logical_bytes: int,
    entries: int,
) -> tuple[int, int]:
    """Stream one Git tree and account for its materialized checkout size."""

    with tempfile.TemporaryFile() as error_output:
        process = subprocess.Popen(
            ["git", "--git-dir", str(bare), "ls-tree", "-r", "-l", "-z", commit],
            stdout=subprocess.PIPE,
            stderr=error_output,
            env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"},
        )
        assert process.stdout is not None
        descriptor = process.stdout.fileno()
        os.set_blocking(descriptor, False)
        selector = selectors.DefaultSelector()
        selector.register(descriptor, selectors.EVENT_READ)
        buffer = bytearray()
        deadline = time.monotonic() + 60
        try:
            finished = False
            while not finished:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("timed out while inspecting repository tree")
                events = selector.select(min(1.0, remaining))
                if not events:
                    if process.poll() is None:
                        continue
                    try:
                        block = os.read(descriptor, 64 * 1024)
                    except BlockingIOError:
                        block = b""
                else:
                    block = os.read(descriptor, 64 * 1024)
                if not block:
                    finished = True
                    continue
                buffer.extend(block)
                while True:
                    boundary = buffer.find(0)
                    if boundary < 0:
                        break
                    record = bytes(buffer[:boundary])
                    del buffer[:boundary + 1]
                    if len(record) > _MAX_TREE_RECORD_BYTES:
                        raise ValueError("repository tree entry exceeds inspection limit")
                    try:
                        metadata, _pathname = record.split(b"\t", 1)
                        _mode, object_type, _object_id, size_text = metadata.split(b" ", 3)
                    except ValueError as exc:
                        raise ValueError("repository tree emitted a malformed entry") from exc
                    entries += 1
                    if entries > MAX_TREE_ENTRIES:
                        raise ValueError(
                            f"repository source and target trees exceed {MAX_TREE_ENTRIES} entries"
                        )
                    if object_type == b"blob":
                        try:
                            size = int(size_text)
                        except ValueError as exc:
                            raise ValueError("repository tree emitted an invalid blob size") from exc
                        if size < 0 or size > MAX_FILE_BYTES:
                            raise ValueError("repository tree contains a blob exceeding 64 MiB")
                        logical_bytes += size
                        if logical_bytes > MAX_HANDOFF_BYTES:
                            raise ValueError("repository source and target checkouts exceed 512 MiB")
                    elif object_type != b"commit" or size_text.strip() != b"-":
                        raise ValueError("repository tree emitted an unsupported object type")
                if len(buffer) > _MAX_TREE_RECORD_BYTES:
                    raise ValueError("repository tree entry exceeds inspection limit")
            if buffer:
                raise ValueError("repository tree output ended mid-entry")
            return_code = process.wait(timeout=5)
            if return_code:
                error_output.seek(0)
                detail = error_output.read(64 * 1024).decode("utf-8", "replace").strip()
                raise ValueError(f"repository tree inspection failed: {detail}")
            return logical_bytes, entries
        finally:
            selector.close()
            process.stdout.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def _verify_bundle(bundle: Path, source_commit: str, target_commit: str) -> None:
    with tempfile.TemporaryDirectory(prefix="modport-handoff-git-") as directory:
        verification_repository = Path(directory) / "verification.git"
        initialized = git_probe(
            ["git", "init", "--bare", str(verification_repository)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
            check=False,
            env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"},
        )
        if initialized.returncode:
            raise ValueError(f"could not initialize bundle verifier: {initialized.stderr.strip()}")
        result = git_probe(
            ["git", "-C", str(verification_repository), "bundle", "verify", str(bundle)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=300,
            check=False,
            env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"},
        )
        if result.returncode:
            raise ValueError(f"repository bundle verification failed: {result.stderr.strip()}")
        bare = Path(directory) / "repository.git"
        clone = git_probe(
            ["git", "clone", "--bare", "--no-local", str(bundle), str(bare)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=300,
            check=False,
            env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"},
        )
        if clone.returncode:
            raise ValueError(f"repository bundle cannot be cloned: {clone.stderr.strip()}")
        for label, commit in (("source", source_commit), ("target", target_commit)):
            check = git_probe(
                ["git", "--git-dir", str(bare), "cat-file", "-e", f"{commit}^{{commit}}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=60,
                check=False,
            )
            if check.returncode:
                raise ValueError(f"repository bundle does not contain {label} commit {commit}")
        ancestry = git_probe(
            [
                "git", "--git-dir", str(bare), "merge-base", "--is-ancestor",
                source_commit, target_commit,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
            check=False,
        )
        if ancestry.returncode:
            raise ValueError("repository bundle source commit is not an ancestor of target commit")
        logical_bytes = 0
        entries = 0
        for commit in (source_commit, target_commit):
            logical_bytes, entries = _tree_checkout_totals(
                bare, commit, logical_bytes=logical_bytes, entries=entries,
            )


def validate_handoff(bundle_dir: Path | str) -> dict[str, Any]:
    """Read and fully validate a handoff directory without modifying it."""

    root = _safe_directory(Path(bundle_dir))
    manifest_path = root / "manifest.json"
    manifest = dict(_read_json(manifest_path, limit=4 * 1024 * 1024))
    if manifest.get("schema") != SCHEMA or manifest.get("version") != VERSION:
        raise ValueError("unsupported artifact handoff schema")
    if manifest.get("acceptance_status") != "unverified":
        raise ValueError("artifact-only handoff must remain unverified")
    expected_manifest_digest = manifest.get("manifest_sha256")
    if expected_manifest_digest != _manifest_digest(manifest):
        raise ValueError("artifact handoff manifest checksum mismatch")

    source = manifest.get("source")
    if not isinstance(source, Mapping):
        raise ValueError("handoff source metadata is missing")
    if not isinstance(source.get("run_id"), str) or not source["run_id"]:
        raise ValueError("handoff source run_id is invalid")
    _validate_request(source.get("request"))
    source_commit = _commit(source.get("source_commit"), label="source_commit")
    target_commit = _commit(source.get("target_commit"), label="target_commit")
    for field in ("run_header_sha256", "source_evidence_sha256"):
        _digest_value(source.get(field), label=field)
    selection = source.get("worktree_selection")
    if selection is not None:
        if (not isinstance(selection, Mapping)
                or selection.get("mode") != "committed_head_only"
                or type(selection.get("excluded_status_entries")) is not int
                or selection["excluded_status_entries"] < 0):
            raise ValueError("handoff worktree selection is invalid")
        _digest_value(selection.get("excluded_status_sha256"), label="excluded_status_sha256")

    bundle_ref = manifest.get("repository_bundle")
    if not isinstance(bundle_ref, Mapping):
        raise ValueError("repository bundle reference is missing")
    bundle_relative = _safe_relative(bundle_ref.get("path"), label="repository bundle path")
    if bundle_relative.as_posix() != "repository.bundle":
        raise ValueError("repository bundle must use its canonical path")
    bundle = root / bundle_relative
    _regular_contained(root, bundle)
    bundle_size = bundle.stat().st_size
    if bundle_ref.get("size") != bundle_size or bundle_size > MAX_FILE_BYTES:
        raise ValueError("repository bundle size mismatch or limit exceeded")
    if bundle_ref.get("sha256") != file_digest(bundle):
        raise ValueError("repository bundle checksum mismatch")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError("handoff must contain selected artifacts")
    seen_source: set[str] = set()
    seen_path: set[str] = set()
    total = bundle_size + manifest_path.stat().st_size
    for item in artifacts:
        if not isinstance(item, Mapping):
            raise ValueError("handoff artifact entry must be an object")
        source_relative = _safe_relative(item.get("source_path"), label="artifact source_path")
        package_relative = _safe_relative(item.get("path"), label="artifact path")
        if package_relative != Path("files") / source_relative:
            raise ValueError("artifact package path does not match source_path")
        if _forbidden(source_relative):
            raise ValueError("handoff manifest references a forbidden scheduler/database payload")
        if source_relative.as_posix() in seen_source or package_relative.as_posix() in seen_path:
            raise ValueError("handoff manifest contains duplicate artifact paths")
        seen_source.add(source_relative.as_posix())
        seen_path.add(package_relative.as_posix())
        path = root / package_relative
        _regular_contained(root, path)
        size = path.stat().st_size
        if item.get("size") != size or size > MAX_FILE_BYTES:
            raise ValueError(f"artifact size mismatch or limit exceeded: {source_relative}")
        if item.get("sha256") != file_digest(path):
            raise ValueError(f"artifact checksum mismatch: {source_relative}")
        total += size
    _validate_environment_handoff_pair(seen_source)
    if total > MAX_HANDOFF_BYTES:
        raise ValueError("handoff package exceeds the 512 MiB limit")
    diagnostics = manifest.get("diagnostics")
    if not isinstance(diagnostics, list):
        raise ValueError("handoff diagnostics must be an array")

    expected_files = {"manifest.json", "repository.bundle", *seen_path}
    observed_files: set[str] = set()
    for directory, directories, filenames in os.walk(root, followlinks=False):
        parent = Path(directory)
        for name in directories:
            child = parent / name
            if child.is_symlink():
                raise ValueError(f"handoff contains a symlink directory: {child.relative_to(root)}")
        for name in filenames:
            child = parent / name
            relative = child.relative_to(root).as_posix()
            if child.is_symlink():
                raise ValueError(f"handoff contains a symlink file: {relative}")
            if not child.is_file():
                raise ValueError(f"handoff contains an unsafe file: {relative}")
            observed_files.add(relative)
    if observed_files != expected_files:
        extra = sorted(observed_files - expected_files)
        missing = sorted(expected_files - observed_files)
        raise ValueError(f"handoff package contents differ from manifest: extra={extra}, missing={missing}")

    _verify_bundle(bundle, source_commit, target_commit)
    _regular_contained(root, bundle)
    if bundle.stat().st_size != bundle_size or bundle_ref.get("sha256") != file_digest(bundle):
        raise ValueError("repository bundle changed during tree verification")
    return manifest


def install_handoff(
    bundle_dir: Path | str,
    new_run_root: Path | str,
) -> dict[str, Any]:
    """Install an authenticated handoff below ``artifacts/handoff``."""

    source_root = _safe_directory(Path(bundle_dir))
    manifest = validate_handoff(source_root)
    run_root = _safe_directory(Path(new_run_root))
    artifacts_root = run_root / "artifacts"
    if artifacts_root.exists():
        _safe_directory(artifacts_root)
    else:
        artifacts_root.mkdir()
    destination = artifacts_root / "handoff"
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"handoff is already installed: {destination}")
    temporary = Path(tempfile.mkdtemp(prefix=".handoff-", dir=artifacts_root))
    try:
        expected_copies = (
            (Path("manifest.json"), file_digest(source_root / "manifest.json")),
            (Path("repository.bundle"), manifest["repository_bundle"]["sha256"]),
        )
        for relative, expected in expected_copies:
            copied, _ = _copy_selected(source_root / relative, temporary / relative)
            if copied != expected:
                raise ValueError(f"handoff changed while being installed: {relative}")
        for item in manifest["artifacts"]:
            relative = _safe_relative(item["path"], label="artifact path")
            target = temporary / relative
            copied, _ = _copy_selected(source_root / relative, target)
            if copied != item["sha256"]:
                raise ValueError(f"handoff changed while being installed: {relative}")
        validate_handoff(temporary)
        os.replace(temporary, destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    installed = validate_handoff(destination)
    manifest_relative = Path("artifacts/handoff/manifest.json")
    refs: dict[str, dict[str, Any]] = {
        "artifact_handoff": {
            "path": manifest_relative.as_posix(),
            "sha256": file_digest(run_root / manifest_relative),
            "media_type": "application/json",
            "metadata": {
                "schema": SCHEMA,
                "version": VERSION,
                "bundle_path": "artifacts/handoff/repository.bundle",
                "bundle_sha256": installed["repository_bundle"]["sha256"],
            },
        },
    }
    for item in installed["artifacts"]:
        installed_relative = Path("artifacts/handoff") / item["path"]
        refs[f"handoff:{item['source_path']}"] = {
            "path": installed_relative.as_posix(),
            "sha256": item["sha256"],
            "media_type": item["media_type"],
            "metadata": {"source_path": item["source_path"]},
        }
    source = installed["source"]
    return {
        "refs": refs,
        "metadata": {
            "source_run_id": source["run_id"],
            "source_logical_run_id": source.get("logical_run_id"),
            "source_commit": source["source_commit"],
            "target_commit": source["target_commit"],
            "acceptance_status": "unverified",
            "diagnostics": installed["diagnostics"],
        },
    }


__all__ = [
    "MAX_FILE_BYTES", "MAX_HANDOFF_BYTES", "MAX_TREE_ENTRIES", "SCHEMA", "VERSION",
    "install_handoff", "prepare_handoff", "validate_handoff",
]
