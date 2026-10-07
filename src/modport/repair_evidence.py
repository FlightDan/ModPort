"""Byte snapshots scoped to repair history, independent of normal stage refs."""
import os
from hashlib import sha256
from pathlib import Path
import tempfile

from .evidence import verified_path
from .workspace import project_relative


class RepairEvidenceError(ValueError):
    """A repair snapshot cannot be constructed safely."""


def is_repair_artifact_ref(value):
    """Distinguish Run refs from characterization's workspace record summaries."""
    return (isinstance(value, dict) and "path" in value
            and ("sha256" in value or "media_type" in value)
            and not {"evidence_kind", "executor", "runtime_operations"} <= value.keys())


def snapshot_repair_evidence(root, value):
    """Copy referenced regular files and return a detached, remapped value.

    Missing/unsafe sources fail closed. Existing content objects are validated
    and reused; ordinary references' diagnostic hashes are never enforced.
    """
    root = Path(root).resolve()
    cache = {}

    def visit(item):
        if isinstance(item, list):
            return [visit(child) for child in item]
        if not isinstance(item, dict):
            return item
        if not is_repair_artifact_ref(item):
            return {key: visit(child) for key, child in item.items()}
        source = verified_path(root, item)
        if source not in cache:
            data = source.read_bytes()
            checksum = sha256(data).hexdigest()
            source_relative = project_relative(root, source)
            if (source_relative.parts[:2] == ("artifacts", "repair-evidence")
                    and source_relative != Path("artifacts/repair-evidence") / checksum / "evidence"):
                raise ValueError("repair evidence object changed")
            relative = Path("artifacts/repair-evidence") / checksum / "evidence"
            target = root / relative
            # Check parents before writing: repair objects must stay in the Run.
            if any(parent.is_symlink() for parent in target.parents if parent != root):
                raise ValueError("repair evidence directory must not contain symlinks")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                existing = verified_path(root, {"path": relative.as_posix()})
                if existing.read_bytes() != data:
                    raise ValueError("repair evidence object changed")
            else:
                fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=".repair-")
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, target)
                    directory = os.open(target.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            cache[source] = (relative.as_posix(), checksum)
        path, checksum = cache[source]
        return {**item, "path": path, "sha256": checksum}

    try:
        return visit(value)
    except (OSError, ValueError) as error:
        raise RepairEvidenceError(str(error)) from error
