"""Host-owned project gap state and disposable human-readable projections."""
from pathlib import Path
from copy import deepcopy
import hashlib

from .evidence import atomic_json
from .analysis_contract import HOST_GAP_FIELDS, encoded_gap_id


VERIFICATION_STAGES = frozenset({'target_build', 'test_execute', 'client_smoke'})


def verification_stage(row, *, require_due=False):
    """Use one stage value; never validate one field and execute another."""
    stage = row.get('due_stage') if require_due else row.get('resolution_stage', row.get('due_stage'))
    if not isinstance(stage, str) or stage not in VERIFICATION_STAGES:
        raise ValueError('invalid verification requirement stage')
    for field in ('due_stage', 'resolution_stage'):
        if field in row and row[field] != stage:
            raise ValueError('verification requirement stage fields conflict')
    return stage


def merge_verification_obligation(existing, incoming):
    """Supplement a host obligation without replacing its scope or evidence."""
    stage = verification_stage(incoming)
    if existing and verification_stage(existing) != stage:
        raise ValueError('existing verification obligation stage cannot change')
    row = deepcopy(incoming)
    if existing:
        row.update(deepcopy(existing))
        for field in ('closure_criteria', 'affected_tasks', 'evidence', 'usage_locations'):
            if field in existing or field in incoming:
                values = deepcopy(existing.get(field, []))
                for value in incoming.get(field, []):
                    if value not in values:
                        values.append(deepcopy(value))
                row[field] = values
    row.setdefault('applicable', True)
    row.update(kind='verification', project_status='pending', resolution_stage=stage)
    row.setdefault('status', 'unresolved')
    if existing and existing.get('applicable') is False and incoming.get('applicable') is True:
        row.update(applicable=True, status='unresolved', verification_status='pending')
    if existing and row.get('closure_criteria') != existing.get('closure_criteria'):
        row.update(status='unresolved', verification_status='pending')
    if 'due_stage' in row:
        row['due_stage'] = stage
    return row


def _disambiguated_requirement_id(identity, parent, reserved):
    """Derive a stable host identity without changing the submitted identity."""
    digest = hashlib.sha256((identity + "\0" + parent).encode("utf-8")).hexdigest()
    for length in range(12, len(digest) + 1, 4):
        candidate = f"{identity}:parent-{digest[:length]}"
        if candidate not in reserved:
            return candidate
    suffix = 2
    while f"{identity}:parent-{digest}:{suffix}" in reserved:
        suffix += 1
    return f"{identity}:parent-{digest}:{suffix}"


def normalize_reviewed_requirements(obligations, requirements, research_gaps=None):
    """Merge compatible duplicates and assign stable IDs to parent collisions.

    The returned rows contain only validated requirement fields plus private
    source records for ``merge_reviewed_requirements``. Input rows are never
    modified, and existing host evidence is not treated as model-owned input.
    """
    if (not isinstance(obligations, dict)
            or research_gaps is not None and not isinstance(research_gaps, dict)):
        raise ValueError('project requirements must be mappings')
    if not isinstance(requirements, list):
        raise ValueError('verification_requirements must be an array')
    parents = set(obligations).union(research_gaps or {})
    grouped = {}
    order = []
    for index, requirement in enumerate(requirements):
        if not isinstance(requirement, dict):
            raise ValueError('verification requirement must be an object')
        identity = requirement.get('gap_id', requirement.get('id'))
        parent = requirement.get('research_gap_id')
        if (not isinstance(identity, str) or not identity.strip()
                or ('id' in requirement and requirement['id'] != identity)
                or not isinstance(parent, str) or not parent.strip()):
            raise ValueError('invalid verification requirement identity')
        if parent not in parents:
            raise ValueError('reviewed verification requirement references an unknown gap')
        stage = verification_stage(requirement, require_due=True)
        criteria = requirement.get('closure_criteria')
        if (not isinstance(criteria, list) or not criteria
                or any(not isinstance(value, str) or not value.strip() for value in criteria)):
            raise ValueError('verification requirement needs nonempty closure criteria')
        key = (identity, parent)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append({
            'source_index': index,
            'gap_id': identity,
            'research_gap_id': parent,
            'due_stage': stage,
            'resolution_stage': stage,
            'closure_criteria': deepcopy(criteria),
        })

    by_identity = {}
    for identity, parent in grouped:
        by_identity.setdefault(identity, set()).add(parent)
    reserved = set(obligations).union(by_identity)
    canonical = {}
    for identity in sorted(by_identity):
        incoming_parents = by_identity[identity]
        existing_by_parent = {}
        for existing_id, row in obligations.items():
            if not isinstance(row, dict):
                continue
            if existing_id != identity and row.get('source_gap_id') != identity:
                continue
            parent = row.get('research_gap_id')
            if parent is None and existing_id == identity and identity in parents:
                parent = identity
            if parent not in incoming_parents:
                continue
            if parent in existing_by_parent and existing_by_parent[parent] != existing_id:
                raise ValueError('duplicate host requirement binding for submitted identity')
            existing_by_parent[parent] = existing_id
        for parent, existing_id in existing_by_parent.items():
            if existing_id in by_identity and existing_id != identity:
                raise ValueError('normalized requirement identity conflicts with submitted identity')
            canonical[(identity, parent)] = existing_id
        if identity not in obligations and not any(
                existing_id == identity for existing_id in existing_by_parent.values()):
            owner = min(incoming_parents)
            canonical[(identity, owner)] = identity
        for parent in sorted(incoming_parents):
            key = (identity, parent)
            if key in canonical:
                reserved.add(canonical[key])
                continue
            candidate = _disambiguated_requirement_id(identity, parent, reserved)
            canonical[key] = candidate
            reserved.add(candidate)

    normalized = []
    for key in order:
        identity, parent = key
        sources = grouped[key]
        stages = {source['due_stage'] for source in sources}
        if len(stages) != 1:
            raise ValueError('duplicate verification requirement stages conflict')
        criteria = []
        for source in sources:
            for value in source['closure_criteria']:
                if value not in criteria:
                    criteria.append(value)
        assigned = canonical[key]
        row = {'gap_id': assigned, 'research_gap_id': parent,
               'due_stage': sources[0]['due_stage'],
               'resolution_stage': sources[0]['resolution_stage'],
               'closure_criteria': criteria,
               '_requirement_sources': sources}
        if assigned != identity:
            row['source_gap_id'] = identity
        normalized.append(row)
    return normalized


def merge_reviewed_requirements(obligations, requirements, execution_id, research_gaps=None,
                                *, normalize_duplicates=False):
    """Validate the complete batch before returning an updated host mapping."""
    if not isinstance(requirements, list):
        raise ValueError('verification_requirements must be an array')
    if normalize_duplicates:
        requirements = normalize_reviewed_requirements(
            obligations, requirements, research_gaps)
    merged = deepcopy(obligations)
    seen = set()
    for requirement in requirements:
        if not isinstance(requirement, dict):
            raise ValueError('verification requirement must be an object')
        identity = requirement.get('gap_id', requirement.get('id'))
        parent = requirement.get('research_gap_id')
        if (not isinstance(identity, str) or not identity.strip() or identity in seen
                or ('id' in requirement and requirement['id'] != identity)
                or not isinstance(parent, str) or not parent.strip()):
            raise ValueError('invalid or duplicate verification requirement identity')
        if parent not in obligations and parent not in (research_gaps or {}):
            raise ValueError('reviewed verification requirement references an unknown gap')
        stage = verification_stage(requirement, require_due=True)
        criteria = requirement.get('closure_criteria')
        if (not isinstance(criteria, list) or not criteria
                or any(not isinstance(value, str) or not value.strip() for value in criteria)):
            raise ValueError('verification requirement needs nonempty closure criteria')
        existing = obligations.get(identity)
        if existing and existing.get('research_gap_id', parent) != parent:
            raise ValueError('existing verification obligation research binding cannot change')
        # Only the declared review fields enter host state. Arbitrary model
        # fields cannot replace provenance, task scope, or verification status.
        incoming = {'gap_id': identity, 'research_gap_id': parent, 'due_stage': stage,
                    'resolution_stage': stage, 'closure_criteria': deepcopy(criteria)}
        if normalize_duplicates and requirement.get('source_gap_id') != identity:
            source_gap_id = requirement.get('source_gap_id')
            if isinstance(source_gap_id, str) and source_gap_id:
                incoming['source_gap_id'] = source_gap_id
        source = obligations.get(parent, (research_gaps or {}).get(parent, {}))
        if source.get('affected_tasks'):
            incoming['affected_tasks'] = deepcopy(source['affected_tasks'])
        row = merge_verification_obligation(existing, incoming)
        row.update(status='unresolved', verification_status='pending')
        reviews = row.setdefault('requirement_reviews', [])
        contributions = (requirement.get('_requirement_sources', [])
                         if normalize_duplicates else [incoming])
        for contribution in contributions:
            review = {'execution_id': execution_id,
                      'gap_id': identity,
                      'research_gap_id': parent,
                      'due_stage': stage,
                      'resolution_stage': stage,
                      'closure_criteria': deepcopy(contribution['closure_criteria'])}
            if normalize_duplicates:
                review['source_index'] = contribution['source_index']
                if contribution['gap_id'] != identity:
                    review['source_gap_id'] = contribution['gap_id']
            if review not in reviews:
                reviews.append(review)
        merged[identity] = row
        seen.add(identity)
    return merged


def gap_id(row):
    entry = row.get("entry_id")
    if entry is not None:
        if not isinstance(entry, str) or not entry.strip() or row.get("kind") not in ("knowledge", "verification"):
            raise ValueError("gap identity requires kind and nonblank entry_id")
        identity = row["kind"] + ":" + entry
        qualified = row["kind"] + ":" + str(row.get("skill", "")) + ":" + entry
        supplied = row.get("gap_id", identity)
        encoded = encoded_gap_id(row["kind"], row.get("skill"), entry)
        if supplied != identity and (not row.get("skill") or supplied not in (qualified, encoded)):
            raise ValueError("gap_id must equal kind:entry_id, kind:skill:entry_id or the host-published encoded tuple ID")
        return supplied
    if isinstance(row.get("gap_id"), str) and row["gap_id"].strip():
        return row["gap_id"]
    if isinstance(row.get("skill"), str) and type(row.get("index")) is int and row["index"] >= 0:
        return f"{row['skill']}:{row['index']}"
    raise ValueError("gap identity requires entry_id or legacy skill/index")


def license_header_only(row):
    """Root-license policy excludes historical header discrepancies from research."""
    issue = row.get("issue_type")
    if issue in ("license_header_conflict", "legacy_license_header", "historical_license_header"):
        return True
    if issue is not None:
        return False
    text = " ".join(str(row.get(key, "")) for key in ("question", "missing_information", "existing_answer")).lower()
    return "license" in text and "header" in text and any(word in text for word in ("conflict", "historical", "legacy"))


def project_gap_rows(assessments):
    research, verification = [], []
    for original in assessments:
        row = {key: value for key, value in original.items() if key not in HOST_GAP_FIELDS}
        row["gap_id"] = gap_id(row)
        row["project_status"] = row.get("status", "unresolved")
        row.setdefault("attempted_alternatives", [])
        if row["kind"] == "knowledge":
            if not license_header_only(row):
                research.append(row)
        else:
            row.setdefault("verification_status", "pending")
            verification.append(row)
    return research, verification


def write_gap_files(root, app, run_id):
    """Project authenticated host state outward; edited projections are never inputs."""
    root = Path(root)
    for key, name in (("project_research_gaps", "research-gaps.json"),
                      ("project_verification_gaps", "verification-gaps.json")):
        rows = app.get(key, {})
        if not isinstance(rows, dict):
            raise ValueError(f"{key} must be a host-owned mapping")
        destination = root / "artifacts" / name
        if destination.parent.resolve() != destination.parent.absolute() or destination.is_symlink():
            raise ValueError("gap projection cannot use symlinks")
        atomic_json(destination, {"schema_version": 1, "run_id": run_id,
                                  "gaps": [dict(rows[identity]) for identity in sorted(rows)]})
