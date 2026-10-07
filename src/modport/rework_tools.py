"""Reviewer-local tool sessions and raw context returned by host rework tasks."""
from dataclasses import replace
import json
import math
from pathlib import Path
import time

from .contracts import OperationInput, OperationResult
from .evidence import atomic_json, file_digest, verified_path
from .progress_policy import progress_supervised


REVIEW_AUTHORS = {
    'parallel_review': {'migration_inventory', 'migration_plan', 'migration_tasks'},
    'contract_repair_review': {'contract_diagnose', 'contract_repair_plan', 'contract_repair_tasks'},
    'target_repair_review': {'target_diagnose', 'target_repair_plan', 'target_repair_tasks'},
    'contract_review': {'contract_draft', 'coder'},
    'code_review': {'coder', 'code_cleanup'},
    'research_review': {'gap_research'},
    'admin_review': {'gap_research'},
    'gap_plan_review': {'gap_plan', 'gap_research'},
    'platform_skill_review': {'platform_diff'},
    'java_skill_review': {'java_diff'},
    'test_review': {'test_design'},
    'gap_review': {'gap_research', 'mod_analysis'},
}
REVIEW_REPORT_PATHS = {
    'contract_review': ['.modport/contract-review.json'],
    'code_review': ['.modport/code-review.json'],
    'research_review': ['.modport/research-review.json'],
    'admin_review': ['.modport/admin-review.json'],
    'gap_plan_review': ['.modport/gap-plan-review.json'],
    'gap_review': ['.modport/gap-review.json'],
    'platform_skill_review': ['review.json'],
    'java_skill_review': ['review.json'],
}

DOWNSTREAM_TOOLCALL_POLICY = 'downstream_toolcall'
CONTINUATION_REWORK_SOURCES = 'continuation:rework_sources'
_SETTLED = frozenset({'succeeded', 'failed', 'cancelled', 'timed_out', 'dead'})

FORWARD_ONLY_PLANNING_ROLES = frozenset({
    'migration_inventory', 'migration_plan', 'migration_tasks', 'parallel_review',
    'contract_diagnose', 'contract_repair_plan', 'contract_repair_tasks',
    'contract_repair_review', 'target_diagnose', 'target_repair_plan',
    'target_repair_tasks', 'target_repair_review',
})


def is_forward_only_planning(command):
    # Revival has its own host-owned SDK decision route. A generic rework call
    # from this planner would wait for an author while the host waits for the
    # planner result, so never expose the reviewer rework transport here.
    if (command.stage_id == 'coder_revival_plan'
            and command.options.get('workflow_version', 0) >= 25):
        return True
    return (not uses_downstream_toolcall(command)
            and command.options.get('workflow_version', 0) >= 15
            and command.stage_id in FORWARD_ONLY_PLANNING_ROLES)


def uses_downstream_toolcall(command):
    """Expose explicit author requests in v16 and business-gate-free workflows."""
    from .business_policy import business_gates_disabled
    if business_gates_disabled(command):
        return True
    version = command.options.get('workflow_version', 0)
    return (type(version) is int and version >= 16
            and command.options.get('gate_policy') == DOWNSTREAM_TOOLCALL_POLICY)


def _is_agent_stage(stage):
    # Import lazily because workflow construction imports operation helpers.
    from .workflow import AGENT_STAGES
    return stage in AGENT_STAGES or stage == 'gate_handoff'


def _is_watchdog_supervisor(command):
    from collections.abc import Mapping
    return (command.stage_id == 'supervisor'
            and isinstance(command.payload.get('watchdog_incident'), Mapping)
            and 'progress_supervision' not in command.payload
            and 'desktop_chat' not in command.payload)


def is_interactive_review(command):
    if command.stage_id in {'research_cleanup', 'code_cleanup', 'final_cleanup'}:
        # Cleanup workers may themselves be rework targets, but they do not
        # open nested reviewer sessions or release their write-scope lock.
        return False
    if is_forward_only_planning(command):
        return False
    targets = bool(command.payload.get('review_rework_targets'))
    if uses_downstream_toolcall(command):
        return ((_is_agent_stage(command.stage_id) or _is_watchdog_supervisor(command))
                and not isinstance(command.payload.get('reviewer_rework'), dict)
                and targets)
    return (not is_forward_only_planning(command)
            and command.stage_id in REVIEW_AUTHORS and targets)


def interactive_review_timeout_cap(command, *, default=7200.0, now=None):
    """Keep supervised assignments inside their original remaining Run deadline.

    A reviewer synchronously waits for its author and fresh verifier.  Capping
    the parent at the ordinary two-hour agent window cuts off useful author
    work even when the shared Run budget still has time available.
    """
    if progress_supervised(command):
        deadline = command.options.get('deadline_epoch')
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise ValueError('Progress-supervised assignment requires the original Run deadline.')
        return max(0.0, deadline - (time.time() if now is None else now))
    if command.options.get('workflow_version', 0) < 26 or not is_interactive_review(command):
        return default
    deadline = command.options.get('deadline_epoch')
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        return default
    return max(default, deadline - (time.time() if now is None else now))


def session_directory(root, reviewer_execution_id):
    relative = Path('artifacts/rework-tools') / reviewer_execution_id
    if (not isinstance(reviewer_execution_id, str) or not reviewer_execution_id
            or Path(reviewer_execution_id).name != reviewer_execution_id
            or reviewer_execution_id in {'.', '..'}):
        raise ValueError('invalid reviewer execution identity')
    path = Path(root) / relative
    if path.resolve() != path.absolute():
        raise ValueError('unsafe reviewer session directory')
    return path


def continuation_rework_sources(snapshot, operation):
    """Read prior-segment sources only through the current frozen continuation."""
    ref = operation.artifact_refs.get(CONTINUATION_REWORK_SOURCES)
    if not isinstance(ref, dict):
        return {}
    wanted = {
        result.get('command_id')
        for result in operation.upstream_results.values()
        if isinstance(result, dict) and isinstance(result.get('command_id'), str)
    }
    if not wanted:
        return {}
    root = Path(operation.run_dir)
    try:
        expected_path = ('artifacts/continuations/' + str(snapshot.get('run_id'))
                         + '/rework-sources.json')
        if ref.get('path') != expected_path:
            return {}
        metadata = ref.get('metadata')
        if (not isinstance(metadata, dict)
                or metadata.get('continuation_rework_sources') is not True
                or metadata.get('next_run_id') != snapshot.get('run_id')):
            return {}
        marker = root / 'run.json'
        if (not marker.is_file() or marker.is_symlink()
                or marker.resolve() != marker.absolute()):
            return {}
        header = json.loads(marker.read_text())
        if not isinstance(header, dict):
            return {}
        if header.get('run_id') == snapshot.get('run_id'):
            continuation = header.get('continuation')
            if not isinstance(continuation, dict):
                return {}
            frozen_ref = continuation.get('support_refs', {}).get(CONTINUATION_REWORK_SOURCES)
            expected = {
                'previous_run_id': continuation.get('previous_run_id'),
            }
            if (frozen_ref != ref
                    or metadata.get('previous_run_id') != expected['previous_run_id']
                    or operation.run_id != header.get(
                        'logical_run_id', metadata.get('previous_run_id'))):
                return {}
        elif (snapshot.get('tasks') != {}
                or header.get('run_id') != metadata.get('previous_run_id')
                or operation.run_id != header.get('logical_run_id', header.get('run_id'))):
            # During atomic continuation preparation, the prior header remains
            # current until every successor command has been frozen.
            return {}
        # The archive reader validates the digest, every row identity and all
        # document metadata while decoding one catalog row at a time.  Supplying
        # only the caller's upstream executions retains the same eligible targets
        # without keeping an unrelated, potentially huge catalog in memory.
        from .rework_source_archive import _read_sources
        _, selected, _ = _read_sources(
            root,
            # Historical callers did not require a media type. Preserve that
            # eligibility while using the stricter shared archive reader.
            {**ref, 'media_type': 'application/json'},
            operation.run_id,
            snapshot.get('run_id'),
            wanted,
        )
        sources = {}
        for execution_id, entry in selected.items():
            row = entry['row']
            source = entry['command']
            sources[source.command_id] = {'command': source,
                                          'target_agent': row.get('target_agent'),
                                          'terminal_state': row['terminal_state']}
        for row in sources.values():
            source = row['command']
            if row['target_agent'] != source.task_id:
                return {}
            request = source.payload.get('reviewer_rework')
            if (isinstance(request, dict)
                    and sources.get(request.get('source_execution_id')) is None):
                return {}
        return sources
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        return {}


def rework_targets(snapshot, operation):
    """Expose actual upstream author assignments, not arbitrary commands."""
    if operation.stage_id in {'research_cleanup', 'code_cleanup', 'final_cleanup'}:
        return []
    if is_forward_only_planning(operation):
        return []
    downstream = uses_downstream_toolcall(operation)
    if downstream:
        if (not (_is_agent_stage(operation.stage_id) or _is_watchdog_supervisor(operation))
                or isinstance(operation.payload.get('reviewer_rework'), dict)):
            return []
        allowed = None
    else:
        allowed = REVIEW_AUTHORS.get(operation.stage_id, set())
    if allowed is not None and not allowed:
        return []
    upstream = {result.get('command_id'): result
                for result in operation.upstream_results.values()
                if isinstance(result, dict) and isinstance(result.get('command_id'), str)}
    attempts = {row['command']['execution_id']: (task_id, row)
                for task_id, task in snapshot.get('tasks', {}).items()
                for row in task.get('attempts', [])}
    carried = continuation_rework_sources(snapshot, operation) if downstream else {}
    targets = {}
    for execution, upstream_result in upstream.items():
        carried_source = carried.get(execution)
        attempt = None
        if execution not in attempts and carried_source is None:
            continue
        if carried_source is None:
            task_id, attempt = attempts[execution]
            source = OperationInput.from_dict(attempt['command']['payload'])
            terminal_state = attempt.get('state')
        else:
            source = carried_source['command']
            task_id = carried_source['target_agent']
            terminal_state = carried_source['terminal_state']
        if downstream and (source.run_id != operation.run_id
                or any(upstream_result.get(name) != getattr(source, name)
                       for name in ('run_id', 'task_id', 'stage_id', 'command_id'))):
            continue
        request = source.payload.get('reviewer_rework')
        if isinstance(request, dict):
            original_execution = request.get('source_execution_id')
            original = attempts.get(original_execution)
            carried_original = carried.get(original_execution)
            if original is None and carried_original is not None:
                task_id = carried_original['target_agent']
                source = carried_original['command']
            if original is None:
                if carried_original is None:
                    continue
            else:
                task_id, original_attempt = original
                source = OperationInput.from_dict(original_attempt['command']['payload'])
            if downstream and source.run_id != operation.run_id:
                continue
        if (source.stage_id == 'final_cleanup'
                or (allowed is not None and source.stage_id not in allowed)
                or terminal_state not in _SETTLED):
            continue
        restored_harness = False
        if (downstream
                and operation.options.get('workflow_version', 0) >= 21
                and source.stage_id == 'contract_restore'
                and source.task_id == 'contract_restore'
                and terminal_state == 'succeeded'
                and attempt is not None):
            try:
                restore_outcome = OperationResult.from_dict(attempt['result']['value'])
                restore_outcome.validate_for(source)
                restored_harness = (
                    restore_outcome.status == 'completed'
                    and all(upstream_result.get(name) == getattr(restore_outcome, name)
                            for name in ('run_id', 'task_id', 'stage_id', 'command_id', 'status'))
                )
            except (KeyError, TypeError, ValueError):
                restored_harness = False
        recheck = False
        if downstream and not _is_agent_stage(source.stage_id):
            # A handoff may explicitly rerun the actual failed deterministic
            # check. A v21 restored harness is the authenticated source for a
            # new contract author; the restore handler itself is never rerun.
            # Other downstream agents can only revise author work.
            if restored_harness:
                pass
            elif (operation.stage_id == 'gate_handoff'
                    and upstream_result.get('status') != 'completed'):
                recheck = True
            else:
                continue
        if source.stage_id == 'coder':
            scope = source.payload.get('goal_scope', 'migration')
            if (not downstream
                    and (operation.stage_id == 'contract_review') != (scope == 'contract')):
                continue
            task_description = source.payload.get('development_task', {}).get('objective', '')
        else:
            task_description = source.stage_id
        target_agent = 'contract_restore' if restored_harness else task_id
        targets[target_agent] = {
            'target_agent': target_agent,
            'execution_id': source.command_id,
            'stage': source.stage_id,
            'description': ('repair the authenticated restored baseline harness'
                            if restored_harness else task_description),
        }
        if operation.options.get('workflow_version', 0) >= 18:
            task = source.payload.get('development_task', {})
            targets[target_agent].update(
                owned_paths=task.get('owned_paths', []),
                dependencies=task.get('dependencies', []),
                goal_scope=('contract' if restored_harness else
                            source.payload.get('goal_scope', 'migration')),
            )
        if restored_harness:
            targets[target_agent]['restored_harness'] = True
        if recheck:
            targets[target_agent]['recheck'] = True
    return [targets[key] for key in sorted(targets)]


def prepare_session(command, workspace, timeout):
    from .workspace import project_relative
    if not is_interactive_review(command):
        return None
    directory = session_directory(command.run_dir, command.command_id)
    descriptor = {'run_id': command.run_id, 'reviewer_execution_id': command.command_id,
                  'reviewer_task_id': command.task_id, 'reviewer_stage': command.stage_id,
                  'workflow_version': command.options.get('workflow_version', 0),
                  'workspace': project_relative(command.run_dir, workspace).as_posix(),
                  'deadline_epoch': time.time() + timeout,
                  'drain_pending_on_eof': _drain_pending_on_eof(command),
                  'targets': [dict(target) for target in command.payload['review_rework_targets']]}
    if progress_supervised(command):
        deadline = command.options.get('deadline_epoch')
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise ValueError('Progress-supervised assignment requires the original Run deadline.')
        descriptor['deadline_epoch'] = min(descriptor['deadline_epoch'], deadline)
    path = directory / 'session.json'
    # A restarted transport retains its request ledger and original deadline.
    if path.is_file():
        previous = json.loads(path.read_text())
        descriptor['deadline_epoch'] = min(previous['deadline_epoch'], descriptor['deadline_epoch'])
        if 'repair_context' in previous:
            descriptor['repair_context'] = previous['repair_context']
            descriptor['targets'] = previous['targets']
    if (command.options.get('workflow_version', 0) >= 18 and 'repair_context' not in descriptor
            and not _is_watchdog_supervisor(command)):
        from .repair_context import (publish_inventory, observe_candidate,
                                     inherited_harness_candidate_mode)
        from .repair_routing import route_inventory
        missing = list(command.payload.get('unavailable_artifact_refs', {}))
        if (command.stage_id in {'code_review', 'test_design', 'test_review', 'gap_review'}
                and 'functional_contract_lock' not in command.artifact_refs):
            missing.append('functional_contract_lock')
        host_logs = {}
        inherited_harness = inherited_harness_candidate_mode(command, workspace)
        candidate_id = observe_candidate(workspace, inherited_harness=inherited_harness)
        historical_checks = []
        for result in command.upstream_results.values():
            if isinstance(result, dict) and result.get('stage_id') in {
                    'target_build', 'acceptance_build', 'contract_verify', 'test_execute', 'client_smoke'}:
                outputs = result.get('outputs', {})
                if (candidate_id and result.get('run_id') == command.run_id
                        and outputs.get('verification_candidate_id') == candidate_id):
                    host_logs.update(outputs.get('artifact_refs', {}))
                elif result.get('command_id') not in historical_checks:
                    historical_checks.append(result.get('command_id'))
        try:
            inventory, ref = publish_inventory(command, workspace, outputs={'artifact_refs': host_logs}, missing_inputs=missing,
                                               label='review-inventory',
                                               inherited_harness=inherited_harness)
            routes = route_inventory(inventory, descriptor['targets'])
            atomic_json(directory / 'repair-routes.json', routes)
            descriptor['repair_context'] = {
                'inventory_ref': ref,
                'routes_path': str((directory / 'repair-routes.json').relative_to(command.run_dir)),
                'candidate_id': inventory.get('candidate_id'),
                'issue_count': len(inventory['issues']),
                'coverage_complete': inventory['coverage'].get('complete', False),
                'historical_check_execution_ids': historical_checks[:20],
                'unassigned_issue_count': len(routes['unassigned_issue_ids'])}
            for target in descriptor['targets']:
                ids = [r['issue_id'] for r in routes['routes']
                       if target['target_agent'] in r['target_agents']]
                target['recommended_issue_ids'] = ids[:100]
                target['recommended_issue_count'] = len(ids)
        except (OSError, ValueError) as exc:
            descriptor['repair_context'] = {'error': str(exc), 'acceptance_evidence': False}
    atomic_json(path, descriptor)
    return path


def opencode_tool_config(session, timeout):
    """Configure the existing request ledger as an OpenCode local MCP server.

    OpenCode's timeout is in milliseconds.  The request tool itself retains
    the SDK task deadline and response ledger across transport disconnects.
    """
    if session is None:
        return {}
    from .mcp_launcher import trusted_mcp_command
    return {'modport_rework': {
        'type': 'local',
        'command': trusted_mcp_command('modport.rework_mcp', __file__, session),
        'environment': {},
        'enabled': True,
        'timeout': max(1000, math.ceil(timeout * 1000)),
    }}


def _drain_pending_on_eof(command):
    """Use the v17 lifecycle guard without changing frozen older workflows."""
    from .business_policy import business_gates_disabled
    return business_gates_disabled(command)


def tool_prompt(command):
    if is_forward_only_planning(command):
        return ''
    phase = command.options.get('dialogue_phase')
    interactive = is_interactive_review(command)
    if interactive and phase == 'plan':
        return ('\nThis is the planning turn. Do not call list_rework_targets or request_rework, '
                'and do not start upstream rework while preparing the plan. Tool use, if the '
                'execution evidence requires it, is reserved for the execution turn.')
    if interactive:
        turn = ('This is the execution turn. ' if phase == 'execute'
                else 'During this assignment, ')
        if _is_watchdog_supervisor(command):
            return ('\n' + turn + 'the modport_rework MCP tools expose only host-supplied '
                    'upstream targets. Inspect list_rework_targets and call request_rework '
                    'with concrete causal repair instructions when broader upstream work is '
                    'needed. Wait for the actual returned result or explicit deadline error '
                    'before finalizing; inspect raw failure feedback and verification evidence. '
                    'A prose request or published diagnostic is not evidence of execution. '
                    'Do not compute candidate identities or introduce verification gates.')
        wait_instruction = ''
        if _drain_pending_on_eof(command):
            wait_instruction = (
                ' After calling request_rework, do not issue a final answer while that call '
                'is still in progress and do not poll the host process or its private state as '
                'a substitute for the tool result. Wait for the call to return either the author '
                'result or an explicit session-deadline error.')
        if command.options.get('workflow_version', 0) >= 18:
            wait_instruction += (
                ' list_rework_targets also returns the host issue inventory and routing artifact paths; '
                'read their exact file/line/symbol locations and inspect the code. Search all modules, '
                'resources and build files; a literal import replacement alone does not prove API compatibility. '
                'Include issue IDs, affected paths, expected behavior and verification steps in rework prose. '
                'For cross-module defects, select the relevant available authors, order interface/provider '
                'changes before callers, and designate an integration author. Broad scope is a reason to '
                'split concrete requests, not by itself a reason to abandon repair. Ownership and routes '
                'are advisory; unassigned issues require your explicit author choice. Missing contracts '
                'require repair of authentic producer evidence/references; never fabricate an empty lock. '
                'After each call read the repair feedback first: distinguish author changes, the actual '
                'host build, behavior verification, candidate identity and progress. Missing or stale '
                'verification is unknown; new commits or SDK success do not prove progress or acceptance. '
                'Inspect updated inventory paths before requesting another author. If no request is made, '
                'explain the concrete reason (no actionable defect, unavailable author, exhausted budget, '
                'missing external input, or repeated failure without a new repair hypothesis).')
        return ('\n' + turn + 'you have the modport_rework MCP tools and real '
                'upstream targets. If your own inspection finds concrete defects in upstream work '
                'that require correction, first call list_rework_targets, then call '
                'request_rework with target_agent and concrete natural-language instructions. '
                'This is an explicit reviewer decision, not an automatic response to a rejected report '
                'or failed host check. The call waits for that author to finish and returns its result '
                'and current artifact paths to this session.' + wait_instruction + ' Read that returned result and the relevant '
                'updated artifacts, then continue the review against the new evidence. Do not merely '
                'describe needed rework in report prose: prose does not invoke the author. Do not launch '
                'a replacement author yourself. A tool failure is not a successful revision, and no '
                'automatic retry is required. Targets marked recheck rerun that failed deterministic '
                'check. Allowed targets: '
                + json.dumps(command.payload['review_rework_targets'], ensure_ascii=False))

    downstream_eligible = (uses_downstream_toolcall(command)
                           and _is_agent_stage(command.stage_id)
                           and not isinstance(command.payload.get('reviewer_rework'), dict))
    legacy_eligible = (not uses_downstream_toolcall(command)
                       and not is_forward_only_planning(command)
                       and command.stage_id in REVIEW_AUTHORS)
    if not (downstream_eligible or legacy_eligible):
        return ''
    planning = (' This is the planning turn; do not attempt upstream rework here.'
                if phase == 'plan' else '')
    return ('\nNo upstream rework targets are available to this assignment, so '
            'list_rework_targets and request_rework cannot be used.' + planning
            + ' If inspection shows that upstream repair is needed, preserve the exact failure and '
              'state honestly that this session cannot request the repair. Do not claim that a tool '
              'call or revision occurred.')


def responses(command):
    directory = session_directory(command.run_dir, command.command_id) / 'responses'
    result = []
    if directory.is_dir():
        for path in directory.glob('*.json'):
            verified_path(Path(command.run_dir), {'path': str(path.relative_to(command.run_dir))})
            value = json.loads(path.read_text())
            if isinstance(value, dict):
                result.append(value)
    return sorted(result, key=lambda row: (row.get('sequence', 0), row.get('request_id', '')))


def review_rework_observation(command, stdout):
    """Describe a rework call that the reviewer finalized without observing.

    This is a v17 diagnostic only.  A response file that appeared while the
    MCP transport drained is retained as a late result; its existence does not
    imply that the already-written review consumed it.
    """
    if not _drain_pending_on_eof(command) or not isinstance(stdout, str):
        return None

    started = {}
    completed = {}
    last_agent_message = -1
    for position, line in enumerate(stdout.splitlines()):
        try:
            event = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(event, dict):
            continue
        item = event.get('item')
        if not isinstance(item, dict):
            continue
        if item.get('type') == 'agent_message':
            last_agent_message = position
            continue
        if (item.get('type') != 'mcp_tool_call'
                or item.get('server') != 'modport_rework'
                or item.get('tool') != 'request_rework'):
            continue
        identity = item.get('id')
        if not isinstance(identity, str) or not identity:
            continue
        event_type = event.get('type')
        if event_type == 'item.started' or item.get('status') == 'in_progress':
            started.setdefault(identity, (position, item))
        if event_type == 'item.completed' or item.get('status') == 'completed':
            completed[identity] = position

    unresolved = [(identity, position, item)
                  for identity, (position, item) in started.items()
                  if (identity not in completed
                      or completed[identity] > last_agent_message)]
    if not unresolved:
        return None

    root = Path(command.run_dir)
    directory = session_directory(root, command.command_id)
    try:
        descriptor = json.loads(verified_path(root, {
            'path': (directory / 'session.json').relative_to(root).as_posix(),
        }).read_text())
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        descriptor = {}
    if not isinstance(descriptor, dict):
        descriptor = {}
    deadline = descriptor.get('deadline_epoch')
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        deadline = None

    request_rows = []
    requests_dir = directory / 'requests'
    if requests_dir.is_dir() and not requests_dir.is_symlink():
        for path in sorted(requests_dir.glob('*.json')):
            if path.name.endswith('.cancel.json'):
                continue
            try:
                relative = path.relative_to(root).as_posix()
                verified = verified_path(root, {'path': relative})
                document = json.loads(verified.read_text())
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            if (isinstance(document, dict)
                    and document.get('reviewer_execution_id') == command.command_id
                    and isinstance(document.get('request_id'), str)
                    and document.get('request_id')
                    and path.stem == document.get('request_id')):
                request_rows.append((verified, document))

    raw_targets = command.payload.get('review_rework_targets')
    if not isinstance(raw_targets, list):
        raw_targets = []
    targets = {}
    for row in raw_targets:
        if not isinstance(row, dict):
            continue
        target = row.get('target_agent')
        if isinstance(target, str) and target:
            targets[target] = row.get('stage') if isinstance(row.get('stage'), str) else None
    used_requests = set()
    calls = []
    for identity, position, item in unresolved:
        arguments = item.get('arguments')
        if not isinstance(arguments, dict):
            arguments = {}
        target_agent = arguments.get('target_agent')
        instructions = arguments.get('instructions')
        if not isinstance(target_agent, str):
            target_agent = None
        if not isinstance(instructions, str):
            instructions = None
        match = next(((path, document) for path, document in request_rows
                      if document.get('request_id') not in used_requests
                      and target_agent is not None and instructions is not None
                      and document.get('target_agent') == target_agent
                      and document.get('instructions') == instructions), None)
        call = {
            'tool_call_id': identity,
            'target_agent': target_agent,
            'target_stage': targets.get(target_agent) if target_agent is not None else None,
            'final_message_after_call': last_agent_message > position,
            'tool_completed_after_final': (identity in completed
                                           and completed[identity] > last_agent_message),
        }
        if match is None:
            call['outcome'] = 'request_artifact_unavailable'
            calls.append(call)
            continue
        request_path, request = match
        request_id = request.get('request_id')
        used_requests.add(request_id)
        call['request_id'] = request_id
        call['request_ref'] = {
            'path': request_path.relative_to(root).as_posix(),
            'sha256': file_digest(request_path),
            'media_type': 'application/json',
        }
        response_path = directory / 'responses' / (str(request_id) + '.json')
        try:
            relative = response_path.relative_to(root).as_posix()
            response_path = verified_path(root, {'path': relative})
        except (OSError, ValueError):
            response_path = None
        if response_path is not None:
            call['outcome'] = 'response_available_after_reviewer_final'
            call['response_ref'] = {
                'path': response_path.relative_to(root).as_posix(),
                'sha256': file_digest(response_path),
                'media_type': 'application/json',
            }
        elif deadline is not None and time.time() >= deadline:
            call['outcome'] = 'session_deadline_exceeded'
        else:
            call['outcome'] = 'request_still_pending'
        if deadline is not None:
            call['deadline_epoch'] = float(deadline)
        calls.append(call)

    return {
        'status': 'unobserved',
        'detail': ('The reviewer finalized while request_rework was still in progress; '
                   'the review report did not observe these results.'),
        'calls': calls,
    }


def refresh_review_command(command):
    """Use host-produced updates after an awaited call, retaining raw reports."""
    refs, upstream = dict(command.artifact_refs), dict(command.upstream_results)
    options = dict(command.options)
    payload = dict(command.payload)
    for response in responses(command):
        payload.update(response.get('current_context', {}))
        if command.options.get('workflow_version', 0) >= 18 and 'repair_feedback' in response:
            payload['latest_repair_feedback'] = response['repair_feedback']
        for update in response.get('updates', []):
            stage, result = update['stage'], update['result']
            upstream[stage] = result
            upstream[update.get('target_agent', stage)] = result
            if (result.get('status') != 'completed'
                    and not uses_downstream_toolcall(command)):
                continue
            produced = result.get('outputs', {}).get('artifact_refs', {})
            if not isinstance(produced, dict):
                produced = {}
            if stage in {'coder', 'agent_rework'}:
                # A continuation's one-task launch plan is not the global plan.
                refs.update({'rework:' + response['request_id'] + ':' + key: ref
                             for key, ref in produced.items()})
            else:
                refs.update(produced)
            if (result.get('status') == 'completed' and stage == 'test_design'
                    and command.stage_id == 'test_review'):
                options['workspace'] = result['outputs']['workspace']
    return replace(command, artifact_refs=refs, upstream_results=upstream, options=options, payload=payload)


def rework_instruction(command):
    if is_forward_only_planning(command):
        return ''
    request = command.payload.get('reviewer_rework')
    if not isinstance(request, dict):
        return ''
    instruction = ('\nThe reviewing agent requested another revision of your assignment. '
                   'Address the following request, inspect your previous output and the supplied current context, '
                   'and return the result for that same reviewer. Instructions:\n'
                   + request.get('instructions', ''))
    if (command.options.get('workflow_version', 0) >= 26
            and command.stage_id in {'contract_draft', 'contract_revise'}):
        instruction += (
            '\nFor this contract author rework, implement the requested harness and contract edits. '
            'The host runs the fresh nonce-bound contract verifier after your result returns. '
            'A bare runClient through the free-form project command tool launches the ordinary game '
            'without that verifier wiring and cannot fulfill the requested witness.')
    return instruction
