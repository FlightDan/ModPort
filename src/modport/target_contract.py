"""Freeze executable target tests against source-reading behavior requirements."""
from .workspace import project_path, is_project_workspace
from collections.abc import Mapping
from copy import deepcopy
import json
from pathlib import Path, PurePosixPath
import re

from .contracts import OperationResult
from .evidence import atomic_json, verified_path


def target_only_workflow(value):
    if hasattr(value, 'options'):
        value = value.options
    if not isinstance(value, Mapping):
        return False
    definition = value.get('definition', value)
    return definition.get('workflow_version', value.get('workflow_version', 0)) >= 34


def read_behavior_requirements(command):
    root = Path(command.run_dir)
    ref = command.artifact_refs.get('behavior_requirements')
    if not isinstance(ref, Mapping):
        frozen = command.upstream_results.get('behavior_freeze', {})
        ref = frozen.get('outputs', {}).get('artifact_refs', {}).get('behavior_requirements')
    if not isinstance(ref, Mapping):
        raise ValueError('frozen source-reading behavior requirements are missing')
    from .behavior_requirements import read_requirements
    value = read_requirements(root, ref)
    return value, dict(ref)


def live_target_contract(command):
    """Read the author/runtime contract, independently of the archived lock."""
    path = verified_path(Path(command.run_dir),
                         {'path': 'worktree/.modport/functional-contract.json'})
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, Mapping):
        raise ValueError('target functional contract must be an object')
    return value.get('contract', value)


def _ids(values, label):
    if (not isinstance(values, list) or not values
            or any(not isinstance(item, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]+', item)
                   for item in values) or len(values) != len(set(values))):
        raise ValueError(label + ' must contain unique nonempty identifiers')
    return values


def _contained_path(value, prefix, suffix=None):
    if not isinstance(value, str):
        raise ValueError('target declaration requires a contained path')
    path = PurePosixPath(value)
    if (path.is_absolute() or '\\' in value or '..' in path.parts
            or any(part in {'', '.'} for part in value.split('/'))
            or not value.startswith(prefix) or (suffix and path.suffix != suffix)):
        raise ValueError('target declaration path must be contained under ' + prefix)
    return value


def validate_target_contract(requirements, candidate, *, workspace=None):
    """Require all frozen assertions and real executable target declarations.

    Test IDs belong to the target author. Source requirements carry behavior
    and assertion IDs, never an obligation to port source harness test IDs.
    Original anchors (including symbol-only anchors) are copied without
    inventing line ranges or recomputing source identities.
    """
    if not isinstance(candidate, Mapping):
        raise ValueError('target functional contract must be an object')
    required_rows = requirements.get('behaviors')
    target_rows = candidate.get('behaviors')
    if not isinstance(required_rows, list) or not required_rows:
        raise ValueError('source-reading requirements contain no behaviors')
    if not isinstance(target_rows, list) or not target_rows:
        raise ValueError('target declarations contain no behaviors')
    required = {}
    assertions = set()
    for row in required_rows:
        if not isinstance(row, Mapping):
            raise ValueError('source-reading behavior must be an object')
        behavior_id = row.get('behavior_id')
        _ids([behavior_id], 'behavior ID')
        if behavior_id in required:
            raise ValueError('source-reading behavior IDs repeat')
        anchors, expected = row.get('source_anchors'), row.get('assertions')
        if not isinstance(anchors, list) or not anchors:
            raise ValueError(behavior_id + ': source anchors are missing')
        if not isinstance(expected, list) or not expected:
            raise ValueError(behavior_id + ': assertions are missing')
        for anchor in anchors:
            if (not isinstance(anchor, Mapping) or not isinstance(anchor.get('symbol'), str)
                    or not anchor['symbol'].strip()):
                raise ValueError(behavior_id + ': a source anchor needs a symbol')
            _contained_path(anchor.get('path'), '')
        for assertion in expected:
            if not isinstance(assertion, Mapping):
                raise ValueError(behavior_id + ': assertion must be an object')
            assertion_id = assertion.get('assertion_id')
            _ids([assertion_id], 'assertion ID')
            if assertion_id in assertions:
                raise ValueError('source-reading assertion IDs repeat')
            if not isinstance(assertion.get('expected'), str) or not assertion['expected'].strip():
                raise ValueError(assertion_id + ': expected observation is missing')
            assertions.add(assertion_id)
        required[behavior_id] = row
    targets = {}
    for row in target_rows:
        if not isinstance(row, Mapping):
            raise ValueError('target behavior must be an object')
        behavior_id = row.get('id', row.get('behavior_id'))
        if not isinstance(behavior_id, str) or behavior_id in targets:
            raise ValueError('target behavior IDs are missing or repeated')
        targets[behavior_id] = row
    if set(targets) != set(required):
        raise ValueError('target behavior IDs must cover all source-reading requirements')
    normalized = deepcopy(dict(candidate))
    normalized_rows, all_tests = [], []
    for behavior_id, source in required.items():
        row = deepcopy(dict(targets[behavior_id]))
        tests = _ids(row.get('test_mapping'), behavior_id + ' test_mapping')
        all_tests.extend(tests)
        if row.get('side') not in {'client', 'server', 'both'}:
            raise ValueError(behavior_id + ': target side is missing')
        for key in ('preconditions', 'action'):
            values = row.get(key)
            if (not isinstance(values, list) or not values
                    or any(not isinstance(value, str) or not value.strip() for value in values)):
                raise ValueError(behavior_id + ': executable ' + key + ' is missing')
        records = row.get('assertion_contracts')
        if not isinstance(records, list) or any(not isinstance(item, Mapping) for item in records):
            raise ValueError(behavior_id + ': assertion_contracts are missing')
        by_id = {item.get('assertion_id'): item for item in records}
        expected_ids = {item['assertion_id'] for item in source['assertions']}
        if len(by_id) != len(records) or set(by_id) != expected_ids:
            raise ValueError(behavior_id + ': every frozen assertion must have target coverage')
        mapped, frozen_assertions = set(), []
        for expected in source['assertions']:
            assertion_id = expected['assertion_id']
            record = deepcopy(dict(by_id[assertion_id]))
            links = _ids(record.get('test_ids'), assertion_id + ' test_ids')
            if not set(links).issubset(tests):
                raise ValueError(assertion_id + ': assertion links undeclared target tests')
            if record.get('text') != expected['expected']:
                raise ValueError(assertion_id + ': target assertion must preserve the frozen expectation')
            mapped.update(links)
            record['source_anchors'] = deepcopy(source['source_anchors'])
            record.pop('source_anchor', None)
            for key in ('trigger', 'conditions'):
                if key in expected:
                    record[key] = deepcopy(expected[key])
            frozen_assertions.append(record)
        if mapped != set(tests):
            raise ValueError(behavior_id + ': target tests have no frozen assertion coverage')
        row.update(id=behavior_id, source_evidence=source['description'],
                   source_anchors=deepcopy(source['source_anchors']),
                   assertions=[item['expected'] for item in source['assertions']],
                   assertion_contracts=frozen_assertions)
        normalized_rows.append(row)
    _ids(all_tests, 'target test IDs')
    declarations = candidate.get('test_evidence')
    if not isinstance(declarations, Mapping) or set(declarations) != set(all_tests):
        raise ValueError('target test_evidence must declare every mapped test exactly once')
    tasks = candidate.get('baseline_gradle_tasks')
    if (not isinstance(tasks, list) or not tasks or len(tasks) != len(set(tasks))
            or any(not isinstance(task, str)
                   or not re.fullmatch(r':?[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)*', task)
                   or task.split(':')[-1] in {'help', 'tasks', 'properties', 'runClient', 'runServer'}
                   for task in tasks)):
        raise ValueError('target verification needs executable test Gradle tasks')
    paths, identities = [], set()
    for test_id, declaration in declarations.items():
        if (not isinstance(declaration, Mapping) or declaration.get('evidence_kind') != 'runtime'
                or declaration.get('executor') not in {'junit', 'gametest'}):
            raise ValueError(test_id + ': an executable target runtime declaration is required')
        path = _contained_path(declaration.get('path'), '.modport/evidence/', '.json')
        paths.append(path)
        operations = declaration.get('runtime_operations')
        if (not isinstance(operations, list) or not operations
                or any(not isinstance(value, str) or not value.strip() for value in operations)):
            raise ValueError(test_id + ': concrete runtime operations are required')
        identity = declaration.get('result_identity')
        if (not isinstance(identity, Mapping)
                or set(identity) != {'kind', 'gradle_task', 'classname', 'name'}
                or identity.get('kind') != 'junit_xml'):
            raise ValueError(test_id + ': exact target XML result identity is required')
        identity_task = identity.get('gradle_task')
        if not isinstance(identity_task, str) or ':' + identity_task.lstrip(':') not in {
                ':' + task.lstrip(':') for task in tasks}:
            raise ValueError(test_id + ': XML result task is not declared')
        for key in ('classname', 'name'):
            if not isinstance(identity.get(key), str) or not identity[key].strip():
                raise ValueError(test_id + ': XML result ' + key + ' is missing')
        identity_key = (':' + identity_task.lstrip(':'), identity['classname'], identity['name'])
        if identity_key in identities:
            raise ValueError('target XML result identities repeat')
        identities.add(identity_key)
        sources = declaration.get('test_source_files')
        if not isinstance(sources, list) or not sources or len(sources) != len(set(sources)):
            raise ValueError(test_id + ': target test source files are missing')
        for source in sources:
            _contained_path(source, '.modport/', '.java')
            if workspace is not None:
                verified_path(Path(workspace), {'path': source})
    files = candidate.get('baseline_evidence_files')
    if len(paths) != len(set(paths)) or not isinstance(files, list) or sorted(paths) != sorted(files):
        raise ValueError('target tests require distinct declared evidence files')
    normalized.update(schema_version=1, behaviors=normalized_rows)
    return normalized


class TargetContractFreezeHandler:
    def __call__(self, command):
        if not target_only_workflow(command):
            raise ValueError('target contract freeze requires the source-reading workflow')
        root = Path(command.run_dir)
        try:
            requirements, ref = read_behavior_requirements(command)
            frozen_requirements = json.loads(verified_path(root, ref).read_text(encoding='utf-8'))
            contract = validate_target_contract(requirements, live_target_contract(command),
                                                workspace=project_path(root, 'worktree'))
            rubric = {}
            rubric_ref = command.artifact_refs.get('acceptance_rubric')
            if isinstance(rubric_ref, Mapping):
                rubric = json.loads(verified_path(root, rubric_ref).read_text(encoding='utf-8'))
            for key in ('rubric_id', 'rubric_version', 'rubric_sha256'):
                if key in rubric:
                    contract[key] = rubric[key]
            source_ref = command.artifact_refs.get('source')
            if isinstance(source_ref, Mapping):
                source = json.loads(verified_path(root, source_ref).read_text(encoding='utf-8'))
                if 'source_commit' in source:
                    contract['source_fingerprint'] = source['source_commit']
            elif 'source_commit' in frozen_requirements:
                contract['source_fingerprint'] = frozen_requirements['source_commit']
            lock = {'schema_version': 1, 'contract': contract, 'source_contract': {},
                    'acceptance_rubric': {key: rubric[key] for key in ('rubric_id', 'rubric_version')
                                         if key in rubric},
                    'behavior_requirements_ref': ref, 'uncovered_assertion_ids': [],
                    'source_assumption': 'user_confirmed_functional',
                    'verification_basis': 'source_reading', 'source_runtime_tested': False,
                    **{key: contract[key] for key in ('baseline_gradle_tasks',
                       'baseline_evidence_files', 'test_evidence')}}
            target = root / 'artifacts/executions' / command.command_id / 'target-contract-lock.json'
            atomic_json(target, lock)
            # The authored harness consumes top-level behaviors and declarations.
            # Host freeze metadata belongs only in the archived lock; publishing
            # its wrapper here changes the interface after author compilation.
            # Replace the entry to avoid modifying a hard-linked product inode.
            atomic_json(project_path(root, 'worktree/.modport/functional-contract.json'), contract)
            return OperationResult('completed', command.run_id, command.task_id,
                command.stage_id, command.command_id, outputs={'behavior_count': len(contract['behaviors']),
                    'source_runtime_tested': False, 'verification_basis': 'source_reading',
                    'artifact_refs': {'functional_contract_lock': {
                        'path': target.relative_to(root).as_posix(), 'media_type': 'application/json'}}},
                detail='Target runtime declarations cover the frozen source-reading requirements')
        except (OSError, ValueError, TypeError, KeyError) as exc:
            return OperationResult('failed', command.run_id, command.task_id,
                command.stage_id, command.command_id, outputs={'process_executed': False},
                error_code='target_contract_invalid', detail=str(exc))
