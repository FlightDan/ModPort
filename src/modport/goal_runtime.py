"""Host-owned persistent goals backed by managed OpenCode HTTP sessions.

OpenCode supplies a durable conversation, not a Goal API.  ModPort therefore
owns goal identity, deadlines, recovery, progress, cancellation, and acceptance;
the session ID in the compatible ``thread_id`` field binds that state to the
OpenCode transcript.  Project commands are available only through the
credential-free host sandbox MCP bridge.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import copy
from . import platform_files as fcntl
from .platform_files import safe_open
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Mapping
from uuid import NAMESPACE_URL, uuid5

from .business_policy import business_gates_disabled
from .handlers import _agent_model_policy
from .evidence import atomic_json, file_digest
from .opencode_runtime import (
    OpenCodeConfig,
    OpenCodeCleanupError,
    OpenCodeError,
    OpenCodeResponseError,
    OpenCodeServer,
    resolve_model_id,
)
from .opencode_provider import managed_provider_config, provider_failure_kind
from .opencode_shell_mcp import network_tools_disabled
from .progress_policy import progress_supervised


@dataclass(frozen=True)
class GoalRunResult:
    stdout: str
    returncode: int
    metadata: dict[str, Any]


def _public_stdout(metadata):
    return '\n'.join(json.dumps({'type': 'item.completed', 'item': {
        'type': 'agent_message', 'text': text}})
        for text in metadata.get('public_messages', []))


def _stable_mcp_config(config: Mapping[str, Any]) -> tuple[dict[str, Any], int | None]:
    """Remove per-attempt MCP timeouts before hashing durable config identity."""
    if not isinstance(config, Mapping):
        raise TypeError('OpenCode MCP transport config must be a mapping')
    stable: dict[str, Any] = {}
    timeouts: list[int] = []
    for name, raw in config.items():
        if not isinstance(name, str) or not name or not isinstance(raw, Mapping):
            raise ValueError('OpenCode MCP transport config has an invalid server entry')
        spec = copy.deepcopy(dict(raw))
        timeout = spec.pop('timeout', None)
        if timeout is not None:
            if type(timeout) is not int or timeout < 1:
                raise ValueError('OpenCode MCP transport config has an invalid timeout')
            timeouts.append(timeout)
        stable[name] = spec
    return stable, min(timeouts) if timeouts else None


def _transport_config_identity(args: Mapping[str, Any] | None) -> tuple[str | None, int | None]:
    """Hash host-provided MCP configuration, excluding reducible timeouts.

    ``transport_args`` keeps its public name for caller compatibility, but now
    accepts OpenCode's ``{server_name: server_config}`` map.  The old Codex
    ``-c`` argv tuple is rejected rather than interpreted as OpenCode config.
    """
    if not args:
        return None, None
    if not isinstance(args, Mapping):
        raise TypeError('transport_args must be an OpenCode MCP mapping')
    stable, timeout_ms = _stable_mcp_config(args)
    encoded = json.dumps(stable, sort_keys=True, ensure_ascii=False,
                         separators=(',', ':')).encode()
    return sha256(encoded).hexdigest(), timeout_ms


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, sort_keys=True, ensure_ascii=False) + '\n',
                         encoding='utf-8')
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _redact_error_text(value: Any) -> Any:
    """Keep useful error diagnostics while removing known and patterned secrets."""
    from .telemetry import _secrets, redact

    return redact(value, _secrets())


def _process_birth(pid: int) -> str | None:
    """Native birth identity prevents confusing a recycled PID with our child."""
    if os.name == "nt":
        from .platform_runtime import process_birth
        return process_birth(pid)
    try:
        stat = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        return f'{boot}:{stat[19]}'
    except (OSError, IndexError):
        return None


def _previous_process_alive(metadata: dict) -> bool:
    if (metadata.get('cleanup_unconfirmed') is True
            and metadata.get('producer_stopped') is not True):
        # A dead leader does not prove its OpenCode process group stopped.
        # Only the cleanup-only host reconciliation may settle this state.
        return True
    pid = metadata.get('owned_pid')
    if not isinstance(pid, int) or pid <= 0:
        return False
    if metadata.get('producer_stopped') is True:
        return False
    if os.name == 'nt':
        observed = _process_birth(pid)
        birth = metadata.get('owned_process_birth')
        # Missing process access is uncertain; never use os.kill(pid, 0),
        # which can terminate a process on Windows.
        return observed is None or birth is None or birth == observed
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    birth = metadata.get('owned_process_birth')
    observed = _process_birth(pid)
    # Unknown identity is conservatively live; never signal another process.
    return not birth or not observed or birth == observed


def _token_count(info: Mapping[str, Any]) -> int:
    tokens = info.get('tokens')
    if not isinstance(tokens, Mapping):
        return 0
    total = 0
    for key in ('input', 'output', 'reasoning'):
        value = tokens.get(key)
        if type(value) is int and value > 0:
            total += value
    cache = tokens.get('cache')
    if isinstance(cache, Mapping):
        for value in cache.values():
            if type(value) is int and value > 0:
                total += value
    return total


def _duration_seconds(info: Mapping[str, Any]) -> float:
    value = info.get('time')
    if not isinstance(value, Mapping):
        return 0.0
    created, completed = value.get('created'), value.get('completed')
    if (isinstance(created, (int, float)) and not isinstance(created, bool)
            and isinstance(completed, (int, float)) and not isinstance(completed, bool)
            and completed >= created):
        return (completed - created) / 1000.0
    return 0.0


def _response_text(response: Mapping[str, Any]) -> str:
    parts = response.get('parts', ())
    if not isinstance(parts, list):
        return ''
    return ''.join(part.get('text', '') for part in parts
                   if isinstance(part, Mapping) and part.get('type') == 'text'
                   and isinstance(part.get('text'), str))


def _bounded_text_digest(text: str, *, limit: int = 8 * 1024 * 1024) -> tuple[int, str | None]:
    """Hash a response without building a second unbounded encoded copy."""
    hasher = sha256()
    size = 0
    for offset in range(0, len(text), 64 * 1024):
        chunk = text[offset:offset + 64 * 1024].encode('utf-8')
        size += len(chunk)
        if size > limit:
            return size, None
        hasher.update(chunk)
    return size, hasher.hexdigest()


def _bounded_value_digest(value: Any, *, limit: int = 8 * 1024 * 1024,
                          node_limit: int = 100_000) -> tuple[int, str | None]:
    """Hash JSON-shaped structured output with byte, node, and depth bounds."""
    hasher = sha256()
    size = 0
    nodes = 0

    def write(data: bytes) -> bool:
        nonlocal size
        size += len(data)
        if size > limit:
            return False
        hasher.update(data)
        return True

    def visit(item: Any, depth: int) -> bool:
        nonlocal nodes
        nodes += 1
        if nodes > node_limit or depth > 64:
            return False
        if item is None:
            return write(b'n;')
        if item is True:
            return write(b'b1;')
        if item is False:
            return write(b'b0;')
        if isinstance(item, str):
            if not write(b's'):
                return False
            for offset in range(0, len(item), 64 * 1024):
                if not write(item[offset:offset + 64 * 1024].encode('utf-8')):
                    return False
            return write(b';')
        if type(item) is int or isinstance(item, float):
            try:
                return write((type(item).__name__ + ':' + repr(item) + ';').encode('ascii'))
            except (UnicodeError, ValueError, OverflowError):
                return False
        if isinstance(item, list):
            if not write(b'l['):
                return False
            for child in item:
                if not visit(child, depth + 1):
                    return False
            return write(b']')
        if isinstance(item, Mapping):
            if not write(b'd{'):
                return False
            for key, child in item.items():
                if not isinstance(key, str) or not visit(key, depth + 1) \
                        or not visit(child, depth + 1):
                    return False
            return write(b'}')
        return False

    if not visit(value, 0):
        return size, None
    return size, hasher.hexdigest()


def _model_result_summary(metadata: Mapping[str, Any], command_id: str) -> dict[str, Any]:
    """Keep the separate model-result lane compact and content-addressed."""
    turns = []
    for source in ('planning_turns', 'turns'):
        rows = metadata.get(source, [])
        if not isinstance(rows, list):
            continue
        for row in rows[-32:]:
            if not isinstance(row, Mapping):
                continue
            summary = {}
            for key in ('id', 'user_message_id', 'phase', 'status', 'provider_id',
                        'model_id', 'variant', 'response_sha256',
                        'response_digest_status', 'structured_result_sha256'):
                value = row.get(key)
                if isinstance(value, str):
                    summary[key] = value[:256]
            if type(row.get('structured_result_bytes')) is int:
                summary['structured_result_bytes'] = row['structured_result_bytes']
                summary['structured_result_digest_status'] = (
                    'captured' if isinstance(row.get('structured_result_sha256'), str)
                    else 'exceeds_bounds_or_unavailable')
            tokens = row.get('tokens')
            if isinstance(tokens, Mapping):
                summary['tokens'] = {key: value for key, value in tokens.items()
                                     if key in {'input', 'output', 'reasoning'}
                                     and type(value) is int and 0 <= value < 2**63}
            turns.append(summary)
    turns = turns[-64:]
    pending = metadata.get('pending_turn')
    if isinstance(pending, Mapping):
        model_status = ('provider_error' if isinstance(metadata.get('pending_response_error'), Mapping)
                        else 'pending')
    else:
        model_status = metadata.get('model_terminal_status')
        if model_status not in {'completed', 'interrupted', 'provider_error', 'not_started'}:
            model_status = 'completed' if turns else 'not_started'
    observed_response = any(
        isinstance(row, Mapping) and row.get('status') == 'completed'
        for source in ('planning_turns', 'turns')
        for row in (metadata.get(source, []) if isinstance(metadata.get(source), list) else [])
    )
    return {
        'schema': 'modport.native-goal-model-result.v1',
        'command_id': command_id,
        'thread_id': (metadata.get('thread_id')[:256]
                      if isinstance(metadata.get('thread_id'), str) else None),
        'model_terminal_status': model_status,
        'model_terminal_turn_id': metadata.get('model_terminal_turn_id'),
        'host_status': metadata.get('status'),
        'native_goal_status': metadata.get('native_goal_status'),
        'host_accepted': metadata.get('host_accepted') is True,
        'response_observed': observed_response,
        'planning_turn_count': len(metadata.get('planning_turns', []))
            if isinstance(metadata.get('planning_turns'), list) else 0,
        'execution_turn_count': len(metadata.get('turns', []))
            if isinstance(metadata.get('turns'), list) else 0,
        'turns': turns,
        'pending_turn_id': (pending.get('message_id')
                            if isinstance(pending, Mapping) else None),
    }


def _tool_summaries(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    result = []
    for part in response.get('parts', ()):
        if not isinstance(part, Mapping) or part.get('type') != 'tool':
            continue
        state = part.get('state')
        result.append({
            'id': str(part.get('callID') or part.get('id') or ''),
            'tool': part.get('tool') if isinstance(part.get('tool'), str) else None,
            'status': state.get('status') if isinstance(state, Mapping) else None,
        })
    return result


def _aggregate_assistant_messages(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Build one auditable response from every assistant step in a user turn."""
    infos = [row.get('info') for row in rows]
    infos = [dict(info) for info in infos if isinstance(info, Mapping)]
    if not infos:
        raise OpenCodeError('OpenCode transcript assistant messages have no metadata')
    info = infos[-1]
    if isinstance(info.get('tokens'), Mapping):
        info['last_step_tokens'] = dict(info['tokens'])

    tokens: dict[str, Any] = {}
    for key in ('input', 'output', 'reasoning'):
        values = [entry.get('tokens', {}).get(key) for entry in infos
                  if isinstance(entry.get('tokens'), Mapping)]
        valid = [value for value in values
                 if type(value) is int and value >= 0]
        if valid:
            tokens[key] = sum(valid)
    cache_keys = set()
    for entry in infos:
        raw = entry.get('tokens')
        if isinstance(raw, Mapping) and isinstance(raw.get('cache'), Mapping):
            cache_keys.update(raw['cache'])
    cache = {}
    for key in cache_keys:
        values = [entry['tokens']['cache'].get(key) for entry in infos
                  if isinstance(entry.get('tokens'), Mapping)
                  and isinstance(entry['tokens'].get('cache'), Mapping)]
        valid = [value for value in values if type(value) is int and value >= 0]
        if valid:
            cache[key] = sum(valid)
    if cache:
        tokens['cache'] = cache
    if tokens:
        info['tokens'] = tokens

    times = [entry.get('time') for entry in infos
             if isinstance(entry.get('time'), Mapping)]
    starts = [stamp.get('created') for stamp in times
              if isinstance(stamp.get('created'), (int, float))
              and not isinstance(stamp.get('created'), bool)]
    completions = [stamp.get('completed') for stamp in times
                   if isinstance(stamp.get('completed'), (int, float))
                   and not isinstance(stamp.get('completed'), bool)]
    if starts or completions:
        aggregate_time = dict(info.get('time', {}))
        if starts:
            aggregate_time['created'] = min(starts)
        if completions:
            aggregate_time['completed'] = max(completions)
        info['time'] = aggregate_time

    costs = [entry.get('cost') for entry in infos
             if isinstance(entry.get('cost'), (int, float))
             and not isinstance(entry.get('cost'), bool)]
    if costs:
        info['cost'] = sum(costs)
    parts: list[Mapping[str, Any]] = []
    for row in rows:
        row_parts = row.get('parts')
        if isinstance(row_parts, list):
            parts.extend(part for part in row_parts if isinstance(part, Mapping))
    return {'info': info, 'parts': parts}


def _session_response(
    messages: list[dict[str, Any]], user_message_id: str, *,
    expected_assistant_id: str | None = None,
) -> tuple[str, dict[str, Any] | None]:
    """Reconcile a turn from all assistant steps linked to its user message."""
    user_exists = False
    candidates: list[dict[str, Any]] = []
    for row in messages:
        info = row.get('info')
        if not isinstance(info, Mapping):
            continue
        if info.get('id') == user_message_id and info.get('role') == 'user':
            user_exists = True
        if info.get('parentID') == user_message_id and info.get('role') == 'assistant':
            candidates.append(row)
    if expected_assistant_id is not None:
        matching = [index for index, row in enumerate(candidates)
                    if row.get('info', {}).get('id') == expected_assistant_id]
        if not matching:
            return ('incomplete' if user_exists else 'absent'), None
        candidates = candidates[:matching[-1] + 1]
    if not candidates:
        return ('incomplete' if user_exists else 'absent'), None

    latest = candidates[-1]
    info = latest.get('info', {})
    info = info if isinstance(info, Mapping) else {}
    error = info.get('error')
    if isinstance(error, Mapping):
        return 'error', _aggregate_assistant_messages(candidates)
    stamp = info.get('time')
    completed_at = stamp.get('completed') if isinstance(stamp, Mapping) else None
    completed = (isinstance(completed_at, (int, float))
                 and not isinstance(completed_at, bool) and completed_at > 0)
    parts = latest.get('parts', ())
    has_text = any(isinstance(part, Mapping) and part.get('type') == 'text'
                   and isinstance(part.get('text'), str) and part.get('text').strip()
                   for part in parts)
    has_tool = any(isinstance(part, Mapping) and part.get('type') == 'tool'
                    for part in parts)
    finish = info.get('finish')
    if expected_assistant_id is not None and not has_text:
        return 'incomplete', None
    # `finish: tool-calls` closes one assistant step, not the user turn. Older
    # server responses may omit `finish`, so require final text and no tool
    # part in that case.
    turn_finished = finish != 'tool-calls' and (finish is not None or (has_text and not has_tool))
    if completed and turn_finished:
        return 'completed', _aggregate_assistant_messages(candidates)
    return ('incomplete' if user_exists else 'absent'), None


def _is_blocked_response(text: str) -> bool:
    return bool(re.search(r'(?m)^\s*MODPORT_GOAL_STATUS:\s*blocked\s*$', text))


def run_goal(*, command, root: Path, worktree: Path, prompt: str,
             objective: str, validate: Callable[[], dict], timeout: float,
             planning_prompt: str | None = None, plan_path: Path | None = None,
             on_report: Callable[[str], None] | None = None,
             transport_args: Mapping[str, Any] | None = None,
             output_format: Mapping[str, Any] | None = None,
             session_context_budget: Mapping[str, Any] | None = None) -> GoalRunResult:
    """Run one persistent host goal through an isolated OpenCode session.

    Host acceptance always comes from ``validate``.  Every call is bounded by
    the original assignment deadline; explicit recovery reuses the same
    OpenCode session and reconciles its transcript before sending another
    request.
    """
    host_deadline_epoch = None
    if command.options.get('workflow_version', 0) >= 17:
        supplied = command.options.get('host_settlement_deadline_epoch')
        if supplied is not None:
            if (not isinstance(supplied, (float, int)) or isinstance(supplied, bool)
                    or not math.isfinite(supplied)):
                return GoalRunResult('', 1, {'host_accepted': False,
                    'error': 'native_goal_host_deadline_invalid'})
            host_deadline_epoch = float(supplied)
            run_bound = command.options.get('deadline_epoch')
            if (isinstance(run_bound, (float, int)) and not isinstance(run_bound, bool)
                    and math.isfinite(run_bound)):
                host_deadline_epoch = min(host_deadline_epoch, float(run_bound))
    root = Path(root).resolve()
    identity = sha256(str(command.command_id).encode()).hexdigest()[:24]
    directory = root / 'artifacts' / 'native-goals' / identity
    directory.mkdir(parents=True, exist_ok=True)
    lock_fd = safe_open(directory, 'session.lock', os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        return GoalRunResult('', 1, {'host_accepted': False,
            'error': 'native_goal_session_already_owned'})
    try:
        return _run_goal(command=command, root=root, worktree=worktree, prompt=prompt,
                         objective=objective, validate=validate, timeout=timeout,
                         lock_fd=lock_fd, planning_prompt=planning_prompt,
                         plan_path=plan_path, on_report=on_report,
                         transport_args=transport_args, output_format=output_format,
                         session_context_budget=session_context_budget,
                         host_deadline_epoch=host_deadline_epoch)
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def _run_goal(*, command, root: Path, worktree: Path, prompt: str,
              objective: str, validate: Callable[[], dict], timeout: float,
              lock_fd: int, planning_prompt: str | None = None,
              plan_path: Path | None = None,
              on_report: Callable[[str], None] | None = None,
              transport_args: Mapping[str, Any] | None = None,
              output_format: Mapping[str, Any] | None = None,
              session_context_budget: Mapping[str, Any] | None = None,
              host_deadline_epoch: float | None = None) -> GoalRunResult:
    root, worktree = Path(root).resolve(), Path(worktree).resolve()
    identity = sha256(str(command.command_id).encode()).hexdigest()[:24]
    directory = root / 'artifacts' / 'native-goals' / identity
    state_path = directory / 'state.json'
    events_path = directory / 'events.jsonl'
    from .workspace import is_project_workspace
    if not root.is_dir() or not worktree.is_dir() or not is_project_workspace(root, worktree):
        return GoalRunResult('', 1, {'host_accepted': False,
            'error': 'unsafe native goal root or workspace'})
    if transport_args is not None and not isinstance(transport_args, Mapping):
        return GoalRunResult('', 1, {'host_accepted': False,
            'error': 'transport_args must be an OpenCode MCP mapping'})
    transport_args = copy.deepcopy(dict(transport_args or {}))
    if 'modport_sandbox' in transport_args:
        return GoalRunResult('', 1, {'host_accepted': False,
            'error': 'modport_sandbox is reserved for the host sandbox bridge'})
    if output_format is not None and not isinstance(output_format, Mapping):
        return GoalRunResult('', 1, {'host_accepted': False,
            'error': 'output_format must be a mapping'})
    if session_context_budget is not None:
        from .session_context_budget import context_budget, SessionContextBudgetError
        try:
            session_context_budget = context_budget(session_context_budget)
        except SessionContextBudgetError as exc:
            return GoalRunResult('', 1, {'host_accepted': False, 'error': str(exc)})

    deny_network_tools = network_tools_disabled()
    immutable = {'command_id': str(command.command_id), 'objective': objective,
                 'worktree': str(worktree), 'prompt_sha256': sha256(prompt.encode()).hexdigest()}
    cleanup_binding = command.payload.get('opencode_cleanup_binding')
    if cleanup_binding is not None:
        if not isinstance(cleanup_binding, Mapping):
            return GoalRunResult('', 1, {'host_accepted': False,
                'error': 'native_goal_cleanup_binding_invalid'})
        immutable['opencode_cleanup_binding'] = dict(cleanup_binding)
    if command.options.get('workflow_version', 0) >= 25:
        immutable['network_tools_disabled'] = deny_network_tools
    if session_context_budget is not None:
        immutable['session_context_budget'] = session_context_budget
    if command.options.get('workflow_version', 0) >= 20 and len(objective.encode('utf-8')) > 4000:
        full_objective = objective
        objective = (
            'Complete the assigned ModPort task under the full objective and constraints '
            'provided in the execution message. Preserve all required evidence and '
            'report the actual outcome. Full objective SHA-256: '
            + sha256(full_objective.encode()).hexdigest())
        prompt = prompt + '\n\n## Full host goal objective (unabridged)\n' + full_objective
        immutable.update(native_objective=objective,
                         execution_prompt_sha256=sha256(prompt.encode()).hexdigest())
    if planning_prompt is not None:
        if (plan_path is None or plan_path.resolve() != plan_path.absolute()
                or not plan_path.resolve().is_relative_to(root)):
            return GoalRunResult('', 1, {'host_accepted': False,
                'error': 'unsafe native plan path'})
        immutable.update(planning_prompt_sha256=sha256(planning_prompt.encode()).hexdigest(),
                         plan_path=str(plan_path.relative_to(root)))

    resume = False
    previous: dict[str, Any] = {}
    try:
        claim_fd = safe_open(directory, 'claim', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            if state_path.is_symlink() or not state_path.is_file():
                previous = {}
            else:
                previous = json.loads(state_path.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            return GoalRunResult('', 1, {'host_accepted': False,
                'error': 'native_goal_state_unreadable'})
        if not isinstance(previous, dict):
            return GoalRunResult('', 1, {'host_accepted': False,
                'error': 'native_goal_state_unreadable'})
        if not command.options.get('native_goal_resume'):
            return GoalRunResult('', 1, {**previous, 'host_accepted': False,
                'error': 'native_goal_replay_requires_explicit_recovery'})
        previous_identity = dict(previous)
        if ('network_tools_disabled' in immutable
                and 'network_tools_disabled' not in previous_identity):
            # Native goals created before this host policy implicitly allowed
            # network-capable tools. Keep their default-allow resume identity.
            previous_identity['network_tools_disabled'] = False
        if any(previous_identity.get(key) != value for key, value in immutable.items()):
            return GoalRunResult('', 1, {**previous, 'host_accepted': False,
                'error': 'native_goal_resume_identity_mismatch'})
        if _previous_process_alive(previous):
            return GoalRunResult('', 1, {**previous, 'host_accepted': False,
                'error': 'native_goal_previous_process_alive'})
        resume = True
    else:
        os.close(claim_fd)
        if command.options.get('native_goal_resume'):
            (directory / 'claim').unlink(missing_ok=True)
            return GoalRunResult('', 1, {'host_accepted': False,
                'error': 'native_goal_resume_thread_missing'})

    started = time.monotonic()
    assignment_deadline = time.time() + max(0, timeout)
    host_assignment_deadline = (assignment_deadline if host_deadline_epoch is None
                                else host_deadline_epoch)
    if resume:
        stored_deadline = previous.get('deadline_epoch')
        if (not isinstance(stored_deadline, (float, int)) or isinstance(stored_deadline, bool)
                or not math.isfinite(stored_deadline)):
            return GoalRunResult('', 1, {**previous, 'host_accepted': False,
                'error': 'native_goal_resume_deadline_missing'})
        assignment_deadline = min(assignment_deadline, float(stored_deadline))
        stored_host_deadline = previous.get('host_deadline_epoch', stored_deadline)
        if (not isinstance(stored_host_deadline, (float, int))
                or isinstance(stored_host_deadline, bool)
                or not math.isfinite(stored_host_deadline)):
            return GoalRunResult('', 1, {**previous, 'host_accepted': False,
                'error': 'native_goal_resume_deadline_missing'})
        host_assignment_deadline = min(host_assignment_deadline, float(stored_host_deadline))
    assignment_deadline = min(assignment_deadline, host_assignment_deadline)
    deadline = started + max(0, assignment_deadline - time.time())
    host_deadline = started + max(0, host_assignment_deadline - time.time())

    metadata = previous if resume else {
        'thread_id': None, 'native_goal_status': None, 'native_goal': None,
        'host_accepted': False, 'validation_records': [], 'turns': [],
        'tokens_used': 0, 'time_used_seconds': 0,
        'state_path': str(state_path.relative_to(root)),
        'events_path': str(events_path.relative_to(root)), **immutable,
        'status': 'starting', 'attempts': [], 'deadline_epoch': assignment_deadline,
    }
    if host_deadline_epoch is not None:
        metadata.setdefault('host_deadline_epoch', host_assignment_deadline)
    if planning_prompt is not None:
        metadata.setdefault('dialogue_phase', 'plan')
        metadata.setdefault('planning_turns', [])
    metadata.setdefault('planning_messages', [])
    metadata.setdefault('public_messages', [])
    metadata.setdefault('public_message_ids', [])
    metadata.setdefault('turn_sequence', 0)
    prior_elapsed = float(metadata.get('elapsed_seconds', 0))
    prior_accepted = metadata.get('host_accepted', False)
    metadata['host_accepted'] = False
    metadata.pop('error', None)
    attempt = {'number': len(metadata.setdefault('attempts', [])) + 1,
               'started_at': time.time(), 'resume': resume}
    metadata['attempts'].append(attempt)
    model_result_path = directory / 'model-result.json'

    transport: OpenCodeServer | None = None
    active_model, active_effort = _agent_model_policy(command)
    resolved_model = resolve_model_id(active_model)
    configured_model = resolved_model
    fallback_selection = None
    policy = command.options.get('model_policy')
    if command.options.get('workflow_version', 0) >= 37 and isinstance(policy, Mapping):
        from .model_policy import resolve_model_selection
        fallback_selection = resolve_model_selection(policy, command.stage_id).get('fallback')
    if metadata.get('provider_fallback'):
        if not isinstance(fallback_selection, Mapping):
            raise RuntimeError('native_goal_fallback_configuration_missing')
        resolved_model = resolve_model_id(fallback_selection['model'])
        active_effort = fallback_selection['reasoning_effort']
    active_session_budget = (dict(session_context_budget) if session_context_budget is not None else None)
    if metadata.get('provider_fallback', {}).get('context_budget') is not None:
        from .session_context_budget import context_budget
        active_session_budget = context_budget(metadata['provider_fallback']['context_budget'])
    downstream_toolcall = command.options.get('gate_policy') == 'downstream_toolcall'
    advisory = business_gates_disabled(command)
    texts = metadata['public_messages']
    planning_texts = metadata['planning_messages']
    read_only = bool(command.options.get('goal_read_only', False))
    session_title = f'ModPort goal {identity}'
    host_tools = {}
    if 'modport_rework' in transport_args:
        host_tools.update({
            'modport_rework_list_rework_targets': 'allow',
            'modport_rework_request_rework': 'allow',
        })
    host_tools['modport_sandbox_run_project_command'] = (
        'deny' if deny_network_tools else 'allow')
    host_tools['modport_sandbox_read_run_artifact'] = 'allow'
    from .opencode_subagents import TASK_PERMISSION, subagent_profiles
    permissions = {'bash': 'deny', 'task': TASK_PERMISSION, 'question': 'deny',
                   'doom_loop': 'deny', 'external_directory': 'deny', **host_tools}
    from .workspace import workspace_spec
    if workspace_spec(root):
        from .local_workspace_sandbox import workspace_sensitive_permissions
        permissions.update(workspace_sensitive_permissions(worktree))
    if read_only:
        permissions['edit'] = 'deny'
    if deny_network_tools:
        permissions.update({'webfetch': 'deny', 'websearch': 'deny'})

    def persist():
        metadata['elapsed_seconds'] = prior_elapsed + time.monotonic() - started
        metadata['time_used_seconds'] = max(
            float(metadata.get('time_used_seconds', 0)), metadata['elapsed_seconds'])
        model_record = _model_result_summary(metadata, command.command_id)
        atomic_json(model_result_path, model_record)
        metadata['model_result_ref'] = {
            'path': model_result_path.relative_to(root).as_posix(),
            'sha256': file_digest(model_result_path),
            'media_type': 'application/json',
        }
        _atomic_json(state_path, metadata)

    def audit(method: str, params: Mapping[str, Any] | None = None):
        with events_path.open('a', encoding='utf-8') as output:
            output.write(json.dumps({'at': time.time(), 'event': {
                'method': method, 'params': dict(params or {})}},
                ensure_ascii=False, separators=(',', ':')) + '\n')

    def close_transport(*, deadline_exceeded=False, cleanup_reason='host_cleanup'):
        nonlocal transport
        if transport is None:
            return True
        try:
            diagnostic = transport.close(deadline_exceeded=deadline_exceeded,
                                         cleanup_reason=cleanup_reason)
        except Exception as exc:
            diagnostic = {'classification': 'unknown', 'target_pid': transport.process.pid,
                          'error': type(exc).__name__, 'detail': str(exc)[:300],
                          'cleanup_reason': cleanup_reason}
        confirmed = (isinstance(diagnostic, Mapping)
                     and diagnostic.get('cleanup_confirmed',
                                        diagnostic.get('returncode') is not None) is True)
        if diagnostic is not None:
            diagnostic = _redact_error_text(json.loads(json.dumps(diagnostic)))
            attempt['process_diagnostic'] = diagnostic
            metadata.setdefault('process_diagnostics', []).append(diagnostic)
            audit('modport/process/diagnostic', diagnostic)
        if not confirmed:
            metadata['producer_stopped'] = False
            metadata['host_accepted'] = False
            metadata['cleanup_unconfirmed'] = True
            if command.options.get('host_collect_candidate'):
                metadata['candidate_capture'] = {
                    'status': 'not_captured',
                    'reason': 'opencode_process_tree_cleanup_unconfirmed',
                }
            if not metadata.get('stop_reason'):
                metadata['stop_reason'] = 'native_goal_producer_cleanup_unconfirmed'
            if metadata.get('status') in {'accepted', 'completed_with_diagnostics', 'validating'}:
                metadata['status'] = 'failed'
                metadata['native_goal_status'] = 'failed'
                if isinstance(metadata.get('native_goal'), dict):
                    metadata['native_goal']['status'] = 'failed'
            persist()
            return False
        transport = None
        metadata['producer_stopped'] = True
        metadata['cleanup_unconfirmed'] = False
        metadata['cleanup_state'] = 'confirmed'
        if command.options.get('host_collect_candidate'):
            metadata.setdefault('candidate_capture', {
                'status': 'pending_host_settlement',
                'reason': 'candidate_patch_is_published_after_native_goal_result',
            })
        persist()
        return True

    def update_goal(status: str):
        metadata['native_goal_status'] = status
        metadata['native_goal'] = {
            'id': metadata.get('thread_id'), 'objective': objective,
            'status': status, 'tokensUsed': metadata.get('tokens_used', 0),
            'timeUsedSeconds': metadata.get('time_used_seconds', 0),
        }
        persist()

    def verify(turn_id: str | None):
        # Candidate capture must not race the model or any MCP tool process.
        if advisory and command.options.get('host_collect_candidate') and transport is not None:
            if not close_transport(cleanup_reason='candidate_capture'):
                raise RuntimeError('OpenCode producer cleanup is unconfirmed before candidate capture')
        if time.monotonic() >= host_deadline:
            raise TimeoutError('native goal host settlement deadline exhausted before verification')
        metadata['status'] = 'validating'
        persist()
        validation_started = time.monotonic()
        try:
            if on_report is not None and texts:
                on_report(texts[-1])
            verification = validate()
            if not isinstance(verification, dict) or type(verification.get('accepted')) is not bool:
                raise ValueError('host validation must return an accepted boolean')
        except (OSError, ValueError, TypeError, KeyError, RuntimeError,
                TimeoutError, Exception) as exc:
            if not advisory:
                raise
            verification = {'accepted': False, 'failures': [str(exc)], 'evidence': {},
                            'validation_error': type(exc).__name__}
        verification = _redact_error_text(json.loads(json.dumps(verification)))
        record = {'turn_id': turn_id, 'at': time.time(),
                  'attempt': attempt['number'],
                  'duration_seconds': time.monotonic() - validation_started,
                  'result': verification}
        metadata['validation_records'].append(record)
        audit('modport/goal/validation', {
            'turn_id': turn_id, 'attempt': attempt['number'],
            'accepted': verification['accepted'],
        })
        persist()
        if time.monotonic() >= host_deadline:
            raise TimeoutError('native goal host settlement deadline exhausted during verification')
        if verification['accepted']:
            metadata['host_accepted'] = True
            metadata['status'] = 'accepted'
            update_goal('complete')
        else:
            metadata['status'] = 'validation_rejected'
            if advisory:
                metadata['status'] = 'completed_with_diagnostics'
                metadata['validation_failure'] = verification
            elif downstream_toolcall:
                metadata['stop_reason'] = 'native_goal_validation_rejected'
                metadata['downstream_rework_required'] = True
                metadata['validation_failure'] = verification
            elif (not progress_supervised(command)
                  and command.options.get('workflow_version', 0) >= 15):
                maximum = getattr(command, 'payload', {}).get('request', {}).get(
                    'budget', {}).get('max_rework_rounds', 10)
                if type(maximum) is not int or maximum < 0:
                    raise ValueError('native goal requires a finite nonnegative rework budget')
                rejected = len({row.get('turn_id') for row in metadata['validation_records']
                                if row.get('result', {}).get('accepted') is False})
                metadata['rejected_completions'] = rejected
                metadata['rework_limit'] = maximum
                if rejected > maximum:
                    metadata['stop_reason'] = 'native_goal_rework_rounds_exhausted'
                    persist()
                    raise RuntimeError('native_goal_rework_rounds_exhausted')
            update_goal('active')
        return verification

    def safe_tools(*, planning=False) -> dict[str, bool]:
        assert transport is not None
        settings = {}
        for name in transport.tool_ids(cwd=worktree, deadline=deadline):
            if name in {'bash', 'shell', 'terminal', 'question'} or (planning and name == 'task'):
                settings[name] = False
            if planning and name in {'edit', 'write', 'patch', 'apply_patch', 'multiedit'}:
                settings[name] = False
        settings['modport_sandbox_run_project_command'] = (
            not planning and not deny_network_tools)
        if deny_network_tools:
            settings.update({'webfetch': False, 'websearch': False})
        if 'modport_rework' in transport_args:
            settings['modport_rework_list_rework_targets'] = not planning
            settings['modport_rework_request_rework'] = not planning
        return settings

    def require_mcp_servers():
        """Wait briefly for every configured host MCP to become connected."""
        if not mcp_config:
            return
        assert transport is not None
        expected = []
        for name, spec in mcp_config.items():
            if not isinstance(spec, Mapping):
                raise OpenCodeError(f'configured OpenCode MCP {name!r} has an invalid config')
            if spec.get('enabled', True) is not False:
                expected.append(name)
        latest: dict[str, Any] = {}
        while time.monotonic() < deadline:
            try:
                latest = transport.mcp_status(
                    cwd=worktree, deadline=min(deadline, time.monotonic() + 3.0))
            except TimeoutError:
                # This HTTP probe has a short timeout of its own. A slow
                # response does not exhaust the assignment's actual deadline.
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
                continue
            disconnected = []
            for name in expected:
                entry = latest.get(name)
                status = entry.get('status') if isinstance(entry, Mapping) else entry
                if status == 'connected':
                    continue
                if isinstance(status, str) and status in {'failed', 'error', 'disconnected'}:
                    detail = (entry.get('error') if isinstance(entry, Mapping) else None)
                    raise OpenCodeError(
                        f'configured OpenCode MCP {name!r} failed to connect'
                        + (f': {str(detail)[:250]}' if detail else ''))
                disconnected.append(name)
            if not disconnected:
                metadata['mcp_status'] = {name: 'connected' for name in expected}
                persist()
                return
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        metadata['mcp_status'] = {
            name: (latest.get(name, {}).get('status')
                   if isinstance(latest.get(name), Mapping) else latest.get(name))
            for name in expected
        }
        raise TimeoutError('configured OpenCode MCP connection deadline exhausted')

    def make_recovery_prompt(pending: Mapping[str, Any]) -> str:
        original = pending.get('prompt')
        if not isinstance(original, str):
            original = planning_prompt if pending.get('phase') == 'plan' else prompt
        if pending.get('phase') == 'plan':
            return ('The previous planning request was interrupted. Continue from the current '
                    'session context and return the complete plan.\n\n' + original)
        return (
            'The previous execution request was interrupted while the managed session was '
            'running. Inspect the current worktree and this session transcript, reconcile any '
            'completed tool results, and continue the same persistent ModPort goal. Do not '
            'repeat a tool call whose effect may already have completed unless you verify it '
            'did not. Preserve the full original assignment and constraints below.\n\n'
            + original)

    def prepare_plan(response: Mapping[str, Any], turn_id: str):
        plan = _response_text(response).strip()
        if not plan:
            metadata.setdefault('business_diagnostics', []).append('planning reply was empty')
        assert plan_path is not None
        target = plan_path.resolve()
        if target != plan_path.absolute() or not target.is_relative_to(root):
            raise ValueError('unsafe native plan path')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(plan + ('\n' if plan else ''), encoding='utf-8')
        metadata['dialogue_phase'] = 'execute'
        metadata.setdefault('planning_turns', []).append({
            'id': turn_id, 'status': 'completed',
        })
        audit('modport/goal/planning_completed', {'turn_id': turn_id,
              'plan_sha256': sha256(plan.encode()).hexdigest()})
        persist()

    def record_response(response: Mapping[str, Any], pending: Mapping[str, Any]) -> tuple[str, str]:
        info = response.get('info')
        info = info if isinstance(info, Mapping) else {}
        turn_id = info.get('id') if isinstance(info.get('id'), str) else pending['message_id']
        phase = pending.get('phase', 'execute')
        user_message_id = pending['message_id']
        public_id = str(turn_id)
        text = _response_text(response)
        from .telemetry import redact
        text = redact(text)
        known = metadata.setdefault('public_message_ids', [])
        if text and public_id not in known:
            known.append(public_id)
            if phase == 'plan':
                planning_texts.append(text)
            else:
                texts.append(text)
        tokens = _token_count(info)
        duration = _duration_seconds(info)
        metadata['tokens_used'] = int(metadata.get('tokens_used', 0)) + tokens
        metadata['time_used_seconds'] = float(metadata.get('time_used_seconds', 0)) + duration
        turn_record = {
            'id': public_id, 'user_message_id': user_message_id,
            'status': 'completed', 'phase': phase, 'model_id': info.get('modelID'),
            'provider_id': info.get('providerID'), 'variant': info.get('variant'),
            'tokens': info.get('tokens') if isinstance(info.get('tokens'), Mapping) else {},
            'duration_seconds': duration,
            'tool_calls': _tool_summaries(response),
        }
        response_bytes, response_sha256 = _bounded_text_digest(text)
        turn_record['response_sha256'] = response_sha256
        if response_sha256 is None:
            turn_record['response_bytes_lower_bound'] = response_bytes
            turn_record['response_digest_status'] = 'exceeds_8_mib_limit'
        structured = info.get('structured')
        if structured is not None:
            structured_size, structured_sha256 = _bounded_value_digest(structured)
            turn_record['structured_result_bytes'] = structured_size
            turn_record['structured_result_sha256'] = structured_sha256
        if session_context_budget is not None:
            from .session_context_budget import prior_context_usage
            final_tokens = info.get('last_step_tokens')
            turn_record['context_usage'] = prior_context_usage(
                tokens=final_tokens if isinstance(final_tokens, Mapping) else None,
                prior_prompt=pending['prompt'], response=response)
        if command.options.get('workflow_version', 0) >= 20:
            from .goal_progress import observe_goal_item
            observations = metadata.setdefault('progress_observations', {})
            for part in response.get('parts', ()):
                if not isinstance(part, Mapping) or part.get('type') != 'tool':
                    continue
                state = part.get('state')
                if not isinstance(state, Mapping) or state.get('status') != 'completed':
                    continue
                tool = part.get('tool')
                if tool in {'edit', 'write', 'patch', 'apply_patch'}:
                    observe_goal_item(observations, {'type': 'fileChange'})
                    continue
                item = {
                    'type': 'mcpToolCall' if isinstance(tool, str) and tool.startswith('modport_')
                            else 'dynamicToolCall',
                    'server': (tool.split('_', 2)[1]
                               if isinstance(tool, str) and tool.startswith('modport_') else None),
                    'tool': tool,
                    'arguments': state.get('input'),
                    'result': state.get('output'),
                    'error': state.get('error'),
                }
                observe_goal_item(observations, item)
        if phase == 'plan':
            metadata.setdefault('planning_turns', []).append(turn_record)
        else:
            metadata.setdefault('turns', []).append(turn_record)
        metadata.pop('pending_turn', None)
        metadata.pop('interrupted_context_usage', None)
        metadata['active_turn_id'] = None
        metadata['model_terminal_status'] = 'completed'
        metadata['model_terminal_turn_id'] = public_id
        metadata['status'] = 'running'
        update_goal('active')
        audit('modport/goal/turn_completed', {
            'turn_id': public_id, 'user_message_id': user_message_id,
            'phase': phase, 'tokens': tokens, 'tool_calls': turn_record['tool_calls'],
        })
        persist()
        return phase, public_id

    def process_response(response: Mapping[str, Any], pending: Mapping[str, Any]):
        phase, turn_id = record_response(response, pending)
        if phase == 'plan':
            prepare_plan(response, turn_id)
            return None
        text = _response_text(response)
        if _is_blocked_response(text):
            metadata['status'] = 'blocked'
            metadata['stop_reason'] = 'model_reported_blocked'
            update_goal('blocked')
            return {'blocked': True}
        return verify(turn_id)

    def last_context_usage() -> Mapping[str, Any] | None:
        previous_turns = metadata.get('planning_turns', []) + metadata.get('turns', [])
        previous = next((turn for turn in reversed(previous_turns)
                         if isinstance(turn, Mapping) and 'context_usage' in turn), None)
        return previous['context_usage'] if previous is not None else None

    def activate_fallback(error, pending=None):
        nonlocal resolved_model, active_effort, active_session_budget
        kind = provider_failure_kind(error)
        if kind is None or not isinstance(fallback_selection, Mapping) or metadata.get('provider_fallback'):
            return False
        if pending is not None:
            # Definitive provider errors may follow completed tool work within
            # a turn. Keep those on the explicit recovery path, never replay.
            rows = transport.messages(metadata['thread_id'], cwd=worktree, deadline=deadline)
            current = [row for row in rows if isinstance(row, Mapping)
                       and isinstance(row.get('info'), Mapping)
                       and row['info'].get('parentID') == pending['message_id']]
            if any(part.get('type') == 'tool' for row in current
                   for part in row.get('parts', []) if isinstance(part, Mapping)):
                return False
            prior = metadata.get('interrupted_context_usage') or last_context_usage() or {}
            metadata['interrupted_context_usage'] = {
                'tokens': max(prior.get('tokens', 0),
                    len(json.dumps(rows, ensure_ascii=False, default=str).encode()),
                    len(str(pending.get('prompt', '')).encode())),
                'source': 'failed_provider_transcript_bound'}
        selected_model = resolve_model_id(fallback_selection['model'])
        selected_effort = fallback_selection['reasoning_effort']
        transport.require_model(cwd=worktree, model=selected_model,
                                variant=selected_effort, deadline=deadline)
        if active_session_budget is not None:
            catalogue = transport.providers(cwd=worktree, deadline=deadline)
            provider_id, model_id = selected_model.split('/', 1)
            provider = next((row for row in catalogue['all'] if row.get('id') == provider_id), {})
            limits = provider.get('models', {}).get(model_id, {}).get('limit', {})
            if any(type(limits.get(key)) is not int or limits[key] <= 0
                   for key in ('context', 'input', 'output')):
                raise OpenCodeError('fallback model context limits are unavailable')
            from .session_context_budget import context_budget
            capped = dict(active_session_budget)
            capped['context_window'] = min(capped['context_window'], limits['context'])
            capped['output_token_reserve'] = min(capped['output_token_reserve'], limits['output'])
            capped['input_token_budget'] = min(capped['input_token_budget'], limits['input'],
                capped['context_window'] - capped['output_token_reserve'] - capped['tool_token_reserve'])
            active_session_budget = context_budget(capped)
        metadata['provider_fallback'] = {'reason': kind, 'model': selected_model,
            'reasoning_effort': selected_effort,
            'context_budget': active_session_budget,
            'failed_message_id': pending.get('message_id') if pending else None,
            'activated_at': time.time()}
        resolved_model, active_effort = selected_model, selected_effort
        persist()
        return True

    def send_turn(text: str, *, phase: str, message_id: str | None = None):
        assert transport is not None
        from .coder_readability import append_readability
        text = append_readability(text, command, root)
        if time.monotonic() >= deadline:
            raise TimeoutError('native goal original assignment deadline exhausted')
        if active_session_budget is not None:
            prior = metadata.get('interrupted_context_usage') or last_context_usage()
            if prior is None and metadata.get('provider_fallback'):
                prior = {'tokens': 0, 'source': 'initial_fallback_turn'}
            if prior is not None:
                from .session_context_budget import require_next_turn_capacity
                metadata['context_preflight'] = require_next_turn_capacity(
                    active_session_budget, prior, text,
                    output_format if phase == 'execute' else None)
            elif phase == 'execute' and planning_prompt is not None:
                from .session_context_budget import SessionContextBudgetError
                raise SessionContextBudgetError(
                    'prior planning context usage is unavailable for execution')
        if message_id is None:
            sequence = int(metadata.get('turn_sequence', 0)) + 1
            metadata['turn_sequence'] = sequence
            message_id = 'msg' + uuid5(
                NAMESPACE_URL, str(command.command_id) + ':goal:' + str(sequence)).hex
        pending = {
            'message_id': message_id, 'phase': phase,
            'prompt_sha256': sha256(text.encode()).hexdigest(), 'prompt': text,
            'started_at': time.time(),
        }
        metadata['pending_turn'] = pending
        metadata['active_turn_id'] = message_id
        metadata['model_terminal_status'] = 'pending'
        metadata['model_terminal_turn_id'] = message_id
        metadata['status'] = 'running'
        persist()  # durable request intent precedes the HTTP side effect
        from .execution_progress import mark_current_execution_progress
        mark_current_execution_progress('model_started')
        try:
            from .opencode_agent import observe_turn_usage
            with observe_turn_usage(transport, cwd=worktree, session_id=metadata['thread_id'],
                                    message_id=message_id, deadline=deadline):
                response = transport.send_message(
                    metadata['thread_id'], text, cwd=worktree,
                    model=resolved_model, variant=active_effort,
                    system=(
                        'You are continuing a persistent ModPort goal. Host goal objective: '
                        + objective + '\nThe host validator is authoritative; finishing one '
                        'assistant response does not itself complete the goal. If the goal cannot '
                        'be continued, include a final line exactly: '
                        'MODPORT_GOAL_STATUS: blocked.'),
                    tools=safe_tools(planning=phase == 'plan'),
                    output_format=(output_format if phase == 'execute' else None),
                    deadline=deadline, message_id=message_id,
                )
        except OpenCodeResponseError as exc:
            metadata['model_terminal_status'] = 'provider_error'
            metadata['model_terminal_turn_id'] = message_id
            metadata['pending_response_error'] = {
                'name': _redact_error_text(exc.error.get('name')),
                'fallback_reason': provider_failure_kind(exc),
                'message': _redact_error_text(
                    exc.error.get('data', {}).get('message')
                    if isinstance(exc.error.get('data'), Mapping) else None),
            }
            persist()
            if activate_fallback(exc, pending):
                metadata.setdefault('response_errors', []).append({
                    'message_id': message_id,
                    'name': metadata['pending_response_error']['name'],
                    'fallback_reason': metadata['pending_response_error']['fallback_reason']})
                metadata.pop('pending_turn', None)
                metadata.pop('pending_response_error', None)
                persist()
                return send_turn(text, phase=phase)
            raise
        metadata.pop('pending_response_error', None)
        response_info = response.get('info')
        assistant_message_id = (response_info.get('id')
                                if isinstance(response_info, Mapping) else None)
        if not isinstance(assistant_message_id, str):
            raise OpenCodeError('OpenCode response has no assistant message ID')
        transcript = transport.messages(
            metadata['thread_id'], cwd=worktree, deadline=deadline)
        transcript_state, transcript_response = _session_response(
            transcript, message_id, expected_assistant_id=assistant_message_id)
        if transcript_state == 'error' and transcript_response is not None:
            transcript_info = transcript_response.get('info', {})
            transcript_error = (transcript_info.get('error')
                                if isinstance(transcript_info, Mapping) else None)
            if isinstance(transcript_error, Mapping):
                raise OpenCodeResponseError(transcript_error, transcript_response)
        if transcript_state != 'completed' or transcript_response is None:
            raise OpenCodeError(
                'OpenCode final assistant response was not confirmed in its session transcript')
        response = transcript_response
        return process_response(response, pending)

    def reconcile_pending() -> tuple[bool, str | None]:
        """Return (response-was-processed, next recovery prompt)."""
        pending = metadata.get('pending_turn')
        if not isinstance(pending, Mapping):
            return False, None
        assert transport is not None
        messages = transport.messages(metadata['thread_id'], cwd=worktree,
                                      deadline=deadline)
        state, response = _session_response(messages, pending['message_id'])
        if state == 'error' and response is not None:
            error = response.get('info', {}).get('error', {})
            activate_fallback(error, pending)
        if state == 'completed' and response is not None:
            result = process_response(response, pending)
            return True, ('__blocked__' if isinstance(result, Mapping) and result.get('blocked')
                          else None)
        if state == 'absent':
            # The durable intent is absent from the OpenCode transcript, so
            # retry the same idempotency key rather than creating a duplicate.
            metadata['recovery_phase'] = pending.get('phase', 'execute')
            return True, '__resend_pending__'
        if session_context_budget is not None:
            from .session_context_budget import prior_context_usage
            previous = last_context_usage()
            if previous is None and pending.get('phase') == 'execute' and planning_prompt is not None:
                from .session_context_budget import SessionContextBudgetError
                raise SessionContextBudgetError(
                    'prior planning context usage is unavailable for recovery')
            pending_rows = [row for row in messages
                            if isinstance(row, Mapping)
                            and isinstance(row.get('info'), Mapping)
                            and (row['info'].get('id') == pending['message_id']
                                 or row['info'].get('parentID') == pending['message_id'])]
            pending_bytes = len(json.dumps(pending_rows, ensure_ascii=False,
                                           default=str, separators=(',', ':')).encode('utf-8'))
            pending_prompt = pending.get('prompt')
            prompt_bytes = len(pending_prompt.encode('utf-8')) if isinstance(pending_prompt, str) else 0
            prior_tokens = previous['tokens'] if previous is not None else 0
            observed = prior_context_usage(
                tokens=(response.get('info', {}).get('last_step_tokens')
                        if isinstance(response, Mapping) else None),
                prior_prompt=pending_prompt if isinstance(pending_prompt, str) else '',
                response=pending_rows)
            metadata['interrupted_context_usage'] = {
                'tokens': max(prior_tokens + max(prompt_bytes, pending_bytes),
                              observed['tokens']),
                'source': 'interrupted_transcript_bound',
            }
        # The user request reached OpenCode but has no completed assistant
        # response. Abort it, then continue with a new ID after inspecting the
        # transcript; never blindly replay a potentially side-effecting request.
        try:
            transport.abort_session(metadata['thread_id'], cwd=worktree,
                                    deadline=min(deadline, time.monotonic() + 2.0))
        except (OpenCodeError, TimeoutError):
            pass
        recovery = make_recovery_prompt(pending)
        metadata['recovery_phase'] = pending.get('phase', 'execute')
        if state == 'error' and response is not None:
            info = response.get('info', {})
            error = info.get('error', {}) if isinstance(info, Mapping) else {}
            metadata.setdefault('response_errors', []).append({
                'message_id': pending['message_id'],
                'name': _redact_error_text(
                    error.get('name') if isinstance(error, Mapping) else None),
                'message': _redact_error_text(
                    error.get('data', {}).get('message')
                    if isinstance(error, Mapping)
                    and isinstance(error.get('data'), Mapping) else None),
            })
            recovery = ('The prior OpenCode response ended with a provider error. Treat it as '
                        'a failed request with no accepted result. Continue only after checking '
                        'the current worktree and session transcript.\n\n' + recovery)
        metadata.setdefault('interrupted_turns', []).append({
            'message_id': pending['message_id'], 'phase': pending.get('phase'),
            'status': 'interrupted',
        })
        metadata['model_terminal_status'] = 'interrupted'
        metadata['model_terminal_turn_id'] = pending['message_id']
        metadata.pop('pending_turn', None)
        metadata.pop('pending_response_error', None)
        metadata['active_turn_id'] = None
        persist()
        return True, recovery

    persist()
    try:
        if time.monotonic() >= deadline:
            raise TimeoutError('native goal original assignment deadline exhausted')

        # A completed prior host acceptance is rechecked without launching a
        # model. Changed inputs or validation results still follow explicit
        # recovery through the same OpenCode session.
        if resume and prior_accepted:
            last_turn = metadata['turns'][-1]['id'] if metadata.get('turns') else None
            verdict = verify(last_turn)
            if verdict['accepted'] or advisory:
                return GoalRunResult(_public_stdout(metadata), 0, metadata)
            if downstream_toolcall:
                metadata['status'] = 'failed'
                persist()
                return GoalRunResult('', 1, metadata)

        remaining = max(0.0, deadline - time.monotonic())
        if remaining <= 0:
            raise TimeoutError('native goal original assignment deadline exhausted')
        from .opencode_shell_mcp import prepare_sandbox_tool
        shell_config = prepare_sandbox_tool(
            root, worktree, str(command.command_id), remaining, read_only=read_only)
        mcp_config = {**transport_args, **shell_config}
        opencode_config = OpenCodeConfig(
            mcp=mcp_config,
            permission=permissions,
            provider=managed_provider_config(),
            model=configured_model,
            agent=subagent_profiles(model_policy=policy, model=active_model,
                                    variant=active_effort, permissions=permissions,
                                    inherit_coder=True),
            extra={'tools': {
                **({name: False for name in (
                    'modport_sandbox_run_project_command', 'webfetch', 'websearch')}
                   if deny_network_tools else {}),
            }},
        )
        effective_config = opencode_config.to_dict()
        stable_mcp, timeout_ms = _stable_mcp_config(effective_config.get('mcp', {}))
        identity_config = dict(effective_config)
        identity_config['mcp'] = stable_mcp
        config_json = json.dumps(identity_config, sort_keys=True, ensure_ascii=False,
                                 separators=(',', ':')).encode()
        config_hash = sha256(config_json).hexdigest()
        stored_config_hash = metadata.get('transport_config_sha256')
        stored_timeout_ms = metadata.get('transport_tool_timeout_ms')
        if resume and stored_config_hash and stored_config_hash != config_hash:
            # Timeout decreases do not change the canonical MCP config hash.
            raise RuntimeError('native_goal_resume_identity_mismatch')
        if (resume and stored_timeout_ms is not None
                and (type(stored_timeout_ms) is not int
                     or timeout_ms is not None and timeout_ms > stored_timeout_ms)):
            raise RuntimeError('native_goal_resume_identity_mismatch')
        metadata.setdefault('transport_config_sha256', config_hash)
        metadata.setdefault('transport_tool_timeout_ms', timeout_ms)
        metadata.setdefault('transport_tool_timeout_sec',
                            math.ceil(timeout_ms / 1000) if timeout_ms is not None else None)

        metadata['status'] = 'starting'
        metadata['opencode_state_path'] = str((directory / 'opencode-state').relative_to(root))
        persist()
        transport = OpenCodeServer.start(
            cwd=worktree, config=opencode_config,
            xdg_root=directory / 'opencode-state', deadline=deadline,
            lock_fd=lock_fd,
        )
        from .token_budget import bind_token_budget
        bind_token_budget(transport, root)
        if resume:
            prior_hash = metadata.get('opencode_executable_sha256')
            prior_version = metadata.get('opencode_version')
            if (prior_hash is not None and prior_hash != transport.executable_sha256
                    or prior_version is not None and prior_version != transport.version):
                raise RuntimeError('native_goal_resume_opencode_identity_mismatch')
        metadata['producer_stopped'] = False
        metadata['opencode_version'] = transport.version
        metadata['opencode_executable'] = transport.executable
        metadata['opencode_executable_sha256'] = transport.executable_sha256
        metadata['owned_pid'] = transport.process.pid
        metadata['owned_process_birth'] = transport.process_birth
        metadata['owned_process_kind'] = 'opencode-server'
        process_identity = transport.ownership_record()
        if isinstance(cleanup_binding, Mapping):
            process_identity['cleanup_binding'] = dict(cleanup_binding)
        metadata['owned_process_identity'] = process_identity
        if isinstance(cleanup_binding, Mapping):
            metadata['opencode_cleanup_binding'] = dict(cleanup_binding)
        persist()

        require_mcp_servers()

        try:
            model_ref = transport.require_model(
                cwd=worktree, model=resolved_model, variant=active_effort,
                deadline=deadline)
        except OpenCodeError as exc:
            if not activate_fallback(exc, metadata.get('pending_turn')):
                raise
            model_ref = transport.require_model(
                cwd=worktree, model=resolved_model, variant=active_effort,
                deadline=deadline)
        resolved_model = f"{model_ref['providerID']}/{model_ref['modelID']}"
        if resume and metadata.get('thread_id'):
            session = transport.get_session(metadata['thread_id'], cwd=worktree,
                                            deadline=deadline)
            session_directory = session.get('directory')
            if session_directory and Path(session_directory).resolve() != worktree:
                raise RuntimeError('native_goal_resume_session_workspace_mismatch')
            if not session.get('id') == metadata['thread_id']:
                raise RuntimeError('native_goal_resume_thread_missing')
        elif resume and metadata.get('session_creation_intent'):
            matching = [item for item in transport.sessions(cwd=worktree, deadline=deadline)
                        if item.get('title') == metadata['session_creation_intent']]
            if len(matching) > 1:
                raise RuntimeError('native_goal_resume_session_ambiguous')
            if matching:
                session = matching[0]
                metadata['thread_id'] = session['id']
                metadata.pop('session_creation_intent', None)
                persist()
            else:
                session = transport.create_session(
                    cwd=worktree, title=session_title, model=resolved_model,
                    variant=active_effort, deadline=deadline)
                metadata['thread_id'] = session['id']
                metadata.pop('session_creation_intent', None)
                persist()
        elif resume and (metadata.get('status') in {'starting', 'failed', 'cancelled', 'timed_out'}
                         and not metadata.get('turns') and not metadata.get('pending_turn')):
            # No creation intent means the old process stopped before it could
            # ask OpenCode to create a session. If intent exists, the preceding
            # branch reconciles it against GET /session first.
            session = transport.create_session(
                cwd=worktree, title=session_title, model=resolved_model,
                variant=active_effort, deadline=deadline)
            metadata['thread_id'] = session['id']
            metadata.pop('session_creation_intent', None)
            persist()
        elif resume:
            raise RuntimeError('native_goal_resume_thread_missing')
        else:
            metadata['session_creation_intent'] = session_title
            persist()
            session = transport.create_session(
                cwd=worktree, title=session_title, model=resolved_model,
                variant=active_effort, deadline=deadline)
            metadata['thread_id'] = session['id']
            metadata.pop('session_creation_intent', None)
            metadata['dialogue_phase'] = 'plan' if planning_prompt is not None else 'execute'
            persist()
        if not isinstance(metadata.get('thread_id'), str) or not metadata['thread_id'].startswith('ses'):
            raise RuntimeError('OpenCode returned an invalid persistent session ID')
        metadata['native_goal_status'] = 'active'
        metadata['native_goal'] = {
            'id': metadata['thread_id'], 'objective': objective,
            'status': 'active', 'tokensUsed': metadata.get('tokens_used', 0),
            'timeUsedSeconds': metadata.get('time_used_seconds', 0),
        }
        persist()

        recovery_prompt = None
        if resume:
            reconciled, recovery = reconcile_pending()
            if recovery == '__blocked__':
                return GoalRunResult('', 1, metadata)
            if recovery == '__resend_pending__':
                pending = metadata.get('pending_turn')
                if not isinstance(pending, Mapping):
                    raise RuntimeError('native_goal_pending_turn_missing')
                pending_prompt = pending.get('prompt')
                if not isinstance(pending_prompt, str):
                    raise RuntimeError('native_goal_pending_prompt_missing')
                result = send_turn(pending_prompt,
                                   phase=metadata.get('recovery_phase', 'execute'),
                                   message_id=pending['message_id'])
                metadata.pop('recovery_phase', None)
                if isinstance(result, Mapping) and result.get('blocked'):
                    return GoalRunResult('', 1, metadata)
                recovery_prompt = None
            else:
                recovery_prompt = recovery
            if (not reconciled and planning_prompt is not None
                    and metadata.get('dialogue_phase') == 'plan'
                    and not metadata.get('planning_turns')):
                recovery_prompt = planning_prompt
            elif (not reconciled and planning_prompt is not None
                  and metadata.get('dialogue_phase') == 'execute_pending'):
                metadata['dialogue_phase'] = 'execute'
                recovery_prompt = prompt
            elif (not reconciled and not metadata.get('pending_turn')
                  and not metadata.get('turns')):
                if planning_prompt is not None and metadata.get('dialogue_phase') == 'plan':
                    recovery_prompt = planning_prompt
                else:
                    recovery_prompt = prompt
            if (not reconciled and not recovery_prompt and metadata.get('turns')
                    and metadata.get('validation_records')
                    and metadata['validation_records'][-1]['result'].get('accepted') is False):
                failure = metadata['validation_records'][-1]['result']
                recovery_prompt = (
                    'Independent host verification rejected the candidate. Continue this same '
                    'persistent goal, resolve the reported failures, and preserve the original '
                    'assignment constraints.\nLatest host verification: '
                    + json.dumps(failure, ensure_ascii=False) + '\nOriginal assignment:\n' + prompt)
            if (not reconciled and not recovery_prompt and metadata.get('turns')
                    and not metadata.get('validation_records')):
                recovery_prompt = prompt

        if resume and prior_accepted and metadata.get('host_accepted'):
            return GoalRunResult(_public_stdout(metadata), 0, metadata)

        if recovery_prompt is not None:
            phase = metadata.pop('recovery_phase', None)
            if phase is None:
                phase = ('plan' if planning_prompt is not None
                         and recovery_prompt == planning_prompt else 'execute')
            result = send_turn(recovery_prompt, phase=phase)
        elif not resume and planning_prompt is not None:
            result = send_turn(planning_prompt, phase='plan')
        elif not resume:
            result = send_turn(prompt, phase='execute')
        else:
            result = None

        while True:
            if result is not None and isinstance(result, Mapping) and result.get('blocked'):
                break
            status = metadata.get('status')
            if status in {'accepted', 'completed_with_diagnostics', 'failed', 'blocked'}:
                break
            if not metadata.get('turns'):
                # A recovered planning response transitioned to execution.
                if planning_prompt is not None and metadata.get('dialogue_phase') == 'execute':
                    result = send_turn(prompt, phase='execute')
                    continue
                break
            if advisory:
                # Diagnostic-only workflows observe one candidate and continue
                # the SDK route without treating validation as an execution gate.
                break
            if downstream_toolcall:
                break
            validation = metadata.get('validation_records', [])
            if not validation or validation[-1].get('result', {}).get('accepted') is not False:
                break
            failure = validation[-1]['result']
            followup = (
                'Independent host verification rejected the candidate. Continue this same '
                'persistent goal, resolve the reported failures, and preserve every original '
                'assignment constraint.\nLatest host verification: '
                + json.dumps(failure, ensure_ascii=False) + '\nOriginal assignment:\n' + prompt)
            result = send_turn(followup, phase='execute')

    except (Exception, KeyboardInterrupt) as exc:
        metadata['status'] = 'cancelled' if isinstance(exc, KeyboardInterrupt) else 'failed'
        metadata['error'] = _redact_error_text(str(exc))
        if isinstance(exc, OpenCodeCleanupError):
            diagnostic = {key: exc.cleanup_diagnostic.get(key) for key in (
                'classification', 'returncode', 'target_pid', 'target_birth',
                'error_type', 'leader_exited', 'process_group_gone',
                'group_exit_wait_seconds', 'process_group_observation',
                'collection_errors', 'cleanup_reason', 'host_requested_signal',
                'process_group_id', 'session_id', 'target_pid_namespace',
                'target_start_ticks', 'target_argv', 'target_cwd',
                'target_executable', 'target_executable_sha256')
                if key in exc.cleanup_diagnostic}
            diagnostic['cleanup_confirmed'] = False
            attempt['process_diagnostic'] = diagnostic
            metadata.setdefault('process_diagnostics', []).append(diagnostic)
            argv = diagnostic.get('target_argv')
            if (type(diagnostic.get('target_pid')) is int
                    and isinstance(argv, list)
                    and isinstance(cleanup_binding, Mapping)):
                metadata['owned_process_identity'] = {
                    'pid': diagnostic.get('target_pid'),
                    'birth': diagnostic.get('target_birth'),
                    'start_ticks': diagnostic.get('target_start_ticks'),
                    'pid_namespace': diagnostic.get('target_pid_namespace'),
                    'process_group_id': diagnostic.get('process_group_id'),
                    'session_id': diagnostic.get('session_id'),
                    'cwd': diagnostic.get('target_cwd'), 'argv': argv,
                    'argv_sha256': sha256(json.dumps(
                        argv, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest(),
                    'executable': diagnostic.get('target_executable'),
                    'executable_sha256': diagnostic.get('target_executable_sha256'),
                    'cleanup_binding': dict(cleanup_binding),
                }
                metadata['opencode_cleanup_binding'] = dict(cleanup_binding)
            metadata['producer_stopped'] = False
            metadata['host_accepted'] = False
            metadata['cleanup_unconfirmed'] = True
            metadata['stop_reason'] = 'native_goal_producer_cleanup_unconfirmed'
            if command.options.get('host_collect_candidate'):
                metadata['candidate_capture'] = {
                    'status': 'not_captured',
                    'reason': 'opencode_process_tree_cleanup_unconfirmed',
                }
            audit('modport/process/diagnostic', diagnostic)
        if isinstance(exc, TimeoutError):
            metadata['status'] = 'timed_out'
            metadata['native_goal_status'] = 'budgetLimited'
            if transport is not None and metadata.get('thread_id'):
                try:
                    transport.abort_session(
                        metadata['thread_id'], cwd=worktree,
                        deadline=time.monotonic() + 2.0)
                except Exception:
                    pass
        elif isinstance(exc, KeyboardInterrupt):
            metadata['native_goal_status'] = 'paused'
            if transport is not None and metadata.get('thread_id'):
                try:
                    transport.abort_session(
                        metadata['thread_id'], cwd=worktree,
                        deadline=min(deadline, time.monotonic() + 2.0))
                except Exception:
                    pass
        elif metadata.get('native_goal_status') not in {'blocked', 'complete'}:
            metadata['native_goal_status'] = 'failed'
        persist()
    finally:
        if transport is not None:
            status = metadata.get('status')
            close_transport(
                deadline_exceeded=status == 'timed_out',
                cleanup_reason={
                    'timed_out': 'deadline_exceeded',
                    'cancelled': 'host_cancelled',
                    'accepted': 'goal_accepted',
                    'completed_with_diagnostics': 'goal_completed',
                    'blocked': 'goal_blocked',
                    'failed': 'goal_failed',
                }.get(status, 'goal_session_closed'),
            )
        if (command.options.get('host_collect_candidate')
                and not isinstance(metadata.get('candidate_capture'), Mapping)):
            metadata['candidate_capture'] = {
                'status': 'not_captured',
                'reason': ('opencode_process_tree_cleanup_unconfirmed'
                           if metadata.get('producer_stopped') is not True
                           else 'host_candidate_collection_did_not_complete'),
            }
        attempt['finished_at'] = time.time()
        attempt['status'] = metadata['status']
        persist()

    stdout = _public_stdout(metadata)
    observed = metadata.get('status') == 'completed_with_diagnostics'
    return GoalRunResult(stdout, 0 if metadata.get('host_accepted')
                         or advisory and observed else 1, metadata)
