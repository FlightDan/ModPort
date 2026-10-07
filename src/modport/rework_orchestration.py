"""Durable SDK scheduling behind blocking reviewer tool calls."""
from pathlib import Path
import json
import math
import re
import time

from .contracts import OperationInput, OperationResult, json_copy
from .business_policy import business_gates_disabled
from .evidence import atomic_json, verified_path
from .progress_policy import progress_supervised
from .rework_tools import (REVIEW_AUTHORS, REVIEW_REPORT_PATHS, is_interactive_review,
                           continuation_rework_sources, rework_targets,
                           session_directory, uses_downstream_toolcall)


_DONE = frozenset({'succeeded', 'failed', 'cancelled', 'timed_out', 'dead'})
_REQUEST_ID = re.compile(r'^[A-Za-z0-9_-]{1,80}$')
_HOST_PAYLOAD_FIELDS = frozenset({
    'request', 'locked_artifacts', 'rework_context', 'planning_generation', 'repair_generation',
    'repair_scope', 'format_context', 'diagnosis_escalated', 'knowledge_gap_context',
    'unresolved_knowledge_gaps', 'known_knowledge_gap_ids', 'gap_identity_aliases',
    'research_kinds', 'research_budget', 'knowledge_revisions', 'project_research_gaps',
    'approved_gap_resolutions', 'gap_obligations', 'repair_context', 'continuation_feedback',
    'repair_feedback', 'review_rework_targets', 'gate_diagnostic', 'gate_diagnostics',
    'unavailable_artifact_refs',
    'latest_repair_feedback', 'reviewer_context_workspace',
})


def _attempts(snapshot):
    return {row['command']['execution_id']: (task_id, row)
            for task_id, task in snapshot.get('tasks', {}).items()
            for row in task.get('attempts', [])}


def _request_batches(sessions, *, ordered):
    batches = [(descriptor, [path for path in sorted(
        (descriptor.parent / 'requests').glob('*.json'))
        if not path.name.endswith('.cancel.json')])
        for descriptor in sorted(sessions.glob('*/session.json'))]
    if not ordered:
        return batches
    # The bridge publishes requests atomically. Use host-observed arrival order,
    # with a stable tie-breaker, rather than reviewer names or random UUIDs.
    arrivals = []
    for descriptor, paths in batches:
        for path in paths:
            try:
                arrivals.append((path.lstat().st_mtime_ns, str(path), descriptor, path))
            except FileNotFoundError:
                continue
    return [(descriptor, [path]) for _, _, descriptor, path in sorted(arrivals)]


def _source_command(snapshot, execution_id, caller=None):
    attempt = _attempts(snapshot).get(execution_id)
    if attempt is not None:
        return OperationInput.from_dict(attempt[1]['command']['payload'])
    if caller is not None:
        carried = continuation_rework_sources(snapshot, caller).get(execution_id)
        if carried is not None:
            return carried['command']
    raise KeyError(execution_id)


def _outcome(attempt):
    command = OperationInput.from_dict(attempt['command']['payload'])
    if attempt['state'] == 'succeeded':
        result = OperationResult.from_dict(attempt['result']['value'])
        result.validate_for(command)
        return result
    return OperationResult('failed', command.run_id, command.task_id, command.stage_id,
                           command.command_id, detail='Rework execution ' + attempt['state'],
                           error_code='rework_execution_' + attempt['state'])


def _result_text(root, outcome, *, max_chars=None):
    outputs = outcome.outputs
    refs = outputs.get('artifact_refs', {})
    paths = {key: ref.get('path') for key, ref in refs.items() if isinstance(ref, dict)}
    parts = ['Result: ' + outcome.status, 'Result artifact paths:\n' + json.dumps(paths, ensure_ascii=False)]
    if outcome.detail:
        parts.append(outcome.detail)
    # Stage reports and last messages are context, not machine output contracts.
    candidates = []
    if isinstance(outputs.get('last_message'), str):
        relative = Path(outputs['last_message'])
        if relative.is_absolute():
            relative = relative.relative_to(root)
        candidates.append({'path': relative.as_posix()})
    for key in (outcome.stage_id, 'gap_research', 'gap_plan', 'goal_acceptance_report'):
        if isinstance(refs.get(key), dict):
            candidates.append(refs[key])
    seen = set()
    for ref in candidates:
        try:
            path = verified_path(root, ref)
            if str(path) in seen:
                continue
            seen.add(str(path))
            with path.open(encoding='utf-8', errors='replace') as stream:
                text = stream.read(max_chars) if max_chars else stream.read()
            # Only the host wrapper is interpreted. The enclosed report is untouched.
            try:
                wrapper = json.loads(text)
                if isinstance(wrapper, dict) and wrapper.get('report_format') == 'raw_text':
                    text = wrapper['raw_report']
            except ValueError:
                pass
            parts.append(text)
        except (OSError, ValueError, TypeError):
            continue
    if outputs.get('after_head'):
        label = ('Candidate commit (no integrated changes): '
                 if outputs.get('integration_status') == 'no_changes'
                 else 'Revised candidate commit: ')
        parts.append(label + outputs['after_head'])
    text = '\n\n'.join(parts)
    return text[:max_chars] if max_chars else text


def _has_rework_candidate(outcome):
    """Whether a failed coder rework returned an applied candidate to verify."""
    outputs = outcome.outputs
    if not isinstance(outputs, dict):
        return False
    if outputs.get('integration_status') == 'integrated':
        return True
    if outputs.get('integration_status') == 'no_changes':
        return False
    if outputs.get('after_candidate_id'):
        return True
    after = outputs.get('after_head') or outputs.get('head')
    if not isinstance(after, str) or not after:
        return False
    before = outputs.get('before_head')
    return not isinstance(before, str) or not before or after != before


class ReviewReworkOrchestration:
    def _settle_user_cancelled_review_requests(self, snapshot, app):
        """Close only rework tools whose reviewer and child have both stopped."""
        if (app.get("user_cancelled") is not True
                or app.get("stop_reason") != "user_cancelled"):
            return
        ledger = app.get("review_rework", {}).get("requests", {})
        attempts = _attempts(snapshot)
        tasks = snapshot.get("tasks", {})
        for record in ledger.values():
            if record.get("state") != "running":
                continue
            reviewer_id = record.get("reviewer_execution_id")
            caller = attempts.get(reviewer_id)
            if caller is not None and caller[1].get("state") not in _DONE:
                continue
            task_id = record.get("task_id")
            task = tasks.get(task_id)
            if task is None:
                child_state = "task_missing"
            elif not task.get("attempts"):
                child_state = "no_attempt"
            else:
                child = task["attempts"][-1]
                child_state = child.get("state")
                if child_state not in _DONE:
                    continue
            record.update(
                state="failed",
                waiting_resources=False,
                resource_wait=None,
                caller_closed=True,
                cancellation_settlement={
                    "reason": "user_cancelled",
                    "reviewer_execution_id": reviewer_id,
                    "child_task_id": task_id,
                    "child_execution_state": child_state,
                    "accepted": False,
                },
                error="Run cancelled; rework output was retained but not accepted.",
            )

    def _review_rework_decision(self, snapshot, header, app):
        advisory = business_gates_disabled(header)
        repair_diagnostics = header.get('definition', {}).get('workflow_version', 0) >= 18
        queue_resources = header.get('definition', {}).get('workflow_version', 0) >= 21
        app.get("memory_waits", {}).pop("rework", None)
        ledger = app.setdefault('review_rework', {'requests': {}, 'latest_targets': {}, 'sequence': 0})
        attempts = _attempts(snapshot)
        root = Path(header['run_dir'])
        operations = []
        # Collect accepted child work before considering the next request. These
        # child tasks are not members of the reviewer's normal workflow group.
        for key, record in ledger['requests'].items():
            if record['state'] != 'running':
                continue
            caller = attempts.get(record['reviewer_execution_id'])
            task = snapshot['tasks'].get(record['task_id'])
            if task is None:
                continue  # Dispatch and application state commit atomically.
            attempt = task['attempts'][-1]
            cancel = self._tool_cancelled(root, record)
            caller_closed = caller is None or caller[1]['state'] in _DONE
            if record.get('waiting_resources') and attempt['state'] not in _DONE:
                # A planned SDK task holds no worker. Preserve its command and
                # identity across ticks/restarts, then dispatch that same task.
                expired = self.clock() >= record['queue_deadline_epoch']
                if cancel or (caller_closed and not advisory) or expired:
                    reason = ('Rework queue deadline exhausted.' if expired else
                              'Reviewer tool call expired or was cancelled.')
                    operations.append({'kind': 'cancel', 'task_id': record['task_id'],
                                       'reason': reason})
                    record.update(state='failed', error=reason, waiting_resources=False)
                    continue
                if not self._memory_capacity(snapshot, header, app, 'rework',
                        requested_stage=('agent_rework' if record.get('target_stage') == 'coder'
                                         else record.get('target_stage', 'agent_rework')),
                        exclude_execution_ids=(record['reviewer_execution_id'],
                                               attempt['command']['execution_id']),
                        retained_memory_execution_ids=(record['reviewer_execution_id'],)):
                    record['resource_wait'] = json_copy(app.get('memory_waits', {}).get(
                        'rework', {'reason': 'hard_cap_reached'}))
                    continue
                # The memory probe may block long enough to exhaust the
                # original tool deadline. Recheck immediately before dispatch.
                if self.clock() >= record['queue_deadline_epoch']:
                    reason = 'Rework queue deadline exhausted.'
                    operations.append({'kind': 'cancel', 'task_id': record['task_id'],
                                       'reason': reason})
                    record.update(state='failed', error=reason, waiting_resources=False)
                    continue
                operations.append({'kind': 'dispatch', 'task_id': record['task_id']})
                record.update(waiting_resources=False, resource_wait=None)
                continue
            if caller_closed:
                record['caller_closed'] = True
            if cancel or (caller_closed and not advisory and not record.get('followup_stage')):
                if attempt['state'] not in _DONE:
                    execution = attempt['command']['execution_id']
                    if execution not in app['cancel_sent']:
                        operations.append({'kind': 'cancel', 'task_id': record['task_id'],
                                           'reason': 'reviewer tool call closed'})
                        app['cancel_sent'].append(execution)
                    continue
                # A terminal child may already have committed code or reports.
                # Reconcile that outcome even when its caller stopped waiting.
            if attempt['state'] not in _DONE:
                continue
            outcome = _outcome(attempt)
            source_stage = record.get('followup_stage', record['target_stage'])
            update = {'stage': source_stage,
                      'target_agent': (source_stage if record.get('followup_stage')
                                       or record.get('restored_harness_repair')
                                       else record['target_agent']),
                      'result': outcome.to_dict()}
            record.setdefault('updates', []).append(update)
            record['text'] = '\n\n'.join(filter(None, [record.get('text'),
                _result_text(root, outcome, max_chars=12000 if repair_diagnostics else None)]))
            if (header.get('definition', {}).get('workflow_version', 0) >= 26
                    and source_stage == 'contract_freeze'
                    and outcome.status == 'completed'):
                lock_ref = outcome.outputs.get('artifact_refs', {}).get('functional_contract_lock')
                try:
                    from .evidence import file_digest
                    if not isinstance(lock_ref, dict):
                        raise ValueError('freeze did not publish a contract lock reference')
                    path = verified_path(root, lock_ref)
                    if lock_ref.get('sha256') != file_digest(path):
                        raise ValueError('contract lock reference digest does not match file')
                except (OSError, ValueError, TypeError) as exc:
                    record['lock_handoff_error'] = str(exc)
                    record['text'] += '\n\nContract lock handoff failed: ' + str(exc)
                else:
                    record['text'] += ('\n\nAuthenticated contract lock: '
                        + json.dumps({'freeze_execution_id': outcome.command_id,
                                      'functional_contract_lock': lock_ref}, ensure_ascii=False))
            if repair_diagnostics and record.get('followup_stage') in {'target_build', 'contract_verify'}:
                record['verification_execution_id'] = outcome.command_id
                record['after_inventory_ref'] = outcome.outputs.get('repair_inventory_ref')
            app['processed'].append(outcome.command_id)
            app['history'].append({'stage': outcome.stage_id, 'task_id': outcome.task_id,
                                   'execution_id': outcome.command_id, 'state': outcome.status,
                                   'reviewer_execution_id': record['reviewer_execution_id'],
                                   'rework_target': record['target_agent']})
            if (advisory or outcome.status == 'completed' or record.get('followup_stage')
                    or record.get('downstream_toolcall')):
                # The v16 downstream caller needs the actual requested outcome
                # even when the author or explicit recheck reports a failure.
                # Valid artifacts remain context; no retry is inferred.
                self._apply_tool_update(app, record, source_stage, outcome)
            if outcome.status == 'completed' or advisory:
                if source_stage == 'mod_analysis' and outcome.status == 'completed':
                    self._ingest_analysis(app, outcome.outputs)
                if not record.get('followup_stage'):
                    ledger['latest_targets'][record['target_agent']] = outcome.command_id
                verification = None
                # The initial contract reviewer consumes the fresh verifier
                # itself. A later caller needs a new independent review before
                # its revised candidate can replace the effective freeze.
                if repair_diagnostics and record.get('followup_stage') == 'contract_verify':
                    if (header.get('definition', {}).get('workflow_version', 0) >= 26
                            and (record['reviewer_stage'] != 'contract_review'
                                 or 'contract_freeze' in app.get('effective', {}))):
                        verification = 'contract_review'
                    elif header.get('definition', {}).get('workflow_version', 0) < 26:
                        verification = 'contract_freeze'
                elif (repair_diagnostics and record.get('followup_stage') == 'contract_review'
                        and header.get('definition', {}).get('workflow_version', 0) >= 26):
                    verification = 'contract_freeze'
                if not record.get('followup_stage'):
                    if repair_diagnostics and source_stage in {'coder', 'contract_draft', 'project_init'}:
                        verification = ('contract_verify' if source_stage == 'contract_draft'
                            or record.get('target_scope') == 'contract' else 'target_build')
                    elif record['reviewer_stage'] == 'contract_review' and source_stage in {'contract_draft', 'coder'}:
                        verification = 'contract_verify'
                    elif (record['reviewer_stage'] == 'code_review'
                          and source_stage in {'coder', 'code_cleanup'}):
                        verification = 'target_build'
                if (outcome.error_code == 'budget_exhausted'
                        and self.clock() >= record.get('queue_deadline_epoch', float('inf'))):
                    # A timed-out tool call cannot receive a fresh verification
                    # result. Keep its failure instead of launching work that
                    # the closed caller will immediately cancel.
                    verification = None
                if (header.get('definition', {}).get('workflow_version', 0) >= 30
                        and source_stage == 'coder'
                        and not record.get('followup_stage')
                        and outcome.status != 'completed'
                        and not _has_rework_candidate(outcome)):
                    # A pre-coder failure has no revised candidate for target_build.
                    # Keep and return the author's failure; verify failed rework
                    # whenever it did integrate a candidate above.
                    verification = None
                if verification and not cancel:
                    record['followup_stage'] = verification
                    suffix = {'contract_freeze': '.freeze', 'contract_review': '.review'}.get(
                        verification, '.verify')
                    followup_id = record['task_id'] + suffix
                    preserved_reports = (
                        {'reviewer_report_paths': REVIEW_REPORT_PATHS['code_review']}
                        if (header.get('definition', {}).get('workflow_version', 0) >= 30
                            and verification == 'target_build'
                            and source_stage == 'code_cleanup'
                            and record['reviewer_stage'] == 'code_review') else {})
                    followup = self._schedule(snapshot, header, app, verification,
                        task_id=followup_id, dependencies=[record['task_id']], activate=False,
                        extra_options=({'deadline_epoch': record['queue_deadline_epoch']}
                                       if (header.get('definition', {}).get('workflow_version', 0) >= 26
                                           and 'queue_deadline_epoch' in record) else None),
                        payload={'reviewer_rework': {'request_id': record['request_id'],
                                 'reviewer_execution_id': record['reviewer_execution_id'],
                                 'instructions': 'Run fresh verification of the reviewer-requested revision.'},
                                 **preserved_reports},
                        causation_id=outcome.command_id)
                    if any(op['kind'] in {'add_task', 'new_attempt', 'dispatch'} for op in followup):
                        operations += followup
                        record['task_id'] = followup_id
                        continue
                    if followup:
                        operations += followup
                        record.update(state='failed', error='Fresh ' + verification
                                      + ' could not start before the Run budget was exhausted.')
                        continue
                    record.update(state='failed', error='Could not schedule fresh ' + verification + '.')
                    continue
                record['state'] = ('failed' if cancel else
                                   'completed' if outcome.status == 'completed' else 'failed')
                if cancel:
                    record['error'] = 'Reviewer tool call expired or was cancelled.'
                elif outcome.status != 'completed':
                    record['error'] = outcome.detail or outcome.error_code or 'Rework failed.'
            else:
                record.update(state='failed', error=outcome.detail or outcome.error_code or 'Rework failed.')
            if repair_diagnostics:
                failed_updates = [row for row in record['updates'] if row['result'].get('status') != 'completed']
                if failed_updates:
                    failure = failed_updates[-1]
                    record.update(state='failed', error=failure['stage'] + ': ' +
                        (failure['result'].get('detail') or failure['result'].get('error_code') or 'failed'))
                if record.get('lock_handoff_error'):
                    record.update(state='failed', error='Contract lock handoff failed: '
                                  + record['lock_handoff_error'])
                from .repair_context import load_inventory
                from .repair_progress import build_repair_feedback, render_repair_feedback
                previous = [row for row in ledger['requests'].values()
                            if row.get('target_agent') == record['target_agent']
                            and row.get('sequence', 0) < record.get('sequence', 0)
                            and isinstance(row.get('repair_feedback'), dict)]
                previous_feedback = (max(previous, key=lambda row: row.get('sequence', 0))['repair_feedback']
                                     if previous else None)
                context = {**record,
                    'before_inventory': load_inventory(root, record.get('before_inventory_ref')),
                    'after_inventory': load_inventory(root, record.get('after_inventory_ref'))}
                record['repair_feedback'] = build_repair_feedback(context, previous=previous_feedback)
                record['text'] = (render_repair_feedback(record['repair_feedback']) + '\n'
                                  + json.dumps(record['repair_feedback'], ensure_ascii=False)
                                  + '\n\n' + record['text'])
            if record.get('caller_closed') and not advisory and not record.get('downstream_toolcall'):
                # An approval emitted before this result was delivered cannot
                # authorize the revised candidate. Preserve results and stop.
                app['stop_reason'] = 'reviewer_closed_before_rework_result'
                app['stop_state'] = 'failed'

        # One revision at a time avoids simultaneous changes to context shared
        # by waiting reviews. It also bounds the extra executor capacity needed.
        active = any(row['state'] == 'running' for row in ledger['requests'].values())
        sessions = root / 'artifacts/rework-tools'
        if not sessions.is_dir():
            return operations
        for descriptor_path, request_paths in _request_batches(sessions, ordered=queue_resources):
            key = record = None
            try:
                descriptor = json.loads(verified_path(root, {'path': str(descriptor_path.relative_to(root))}).read_text())
                caller_id = descriptor['reviewer_execution_id']
                caller_entry = attempts.get(caller_id)
                if caller_entry is None:
                    continue
                caller = OperationInput.from_dict(caller_entry[1]['command']['payload'])
                downstream = uses_downstream_toolcall(caller)
                if (caller.run_id != descriptor.get('run_id')
                        or (not downstream and caller.stage_id not in REVIEW_AUTHORS)
                        or (downstream and not is_interactive_review(caller))):
                    continue
                available = (rework_targets(snapshot, caller) if downstream
                             else caller.payload.get('review_rework_targets', []))
                targets = {row['target_agent']: row for row in available}
                for path in request_paths:
                    if path.name.endswith('.cancel.json'):
                        continue
                    request_id = path.stem
                    if not _REQUEST_ID.fullmatch(request_id):
                        continue
                    key = caller_id + '/' + request_id
                    if key in ledger['requests']:
                        continue
                    request = json.loads(verified_path(root, {'path': str(path.relative_to(root))}).read_text())
                    record = {'request_id': request_id, 'reviewer_execution_id': caller_id,
                              'reviewer_stage': caller.stage_id, 'sequence': ledger['sequence'] + 1,
                              'state': 'pending', 'updates': [],
                              'downstream_toolcall': downstream}
                    if repair_diagnostics:
                        record['repair_diagnostics'] = True
                        record['run_id'] = caller.run_id
                        record['target_agent'] = request.get('target_agent')
                    error = None
                    target = targets.get(request.get('target_agent'))
                    if (request.get('request_id') != request_id or request.get('run_id') != caller.run_id
                            or request.get('reviewer_execution_id') != caller_id):
                        error = 'Request does not belong to this reviewer execution.'
                    elif target is None:
                        error = 'Target is not an upstream author available to this reviewer.'
                    elif not isinstance(request.get('instructions'), str) or not request['instructions'].strip():
                        error = 'Provide the author with rework instructions.'
                    elif (caller_entry[1]['state'] in _DONE and not advisory) or self._tool_cancelled(root, record):
                        error = 'Reviewer is no longer waiting for this request.'
                    elif self.clock() >= descriptor['deadline_epoch']:
                        error = 'Reviewer time budget exhausted.'
                    elif (queue_resources
                          and type(request.get('response_deadline_epoch')) in (int, float)
                          and math.isfinite(request['response_deadline_epoch'])
                          and self.clock() >= request['response_deadline_epoch']):
                        error = 'Rework response deadline exhausted before admission.'
                    if error:
                        record.update(state='failed', error=error)
                        ledger['sequence'] = record['sequence']
                        ledger['requests'][key] = record
                        continue
                    if active:
                        # Leave the request on disk until the in-flight mutation settles.
                        continue
                    budget = header['request']['budget']
                    limit = budget['max_agent_assignments']
                    family = 'review_rework:' + request['target_agent']
                    if limit is not None and app['agent_assignments'] >= limit:
                        error = 'Agent assignment budget exhausted.'
                    elif (not progress_supervised(header)
                          and app['rounds'].get(family, 0) >= budget['max_rework_rounds']):
                        error = 'Rework round budget exhausted for this author.'
                    if error:
                        record.update(state='failed', error=error)
                        ledger['sequence'] = record['sequence']
                        ledger['requests'][key] = record
                        continue
                    original = _source_command(snapshot, target['execution_id'], caller)
                    restored_harness_repair = (
                        queue_resources
                        and target.get('restored_harness') is True
                        and request.get('target_agent') == 'contract_restore'
                        and original.stage_id == 'contract_restore'
                        and original.task_id == 'contract_restore'
                    )
                    rework_stage = ('contract_draft' if restored_harness_repair
                                    else 'agent_rework' if original.stage_id == 'coder'
                                    else original.stage_id)
                    capacity = self._memory_capacity(
                            snapshot, header, app, 'rework', requested_stage=rework_stage,
                            # The reviewer is synchronously blocked in the
                            # tool call until this child settles. Reuse its
                            # active coder slot for the child; the child still
                            # goes through its own memory admission and may
                            # wait for real host memory before starting.
                            exclude_execution_ids=(caller_id,),
                            retained_memory_execution_ids=(caller_id,))
                    if not capacity and not queue_resources:
                        # The caller is alive and synchronously waiting. Do
                        # not queue work that cannot start: there is no later
                        # host tick that could wake this tool call safely.
                        record.update(state='failed', error='Insufficient memory or coder capacity for nested rework; submit a new request when capacity is available.')
                        ledger['sequence'] = record['sequence']
                        ledger['requests'][key] = record
                        app.get('memory_waits', {}).pop('rework', None)
                        continue
                    latest_id = ledger['latest_targets'].get(request['target_agent'])
                    try:
                        latest = (_source_command(snapshot, latest_id, caller)
                                  if latest_id else original)
                    except KeyError:
                        latest = original
                    # Native coder continuations always retain the original task;
                    # report authors can use their most recent amended context.
                    if original.stage_id == 'coder':
                        latest = original
                    task_id = 'agent-rework.' + request_id
                    record.update(target_agent=request['target_agent'],
                                  target_stage=('contract_draft' if restored_harness_repair
                                                else original.stage_id),
                                  source_execution_id=original.command_id, task_id=task_id,
                                  instructions=request['instructions'])
                    if restored_harness_repair:
                        record['restored_harness_repair'] = True
                    if repair_diagnostics:
                        scope = ('contract' if restored_harness_repair else
                                 original.payload.get('goal_scope', 'migration'))
                        check = 'contract_verify' if scope == 'contract' or original.stage_id == 'contract_draft' else 'target_build'
                        baseline = app['effective'].get(check, {})
                        baseline_outputs = baseline.get('outputs', {})
                        record.update(run_id=caller.run_id, target_scope=scope,
                            before_inventory_ref=baseline_outputs.get('repair_inventory_ref')
                                or descriptor.get('repair_context', {}).get('inventory_ref'),
                            baseline_verification={key: baseline[key] for key in
                                ('run_id', 'task_id', 'stage_id', 'command_id', 'status', 'error_code', 'detail')
                                if key in baseline})
                        record['baseline_verification']['outputs'] = {
                            key: baseline_outputs[key] for key in ('verification_candidate_id',
                                'build_status', 'verification_status', 'build_error_code',
                                'verification_error_code', 'failure_signature', 'build_executed',
                                'verification_executed', 'build_detail', 'verification_detail',
                                'build_failure_signature', 'verification_failure_signature')
                            if key in baseline_outputs}
                    workspace = descriptor['workspace']
                    if Path(workspace).is_absolute() or '..' in Path(workspace).parts:
                        raise ValueError('unsafe reviewer workspace')
                    proposed = json_copy(app)
                    payload = {**{name: value for name, value in latest.payload.items()
                                  if name not in _HOST_PAYLOAD_FIELDS},
                        'reviewer_rework': {**request, 'source_execution_id': original.command_id},
                        'reviewer_execution_id': caller_id,
                        'reviewer_workspace': workspace,
                        'reviewer_report_paths': ([
                            '.modport/gate-handoffs/' + caller.command_id + '.md']
                            if caller.stage_id == 'gate_handoff' else
                            REVIEW_REPORT_PATHS.get(caller.stage_id, []) +
                            (['.modport/test-assessment.json'] if caller.stage_id == 'contract_review'
                             and caller.options.get('workflow_version', 0) >= 31 else []))}
                    if restored_harness_repair:
                        payload['goal_scope'] = 'contract'
                    extra_options = {key: value for key, value in latest.options.items()
                                     if key not in {'agent_assignment', 'deadline_epoch', 'rework_round', 'native_goal_resume'}}
                    if restored_harness_repair:
                        # The authenticated source is deterministic, but this
                        # request launches a real contract author. Let the
                        # scheduler supply the author model and baseline
                        # workspace instead of inheriting restore options.
                        for name in ('model', 'reasoning_effort', 'workspace'):
                            extra_options.pop(name, None)
                    if queue_resources:
                        # Queue time consumes the original caller/Run budget.
                        # A later dispatch never creates a fresh waiting budget.
                        bounds = [descriptor['deadline_epoch']]
                        if header.get('definition', {}).get('workflow_version', 0) < 26:
                            bounds.append(self.clock() + 1200)
                        response_deadline = request.get('response_deadline_epoch')
                        if (type(response_deadline) in (int, float)
                                and math.isfinite(response_deadline)):
                            bounds.append(response_deadline)
                        run_deadline = self._effective_deadline(header, app)
                        if run_deadline is not None:
                            bounds.append(run_deadline)
                        record['queue_deadline_epoch'] = min(bounds)
                        extra_options['deadline_epoch'] = record['queue_deadline_epoch']
                    stage = ('contract_draft' if restored_harness_repair
                             else original.stage_id)
                    if stage == 'coder':
                        stage = 'agent_rework'
                        payload.update(rework_original_command=original.to_dict(),
                                       rework_generation=app['agent_assignments'] + 1)
                        if repair_diagnostics:
                            # The caller's inspection copy can differ from the
                            # product this coder must repair (baseline/target).
                            payload['reviewer_context_workspace'] = workspace
                            payload['reviewer_workspace'] = ('baseline'
                                if original.payload.get('goal_scope') == 'contract' else 'worktree')
                        extra_options.pop('workspace', None)
                    elif stage == 'test_design':
                        extra_options['workspace'] = 'workspaces/tests/rework-' + request_id
                    # Overlay current author/reviewer refs; never replay a stale
                    # code-review or old planning artifact over a newer one.
                    current_refs = {**latest.artifact_refs, **caller.artifact_refs, **self._refs(header, app)}
                    if (stage == 'agent_rework'
                            and header.get('definition', {}).get('workflow_version', 0) >= 26):
                        from .supervised_goals import target_key
                        current_refs.pop('supervised_goal_revision', None)
                        source_task = original.payload.get('development_task')
                        source_plan = original.artifact_refs.get('development_plan')
                        selected_revision = None
                        if isinstance(source_task, dict) and isinstance(source_plan, dict):
                            revision = app.get('supervision', {}).get('goal_revisions', {}).get(
                                target_key(source_task, source_plan))
                            if isinstance(revision, dict):
                                selected_revision = revision.get('revision_ref')
                        if selected_revision is None:
                            selected_revision = original.artifact_refs.get('supervised_goal_revision')
                        if isinstance(selected_revision, dict):
                            current_refs['supervised_goal_revision'] = json_copy(selected_revision)
                    if stage == 'test_design':
                        current_refs.update(caller.artifact_refs)
                    if queue_resources and self.clock() >= record['queue_deadline_epoch']:
                        record.update(state='failed', error='Rework response deadline exhausted before admission.')
                        ledger['sequence'] = record['sequence']
                        ledger['requests'][key] = record
                        continue
                    dispatched = self._schedule(snapshot, header, proposed, stage,
                        task_id=task_id, dependencies=[], activate=False, payload=payload,
                        extra_options=extra_options, artifact_overrides=current_refs,
                        upstream_overrides={**latest.upstream_results, **caller.upstream_results, **app['effective']},
                        causation_id=caller_id)
                    if not any(op['kind'] in {'add_task', 'new_attempt'} for op in dispatched):
                        record.update(state='failed', error='No rework was dispatched: the author research or execution budget is exhausted.')
                        ledger['sequence'] = record['sequence']
                        ledger['requests'][key] = record
                        continue
                    self._charge_rework(header, proposed, family)
                    # _schedule mutates only the proposed application; accept it
                    # only once a real SDK dispatch is available.
                    app.clear()
                    app.update(proposed)
                    ledger = app['review_rework']
                    record['state'] = 'running'
                    if queue_resources and not capacity:
                        # add_task persists the complete work in Orchestrator;
                        # only dispatch is deferred. No replacement request or
                        # second author attempt is needed when capacity returns.
                        dispatched = [op for op in dispatched if op['kind'] != 'dispatch']
                        record.update(waiting_resources=True,
                            resource_wait=json_copy(app.get('memory_waits', {}).get(
                                'rework', {'reason': 'hard_cap_reached'})))
                    ledger['sequence'] = record['sequence']
                    ledger['requests'][key] = record
                    operations += dispatched
                    active = True
            except (OSError, ValueError, KeyError, TypeError) as exc:
                if key and record and record.get('state') == 'pending':
                    record.update(state='failed', error='Rework could not be scheduled: ' + str(exc))
                    ledger['sequence'] = record['sequence']
                    ledger['requests'][key] = record
                continue
        return operations

    @staticmethod
    def _tool_cancelled(root, record):
        directory = session_directory(root, record['reviewer_execution_id'])
        return ((directory / 'closed.json').exists()
                or (directory / 'requests' / (record['request_id'] + '.cancel.json')).exists())

    @staticmethod
    def _apply_tool_update(app, record, stage, outcome):
        canonical = outcome.to_dict()
        if record.get('repair_diagnostics'):
            app.setdefault('locked_artifacts', {}).update(outcome.outputs.get('locked_artifacts', {}))
        result = canonical
        if stage == 'coder':
            # Keep proof artifacts without replacing the global execution plan.
            result = json_copy(canonical)
            refs = result.get('outputs', {}).get('artifact_refs', {})
            result['outputs']['artifact_refs'] = {'review_rework:' + record['request_id'] + ':' + alias: ref
                                                  for alias, ref in refs.items()}
        else:
            app['effective'].pop(stage, None)
            app['effective'][stage] = result
        restored_harness_repair = (record.get('restored_harness_repair')
                                   and not record.get('followup_stage'))
        group = app.get('active_group')
        target_agent = record['target_agent']
        if (not restored_harness_repair and not record.get('followup_stage')
                and isinstance(group, dict)
                and target_agent in group.get('results', {})):
            # Group schedulers consume their own result map rather than the
            # top-level effective aliases. Replace the reviewed target there
            # as well, or a later join will restore and execute the old result.
            group['results'][target_agent] = (canonical if record.get('downstream_toolcall')
                                               else result)
        if not restored_harness_repair:
            app['effective'][stage if record.get('followup_stage') else target_agent] = result


def project_rework_responses(root, app):
    """Publish only decisions already committed by the public SDK API."""
    for record in app.get('review_rework', {}).get('requests', {}).values():
        if record['state'] in {'pending', 'running'}:
            continue
        directory = session_directory(root, record['reviewer_execution_id']) / 'responses'
        path = directory / (record['request_id'] + '.json')
        if path.is_file():
            continue
        response = {key: record[key] for key in ('request_id', 'sequence', 'updates') if key in record}
        response.update(status=record['state'], text=record.get('text', ''), error=record.get('error', ''))
        if 'repair_feedback' in record:
            response['repair_feedback'] = record['repair_feedback']
        elif record.get('repair_diagnostics'):
            from .repair_progress import build_repair_feedback, render_repair_feedback
            feedback = build_repair_feedback(record)
            response['repair_feedback'] = feedback
            response['text'] = render_repair_feedback(feedback) + '\n\n' + response['text']
        response['current_context'] = {
            name: app.get(name, {}) for name in ('gap_identity_aliases', 'research_budget', 'knowledge_revisions')}
        response['current_context'].update(
            known_knowledge_gap_ids=app.get('known_knowledge_gap_ids', []),
            approved_gap_resolutions=app.get('approved_gap_resolutions', []),
            project_research_gaps=list(app.get('project_research_gaps', {}).values()),
            gap_obligations=list(app.get('project_verification_gaps', {}).values()))
        if record.get('repair_diagnostics'):
            try:
                from .repair_context import load_inventory
                from .repair_routing import route_inventory
                inventory = load_inventory(root, record.get('after_inventory_ref'))
                if inventory is not None:
                    descriptor = json.loads((directory.parent / 'session.json').read_text())
                    targets = [dict(row) for row in descriptor['targets']]
                    routes = route_inventory(inventory, targets)
                    route_path = directory.parent / 'routes' / (record['request_id'] + '.json')
                    atomic_json(route_path, routes)
                    context = {'inventory_ref': record['after_inventory_ref'],
                        'routes_path': route_path.relative_to(root).as_posix(),
                        'candidate_id': inventory.get('candidate_id'),
                        'issue_count': len(inventory['issues']),
                        'coverage_complete': inventory['coverage'].get('complete', False),
                        'unassigned_issue_count': len(routes['unassigned_issue_ids'])}
                    for target in targets:
                        ids = [row['issue_id'] for row in routes['routes']
                               if target['target_agent'] in row['target_agents']]
                        target.update(recommended_issue_ids=ids[:100], recommended_issue_count=len(ids))
                    latest_path = directory.parent / 'latest-context.json'
                    latest = json.loads(latest_path.read_text()) if latest_path.is_file() else {}
                    if latest.get('sequence', -1) < record.get('sequence', 0):
                        atomic_json(latest_path, {'sequence': record.get('sequence', 0),
                            'targets': targets, 'repair_context': context})
                    response['repair_context'] = context
                    response['text'] += '\nUpdated issue inventory and routes: ' + json.dumps(context, ensure_ascii=False)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                response['repair_context_error'] = str(exc)[:1000]
                response['text'] += '\nUpdated issue routes unavailable: ' + str(exc)[:1000]
        atomic_json(path, response)
