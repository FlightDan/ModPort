"""Strict definition allowlist for workflow execution-segment upgrades."""

from collections.abc import Mapping
import json
import os
from pathlib import Path
import re
import tempfile

from .evidence import seal_ref
from .models import MigrationRequest
from .repair_evidence import snapshot_repair_evidence
from .workflow import WORKFLOW_VERSION, WorkflowDefinition, compile_migration_workflow


UPGRADE_SOURCE_WORKFLOW_VERSION = 42
UPGRADE_TARGET_WORKFLOW_VERSION = WORKFLOW_VERSION


def _unique_policy_fields(pairs):
    policy = {}
    for key, value in pairs:
        if key in policy:
            raise ValueError('private upgrade policy contains duplicate fields')
        policy[key] = value
    return policy


def _selected_upgrade_source_run_id():
    """Select the one carried predecessor from private host configuration."""
    identifier = os.environ.get('MODPORT_CARRIED_RUN_ID')
    if identifier is None:
        policy_path = Path(__file__).resolve().parents[2] / 'private-upgrade-policy.json'
        try:
            text = policy_path.read_text(encoding='utf-8')
        except FileNotFoundError:
            return None
        try:
            policy = json.loads(text, object_pairs_hook=_unique_policy_fields)
        except (ValueError, UnicodeError) as error:
            raise ValueError('private upgrade policy must be valid JSON') from error
        if not isinstance(policy, dict) or set(policy) != {'carried_run_id'}:
            raise ValueError('private upgrade policy requires only carried_run_id')
        identifier = policy['carried_run_id']
    if (not isinstance(identifier, str)
            or not re.fullmatch(r'[A-Za-z0-9_.:-]+', identifier)
            or identifier in {'.', '..'}):
        raise ValueError('carried predecessor must have a valid Run identity')
    return identifier


UPGRADE_SOURCE_RUN_ID = _selected_upgrade_source_run_id()


def publish_upgrade_rules(root, segment_id):
    """Freeze installed procedural rules without changing diagnostic evidence."""
    if (not isinstance(segment_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]+", segment_id)
            or segment_id in {".", ".."}):
        raise ValueError("unsafe workflow upgrade segment identity")
    root = Path(root).resolve()
    directory = root / "artifacts" / "continuations" / segment_id / "procedural-rules"
    if directory.is_symlink() or directory.resolve() != directory.absolute():
        raise ValueError("unsafe workflow upgrade rules path")
    directory.mkdir(parents=True, exist_ok=True)
    refs = {}
    for key, name in (("agent_rules", "AGENT_RULES.md"), ("evidence_protocol", "EVIDENCE_PROTOCOL.md")):
        data = (Path(__file__).parent / "rules" / name).read_bytes()
        target = directory / name
        descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".rule-")
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                if target.is_symlink() or not target.is_file() or target.read_bytes() != data:
                    raise ValueError("workflow upgrade rule already differs for this segment")
        finally:
            os.unlink(temporary)
        ref = seal_ref(root, {"path": target.relative_to(root).as_posix(),
                             "media_type": "text/markdown",
                             "metadata": {"procedural_rule": key, "workflow_version": WORKFLOW_VERSION}},
                       execution_id="workflow-upgrade:" + segment_id)
        refs[key] = snapshot_repair_evidence(root, ref)
    return refs


def validate_upgrade_definition(header):
    """Return the canonical target after authenticating a known predecessor.

    The comparison covers the complete persisted definition.  A matching
    version number alone never authorizes an upgrade.
    """
    if not isinstance(header, Mapping):
        raise TypeError("workflow upgrade header must be a mapping")
    request_value = header.get("request")
    source = header.get("definition")
    if not isinstance(request_value, Mapping) or not isinstance(source, Mapping):
        raise ValueError("workflow upgrade requires request and definition mappings")

    request = MigrationRequest.from_mapping(request_value)
    canonical_request = request.to_dict()
    # This carried Run froze its inputs before the optional Wiki setting was
    # serialized. Preserve that omission instead of rewriting its frozen request.
    selected_predecessor = (UPGRADE_SOURCE_RUN_ID is not None
                            and header.get('run_id') == UPGRADE_SOURCE_RUN_ID)
    if ('wiki_enabled' not in request_value
            and (selected_predecessor or source.get('workflow_version') == WORKFLOW_VERSION)):
        canonical_request.pop('wiki_enabled', None)
    if canonical_request != dict(request_value):
        raise ValueError("workflow upgrade request is not canonical")
    current = compile_migration_workflow(request).to_dict()
    current['request'] = canonical_request
    if WORKFLOW_VERSION != UPGRADE_TARGET_WORKFLOW_VERSION or current.get(
            "workflow_version") != UPGRADE_TARGET_WORKFLOW_VERSION:
        raise RuntimeError("workflow upgrade allowlist must be revised for the current workflow")
    version = source.get("workflow_version")
    if version == WORKFLOW_VERSION and dict(source) == current:
        return current
    if UPGRADE_SOURCE_RUN_ID is None or header.get("run_id") != UPGRADE_SOURCE_RUN_ID:
        raise ValueError("only the explicitly carried predecessor Run may upgrade")
    predecessor = WorkflowDefinition(canonical_request, version=UPGRADE_SOURCE_WORKFLOW_VERSION).to_dict()
    if (type(version) is not int or version != UPGRADE_SOURCE_WORKFLOW_VERSION
            or dict(source) != predecessor):
        raise ValueError("workflow definition is not an exact supported upgrade source")
    return current
