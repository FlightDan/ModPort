"""Explicit, bounded continuation with evidence-based extra time.

The caller authorizes this policy once. SDK segments and immutable inputs stay
intact; extra time is a new public continuation, never an edit to a live Run.
"""
from __future__ import annotations

from pathlib import Path
import re
import time

from .continuation import continue_from_planner
from .evidence import atomic_json, read_json, workspace_lock
from .monitor_progress import (collect_progress_evidence, compare_progress_evidence,
                               budget_extension_decision)
from .sdk_compat import inspect_runtime

BUDGET_REASONS = frozenset({'budget_exhausted', 'wall_clock_budget_exhausted',
                           'agent_assignment_budget_exhausted'})


def continue_with_progress(operations, run_dir, run_id, *, next_run_id, reason,
                           initial_seconds=28800, maximum_seconds=43200,
                           clock=time.time):
    """Run an authorized window and at most one evidence-justified extension.

    A durable policy makes retries resume the same segment without resetting
    the absolute cap or recounting previous progress. Open SDK waits remain
    actionable; this wrapper never resolves them or retries arbitrary failures.
    """
    if (not isinstance(next_run_id, str)
            or not re.fullmatch(r'[A-Za-z0-9_.:-]+', next_run_id)
            or next_run_id == run_id):
        raise ValueError('progress continuation requires a distinct safe segment ID')
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('progress continuation requires an authorization reason')
    if (type(initial_seconds) is not int or type(maximum_seconds) is not int
            or initial_seconds <= 0 or maximum_seconds < initial_seconds):
        raise ValueError('progress budget must be positive and bounded')
    root = Path(run_dir).resolve()
    operations._header(root, run_id)
    policy_dir = root / 'artifacts' / 'progress-continuations' / next_run_id
    if policy_dir.resolve() != policy_dir.absolute():
        raise ValueError('unsafe progress continuation path')
    request = {'source_run_id': run_id, 'next_run_id': next_run_id, 'reason': reason,
               'initial_seconds': initial_seconds, 'maximum_seconds': maximum_seconds}
    with workspace_lock(root / '.locks' / 'progress-continuation', blocking=False):
        policy_dir.mkdir(parents=True, exist_ok=True)
        path = policy_dir / 'policy.json'
        if path.exists():
            policy = read_json(path)
            if policy.get('request') != request:
                raise ValueError('progress continuation policy differs from authorized request')
        else:
            policy = {'schema_version': 1, 'request': request, 'started_at': clock(),
                      'baseline': collect_progress_evidence(root), 'extension': None}
            atomic_json(path, policy)
        # Read-only SDK identity/storage evidence before any continuation writer.
        atomic_json(policy_dir / 'sdk-preflight.json', inspect_runtime(root))
        if policy.get('extension'):
            extension = policy['extension']
            run = continue_from_planner(operations, root, next_run_id,
                next_run_id=extension['next_run_id'], reason=extension['reason'],
                additional_seconds=extension['additional_seconds'])
        else:
            run = continue_from_planner(operations, root, run_id, next_run_id=next_run_id,
                reason=reason, additional_seconds=initial_seconds)
            if not policy.get('segment_started'):
                policy['started_at'] = run.snapshot['input']['started_at']
                policy['segment_started'] = True
                atomic_json(path, policy)
        run = operations.execute(run)
        current = collect_progress_evidence(root)
        delta = compare_progress_evidence(policy['baseline'], current)
        app = run.snapshot.get('application_state') or {}
        deadline = (run.snapshot.get('input') or {}).get('deadline_epoch')
        budget_stop = run.snapshot.get('state') in {'failed', 'cancelled'} and (
            app.get('terminal_reason') in BUDGET_REASONS or app.get('stop_reason') in BUDGET_REASONS)
        # Some worker boundaries settle just before the wall deadline. Wait
        # out only that small remainder; do not turn tolerance into extra time.
        now = clock()
        if budget_stop and isinstance(deadline, (int, float)) and 0 < deadline - now <= 2:
            time.sleep(deadline - now)
        decision = budget_extension_decision(delta, now=clock(), started_at=policy['started_at'],
            current_deadline=deadline, initial_seconds=initial_seconds,
            maximum_seconds=maximum_seconds,
            quantum_seconds=max(1, maximum_seconds - initial_seconds))
        decision['budget_stop'] = budget_stop
        atomic_json(policy_dir / 'assessment.json', {'run_id': run.run_id,
            'observed_at': clock(), 'progress': delta, 'budget': decision})
        if (not policy.get('extension') and budget_stop and decision['eligible']
                and decision['additional_seconds'] > 0):
            extension = {'next_run_id': next_run_id + ':progress-extension',
                         'reason': reason + '; additional time justified by recorded progress evidence',
                         'additional_seconds': decision['additional_seconds']}
            policy['extension'] = extension
            atomic_json(path, policy)
            run = operations.execute(continue_from_planner(operations, root, next_run_id,
                next_run_id=extension['next_run_id'], reason=extension['reason'],
                additional_seconds=extension['additional_seconds']))
        atomic_json(policy_dir / 'status.json', {
            'run_id': run.run_id, 'observed_at': clock(), 'state': run.snapshot.get('state'),
            'revision': run.snapshot.get('revision'),
            'acceptance_status': (run.snapshot.get('application_state') or {}).get('acceptance_status', 'unverified'),
            'absolute_cap': policy['started_at'] + maximum_seconds,
            'extension': policy.get('extension'),
        })
        return run
