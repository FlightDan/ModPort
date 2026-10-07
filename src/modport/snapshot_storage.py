"""Bound the disk cost of observational SDK backups.

SDK 0.6 has no read-only Orchestrator constructor. Small observations still use
its public backup API; large stores must not be copied implicitly by a poll.
"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
if os.name != "nt":
    import resource
import shutil
import signal
import stat
import subprocess
import sys
import tempfile


MAX_SNAPSHOT_BYTES = 256 * 1024 * 1024
MIN_FREE_BYTES = 1024 * 1024 * 1024
COPY_TIMEOUT_SECONDS = 60


class SnapshotLimitError(ValueError):
    """An observation cannot safely allocate an SDK snapshot."""


def _source_bytes(sources):
    total = 0
    for source in sources:
        for suffix in ("", "-wal", "-journal"):
            path = Path(str(source) + suffix)
            try:
                info = path.lstat()
            except FileNotFoundError:
                if not suffix:
                    raise
                continue
            if not stat.S_ISREG(info.st_mode):
                raise SnapshotLimitError("snapshot source must be a regular file")
            total += info.st_size
    return total


def _admit(sources, destination, limit):
    size = _source_bytes(sources)
    if size > limit:
        raise SnapshotLimitError(
            f"status snapshot refused: database and WAL/journal total {size} bytes "
            f"exceeds the {limit}-byte observation limit; no database was copied. "
            "Use an existing exported observation (which may be stale) or an "
            "SDK with a public read-only reader."
        )
    # Reserve the hard copy ceiling, not just the current source size: a live
    # source can grow after this check. Allow another ceiling for SQLite's
    # temporary journal; the completed databases share one total budget.
    required = 2 * limit + MIN_FREE_BYTES
    free = shutil.disk_usage(destination).free
    if free < required:
        raise SnapshotLimitError(
            f"status snapshot refused: {free} bytes free; {required} required "
            "including the observation budget and free-space reserve"
        )


@contextmanager
def _observation_lock():
    # Shared by CLI and all web processes, independent of Run and TMPDIR.
    # Leave the tiny lock file in place: unlinking it would split the lock.
    from . import platform_files as fcntl
    path = Path("/tmp") / f".modport-observation-{os.getuid()}.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise SnapshotLimitError("unsafe observation lock")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SnapshotLimitError("another status snapshot is already in progress") from error
        yield descriptor
    finally:
        os.close(descriptor)


def _copy_databases(sources, destination, limit):
    """Run only in a disposable child; never set process-wide limits in a host."""
    from dispatcher_sdk.storage import backup_database

    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    used = 0
    for source in sources:
        remaining = limit - used
        if remaining <= 0:
            raise SnapshotLimitError("status snapshot exceeded its total byte budget")
        # Enforced by the OS during backup, including growth after admission.
        resource.setrlimit(resource.RLIMIT_FSIZE, (remaining, remaining))
        target = destination / source.name
        backup_database(source, target)
        used += target.stat().st_size
        if shutil.disk_usage(destination).free < MIN_FREE_BYTES:
            raise SnapshotLimitError("status snapshot exhausted its free-space reserve")


def _copy_main():
    # The parent may be killed while waiting. The child must not keep the
    # shared observation lock forever in that case.
    signal.alarm(COPY_TIMEOUT_SECONDS)
    try:
        expected = json.loads(sys.argv[3])
        package = Path(expected["module_path"]).resolve().parent
        sys.path.insert(0, str(package.parent))
        import dispatcher_sdk
        observed = dispatcher_sdk.runtime_identity().to_dict()["module"]
        if observed != expected:
            raise SnapshotLimitError("snapshot helper SDK identity differs from the host")
        _copy_databases([Path(p) for p in sys.argv[4:]], Path(sys.argv[1]), int(sys.argv[2]))
    except Exception as error:
        # Never include database contents or unbounded exception strings.
        print(type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
    finally:
        signal.alarm(0)


@contextmanager
def snapshot_databases(sources, *, prefix="modport-observation-"):
    """Yield bounded, temporary SDK backups; keep the shared lock until cleanup."""
    if os.name == "nt":
        raise SnapshotLimitError(
            "bounded SDK backup observations require a native Windows disk quota backend; "
            "use the SDK public read-only availability and summary inspectors")
    sources = tuple(Path(source).absolute() for source in sources)
    if not sources or len({source.name for source in sources}) != len(sources):
        raise ValueError("snapshot sources must have distinct filenames")
    limit = MAX_SNAPSHOT_BYTES
    temporary_root = Path(tempfile.gettempdir())
    # Refuse huge requests without creating even a temporary directory.
    _admit(sources, temporary_root, limit)
    with _observation_lock() as lock:
        _admit(sources, temporary_root, limit)
        from .sdk_compat import sdk_module_identity
        identity = sdk_module_identity()
        with tempfile.TemporaryDirectory(prefix=prefix) as temporary:
            destination = Path(temporary)
            environment = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL")
                           if key in os.environ}
            try:
                result = subprocess.run([
                    sys.executable, "-I", str(Path(__file__).resolve()),
                    str(destination), str(limit), json.dumps(identity),
                    *(str(source) for source in sources),
                ], env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE, timeout=COPY_TIMEOUT_SECONDS, check=False,
                    pass_fds=(lock,))
            except subprocess.TimeoutExpired as error:
                raise SnapshotLimitError("status snapshot timed out; temporary copies removed") from error
            if result.returncode:
                raise SnapshotLimitError(
                    "status snapshot failed or exceeded its byte budget; temporary copies removed"
                )
            if _source_bytes(destination / source.name for source in sources) > limit:
                raise SnapshotLimitError("status snapshot exceeded its total byte budget")
            yield destination


if __name__ == "__main__":
    _copy_main()
