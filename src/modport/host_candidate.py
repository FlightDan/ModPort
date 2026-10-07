"""Quiescent, host-owned collection of a coder's filesystem changes."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha1, sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
from typing import Iterable, Mapping

from . import handlers


_SHA = re.compile(r"[0-9a-f]{40}\Z")
_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
_GENERATED_DIRECTORIES = frozenset({
    ".cache", ".gradle", ".m2", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".tox", ".venv", "__pycache__", "build", "dist", "node_modules", "out",
    "target", "venv",
})
_CREDENTIAL_DIRECTORIES = frozenset({
    ".aws", ".azure", ".codex", ".docker", ".gnupg", ".kube", ".openai",
    ".ssh", "credentials", "gcloud", "secrets",
})
_CREDENTIAL_FILES = frozenset({
    ".git-credentials", ".netrc", ".npmrc", ".pypirc", "credentials.json",
    "id_dsa", "id_ecdsa", "id_ed25519", "id_rsa", "secrets.json",
    "service-account.json", "token.json",
})
_MAX_FILES = 100_000
_MAX_FILE_BYTES = 256 * 1024 * 1024
_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024


@dataclass(frozen=True)
class HostCandidate:
    """A commit stored in a host-owned bare repository plus its audit record."""

    repository: Path
    record: Mapping[str, object]

    @property
    def head(self) -> str:
        return str(self.record["head"])

    @property
    def paths(self) -> list[str]:
        return list(self.record["paths"])


def _relative_path(value: str) -> str:
    if (not value or "\\" in value or "\ufffd" in value
            or any(ord(character) < 32 or 0xD800 <= ord(character) <= 0xDFFF
                   for character in value)):
        raise ValueError("unsafe candidate path")
    path = PurePosixPath(value)
    parts = value.split("/")
    if path.is_absolute() or any(part in {"", ".", "..", ".git"} for part in parts):
        raise ValueError("candidate paths must be contained relative paths")
    return "/".join(parts)


def _exclusion(relative: str, report_paths: frozenset[str]) -> str | None:
    parts = PurePosixPath(relative).parts
    lowered = tuple(part.lower() for part in parts)
    if ".git" in lowered:
        return "git_metadata"
    if relative in report_paths or lowered[:2] == (".modport", "goal-reports"):
        return "agent_report"
    if any(part in _GENERATED_DIRECTORIES for part in lowered):
        return "generated_output"
    if any(part in _CREDENTIAL_DIRECTORIES for part in lowered):
        return "credential_storage"
    name = lowered[-1]
    if (name in _CREDENTIAL_FILES or name == ".env" or name.startswith(".env.")
            or name.endswith((".jks", ".key", ".pem", ".p12", ".pfx"))):
        return "credential_file"
    return None


def _git(command, repository: Path, *args: str, check: bool = True,
         environment: Mapping[str, str] | None = None, input_text: str | None = None):
    root = Path(command.run_dir)
    result = handlers._exec(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
         "-c", "core.quotePath=true", "-c", "gc.auto=0", "-c", "maintenance.auto=false",
         "--git-dir", str(repository), *args],
        cwd=root,
        log=root / "logs" / f"development-{command.command_id}.log",
        timeout=handlers._remaining_timeout(command, 120),
        env={"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
             "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1",
             **dict(environment or {})},
        input_text=input_text,
    )
    if check and result.returncode:
        raise ValueError(f"host candidate git {args[0]} failed: {result.stdout[-1000:]}")
    return result


def _copy_regular(source: Path, destination: Path) -> int:
    """Copy one stopped-producer file without ever following a link."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source, flags)
    size = 0
    try:
        attributes = os.fstat(descriptor)
        if (not stat.S_ISREG(attributes.st_mode) or attributes.st_nlink != 1
                or attributes.st_size > _MAX_FILE_BYTES):
            raise ValueError("candidate entry is not a private regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as incoming, destination.open("wb") as output:
            while True:
                chunk = incoming.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                size += len(chunk)
                if size > _MAX_FILE_BYTES:
                    raise ValueError("candidate file exceeds the collection limit")
        final_attributes = os.fstat(descriptor)
        if (final_attributes.st_mode != attributes.st_mode
                or final_attributes.st_dev != attributes.st_dev
                or final_attributes.st_ino != attributes.st_ino
                or final_attributes.st_size != attributes.st_size
                or final_attributes.st_mtime_ns != attributes.st_mtime_ns
                or final_attributes.st_nlink != attributes.st_nlink):
            raise ValueError("candidate entry changed during collection")
    finally:
        os.close(descriptor)
    return size


def _workspace_files(workspace: Path, report_paths: frozenset[str]):
    files: dict[str, tuple[Path, str]] = {}
    exclusions: list[dict[str, str]] = []
    selected_bytes = 0
    for folder, directories, names in os.walk(workspace, followlinks=False):
        folder_path = Path(folder)
        kept = []
        for name in sorted(directories):
            path = folder_path / name
            relative = path.relative_to(workspace).as_posix()
            reason = _exclusion(relative, report_paths)
            try:
                attributes = path.lstat()
            except OSError:
                exclusions.append({"path": relative, "reason": "unreadable_directory"})
                continue
            if reason is not None:
                exclusions.append({"path": relative, "reason": reason})
            elif not stat.S_ISDIR(attributes.st_mode):
                exclusions.append({"path": relative, "reason": "nonregular_entry"})
            else:
                kept.append(name)
        directories[:] = kept
        for name in sorted(names):
            path = folder_path / name
            relative = path.relative_to(workspace).as_posix()
            reason = _exclusion(relative, report_paths)
            try:
                normalized = _relative_path(relative)
                attributes = path.lstat()
            except (OSError, ValueError):
                exclusions.append({"path": relative, "reason": "unsafe_path"})
                continue
            if reason is not None:
                exclusions.append({"path": normalized, "reason": reason})
            elif not stat.S_ISREG(attributes.st_mode):
                exclusions.append({"path": normalized, "reason": "nonregular_entry"})
            elif attributes.st_nlink != 1:
                exclusions.append({"path": normalized, "reason": "multiply_linked_file"})
            elif attributes.st_size > _MAX_FILE_BYTES:
                exclusions.append({"path": normalized, "reason": "file_size_limit"})
            elif len(files) >= _MAX_FILES:
                exclusions.append({"path": normalized, "reason": "file_count_limit"})
            elif selected_bytes + attributes.st_size > _MAX_TOTAL_BYTES:
                exclusions.append({"path": normalized, "reason": "collection_size_limit"})
            else:
                mode = "100755" if attributes.st_mode & 0o111 else "100644"
                files[normalized] = (path, mode)
                selected_bytes += attributes.st_size
    return files, exclusions


def _below_exclusion(relative: str, excluded_paths: tuple[str, ...]) -> bool:
    return any(relative == path or relative.startswith(path + "/") for path in excluded_paths)


def _contained_directory(path: Path, root: Path, label: str) -> Path:
    path = Path(path).absolute()
    if (path.is_symlink() or not path.is_dir() or path.resolve() != path
            or not path.is_relative_to(root)):
        raise ValueError(f"unsafe {label}")
    return path


def _projection(command, repository: Path, head: str, report_paths: frozenset[str]):
    entries = []
    total_bytes = 0
    output = _git(command, repository, "ls-tree", "-r", "-l", "-z", head).stdout
    for raw in output.split("\0"):
        if not raw:
            continue
        description, relative = raw.split("\t", 1)
        mode, kind, identity, size = description.split()
        try:
            relative = _relative_path(relative)
        except ValueError:
            continue
        if _exclusion(relative, report_paths) is not None:
            continue
        if kind == "blob" and mode in {"100644", "100755"} and _SHA.fullmatch(identity):
            size = int(size)
            if (size > _MAX_FILE_BYTES or len(entries) >= _MAX_FILES
                    or total_bytes + size > _MAX_TOTAL_BYTES):
                continue
            entries.append({"path": relative, "mode": mode, "blob": identity,
                            "size": size})
            total_bytes += size
    entries.sort(key=lambda entry: entry["path"])
    data = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return entries, sha256(data).hexdigest()


def _safe_blob_identity(source: Path, expected_size: int) -> str | None:
    """Hash one stopped-workspace file using Git's blob identity format."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError:
        return None
    try:
        attributes = os.fstat(descriptor)
        if (not stat.S_ISREG(attributes.st_mode) or attributes.st_nlink != 1
                or attributes.st_size != expected_size
                or attributes.st_size > _MAX_FILE_BYTES):
            return None
        digest = sha1()
        digest.update(f"blob {attributes.st_size}\0".encode("ascii"))
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        final_attributes = os.fstat(descriptor)
        if (final_attributes.st_mode != attributes.st_mode
                or final_attributes.st_dev != attributes.st_dev
                or final_attributes.st_ino != attributes.st_ino
                or final_attributes.st_size != attributes.st_size
                or final_attributes.st_mtime_ns != attributes.st_mtime_ns
                or final_attributes.st_nlink != attributes.st_nlink):
            return None
        return digest.hexdigest()
    except OSError:
        return None
    finally:
        os.close(descriptor)


def _cached_candidate_matches(command, root: Path, workspace: Path, start: str,
                              task_id: str, reports: frozenset[str],
                              candidate: HostCandidate) -> bool:
    """Check whether a prior host candidate still names this stopped workspace."""
    record = candidate.record
    try:
        if (record.get("start") != start or record.get("task_id") != task_id
                or record.get("command_id", command.command_id) != command.command_id):
            return False
        repository_relative = record.get("repository")
        expected_repository = (
            Path("artifacts") / "executions" / command.command_id
            / "host-candidate.git").as_posix()
        if repository_relative != expected_repository:
            return False
        repository = Path(candidate.repository).absolute()
        if (repository != root / repository_relative
                or repository.is_symlink() or not repository.is_dir()):
            return False
        _contained_directory(repository, root, "cached host candidate repository")
        head = record.get("head")
        tree = record.get("tree")
        if (not isinstance(head, str) or not _SHA.fullmatch(head)
                or not isinstance(tree, str) or not _SHA.fullmatch(tree)):
            return False
        ref_identity = sha256((command.command_id + "\0" + task_id).encode()).hexdigest()
        expected_ref = f"refs/modport/candidates/{ref_identity}/{head}"
        if record.get("ref") != expected_ref:
            return False
        if not isinstance(record.get("paths"), list):
            return False
        actual_tree = _git(command, repository, "rev-parse", head + "^{tree}").stdout.strip()
        if actual_tree != tree:
            return False
        entries, projection_sha256 = _projection(command, repository, head, reports)
        if (record.get("safe_projection_sha256") != projection_sha256
                or record.get("safe_projection_file_count") != len(entries)):
            return False
        actual_paths = sorted(filter(None, _git(
            command, repository, "diff", "--name-only", "--no-renames", "-z",
            start, head, "--").stdout.split("\0")))
        if record.get("paths") != actual_paths:
            return False
        files, _exclusions = _workspace_files(workspace, reports)
        if set(files) != {entry["path"] for entry in entries}:
            return False
        for entry in entries:
            source, mode = files[entry["path"]]
            if mode != entry["mode"] or _safe_blob_identity(source, entry["size"]) != entry["blob"]:
                return False
        return True
    except (KeyError, OSError, TypeError, ValueError):
        return False


def collect_host_candidate(command, workspace: Path, start: str, task_id: str,
                           *, report_paths: Iterable[str] = (),
                           cached_candidate: HostCandidate | None = None) -> HostCandidate:
    """Snapshot safe edits into a deterministic commit after the producer stopped.

    The coder's index and HEAD are inputs only.  The candidate index, objects and
    commit all live under this execution's host-owned artifact directory.
    """
    if not _SHA.fullmatch(start):
        raise ValueError("host candidate start must be an exact commit")
    if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
        raise ValueError("host candidate task id is invalid")
    root = Path(command.run_dir).resolve()
    workspace = Path(workspace).absolute()
    if (workspace.is_symlink() or workspace.resolve() != workspace
            or not workspace.is_dir() or not workspace.is_relative_to(root)):
        raise ValueError("host candidate workspace must be a contained regular directory")
    reports = frozenset(_relative_path(value) for value in report_paths)
    if (cached_candidate is not None
            and _cached_candidate_matches(command, root, workspace, start, task_id,
                                          reports, cached_candidate)):
        return cached_candidate
    execution_parent = root / "artifacts" / "executions"
    execution_parent.mkdir(parents=True, exist_ok=True)
    _contained_directory(execution_parent, root, "host candidate execution parent")
    execution = execution_parent / command.command_id
    execution.mkdir(parents=True, exist_ok=True)
    _contained_directory(execution, root, "host candidate execution directory")
    repository = execution / "host-candidate.git"
    if repository.is_symlink() or (repository.exists() and not repository.is_dir()):
        raise ValueError("unsafe host candidate repository")
    if repository.exists():
        _contained_directory(repository, root, "host candidate repository")
    # git init is intentionally idempotent: recovery may find a directory
    # created by an interrupted initialization but no usable object database.
    result = handlers._exec(
        ["git", "-c", "init.defaultBranch=modport", "init", "--bare", "--template=", str(repository)],
        cwd=root, log=root / "logs" / f"development-{command.command_id}.log",
        timeout=handlers._remaining_timeout(command, 120),
        env={"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
             "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode:
        raise ValueError("could not create host candidate repository")
    _contained_directory(repository, root, "host candidate repository")
    if _git(command, repository, "cat-file", "-e", start + "^{commit}", check=False).returncode:
        # Fetch objects without a remote, FETCH_HEAD, or ref.  The coder never
        # needs write access to its .git directory for this read-only transfer.
        _git(command, repository, "fetch", "--no-tags", "--no-write-fetch-head",
             "--no-recurse-submodules", str(workspace), start)
    start_tree = _git(command, repository, "rev-parse", start + "^{tree}").stdout.strip()
    baseline: set[str] = set()
    for entry in _git(command, repository, "ls-tree", "-r", "-z", "--name-only", start).stdout.split("\0"):
        if entry:
            try:
                baseline.add(_relative_path(entry))
            except ValueError:
                # Existing input bytes remain in the base tree; unsafe names
                # are never read from the live workspace or newly collected.
                pass
    files, exclusions = _workspace_files(workspace, reports)
    excluded_paths = tuple(row["path"] for row in exclusions)
    index_descriptor, index_name = tempfile.mkstemp(prefix="host-candidate-index-", dir=execution)
    os.close(index_descriptor)
    Path(index_name).unlink()
    payload_directory = Path(tempfile.mkdtemp(
        prefix="host-candidate-payloads-", dir=execution))
    payload_paths: list[Path] = []
    environment = {"GIT_INDEX_FILE": index_name}
    try:
        _git(command, repository, "read-tree", start, environment=environment)
        index_updates: list[str] = []
        for relative in sorted(baseline - set(files)):
            if (_exclusion(relative, reports) is None
                    and not _below_exclusion(relative, excluded_paths)):
                index_updates.append("0 " + ("0" * 40) + "\t" + relative + "\0")
        total_bytes = 0
        file_entries = sorted(files.items())
        for position, (_relative, (source, _mode)) in enumerate(file_entries):
            handlers._remaining_timeout(command, 120)
            payload = payload_directory / f"{position:08d}"
            payload_paths.append(payload)
            total_bytes += _copy_regular(source, payload)
            if total_bytes > _MAX_TOTAL_BYTES:
                raise ValueError("host candidate exceeds the collection size limit")
        identities: list[str] = []
        if payload_paths:
            # Hash all host-owned copies in one Git process.  The copies are
            # made through _copy_regular first, so Git never reads the
            # coder's stopped workspace directly and cannot follow a link.
            identities = _git(
                command, repository, "hash-object", "-w", "--no-filters",
                "--stdin-paths", input_text="".join(
                    str(path) + "\n" for path in payload_paths)).stdout.splitlines()
        if len(identities) != len(file_entries):
            raise ValueError("host candidate blob count mismatch")
        for (relative, (_source, mode)), identity in zip(file_entries, identities):
            if not _SHA.fullmatch(identity):
                raise ValueError("invalid host candidate blob identity")
            index_updates.append(f"{mode} {identity}\t{relative}\0")
        if index_updates:
            # --index-info accepts both mode-0 deletions and blob additions;
            # submit them together so collection cost does not scale with the
            # number of changed files in separate Git process launches.
            _git(command, repository, "update-index", "-z", "--index-info",
                 environment=environment, input_text="".join(index_updates))
        tree = _git(command, repository, "write-tree", environment=environment).stdout.strip()
        if not _SHA.fullmatch(tree):
            raise ValueError("invalid host candidate tree identity")
        if tree == start_tree:
            head = start
        else:
            commit_environment = {
                **environment,
                "GIT_AUTHOR_NAME": "ModPort Host Collector",
                "GIT_AUTHOR_EMAIL": "modport@localhost",
                "GIT_COMMITTER_NAME": "ModPort Host Collector",
                "GIT_COMMITTER_EMAIL": "modport@localhost",
                "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
                "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
            }
            message = json.dumps({"command_id": command.command_id, "start": start,
                                  "task_id": task_id}, sort_keys=True, separators=(",", ":"))
            head = _git(command, repository, "commit-tree", tree, "-p", start, "-m",
                        "ModPort host candidate " + message,
                        environment=commit_environment).stdout.strip()
            if not _SHA.fullmatch(head):
                raise ValueError("invalid host candidate commit identity")
        paths = sorted(filter(None, _git(
            command, repository, "diff", "--name-only", "--no-renames", "-z", start, head,
            "--").stdout.split("\0")))
        ref_identity = sha256((command.command_id + "\0" + task_id).encode()).hexdigest()
        candidate_ref = "refs/modport/candidates/" + ref_identity + "/" + head
        _git(command, repository, "update-ref", candidate_ref, head)
        projection, projection_sha256 = _projection(command, repository, head, reports)
    finally:
        Path(index_name).unlink(missing_ok=True)
        for payload in payload_paths:
            payload.unlink(missing_ok=True)
        payload_directory.rmdir()
    record = {
        "schema_version": 1,
        "producer": "modport_host",
        "command_id": command.command_id,
        "task_id": task_id,
        "start": start,
        "head": head,
        "tree": tree,
        "paths": paths,
        "ref": candidate_ref,
        "repository": repository.relative_to(root).as_posix(),
        "excluded": exclusions,
        "excluded_count": len(exclusions),
        "collected_file_count": len(files),
        "collected_bytes": total_bytes,
        "safe_projection_file_count": len(projection),
        "safe_projection_sha256": projection_sha256,
    }
    return HostCandidate(repository, record)


def export_host_candidate(command, candidate: HostCandidate, destination: Path) -> bytes:
    """Create or verify the stable binary patch for a collected candidate."""
    root = Path(command.run_dir).resolve()
    repository_parent = _contained_directory(
        candidate.repository.parent, root, "host candidate export directory")
    destination = Path(destination).absolute()
    if destination.parent != repository_parent:
        raise ValueError("host candidate patch destination differs from its execution directory")
    temporary = destination.with_name(destination.name + ".host-collect.tmp")
    temporary.unlink(missing_ok=True)
    try:
        _git(command, candidate.repository, "diff", "--binary", "--full-index",
             "--no-ext-diff", "--no-textconv", "--no-renames",
             f"--output={temporary}", str(candidate.record["start"]), candidate.head, "--")
        data = temporary.read_bytes()
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or not destination.is_file() or destination.read_bytes() != data:
                raise ValueError("existing coder patch differs from host candidate")
        else:
            temporary.replace(destination)
        return data
    finally:
        temporary.unlink(missing_ok=True)


def materialize_host_candidate_view(command, candidate: HostCandidate,
                                    *, report_paths: Iterable[str] = ()) -> Path:
    """Materialize an authenticated safe projection of ``candidate``.

    Generated output, reports and credential locations stay absent from host
    checks.  A second deterministic collection around validation proves that
    this view still names the exported commit.
    """
    root = Path(command.run_dir).resolve()
    reports = frozenset(_relative_path(value) for value in report_paths)
    exclusions = candidate.record.get("excluded")
    if not isinstance(exclusions, list) or any(
            not isinstance(row, Mapping) or not isinstance(row.get("path"), str)
            for row in exclusions):
        raise ValueError("invalid host candidate exclusions")
    entries, projection_sha256 = _projection(
        command, candidate.repository, candidate.head, reports)
    if (candidate.record.get("safe_projection_sha256") != projection_sha256
            or candidate.record.get("safe_projection_file_count") != len(entries)):
        raise ValueError("host candidate safe projection identity mismatch")
    storage = root / "artifacts" / "host-candidate-views"
    storage.mkdir(parents=True, exist_ok=True)
    _contained_directory(storage, root, "host candidate view storage")
    directory = Path(tempfile.mkdtemp(prefix=command.command_id + "-", dir=storage))
    view = directory / "workspace"
    view.mkdir()
    execution = candidate.repository.parent
    descriptor, index_name = tempfile.mkstemp(prefix="host-candidate-view-index-", dir=execution)
    os.close(descriptor)
    Path(index_name).unlink()
    environment = {"GIT_INDEX_FILE": index_name, "GIT_WORK_TREE": str(view)}
    try:
        _git(command, candidate.repository, "read-tree", candidate.head,
             environment=environment)
        # checkout-index gets exact blobs from the host-owned object database.
        # Verify every resulting Git blob below so attributes or configuration
        # can never silently change the authenticated projection.
        paths = "".join(entry["path"] + "\0" for entry in entries)
        if paths:
            _git(command, candidate.repository, "checkout-index", "--force", "-z", "--stdin",
                 "--prefix=" + str(view) + "/", environment=environment, input_text=paths)
        observed_total = 0
        for entry in entries:
            handlers._remaining_timeout(command, 120)
            destination = view / entry["path"]
            if (destination.is_symlink() or not destination.resolve().is_relative_to(view.resolve())):
                raise ValueError("unsafe host candidate materialization path")
            attributes = destination.lstat()
            if (not stat.S_ISREG(attributes.st_mode) or attributes.st_nlink != 1
                    or attributes.st_size != entry["size"]):
                raise ValueError("unsafe host candidate materialized file")
            observed_total += attributes.st_size
            if observed_total > _MAX_TOTAL_BYTES:
                raise ValueError("host candidate materialization exceeds its size limit")
            digest = sha1(f"blob {attributes.st_size}\0".encode())
            with destination.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
            actual_mode = "100755" if attributes.st_mode & 0o111 else "100644"
            if digest.hexdigest() != entry["blob"] or actual_mode != entry["mode"]:
                raise ValueError("host candidate materialization differs from candidate commit")
    finally:
        Path(index_name).unlink(missing_ok=True)
    return view
