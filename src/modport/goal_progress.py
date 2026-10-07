"""Bounded diagnostic repetition telemetry; never a goal-control policy."""

from hashlib import sha256
import json


def observe_goal_item(state, item):
    if not isinstance(item, dict):
        return False
    kind = item.get('type')
    if kind == 'fileChange':
        state['file_change_events'] = state.get('file_change_events', 0) + 1
        state['observations'] = []
        state.pop('repeated_diagnostic', None)
        return True
    if kind not in {'commandExecution', 'mcpToolCall', 'dynamicToolCall'}:
        return False
    # Ignore event/turn/process IDs and elapsed time. Store only digests: raw
    # outputs are already retained by the normal transport audit, not copied
    # into growing session metadata on every tool completion.
    selected = {key: item[key] for key in (
        'type', 'command', 'cwd', 'server', 'tool', 'arguments',
        'exitCode', 'aggregatedOutput', 'result', 'error') if key in item}
    raw = json.dumps(selected, sort_keys=True, ensure_ascii=False, default=str).encode()
    fingerprint = sha256(raw).hexdigest()
    observations = state.setdefault('observations', [])
    row = next((row for row in observations if row['fingerprint'] == fingerprint), None)
    if row is None:
        row = {'fingerprint': fingerprint, 'kind': kind, 'count': 0}
        observations.append(row)
        del observations[:-32]
    row['count'] += 1
    state['tool_completions'] = state.get('tool_completions', 0) + 1
    if row['count'] >= 3:
        state['repeated_diagnostic'] = {
            'code': 'repeated_tool_observation', **row,
            'candidate_identity_verified': False,
            'no_progress_conclusion': False,
            'detail': 'Identical tool input/output observed repeatedly; inspect retained evidence. '
                      'This does not prove the worktree is unchanged and does not stop or restart the goal.',
        }
    return True
