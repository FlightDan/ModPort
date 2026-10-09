"""Host-owned scoped regression scheduling; the SDK executes every assignment."""
from pathlib import Path

from .characterization import CharacterizationContract
from .contracts import OperationInput, OperationResult, json_copy
from .evidence import read_json, verified_path
from .gate_policy import downstream_toolcall, passed
from .business_policy import business_gates_disabled

_TERMINAL = frozenset({'succeeded', 'failed', 'cancelled', 'timed_out', 'dead'})


def frozen_contract(lock):
    """Read the host's source-reading contract without legacy anchor coercion."""
    contract = lock['contract']
    if lock.get('verification_basis') == 'source_reading':
        return contract
    return CharacterizationContract.from_mapping(contract).to_dict()


def frozen_static_behavior_ids(lock):
    """Only frozen, exclusively static-client mappings qualify as static coverage."""
    contract = frozen_contract(lock)
    declarations = lock.get('test_evidence', {})
    result = set()
    for entry in contract['behaviors']:
        if entry.get('side') != 'client' or not entry.get('test_mapping'):
            continue
        records = [declarations.get(identifier, {}) for identifier in entry['test_mapping']]
        if all(record.get('evidence_kind') == 'static_client'
               and isinstance(record.get('static_reason'), str) and record['static_reason'].strip()
               and 'client_smoke' in record.get('acceptance_gates', []) for record in records):
            result.add(entry['id'])
    return result


def partition_scopes(root, refs, obligations, limit, *, separate_static=False):
    """Partition all frozen behavior IDs, keeping every verification obligation."""
    lock = read_json(verified_path(root, refs['functional_contract_lock']))
    contract = frozen_contract(lock)
    identities = sorted(entry['id'] for entry in contract['behaviors'])
    if not identities:
        raise ValueError('scoped regression requires frozen behaviors')
    static = frozen_static_behavior_ids(lock) if separate_static else set()
    runtime = [identity for identity in identities if identity not in static]
    count = min(max(1, limit), max(1, len(runtime)))
    scopes = [{'scope_id': f'scope-{index + 1:03d}',
               'behavior_ids': runtime[index::count], 'gap_obligations': []}
              for index in range(count)]
    for index, identity in enumerate(sorted(static)):
        scopes[index % count]['behavior_ids'].append(identity)
    if separate_static:
        for scope in scopes:
            scope['behavior_ids'].sort()
            scope['static_behavior_ids'] = sorted(set(scope['behavior_ids']) & static)
            scope['runtime_behavior_ids'] = sorted(set(scope['behavior_ids']) - static)
    # An obligation without an explicit behavior mapping still gets a concrete
    # owner. It is never silently discarded or counted as already verified.
    for index, obligation in enumerate(obligations):
        if obligation.get('resolution_stage', obligation.get('due_stage')) != 'test_execute':
            continue
        mapped = set(obligation.get('behavior_ids', []))
        owner = next((scope for scope in scopes if mapped.intersection(scope['behavior_ids'])),
                     scopes[index % count])
        owner['gap_obligations'].append(json_copy(obligation))
    return scopes


def start_regression(policy, snapshot, header, app, review):
    refs = policy._refs(header, app)
    obligations = list(app.get('project_verification_gaps', {}).values()) or app['effective'].get(
        'mod_analysis', {}).get('outputs', {}).get('deferred_verification_gaps', [])
    try:
        scopes = partition_scopes(Path(header['run_dir']), refs, obligations,
                                  header['request'].get('max_parallel_coders', 3),
                                  separate_static=header.get('definition', {}).get('workflow_version', 0) >= 13)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        failure = OperationResult('failed', review.run_id, review.task_id, 'test_design',
                                  review.command_id, detail=str(exc), error_code='independent_test_failed')
        if business_gates_disabled(header):
            policy._flowthrough_diagnostic(app, failure.to_dict())
            return policy._schedule(snapshot, header, app, 'test_design',
                                    dependencies=[], causation_id=review.command_id)
        return policy._repair_failure(snapshot, header, app, 'test_design', failure, review.command_id)
    app['regression_generation'] = app.get('regression_generation', 0) + 1
    app['active_stage'] = None
    app['active_group'] = {'kind': 'regression', 'generation': app['regression_generation'],
                           'scopes': scopes, 'members': [], 'results': {},
                           'artifact_refs': refs, 'review_task_id': review.task_id}
    return regression_decision(policy, snapshot, header, app)


def regression_decision(policy, snapshot, header, app):
    group = app['active_group']
    gates_disabled = business_gates_disabled(header)
    require_review = header.get('definition', {}).get('workflow_version', 0) >= 13
    active = []
    for task_id in group['members']:
        task = snapshot['tasks'].get(task_id)
        if task is None:
            # A recorded dispatch cannot free its slot before SDK publication.
            active.append(task_id)
            continue
        attempt = task['attempts'][-1]
        if attempt['state'] not in _TERMINAL:
            active.append(task_id)
            continue
        execution_id = attempt['command']['execution_id']
        if execution_id in app['processed']:
            continue
        app['processed'].append(execution_id)
        command = OperationInput.from_dict(attempt['command']['payload'])
        if attempt['state'] == 'succeeded':
            outcome = OperationResult.from_dict(attempt['result']['value'])
            outcome.validate_for(command)
        else:
            outcome = OperationResult('blocked', command.run_id, task_id, command.stage_id,
                                      execution_id, error_code='execution_' + attempt['state'])
        result = outcome.to_dict()
        policy._capture_repair_result(header, app, result)
        app['history'].append({'stage': command.stage_id, 'task_id': task_id,
                               'execution_id': execution_id, 'state': outcome.status,
                               'error_code': outcome.error_code})
        app['effective'][task_id] = result
        group['results'][task_id] = result
        if gates_disabled:
            operational = policy._flowthrough_operational_failure(attempt, outcome)
            if operational:
                return policy._finish(app, operational)
        if (not gates_disabled and (outcome.status != 'completed'
                or (command.stage_id == 'test_review'
                    and outcome.outputs.get('verdict') != 'approved'))):
            group.setdefault('failure', result)
        if gates_disabled and (outcome.status != 'completed' or outcome.error_code
                or (command.stage_id == 'test_review'
                    and outcome.outputs.get('verdict') != 'approved')):
            policy._flowthrough_diagnostic(app, result)
    if downstream_toolcall(header):
        failures = [row for row in group['results'].values() if not passed(row)]
        if failures:
            group['failure'] = failures[0]
    if group.get('failure') and not gates_disabled:
        # Let already-running independent scopes settle, preserving all failures.
        # No new scopes or executions start after a business failure.
        if active:
            return []
        failure = OperationResult.from_dict(group['failure'])
        if downstream_toolcall(header):
            return policy._repair_failure(snapshot, header, app, failure.stage_id,
                failure, failure.command_id, location='group')
        app['active_group'] = None
        return policy._repair_failure(snapshot, header, app, failure.stage_id, failure, failure.command_id)

    generation = group['generation']
    def task_id(stage, scope):
        return f'{stage}.g{generation}.{scope["scope_id"]}'
    executions = [task_id('test_execute', scope) for scope in group['scopes']]
    if all(identifier in group['results'] for identifier in executions):
        # The alias retains a real design's identity. Complete design coverage is
        # proved by the following authenticated aggregate execution, not this alias.
        design_id = task_id('test_design', group['scopes'][0])
        app['effective']['test_design'] = group['results'][design_id]
        if require_review:
            app['effective']['test_review'] = group['results'][task_id('test_review', group['scopes'][0])]
        app['active_group'] = None
        return policy._schedule(snapshot, header, app, 'test_execute',
            task_id=f'test_execute.g{generation}.join', dependencies=executions,
            artifact_overrides=group['artifact_refs'],
            payload={'regression_generation': generation, 'regression_scopes': group['scopes'],
                     'regression_results': [group['results'][identifier] for identifier in executions],
                     'regression_designs': [group['results'][task_id('test_design', scope)]
                                            for scope in group['scopes']],
                     **({'regression_reviews': [group['results'][task_id('test_review', scope)]
                                                for scope in group['scopes']]} if require_review else {})})

    capacity = header['request'].get('max_parallel_coders', 3) - len(active)
    operations = []
    # Prioritize execution of ready designs; total in-flight scope work is bounded.
    for stage in (('test_execute', 'test_review', 'test_design') if require_review
                  else ('test_execute', 'test_design')):
        for scope in group['scopes']:
            if capacity <= 0 or app.get('stop_reason'):
                return operations
            identifier = task_id(stage, scope)
            design_id = task_id('test_design', scope)
            review_id = task_id('test_review', scope)
            prerequisite = (review_id if require_review and stage == 'test_execute' else
                            design_id if stage in {'test_execute', 'test_review'} else group['review_task_id'])
            if identifier in group['members'] or (stage != 'test_design' and prerequisite not in group['results']):
                continue
            workspace = f'workspaces/tests/g{generation}-{scope["scope_id"]}'
            refs = dict(group['artifact_refs'])
            upstream = {}
            if stage in {'test_execute', 'test_review'}:
                design = group['results'][design_id]['outputs']
                refs.update(design.get('artifact_refs', {}))
                workspace = design.get('workspace', workspace)
                upstream['test_design'] = group['results'][design_id]
            if stage == 'test_execute' and require_review:
                review = group['results'][review_id]
                refs.update(review['outputs'].get('artifact_refs', {}))
                upstream['test_review'] = review
            dispatched = policy._schedule(snapshot, header, app, stage, task_id=identifier,
                activate=False, dependencies=[prerequisite],
                artifact_overrides=refs, extra_options={'workspace': workspace},
                upstream_overrides=upstream,
                payload={'regression_scope': scope, 'regression_generation': generation,
                         'gap_obligations': scope['gap_obligations']})
            operations.extend(dispatched)
            if any(operation['kind'] == 'dispatch' for operation in dispatched):
                group['members'].append(identifier)
                capacity -= 1
    return operations
