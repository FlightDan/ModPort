"""Small helpers for run artifacts and optional diagnostic identities.

Artifact references may carry a digest when the host creates them.  The
digest is diagnostic metadata; references resolve to the current regular
file in the isolated run and are never used as a checkout lock.  This keeps
normal stage progress from becoming a stale-hash failure.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
import json
from .platform_files import file_os as os
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Mapping

from .manifest import canonical_json
from .workspace import project_path, project_relative

_LOCK_FDS = ContextVar("modport_workspace_lock_fds", default=())


def current_lock_fds():
    return _LOCK_FDS.get()


def digest(value: Any) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def file_digest(path: Path) -> str:
    result = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def atomic_json(path: Path, value: Any):
    if os.name == "nt":
        from .platform_files import atomic_write
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path.absolute(), (canonical_json(value) + "\n").encode("utf-8"))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".modport-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(canonical_json(value) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def verified_path(root: Path, ref: Mapping[str, Any]) -> Path:
    """Resolve a contained regular artifact reference.

    ``sha256`` remains accepted in references written by older runs and is
    retained as metadata in new results.  Reading a reference only checks its
    path and file shape: the run owns the isolated files, so a later update is
    a new stage input rather than evidence of external tampering.
    """
    relative = Path(str(ref.get("path", "")))
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError("evidence path must be relative and contained")
    path = project_path(root, relative)
    if not path.is_file() or path.is_symlink() or path.resolve() != path.absolute():
        raise ValueError(f"evidence is not a contained regular file: {relative}")
    return path


def seal_ref(root: Path, ref: Mapping[str, Any], *, execution_id: str) -> dict:
    """Return a run-local artifact reference without creating a sealed copy.

    Earlier versions copied every reference into ``artifacts/objects`` and
    treated that copy as an immutable chain of custody.  Runs execute in an
    isolated workspace, so the copy added cost and made independent stages
    appear stale when they legitimately advanced their checkout.  Keep the
    public helper for compatibility, but point it at the already verified
    source and recompute its local reference metadata.
    """

    source = verified_path(root, ref)
    checksum = file_digest(source)
    return {
        "path": project_relative(root, source).as_posix(),
        "sha256": checksum,
        "media_type": ref.get("media_type", "application/octet-stream"),
        "metadata": {**dict(ref.get("metadata", {})),
                     "source_path": str(ref.get("metadata", {}).get("source_path", ref["path"])),
                     "execution_id": execution_id},
    }


def candidate_fingerprint(worktree: Path, *, host_generation: Any = None) -> str:
    """Return a cheap diagnostic version identity without scanning the tree.

    Git worktrees use their current ``HEAD``.  A non-Git workspace has no
    repository revision, so its contained absolute path is the stable identity
    available to callers; hosts that have a task generation can supply it
    explicitly.  The value is diagnostic metadata only and must not be used as
    a content or workspace consistency gate.
    """
    if host_generation is not None:
        if isinstance(host_generation, bool) or not isinstance(host_generation, (str, int)):
            raise ValueError("host generation must be a string or integer")
        generation = str(host_generation).strip()
        if not generation:
            raise ValueError("host generation must be non-empty")
        return generation
    if (worktree / ".git").exists():
        from .telemetry import probe_process
        result = probe_process(["git", "rev-parse", "HEAD"], cwd=worktree,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, timeout=15)
        if result.returncode:
            raise ValueError("cannot read candidate Git identity")
        head = result.stdout.strip()
        if not head:
            raise ValueError("candidate Git identity is empty")
        return head
    return str(worktree.resolve())


class WorkspaceLockTimeout(TimeoutError):
    """A worker exhausted its bounded wait for an occupied workspace."""


@contextmanager
def workspace_lock(root: Path, *, blocking: bool = True, timeout_seconds=None,
                   on_wait=None, poll_seconds: float = 0.25):
    """Serialize stage handlers and refuse recovery while a handler holds it.

    Production execution can bound lock acquisition by its remaining SDK
    deadline. The default remains the historical blocking behavior, and
    read-only recovery continues to use ``blocking=False``.
    """
    from . import platform_files as fcntl
    import math
    import time
    if timeout_seconds is not None and (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds) or timeout_seconds < 0):
        raise ValueError("workspace lock timeout must be finite and nonnegative")
    if (isinstance(poll_seconds, bool) or not isinstance(poll_seconds, (int, float))
            or not math.isfinite(poll_seconds) or poll_seconds <= 0):
        raise ValueError("workspace lock poll interval must be positive")
    root.mkdir(parents=True, exist_ok=True)
    from .platform_files import safe_open
    descriptor = safe_open(root.absolute(), ".workspace.lock", os.O_CREAT | os.O_RDWR)
    with os.fdopen(descriptor, "r+b") as handle:
        if timeout_seconds is None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        else:
            deadline = time.monotonic() + timeout_seconds
            first = True
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if first and timeout_seconds == 0:
                        raise WorkspaceLockTimeout("workspace lock wait expired (workspace_wait_timeout)")
                    first = False
                    if callable(on_wait):
                        try:
                            on_wait({"reason": "workspace_lock_busy",
                                     "sampled_at": time.time(),
                                     "waited_seconds": max(0.0, timeout_seconds - max(
                                         0.0, deadline - time.monotonic()))})
                        except Exception:
                            pass
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise WorkspaceLockTimeout(
                            "workspace lock wait expired (workspace_wait_timeout)")
                    time.sleep(min(float(poll_seconds), remaining))
        token = _LOCK_FDS.set((*_LOCK_FDS.get(), handle.fileno()))
        try:
            yield
        finally:
            _LOCK_FDS.reset(token)
            # Closing this descriptor releases only our reference. Inherited
            # descriptors keep the shared flock until SDK cleanup reaps writers.


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))
