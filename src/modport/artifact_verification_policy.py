"""Required behavior completion for the frozen artifact verification workflow."""
from collections.abc import Mapping
import json
from pathlib import Path

from .evidence import atomic_json, verified_path
from .target_contract import target_only_workflow


def required_behavior_policy(value):
    """Select the artifact policy without changing migration diagnostic routing."""
    if hasattr(value, 'options'):
        value = value.options
    if not isinstance(value, Mapping):
        return False
    definition = value.get('definition', value)
    policy = definition.get('validation_policy', {})
    return (isinstance(policy, Mapping)
            and policy.get('scope') == 'artifact_verification'
            and policy.get('required_behavior_completion') is True)


def _result_outputs(result):
    if hasattr(result, 'outputs'):
        result = result.outputs
    elif isinstance(result, Mapping):
        result = result.get('outputs', result)
    return result if isinstance(result, Mapping) else {}


def _assertions(contract):
    result = {}
    behaviors = contract.get('behaviors', contract.get('entries', []))
    for behavior in behaviors if isinstance(behaviors, list) else []:
        if not isinstance(behavior, Mapping):
            continue
        rows = behavior.get('assertion_contracts', [])
        for assertion in rows if isinstance(rows, list) else []:
            if isinstance(assertion, Mapping) and isinstance(assertion.get('assertion_id'), str):
                result[assertion['assertion_id']] = assertion
    return result


def _runtime_witness(records, test_id):
    record = records.get(test_id) if isinstance(records, Mapping) else None
    return (isinstance(record, Mapping) and record.get('evidence_kind') == 'runtime'
            and bool(record.get('path')))


def assess_required_cases(contract, result, *, selected_test_ids=None):
    """Use host-observed execution, cases, assertions and runtime witnesses.

    Aggregate source failure can include a legitimate excluded source defect.
    Only the retained contract is assessed here; callers retain other operation
    failures separately. No new execution identities or artifact checks are made.
    """
    contract = contract if isinstance(contract, Mapping) else {}
    outputs = _result_outputs(result)
    declarations = contract.get('test_evidence', {})
    declarations = declarations if isinstance(declarations, Mapping) else {}
    required = list(declarations) if selected_test_ids is None else list(selected_test_ids)
    assertions = _assertions(contract)
    cases = outputs.get('case_results', {})
    cases = cases if isinstance(cases, Mapping) else {}
    observations = outputs.get('assertion_results', {})
    observations = observations if isinstance(observations, Mapping) else {}
    records = outputs.get('evidence_records', {})
    records = records if isinstance(records, Mapping) else {}
    gaps = []
    if not required:
        gaps.append('required test selection is empty or missing')
    if not assertions:
        gaps.append('required assertions are missing')
    if outputs.get('process_executed') is not True:
        gaps.append('required behavior process was not executed')
    for test_id in required:
        case = cases.get(test_id)
        declaration = declarations.get(test_id)
        if not isinstance(declaration, Mapping) or declaration.get('evidence_kind') != 'runtime':
            gaps.append(f'{test_id}: required live runtime declaration is missing')
        if (not isinstance(case, Mapping) or case.get('status') != 'passed'
                or case.get('test_outcome') != 'passed'):
            gaps.append(f'{test_id}: required case did not pass')
        if not _runtime_witness(records, test_id):
            gaps.append(f'{test_id}: host-validated runtime witness is missing')
    for assertion_id, assertion in assertions.items():
        linked = assertion.get('test_ids', [])
        if not linked or any(test_id not in required for test_id in linked):
            gaps.append(f'{assertion_id}: required assertion has no retained test coverage')
        row = observations.get(assertion_id)
        if not isinstance(row, Mapping) or row.get('status') != 'passed':
            gaps.append(f'{assertion_id}: required assertion did not pass')
    return {'status': 'passed' if not gaps else 'failed',
            'required_test_ids': required, 'required_assertion_ids': list(assertions),
            'gaps': gaps}


def read_source_selection(root, effective):
    """Read the actual host-published reviewer selection, never report prose."""
    review = effective.get('contract_review', {})
    ref = review.get('outputs', {}).get('artifact_refs', {}).get('baseline_test_selection')
    if not isinstance(ref, Mapping):
        raise ValueError('fresh source test selection is missing')
    record = json.loads(verified_path(Path(root), ref).read_text(encoding='utf-8'))
    selection = record.get('selection') if isinstance(record, Mapping) else None
    if not isinstance(selection, Mapping) or not isinstance(selection.get('migration_contract'), Mapping):
        raise ValueError('fresh source test selection has no retained contract')
    return selection


def assess_source_selection(root, effective):
    try:
        selection = read_source_selection(root, effective)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {'status': 'failed', 'required_test_ids': [],
                'required_assertion_ids': [], 'gaps': [str(exc)]}
    contract = selection['migration_contract']
    outputs = _result_outputs(effective.get('contract_verify'))
    observations = outputs.get('assertion_results', {})
    observations = observations if isinstance(observations, Mapping) else {}
    cases = outputs.get('case_results', {})
    cases = cases if isinstance(cases, Mapping) else {}
    records = outputs.get('evidence_records', {})
    projected = dict(observations)
    for assertion_id, assertion in _assertions(contract).items():
        original = observations.get(assertion_id)
        linked = assertion.get('test_ids', [])
        original_ids = original.get('test_ids') if isinstance(original, Mapping) else None
        if (not isinstance(linked, list) or not isinstance(original_ids, list)
                or not linked or not set(linked) < set(original_ids)):
            continue
        # The host's original aggregate included a now-excluded duplicate or
        # genuine defect. Apply its existing all-linked-cases-pass reduction
        # to the retained IDs; an unchanged or absent assertion receipt is
        # never replaced with an invented passing assertion.
        passed = all(isinstance(cases.get(test_id), Mapping)
                     and cases[test_id].get('status') == cases[test_id].get('test_outcome') == 'passed'
                     and _runtime_witness(records, test_id) for test_id in linked)
        projected[assertion_id] = {**original, 'test_ids': linked,
                                   'status': 'passed' if passed else 'unverified'}
    assessment = assess_required_cases(contract,
        {**outputs, 'assertion_results': projected},
        selected_test_ids=selection.get('selected_test_ids', []))
    gaps = [*assessment['gaps'], *(
        f'{assertion_id}: required source assertion remains uncovered'
        for assertion_id in selection.get('uncovered_assertion_ids', []))]
    for defect in selection.get('source_defects', []):
        test_id = defect.get('test_id') if isinstance(defect, Mapping) else None
        if not test_id or not _runtime_witness(records, test_id):
            gaps.append(f'{test_id}: source-defect exclusion lacks a host-validated live runtime witness')
    return {**assessment, 'status': 'passed' if not gaps else 'failed', 'gaps': gaps}


def read_locked_contract(root, effective, *, workflow_version=0):
    frozen = (effective.get('target_contract_freeze', {}) if workflow_version >= 34 else
              effective.get('target_contract_freeze', effective.get('contract_freeze', {})))
    ref = frozen.get('outputs', {}).get('artifact_refs', {}).get('functional_contract_lock')
    if not isinstance(ref, Mapping):
        raise ValueError('required behavior contract is not frozen')
    locked = json.loads(verified_path(Path(root), ref).read_text(encoding='utf-8'))
    if not isinstance(locked, Mapping) or not isinstance(locked.get('contract'), Mapping):
        raise ValueError('required behavior contract is missing')
    return locked


def required_behavior_assessments(value, root, effective):
    """Source reading is an assumption, never a missing runtime acceptance gate."""
    if target_only_workflow(value):
        return {'target': assess_target_selection(root, effective, workflow_version=34)}
    target = assess_target_selection(root, effective)
    return {'source': assess_source_selection(root, effective), 'target': target}


def assess_target_selection(root, effective, *, workflow_version=0):
    try:
        locked = read_locked_contract(root, effective, workflow_version=workflow_version)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {'status': 'failed', 'required_test_ids': [],
                'required_assertion_ids': [], 'gaps': [str(exc)]}
    result = effective.get('artifact_test_execute', {})
    assessment = assess_required_cases(locked['contract'], result)
    gaps = list(assessment['gaps'])
    if result.get('status') != 'completed':
        gaps.append('target behavior verification operation did not complete')
    gaps.extend(f'{assertion_id}: required assertion remains uncovered'
                for assertion_id in locked.get('uncovered_assertion_ids', []))
    return {**assessment, 'status': 'passed' if not gaps else 'failed', 'gaps': gaps}


def archive_required_behavior_failure(header, app, reason):
    """Archive host-owned results when no report task fits the original budget."""
    root = Path(header['run_dir'])
    effective = app.get('effective', {})
    assessments = required_behavior_assessments(header.get('definition', header), root, effective)
    descriptor = {}
    ref = header.get('initial_refs', {}).get('artifact_input')
    if isinstance(ref, Mapping):
        try:
            descriptor = json.loads(verified_path(root, ref).read_text(encoding='utf-8'))
        except (OSError, ValueError, TypeError):
            descriptor = {'artifact_input_ref': dict(ref)}
    report = {'schema_version': 1, 'run_id': header['run_id'],
              'acceptance_status': 'unverified', 'required_behavior_status': 'failed',
              'artifact_input': descriptor,
              'source_verification': effective.get('contract_verify', {}),
              'target_verification': effective.get('artifact_test_execute', {}),
              'required_behavior_assessments': assessments, 'failure_reason': reason}
    if target_only_workflow(header.get('definition', header)):
        report['source_verification'] = {'verification_basis': 'source_reading',
            'source_assumption': 'user_confirmed_functional', 'runtime_tested': False}
    path = root / 'artifacts/artifact-verification-report.json'
    atomic_json(path, report)
    app['required_behavior_assessments'] = assessments
    app['required_behavior_status'] = 'failed'
    app['acceptance_status'] = 'unverified'
    app['artifact_verification_report'] = {
        'path': path.relative_to(root).as_posix(), 'media_type': 'application/json'}
