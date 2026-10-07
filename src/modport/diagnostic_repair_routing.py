"""Carry optional diagnostic edits across host scheduling boundaries.

The ledger contains references only. It cannot restart work, alter a dispatched
command, or change a frozen plan. Receivers decide applicability from the saved
before/after contents in their own workspace.
"""
from collections.abc import Mapping


INTEGRATION_STAGES = frozenset({
    'development_integrate', 'target_repair_integrate', 'development_prepare_integrate',
})


def register(app, command, outcome):
    """Retain completed, host-bound repairs independently of cancellation decisions."""
    if command.options.get('workflow_version', 0) < 40 or outcome.status != 'completed':
        return
    targets = command.payload.get('diagnostic_repair_targets', ())
    ledger = app.setdefault('diagnostic_repairs', [])
    for ref in outcome.outputs.get('diagnostic_repairs', ()):
        if not isinstance(ref, Mapping) or not isinstance(ref.get('path'), str):
            continue
        metadata = ref.get('metadata', {})
        if (not isinstance(metadata, Mapping)
                or metadata.get('applicable') is not True
                or metadata.get('run_id') != command.run_id
                or metadata.get('producer_execution_id') != command.command_id
                or not any(metadata.get('task_id') == target.get('task_id')
                           and metadata.get('plan_ref') == target.get('plan_ref')
                           for target in targets if isinstance(target, Mapping))):
            continue
        if ref not in ledger:
            ledger.append(dict(ref))
            context = app.get('diagnostic_repair_context', {})
            late = (context.get('plan_ref') == metadata.get('plan_ref')
                    and app.get('active_stage') not in INTEGRATION_STAGES
                    and not app.get('active_group'))
            app.setdefault('diagnostic_repair_dispositions', {})[ref['path']] = {
                'status': 'deferred' if late else 'pending',
                'reason': ('The last integration boundary was already dispatched; retained for '
                           'a future matching task or cleanup. No live input was changed.' if late else
                           'Awaiting a matching future coder, integration or cleanup dispatch.'),
                'producer_execution_id': command.command_id,
            }


def bind(app, stage, payload, artifact_refs):
    """Freeze applicable refs into a new dispatch; never update active commands."""
    payload = dict(payload or {})
    if stage not in INTEGRATION_STAGES | {'coder', 'code_cleanup'} or payload.get('goal_scope') == 'contract':
        return payload
    context = app.get('diagnostic_repair_context', {}) if stage == 'code_cleanup' else {}
    plan = context.get('plan_ref') if context else artifact_refs.get('development_plan')
    if not isinstance(plan, Mapping):
        return payload
    task = payload.get('development_task', {})
    selected = list(payload.get('diagnostic_repair_refs', ()))
    for ref in app.get('diagnostic_repairs', ()):
        metadata = ref.get('metadata', {})
        if metadata.get('plan_ref') != plan:
            continue
        if stage == 'coder' and metadata.get('task_id') != task.get('id'):
            continue
        if ref in context.get('included_refs', ()):
            continue
        if ref not in selected:
            selected.append(ref)
    # Coder receipts and refs also survive a supported continuation through
    # the existing retained development results, without scheduler history.
    if stage in INTEGRATION_STAGES:
        for result in payload.get('development_results', ()):
            for ref in result.get('outputs', {}).get('diagnostic_repair_refs', ()):
                if ref.get('metadata', {}).get('plan_ref') == plan and ref not in selected:
                    selected.append(ref)
    if selected:
        payload['diagnostic_repair_refs'] = selected
        if stage == 'code_cleanup' and context:
            payload['diagnostic_repair_targets'] = [
                {'task_id': identifier, 'plan_ref': plan} for identifier in context['task_ids']]
    return payload


def record_result(app, command, outcome):
    """Keep publication, clone edits and successful candidate integration distinct."""
    if command.options.get('workflow_version', 0) < 40:
        return
    context = outcome.outputs.get('diagnostic_repair_context')
    if (command.stage_id in INTEGRATION_STAGES and outcome.status == 'completed'
            and isinstance(context, Mapping)):
        app['diagnostic_repair_context'] = dict(context)
    dispositions = app.setdefault('diagnostic_repair_dispositions', {})
    receipts = outcome.outputs.get('diagnostic_repair_receipts', ())
    for receipt in receipts:
        ref = receipt.get('repair_ref', {})
        if not isinstance(ref, Mapping) or not isinstance(ref.get('path'), str):
            continue
        status = receipt.get('status', 'invalid')
        if status in {'applied', 'already_applied'}:
            if command.stage_id in INTEGRATION_STAGES and outcome.status == 'completed':
                status = 'integrated'
            elif (command.stage_id == 'code_cleanup'
                  and outcome.outputs.get('diagnostic_repair_integration_status') == 'integrated'):
                status = 'integrated'
                current = app.get('diagnostic_repair_context', {})
                if ref.get('metadata', {}).get('plan_ref') == current.get('plan_ref'):
                    included = current.setdefault('included_refs', [])
                    if ref not in included:
                        included.append(dict(ref))
            elif command.stage_id == 'coder' and outcome.outputs.get('artifact_refs', {}).get('coder_patch'):
                status = 'exported_in_coder_patch'
            else:
                status = 'not_integrated'
        dispositions[ref['path']] = {'status': status,
                                     'consumer_execution_id': command.command_id,
                                     'receipt': dict(receipt)}
    if isinstance(context, Mapping) and outcome.status == 'completed':
        for ref in context.get('included_refs', ()):
            dispositions[ref['path']] = {'status': 'integrated',
                                         'consumer_execution_id': command.command_id}
