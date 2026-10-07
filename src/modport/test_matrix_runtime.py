"""Bind planner selections to the original candidate and fresh baseline receipts."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .evidence import atomic_json, file_digest, verified_path
from .test_matrix import select_migration_tests


ASSESSMENT_PATH = '.modport/test-assessment.json'
MATRIX_PATH = '.modport/test-matrix.json'


def _read(root: Path, ref: Mapping[str, Any]) -> dict:
    path = verified_path(root, ref)
    if file_digest(path) != ref.get('sha256'):
        raise ValueError('test selection input digest changed')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('test selection input must be an object')
    return value


def _inputs(command):
    from .handlers import _fresh_baseline_report
    from .opencode_shell_mcp import _workspace_candidate_identity

    root = Path(command.run_dir)
    workspace = root / 'baseline'
    contract_path = verified_path(root, {'path': 'baseline/.modport/functional-contract.json'})
    matrix_path = verified_path(root, {'path': 'baseline/' + MATRIX_PATH})
    contract = json.loads(contract_path.read_text(encoding='utf-8'))
    matrix = json.loads(matrix_path.read_text(encoding='utf-8'))
    report_path = _fresh_baseline_report(command, root)
    report = json.loads(report_path.read_text(encoding='utf-8'))
    if not all(isinstance(value, Mapping) for value in (contract, matrix, report)):
        raise ValueError('baseline selection inputs must be objects')
    candidate = _workspace_candidate_identity(workspace)
    before = report.get('candidate_before', {})
    if (not isinstance(before, Mapping) or report.get('candidate_unchanged') is not True
            or before.get('candidate_id') != candidate['candidate_id']):
        raise ValueError('baseline candidate changed or has no fresh execution identity')
    if (report.get('source_commit') != candidate['head']
            or contract.get('source_fingerprint') != candidate['head']):
        raise ValueError('baseline selection does not identify the immutable original source')
    # The selector classifies observations only after the host authenticates
    # their result artifacts. A planner cannot manufacture a defect receipt.
    cases = report.get('case_results', {})
    if not isinstance(cases, Mapping):
        raise ValueError('baseline case receipts must be an object')
    for case in cases.values():
        if not isinstance(case, dict):
            raise ValueError('baseline case receipt is malformed')
        if case.get('status') in {'passed', 'failed'}:
            ref = case.get('xml_artifact_ref')
            if not isinstance(ref, Mapping):
                raise ValueError('baseline case has no JUnit artifact')
            path = verified_path(root, ref)
            if file_digest(path) != ref.get('sha256'):
                raise ValueError('baseline JUnit artifact digest changed')
            if (case.get('candidate_id') != candidate['candidate_id']
                    or case.get('execution_nonce') != report.get('execution_nonce')):
                raise ValueError('baseline case belongs to another candidate or execution')
    binding = {
        'contract_sha256': file_digest(contract_path),
        'matrix_sha256': file_digest(matrix_path),
        'baseline_report_sha256': file_digest(report_path),
        'baseline_execution_id': command.upstream_results['contract_verify']['command_id'],
        'candidate_id': candidate['candidate_id'],
    }
    return contract, matrix, report, binding


def seal_assessment(command, result) -> dict:
    """Consume the actual planner sidecar snapshot; return a host artifact ref."""
    root = Path(command.run_dir)
    refs = result.outputs.get('artifact_refs', {})
    ref = refs.get('stage_output:contract_review:' + ASSESSMENT_PATH)
    if not isinstance(ref, Mapping):
        raise ValueError('baseline planner did not publish a test assessment')
    assessment = _read(root, ref)
    contract, matrix, report, binding = _inputs(command)
    selection = select_migration_tests(contract, matrix, assessment, report)
    record = {
        'schema_version': 1, 'review_execution_id': command.command_id,
        'binding': binding, 'assessment_ref': ref,
        'selection': selection, 'acceptance_status': 'unverified',
    }
    path = root / 'artifacts' / 'executions' / command.command_id / 'baseline-test-selection.json'
    atomic_json(path, record)
    return {'path': path.relative_to(root).as_posix(), 'sha256': file_digest(path),
            'media_type': 'application/json'}


def frozen_selection(command) -> dict:
    """Recheck the planner selection against the exact verifier used by freeze."""
    root = Path(command.run_dir)
    review = command.upstream_results.get('contract_review', {})
    ref = review.get('outputs', {}).get('artifact_refs', {}).get('baseline_test_selection')
    if not isinstance(ref, Mapping):
        raise ValueError('fresh baseline planner selection is unavailable')
    expected = f"artifacts/executions/{review.get('command_id')}/baseline-test-selection.json"
    if ref.get('path') != expected:
        raise ValueError('baseline selection belongs to another planner execution')
    record = _read(root, ref)
    contract, matrix, report, binding = _inputs(command)
    if (record.get('review_execution_id') != review.get('command_id')
            or record.get('binding') != binding):
        raise ValueError('baseline selection is stale after source or test repair')
    assessment = _read(root, record['assessment_ref'])
    selection = select_migration_tests(contract, matrix, assessment, report)
    if record.get('selection') != selection:
        raise ValueError('baseline selection does not match planner decisions')
    return {**selection, 'selection_ref': dict(ref)}


def executed_excluded_cases(workspace: Path, source_contract: Mapping, selected_ids: list[str]) -> list[dict]:
    """Inspect freshly cleared target XML for original cases excluded by the planner."""
    from .opencode_shell_mcp import (_result_tree_entries, _read_bounded_file,
                                    MAX_JUNIT_XML_FILE_BYTES, MAX_JUNIT_XML_TOTAL_BYTES)
    from .test_selection_execution import find_executed_excluded_junit_cases

    excluded = sorted(set(source_contract.get('test_evidence', {})) - set(selected_ids))
    if not excluded:
        return []
    reports: dict[str, list[bytes]] = {}
    total = 0
    for path, relative, directory in _result_tree_entries(workspace):
        parts = Path(relative).parts
        if directory or path.suffix.lower() != '.xml':
            continue
        if len(parts) != 4 or parts[:2] != ('build', 'test-results'):
            continue
        data = _read_bounded_file(path, MAX_JUNIT_XML_FILE_BYTES)
        total += len(data)
        if total > MAX_JUNIT_XML_TOTAL_BYTES:
            raise ValueError('target JUnit XML exceeds the aggregate size limit')
        reports.setdefault(':' + parts[2], []).append(data)
    return find_executed_excluded_junit_cases(source_contract, excluded, reports)


def baseline_selection_summary(locked: Mapping[str, Any]) -> dict:
    """Report retained tests separately from defects observed in the original mod."""
    from .handlers import _assertion_review_diagnostics

    selection = locked.get('test_selection', {})
    selected = selection.get('selected_test_ids', [])
    observation = locked.get('v29_assertion_observation', {})
    cases = observation.get('case_results', {})
    source_status = observation.get('source_anchor_status')
    passed = [test_id for test_id in selected if isinstance(cases.get(test_id), Mapping)
              and cases[test_id].get('status') == cases[test_id].get('test_outcome') == 'passed'
              and cases[test_id].get('candidate_unchanged') is True]
    gaps = selection.get('uncovered_assertion_ids', [])
    review_diagnostics = _assertion_review_diagnostics(locked.get('contract', {}),
                                                      locked.get('review', {}))
    verified = (bool(selected) and bool(selection.get('selection_ref'))
                and len(passed) == len(selected) and not gaps and not selection.get('diagnostics')
                and source_status == 'resolved' and not review_diagnostics)
    return {
        'status': 'passed' if verified else 'unverified',
        'expected_behavior_tests': len(selected), 'verified_behavior_tests': len(passed),
        'selected_test_ids': selected, 'uncovered_assertion_ids': gaps,
        'source_defects': selection.get('source_defects', []),
        'excluded_cases': selection.get('excluded_cases', []),
        'original_case_results': cases,
        'selection_ref': selection.get('selection_ref'),
        'selected_assertion_review_diagnostics': review_diagnostics,
        'source_anchor_status': source_status,
    }


def selected_assertion_review(contract: Mapping, review: Mapping) -> dict:
    """Keep exact original review rows for assertions retained by the selection."""
    assertion_ids = {assertion['assertion_id']
                     for entry in contract.get('behaviors', contract.get('entries', []))
                     for assertion in entry.get('assertion_contracts', [])}
    projected = dict(review)
    rows = review.get('assertion_reviews')
    if isinstance(rows, list):
        projected['assertion_reviews'] = [row for row in rows
            if isinstance(row, Mapping) and row.get('assertion_id') in assertion_ids]
    return projected
