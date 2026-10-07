"""Advisory issue routing to existing authors; never dispatches work."""
from pathlib import PurePosixPath


def _contains(scope, path):
    if not isinstance(scope, str) or not isinstance(path, str):
        return False
    scope = scope.rstrip('/')
    if not scope or PurePosixPath(scope).is_absolute() or '..' in PurePosixPath(scope).parts:
        return False
    return path == scope or path.startswith(scope + '/')


def route_inventory(inventory, targets):
    """Keep all matching owners and surface unassigned issues explicitly.

    Ownership is context, not an edit restriction. Shared interfaces require a
    reviewer-chosen integration author and ordered explicit requests.
    """
    routes = []
    for issue in inventory.get('issues', []):
        paths = sorted({loc['path'] for loc in issue.get('locations', [])
                        if isinstance(loc.get('path'), str)})
        matches = []
        for target in targets:
            scope = inventory.get('workspace_scope')
            if (scope and target.get('stage') == 'coder'
                    and (target.get('goal_scope') == 'contract') != (scope == 'contract')):
                continue
            scopes = target.get('owned_paths', [])
            if not isinstance(scopes, list):
                scopes = []
            if any(_contains(scope, path) for scope in scopes for path in paths):
                matches.append(target['target_agent'])
        reason = 'path_scope'
        if not matches and issue.get('kind') == 'missing_input':
            # Repair the authentic producer/reference, never invent a lock.
            stages = ({'contract_draft', 'project_init'}
                      if any('contract' in path for path in paths)
                      or 'contract' in issue.get('summary', '').lower()
                      else {'preparation', 'project_init', 'migration_tasks'})
            matches = [t['target_agent'] for t in targets if t.get('stage') in stages]
            reason = 'prerequisite_producer'
        if not matches:
            reason = 'reviewer_assignment_required'
        routes.append({'issue_id': issue.get('issue_id'), 'paths': paths,
                       'target_agents': sorted(set(matches)), 'reason': reason,
                       'coordination_required': len(set(matches)) > 1})
    return {'schema_version': 1, 'kind': 'repair_routes', 'acceptance_evidence': False,
            'candidate_id': inventory.get('candidate_id'), 'routes': routes,
            'unassigned_issue_ids': [r['issue_id'] for r in routes if not r['target_agents']],
            'dispatch_policy': 'explicit_request_rework_only'}
