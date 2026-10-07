"""Source-derived behavior requirements, without executing the original mod."""
from collections.abc import Mapping
import json
from pathlib import Path, PurePosixPath
import re

from .contracts import json_copy
from .evidence import atomic_json, verified_path


def source_reading_policy(value):
    """Recognize current source-reading policy in commands and frozen headers."""
    if hasattr(value, 'options'):
        options = dict(value.options)
        payload = getattr(value, 'payload', {})
        if 'request' not in options and isinstance(payload, Mapping):
            request = payload.get('request')
            if isinstance(request, Mapping):
                options['request'] = request
        value = options
    if not isinstance(value, Mapping):
        return False
    definition = value.get('definition', value)
    if not isinstance(definition, Mapping):
        return False
    version = definition.get('workflow_version', value.get('workflow_version', 0))
    request = value.get('request', definition.get('request', {}))
    request = request if isinstance(request, Mapping) else {}
    mode = value.get('workflow_mode', definition.get('workflow_mode',
                     request.get('workflow_mode', 'migration')))
    return type(version) is int and version >= 34 and mode != 'skill_generation'


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(field + ' must be nonblank text')
    return value


def validate_requirements(document):
    """Validate the same small source protocol supplied to the extracting agent."""
    if not isinstance(document, Mapping) or type(document.get('schema_version')) is not int or document.get('schema_version') != 1:
        raise ValueError('behavior requirements require schema_version=1')
    rows = document.get('behaviors')
    if not isinstance(rows, list) or not rows:
        raise ValueError('behavior requirements must contain a nonempty behaviors array')
    behavior_ids, assertion_ids = set(), set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError('behavior must be an object')
        identifier = _text(row.get('behavior_id'), 'behavior_id')
        if not re.fullmatch(r'[A-Za-z0-9_.:-]+', identifier):
            raise ValueError('behavior_id must be a safe target identifier')
        if identifier in behavior_ids:
            raise ValueError('duplicate behavior_id: ' + identifier)
        behavior_ids.add(identifier)
        _text(row.get('description'), 'behavior description')
        anchors = row.get('source_anchors')
        if not isinstance(anchors, list) or not anchors:
            raise ValueError(identifier + ': source_anchors must be a nonempty array')
        for anchor in anchors:
            if not isinstance(anchor, Mapping):
                raise ValueError('source anchor must be an object')
            value = _text(anchor.get('path'), 'source anchor path')
            path = PurePosixPath(value)
            if path.is_absolute() or '\\' in value or any(part in {'', '.', '..'} for part in value.split('/')):
                raise ValueError('source anchor path must be relative and contained')
            _text(anchor.get('symbol'), 'source anchor symbol')
            for field in ('line', 'start_line', 'end_line'):
                if field in anchor and (type(anchor[field]) is not int or anchor[field] < 1):
                    raise ValueError('source anchor ' + field + ' must be a positive integer')
            if 'lines' in anchor and (not isinstance(anchor['lines'], list)
                    or any(type(line) is not int or line < 1 for line in anchor['lines'])):
                raise ValueError('source anchor lines must contain positive integers')
        assertions = row.get('assertions')
        if not isinstance(assertions, list) or not assertions:
            raise ValueError(identifier + ': assertions must be a nonempty array')
        for assertion in assertions:
            if not isinstance(assertion, Mapping):
                raise ValueError('assertion must be an object')
            assertion_id = _text(assertion.get('assertion_id'), 'assertion_id')
            if not re.fullmatch(r'[A-Za-z0-9_.:-]+', assertion_id):
                raise ValueError('assertion_id must be a safe target identifier')
            if assertion_id in assertion_ids:
                raise ValueError('duplicate assertion_id: ' + assertion_id)
            assertion_ids.add(assertion_id)
            _text(assertion.get('expected'), 'assertion expected')
    return json_copy(dict(document))


def read_requirements(root, ref):
    document = json.loads(verified_path(Path(root), ref).read_text(encoding='utf-8'))
    return validate_requirements(document.get('requirements', document))


def preserve_requirement_ids(previous, current):
    """Keep previously confirmed behavior and assertion identities on continuation."""
    previous = validate_requirements(previous)
    current = validate_requirements(current)
    current_rows = {row['behavior_id']: row for row in current['behaviors']}
    for row in previous['behaviors']:
        retained = current_rows.get(row['behavior_id'])
        if retained is None:
            raise ValueError('confirmed behavior_id was removed: ' + row['behavior_id'])
        retained_ids = {item['assertion_id'] for item in retained['assertions']}
        for assertion in row['assertions']:
            if assertion['assertion_id'] not in retained_ids:
                raise ValueError('confirmed assertion_id was removed: ' + assertion['assertion_id'])


def requirements_from_legacy_contract(document):
    """Carry source behavior text and anchors without carrying old test execution."""
    if not isinstance(document, Mapping):
        raise ValueError('carried source contract must be an object')
    contract = document.get('contract', document.get('source_contract', document))
    if not isinstance(contract, Mapping):
        raise ValueError('carried source contract is missing')
    behaviors = []
    rows = contract.get('behaviors', contract.get('entries', []))
    if not isinstance(rows, list):
        raise ValueError('carried source behaviors must be an array')
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError('carried source behavior must be an object')
        assertions, anchors = [], []
        for item in row.get('assertion_contracts', []):
            if not isinstance(item, Mapping):
                raise ValueError('carried assertion must be an object')
            assertion = {'assertion_id': item.get('assertion_id'), 'expected': item.get('text')}
            if 'action' in row:
                action = row['action']
                assertion['trigger'] = '; '.join(action) if isinstance(action, list) else action
            if 'preconditions' in row:
                assertion['conditions'] = json_copy(row['preconditions'])
            assertions.append(assertion)
            anchor = item.get('source_anchor')
            if not isinstance(anchor, Mapping):
                raise ValueError('carried assertion source anchor is missing')
            # Line references and names describe source; old receipts and
            # candidate identities never become source-reading evidence.
            projected = {'path': anchor.get('path'),
                         'symbol': anchor.get('symbol') or row.get('source_evidence') or row.get('id')}
            for name in ('line', 'lines', 'start_line', 'end_line'):
                if name in anchor:
                    projected[name] = json_copy(anchor[name])
            if projected not in anchors:
                anchors.append(projected)
        description = row.get('description') or row.get('source_evidence') or row.get('id')
        behavior = {'behavior_id': row.get('behavior_id', row.get('id')),
                    'description': description, 'source_anchors': anchors,
                    'assertions': assertions}
        if 'side' in row:
            behavior['side'] = row['side']
        behaviors.append(behavior)
    result = {'schema_version': 1, 'behaviors': behaviors}
    if 'uncertainties' in contract:
        result['uncertainties'] = json_copy(contract['uncertainties'])
    return validate_requirements(result)


class BehaviorExtractHandler:
    def __call__(self, command):
        from .handlers import CodexStageHandler
        from .prompts import STAGE_PROMPTS
        return CodexStageHandler(STAGE_PROMPTS['behavior_extract'], baseline=True,
            required_paths=('.modport/behavior-requirements.json',))(command)


class BehaviorReviewHandler:
    def __call__(self, command):
        from .handlers import CodexStageHandler
        from .prompts import STAGE_PROMPTS
        return CodexStageHandler(STAGE_PROMPTS['behavior_review'], baseline=True,
            read_only=True, required_paths=('.modport/behavior-review.md',))(command)


class BehaviorFreezeHandler:
    def __call__(self, command):
        from .handlers import _result
        root = Path(command.run_dir)
        try:
            candidate = command.artifact_refs.get('behavior_requirements_candidate')
            supplied = command.payload.get('behavior_requirements')
            if isinstance(candidate, Mapping):
                requirements = read_requirements(root, candidate)
            elif isinstance(supplied, Mapping):
                requirements = validate_requirements(supplied.get('requirements', supplied))
            else:
                source = verified_path(root, {'path': 'baseline/.modport/behavior-requirements.json'})
                requirements = validate_requirements(json.loads(source.read_text(encoding='utf-8')))
            previous = command.artifact_refs.get('behavior_requirements')
            if isinstance(previous, Mapping):
                preserve_requirement_ids(read_requirements(root, previous), requirements)
            provenance = json.loads(verified_path(root, {'path': 'artifacts/source.json'}).read_text(encoding='utf-8'))
            if not isinstance(provenance, Mapping):
                raise ValueError('host source evidence must be an object')
            source_commit = _text(provenance.get('source_commit'), 'host source_commit')
            report = {'schema_version': 1, 'requirements': requirements,
                      'source_commit': source_commit,
                      'source_assumption': 'user_confirmed_functional',
                      'verification_basis': 'source_reading',
                      'acceptance_status': 'unverified'}
            review = root / 'baseline/.modport/behavior-review.md'
            if review.is_file() and not review.is_symlink():
                report['review_ref'] = {'path': review.relative_to(root).as_posix(),
                                        'media_type': 'text/markdown'}
            target = root / 'artifacts/executions' / command.command_id / 'behavior-requirements.json'
            if target.resolve() != target.absolute() or not target.resolve().is_relative_to(root.resolve()):
                raise ValueError('unsafe behavior requirements artifact path')
            atomic_json(target, report)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            return _result(command, 'failed', error_code='behavior_requirements_missing',
                           detail=str(exc), outputs={'acceptance_status': 'unverified'})
        return _result(command, 'completed', outputs={
            'source_assumption': report['source_assumption'],
            'verification_basis': report['verification_basis'],
            'acceptance_status': 'unverified',
            'artifact_refs': {'behavior_requirements': {
                'path': target.relative_to(root).as_posix(), 'media_type': 'application/json'}}},
            detail='Source-derived behavior requirements archived; review findings remain diagnostic')
