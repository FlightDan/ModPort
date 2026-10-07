"""Validate artifact-only imports without inheriting scheduler state."""

from pathlib import Path

from .evidence import verified_path


def handoff_harness_files(manifest: dict) -> list[dict]:
    """Select source inputs for an explicitly requested harness restoration.

    Ordinary handoffs remain historical context. This selection never imports
    test results, review approvals, locks or SDK scheduling state.
    """
    from .retry_policy import _harness_path, is_harness_source
    files = []
    for item in manifest["artifacts"]:
        source = item["source_path"]
        if not source.startswith("baseline/.modport/"):
            continue
        relative = source.removeprefix("baseline/")
        if not is_harness_source(relative):
            continue
        _harness_path(relative)
        files.append({"path": relative, "source_path": source})
    if not any(item["path"] == ".modport/functional-contract.json" for item in files):
        raise ValueError("artifact handoff harness requires a selected baseline contract")
    return files


def install_handoff_harness(root: Path, manifest: dict, refs: dict) -> dict:
    """Adapt authenticated artifact inputs to the existing restore stage."""
    from .evidence import atomic_json, file_digest
    files = []
    inherited_refs = {}
    for item in handoff_harness_files(manifest):
        ref = refs["handoff:" + item["source_path"]]
        verified_path(root, ref)
        files.append({"path": item["path"], "ref": ref})
        inherited_refs["inherited_harness:" + item["path"]] = ref
    snapshot = {
        "schema_version": 1,
        "source_commit": manifest["source"]["source_commit"],
        "parent_run_id": manifest["source"]["run_id"],
        "provenance_ref": refs["artifact_handoff"],
        "requires_fresh_verification": True,
        "acceptance_status": "unverified",
        "scheduler_history_imported": False,
        "files": files,
    }
    target = root / "artifacts" / "inherited-harness.json"
    atomic_json(target, snapshot)
    inherited_refs["inherited_harness"] = {
        "path": target.relative_to(root).as_posix(),
        "sha256": file_digest(target), "media_type": "application/json"}
    return inherited_refs


def validate_handoff_request(manifest: dict, request: dict) -> None:
    source = manifest["source"]
    prior = source["request"]
    if request.get("source_revision") != source["source_commit"]:
        raise ValueError("artifact handoff requires its exact baseline source commit")
    for key in ("mod_id", "source_repository", "source_minecraft", "target_minecraft",
                "source_loader", "target_loader", "source_loader_version",
                "target_loader_version", "source_java", "target_java", "mdk_revision"):
        if request.get(key) != prior.get(key):
            raise ValueError(f"artifact handoff request differs from its provenance: {key}")


def source_handoff(root: Path, command):
    """Resolve the authenticated local Git bundle for the source handler."""
    reference = command.artifact_refs.get("artifact_handoff")
    if reference is None:
        return None
    from .artifact_handoff import validate_handoff
    path = verified_path(root, reference)
    expected = root / "artifacts" / "handoff" / "manifest.json"
    if path != expected:
        raise ValueError("artifact handoff must use the installed manifest")
    manifest = validate_handoff(path.parent)
    request = command.payload.get("request", command.payload)
    validate_handoff_request(manifest, request)
    return manifest
