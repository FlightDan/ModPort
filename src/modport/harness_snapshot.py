"""Archive and restore harness source, never successful runtime evidence."""
import json
import os
from pathlib import Path

from .evidence import atomic_json, file_digest, seal_ref, verified_path
from .retry_policy import is_harness_source, _harness_path
from .repair_evidence import snapshot_repair_evidence


def capture_harness(command):
    """Called under the host operation lock after a baseline author finishes."""
    root = Path(command.run_dir)
    baseline = root / "baseline"
    directory = baseline / ".modport"
    if directory.is_symlink() or not directory.is_dir() or directory.resolve() != directory.absolute():
        raise ValueError("unsafe baseline harness directory")
    source = json.loads(verified_path(root, command.artifact_refs["source_evidence"]).read_text())
    refs = {}
    for parent, names, filenames in os.walk(directory, followlinks=False):
        current = Path(parent)
        for name in [*names, *filenames]:
            path = current / name
            if path.is_symlink():
                raise ValueError("harness snapshot cannot contain symlinks")
        names[:] = sorted(name for name in names
                          if is_harness_source((current / name).relative_to(baseline).as_posix()))
        for name in sorted(filenames):
            path = current / name
            relative = path.relative_to(baseline).as_posix()
            if not is_harness_source(relative):
                continue
            _harness_path(relative)
            if not path.is_file():
                raise ValueError("harness snapshot requires regular files")
            # A later author removes required outputs before regenerating them.
            # Preserve these historical bytes while they still exist, instead
            # of leaving the manifest dependent on the mutable checkout.
            refs[relative] = snapshot_repair_evidence(root, seal_ref(root, {
                "path": path.relative_to(root).as_posix(), "sha256": file_digest(path)},
                execution_id=command.command_id))
    candidate = refs.get(".modport/functional-contract.json")
    if candidate is None:
        raise ValueError("harness snapshot requires a candidate contract")
    manifest = {"schema_version": 1, "source_commit": source["source_commit"],
                "candidate_sha256": candidate["sha256"], "files": refs}
    target = root / "artifacts" / "executions" / command.command_id / "baseline-harness-snapshot.json"
    atomic_json(target, manifest)
    return {"baseline_harness_snapshot": seal_ref(root, {
        "path": target.relative_to(root).as_posix(), "sha256": file_digest(target),
        "media_type": "application/json"}, execution_id=command.command_id),
        **{"baseline_harness_source:" + relative: ref for relative, ref in refs.items()}}


class RestoreHarnessHandler:
    """Restore an explicitly inherited source snapshot before fresh verification."""

    def __call__(self, command):
        from . import handlers
        root = Path(command.run_dir)
        baseline = root / "baseline"
        try:
            manifest = json.loads(verified_path(root, command.artifact_refs["inherited_harness"]).read_text())
            if (not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int
                    or manifest.get("schema_version") != 1
                    or manifest.get("requires_fresh_verification") is not True
                    or not isinstance(manifest.get("files"), list)
                    or not isinstance(manifest.get("parent_run_id"), str)
                    or not manifest["parent_run_id"].strip()):
                raise ValueError("inherited snapshot schema is invalid")
            source = json.loads(verified_path(root, command.artifact_refs["source_evidence"]).read_text())
            if (manifest.get("source_commit") != source["source_commit"] or
                    not isinstance(source.get("source_commit"), str)):
                raise ValueError("inherited snapshot belongs to a different source")
            if not isinstance(manifest.get("provenance_ref"), dict):
                raise ValueError("inherited snapshot provenance is missing")
            verified_path(root, manifest["provenance_ref"])
            writes = {}
            for entry in manifest["files"]:
                if not isinstance(entry, dict) or not isinstance(entry.get("ref"), dict):
                    raise ValueError("inherited harness file entry is invalid")
                relative = str(_harness_path(entry["path"]))
                if relative in writes:
                    raise ValueError("duplicate inherited harness path")
                ref = entry["ref"]
                authoritative = command.artifact_refs.get("inherited_harness:" + relative)
                if not isinstance(authoritative, dict):
                    raise ValueError("inherited harness file has no authenticated input")
                # The child command owns the current input reference.  The
                # manifest's digest is historical metadata and must not make
                # an updated inherited file look like tampering.
                data = verified_path(root, authoritative).read_bytes()
                target = baseline / relative
                if (target.resolve() != target.absolute() or target.is_symlink()
                        or (target.exists() and not target.is_file())):
                    raise ValueError("inherited harness path is unsafe")
                for parent in target.parents:
                    if parent == root:
                        break
                    if parent.exists() and not parent.is_dir():
                        raise ValueError("inherited harness parent is not a directory")
                writes[relative] = data
            if ".modport/functional-contract.json" not in writes:
                raise ValueError("inherited contract is missing")
            for relative in writes:
                if any(parent.as_posix() in writes for parent in Path(relative).parents):
                    raise ValueError("inherited harness file conflicts with a parent directory")
            # Validate all destinations and bytes before making any changes.
            for relative, data in writes.items():
                target = baseline / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            key, candidate_ref = handlers._snapshot_stage_output(root, baseline, command,
                ".modport/functional-contract.json")
            refs = {key: candidate_ref, **capture_harness(command)}
            from .business_policy import compile_package_scope
            identity_only = (28 <= command.options.get("workflow_version", 0) < 31
                             and compile_package_scope(command))
            return handlers._result(command, "completed", outputs={"artifact_refs": refs,
                "inherited_from": manifest["parent_run_id"], "requires_fresh_verification": True,
                "verification_scope": "source_identity_only" if identity_only else "baseline_behavior"},
                detail=("authenticated harness restored; fresh source identity check required"
                        if identity_only else
                        "authenticated harness restored; fresh baseline verification required"))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            return handlers._result(command, "blocked", detail=str(exc), error_code="inherited_harness_invalid")
