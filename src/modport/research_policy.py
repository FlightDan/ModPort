"""Host-owned research accounting; no filesystem or SDK effects."""
from .contracts import json_copy

KINDS = ('platform', 'java')
RESEARCH_STAGES = frozenset({'platform_diff', 'java_diff', 'gap_research'})
RESEARCH_POLICY = {'new_pair_assignments': 2, 'existing_pair_assignments': 1,
                   'automatic_supplement_required': False, 'transport_attempts': 1}


def initialize_research(app, outputs):
    budgets = app.setdefault('research_budget', {})
    origins = outputs.get('research_origins', {})
    for kind in KINDS:
        origin = origins.get(kind, 'new' if kind in outputs.get('missing_kinds', []) else 'existing')
        if origin not in ('new', 'existing'):
            raise ValueError('invalid research origin')
        budgets.setdefault(kind, {'origin': origin, 'limit': 2 if origin == 'new' else 1,
                                  'dispatched': 0, 'executions': []})
    revisions = outputs.get('knowledge_revisions', {})
    app.setdefault('knowledge_revisions', {}).update(json_copy(revisions))


def remaining(app, kind):
    budget = app.get('research_budget', {}).get(kind)
    if budget is None:
        return 1
    return max(0, budget['limit'] - budget['dispatched'])


def eligible_kinds(app, gaps):
    return sorted({row['skill'] for row in gaps if row.get('skill') in KINDS
                   and row.get('kind', 'knowledge') == 'knowledge'
                   and row.get('applicable', True) is True
                   and row.get('status', 'unresolved') == 'unresolved'
                   and remaining(app, row['skill']) > 0})


def record_dispatch(app, stage, execution_id, kinds):
    attempts = app.setdefault('research_attempts', {})
    if execution_id in attempts:
        return
    for kind in kinds:
        budget = app.setdefault('research_budget', {}).setdefault(kind,
            {'origin': 'existing', 'limit': 1, 'dispatched': 0, 'executions': []})
        if remaining(app, kind) <= 0:
            raise ValueError('research budget exhausted: ' + kind)
    for kind in kinds:
        budget = app['research_budget'][kind]
        budget['dispatched'] += 1
        budget['executions'].append(execution_id)
    attempts[execution_id] = {'stage': stage, 'kinds': list(kinds), 'status': 'dispatched',
                              'artifacts_complete': False}


def observe_attempts(app, snapshot):
    """Keep cancellation/partial work distinct from usable business results."""
    records = app.setdefault('research_attempts', {})
    for task in snapshot.get('tasks', {}).values():
        for attempt in task['attempts']:
            execution_id = attempt['command']['execution_id']
            if execution_id not in records:
                continue
            state = attempt['state']
            result = (attempt.get('result') or {}).get('value') or {}
            complete = state == 'succeeded' and result.get('status') == 'completed'
            if state in {'succeeded', 'failed', 'cancelled', 'timed_out', 'dead', 'recovery_required'}:
                records[execution_id].update(status='cancelled' if result.get('outputs', {}).get('research_cancelled') else 'completed' if complete else
                    ('interrupted' if state == 'recovery_required' else
                     ('failed' if state == 'succeeded' else state)), artifacts_complete=complete)
