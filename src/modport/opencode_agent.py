"""Host-facing OpenCode assignments, preserving ModPort's evidence interface."""
from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import copy_context
from hashlib import sha256
from pathlib import Path
import re
import subprocess
import threading
import time
from typing import Any, Mapping
import uuid

from .agent_dialogue import AgentDialogueError, _metadata, _planning_log, _safe_output_path
from .opencode_runtime import (
    OpenCodeConfig, OpenCodeEventConnectTimeout, OpenCodeEventStop, OpenCodeServer,
)
from .opencode_provider import managed_provider_config
from .opencode_shell_mcp import network_tools_disabled
from .telemetry import _SECRET_KEY, _secrets, public_last_message, redact


def require_model(server: OpenCodeServer, cwd: Path, model: str, variant: str,
                  deadline: float) -> str:
    """Reject disconnected or missing models and unsupported effort variants."""
    reference = server.require_model(cwd=cwd, model=model, variant=variant,
                                     deadline=deadline)
    return f"{reference['providerID']}/{reference['modelID']}"


def _tool_policy(server: OpenCodeServer, cwd: Path, *, read_only: bool,
                 deadline: float) -> dict[str, bool]:
    # The built-in shell inherits the model provider environment.  Project
    # commands must instead use a host-owned credential-free sandbox tool.
    disabled = {'bash', 'question'}
    if network_tools_disabled():
        disabled.update({'modport_sandbox_run_project_command', 'webfetch', 'websearch'})
    if read_only:
        disabled.update({'edit', 'write', 'apply_patch', 'patch', 'multiedit'})
    return {name: False for name in server.tool_ids(cwd=cwd, deadline=deadline)
            if name in disabled}


def _public_events(session_id: str, response: Mapping[str, Any],
                   transcript: list[Mapping[str, Any]] | None = None) -> str:
    """Audit all assistant steps, including tool calls before the final reply."""
    rows: list[dict[str, Any]] = [{'type': 'opencode.session', 'session_id': session_id}]
    assistant_messages = [item for item in (transcript or [])
                          if isinstance(item, Mapping) and isinstance(item.get('info'), Mapping)
                          and item['info'].get('role') == 'assistant']
    seen_ids = {item['info'].get('id') for item in assistant_messages}
    final_info = response.get('info')
    if (not assistant_messages or not isinstance(final_info, Mapping)
            or final_info.get('id') not in seen_ids):
        assistant_messages.append(response)
    private_values = set(_secrets())
    tool_secrets: set[str] = set()
    tool_secret_overflow = False

    def collect_sensitive_values(value: Any, *, sensitive: bool = False) -> None:
        nonlocal tool_secret_overflow
        if len(tool_secrets) >= 128:
            tool_secret_overflow = True
            return
        if isinstance(value, str):
            if sensitive and value:
                private_values.add(value)
                tool_secrets.add(value)
            elif not sensitive:
                try:
                    parsed = json.loads(value)
                except (TypeError, ValueError):
                    return
                if isinstance(parsed, (Mapping, list)):
                    collect_sensitive_values(parsed)
        elif isinstance(value, Mapping):
            for key, child in value.items():
                collect_sensitive_values(child, sensitive=sensitive or bool(_SECRET_KEY.search(str(key))))
        elif isinstance(value, list):
            for child in value:
                collect_sensitive_values(child, sensitive=sensitive)

    for message in assistant_messages:
        for part in message.get('parts', ()):
            if isinstance(part, Mapping) and part.get('type') == 'tool':
                state = part.get('state')
                if isinstance(state, Mapping):
                    collect_sensitive_values(state.get('output'))
                    collect_sensitive_values(state.get('error'))
    secrets = tuple(private_values)

    def public_text(value: str) -> str:
        if tool_secret_overflow:
            return '[REDACTED]'
        cleaned = redact(value, secrets)
        # The general string redactor skips very short values to avoid damaging
        # prose. A value explicitly returned under a secret key is private even
        # when it is short, so omit the message if it echoes that value.
        if any(secret in cleaned for secret in tool_secrets if len(secret) < 4):
            return '[REDACTED]'
        return cleaned

    for message in assistant_messages:
        info = message.get('info')
        if isinstance(info, Mapping):
            rows.append({'type': 'opencode.message', 'session_id': session_id,
                         'message_id': info.get('id'), 'provider_id': info.get('providerID'),
                         'model_id': info.get('modelID'), 'variant': info.get('variant'),
                         'tokens': info.get('tokens'), 'cost': info.get('cost')})
        for part in message.get('parts', ()):
            if not isinstance(part, Mapping):
                continue
            if part.get('type') == 'text' and isinstance(part.get('text'), str):
                rows.append({'type': 'item.completed', 'item': {
                    'type': 'agent_message', 'text': public_text(part['text'])}})
            elif part.get('type') == 'tool':
                tool = part.get('tool')
                state = part.get('state')
                if not isinstance(tool, str) or not isinstance(state, Mapping):
                    continue
                is_host_mcp = tool.startswith(('modport_rework_', 'modport_sandbox_'))
                item = {'type': 'mcp_tool_call' if is_host_mcp else 'tool_call',
                        'id': str(part.get('callID') or part.get('id') or ''),
                        'status': state.get('status'), 'tool': tool}
                if tool.startswith('modport_rework_'):
                    item.update(server='modport_rework', tool=tool.removeprefix('modport_rework_'))
                elif tool.startswith('modport_sandbox_'):
                    item.update(server='modport_sandbox', tool=tool.removeprefix('modport_sandbox_'))
                for key in ('output', 'error'):
                    value = state.get(key)
                    if isinstance(value, (Mapping, list)):
                        value = json.dumps(value, ensure_ascii=False)
                    if isinstance(value, str):
                        item[key + '_sha256'] = sha256(value.encode('utf-8')).hexdigest()
                        item[key + '_truncated'] = len(value) > 8192
                        if key == 'output' and tool in {
                                'modport_sandbox_read_run_artifact',
                                'modport_sandbox_run_project_command'}:
                            try:
                                read_result = json.loads(value)
                            except (TypeError, ValueError):
                                read_result = None
                            if isinstance(read_result, Mapping):
                                field = ('path' if tool == 'modport_sandbox_read_run_artifact'
                                         else 'artifact_path')
                                path = read_result.get(field)
                                if (isinstance(path, str) and path.startswith('artifacts/')
                                        and '..' not in Path(path).parts):
                                    item['artifact_path'] = path
                                if tool == 'modport_sandbox_read_run_artifact':
                                    for field in ('chunk_sha256', 'verified_sha256'):
                                        digest = read_result.get(field)
                                        if (isinstance(digest, str)
                                                and re.fullmatch(r'[0-9a-f]{64}', digest)):
                                            item[field] = digest
                                    for field in ('offset', 'next_offset', 'total_bytes'):
                                        number = read_result.get(field)
                                        if type(number) is int and number >= 0:
                                            item[field] = number
                status = state.get('status')
                event_type = ('item.completed' if status == 'completed' else
                              'item.failed' if status in {'error', 'failed', 'cancelled'} else
                              'item.started')
                rows.append({'type': event_type, 'item': item})
        if isinstance(info, Mapping) and 'structured' in info and info['structured'] is not None:
            structured_text = public_text(json.dumps(
                redact(info['structured'], secrets), ensure_ascii=False))
            rows.append({'type': 'item.completed', 'item': {
                'type': 'agent_message', 'text': structured_text}})
    return '\n'.join(json.dumps(row, ensure_ascii=False, separators=(',', ':')) for row in rows) + '\n'


def _save_turn(log: Path, prompt: str, session_id: str, response: Mapping[str, Any],
               transcript: list[Mapping[str, Any]] | None = None) -> subprocess.CompletedProcess[str]:
    log.parent.mkdir(parents=True, exist_ok=True)
    Path(str(log) + '.stdin.txt').write_text(prompt, encoding='utf-8')
    stdout = _public_events(session_id, response, transcript)
    info = response.get('info')
    info = info if isinstance(info, Mapping) else {}
    message_id = info.get('id') if isinstance(info.get('id'), str) else None
    content_hash = sha256()
    response_bytes = 0
    digest_complete = True
    for part in response.get('parts', ()):
        if not isinstance(part, Mapping) or part.get('type') != 'text':
            continue
        text = part.get('text')
        if not isinstance(text, str):
            continue
        for offset in range(0, len(text), 64 * 1024):
            chunk = text[offset:offset + 64 * 1024].encode('utf-8')
            response_bytes += len(chunk)
            if response_bytes <= 8 * 1024 * 1024:
                content_hash.update(chunk)
            else:
                digest_complete = False
    model_result = {
        'schema': 'modport.opencode-model-result.v1',
        'session_id': session_id, 'message_id': message_id,
        'terminal_status': 'completed', 'response_observed': message_id is not None,
        'provider_id': info.get('providerID') if isinstance(info.get('providerID'), str) else None,
        'model_id': info.get('modelID') if isinstance(info.get('modelID'), str) else None,
        'variant': info.get('variant') if isinstance(info.get('variant'), str) else None,
        'response_bytes': response_bytes,
        'response_sha256': content_hash.hexdigest() if digest_complete else None,
        'response_digest_status': 'captured' if digest_complete else 'exceeds_8_mib_limit',
    }
    stdout += json.dumps({'type': 'opencode.model_result', 'result': model_result},
                         ensure_ascii=False, separators=(',', ':')) + '\n'
    log.write_text(stdout, encoding='utf-8')
    completed = subprocess.CompletedProcess(['opencode', 'serve', session_id], 0, stdout, '')
    completed.model_result = model_result
    return completed


class _TurnEventReader:
    """Read one session's live events while a single turn is running."""

    def __init__(self, server: OpenCodeServer, *, cwd: Path, session_id: str,
                 message_id: str, deadline: float):
        self.server = server
        self.cwd = cwd
        self.session_id = session_id
        self.message_id = message_id
        self.deadline = deadline
        self._stop = OpenCodeEventStop()
        self._ready = threading.Event()
        captured = copy_context()
        self._thread = threading.Thread(target=captured.run, args=(self._read,), name='modport-opencode-events',
                                        daemon=True)
        self._lock = threading.Lock()
        self._seen_event_ids: set[str] = set()
        self._turn_started = False
        self._assistant_ids: set[str] = set()
        self._assistant_order: list[str] = []
        self._part_message_ids: set[str] = set()
        self._completed_assistant_ids: set[str] = set()
        self._idle = False
        self._stream_active = False
        self._turn_posted = False
        self._expected_final_id: str | None = None
        self._final_complete_seen = False
        self._turn_gap = False
        self._gap_completed_ids: list[frozenset[str]] = []
        self._reconnect_count = 0
        self._startup_error: BaseException | None = None
        self._stream_errors: list[BaseException] = []

    def start(self) -> None:
        self._thread.start()
        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or not self._ready.wait(remaining):
            raise TimeoutError('OpenCode event stream did not become ready before deadline')
        if self._startup_error is not None:
            raise self._startup_error

    def _read(self) -> None:
        while not self._stop.is_set() and time.monotonic() < self.deadline:
            try:
                for event in self.server.events(cwd=self.cwd, deadline=self.deadline,
                                                stop_event=self._stop):
                    if self._stop.is_set():
                        return
                    if not isinstance(event, Mapping):
                        continue
                    event_id = event.get('id')
                    if isinstance(event_id, str):
                        with self._lock:
                            if event_id in self._seen_event_ids:
                                continue
                            self._seen_event_ids.add(event_id)
                    if event.get('type') == 'server.connected':
                        with self._lock:
                            self._stream_active = True
                        self._ready.set()
                        continue
                    properties = event.get('properties')
                    if not isinstance(properties, Mapping) or properties.get('sessionID') != self.session_id:
                        continue
                    event_type = event.get('type')
                    with self._lock:
                        if event_type == 'message.updated':
                            info = properties.get('info')
                            if not isinstance(info, Mapping):
                                continue
                            if info.get('role') == 'user' and info.get('id') == self.message_id:
                                self._turn_started = True
                            elif self._turn_started and info.get('role') == 'assistant':
                                assistant_id = info.get('id')
                                if isinstance(assistant_id, str) and assistant_id.startswith('msg'):
                                    if assistant_id not in self._assistant_ids:
                                        self._assistant_order.append(assistant_id)
                                    self._assistant_ids.add(assistant_id)
                                    message_time = info.get('time')
                                    if (isinstance(message_time, Mapping)
                                            and type(message_time.get('completed')) is int):
                                        self._completed_assistant_ids.add(assistant_id)
                                        if assistant_id == self._expected_final_id:
                                            self._final_complete_seen = True
                        elif event_type in {'message.part.updated', 'message.part.delta'}:
                            if not self._turn_started:
                                continue
                            part = properties.get('part')
                            part_message_id = (part.get('messageID') if isinstance(part, Mapping)
                                               else properties.get('messageID'))
                            if isinstance(part_message_id, str) and part_message_id in self._assistant_ids:
                                self._part_message_ids.add(part_message_id)
                        elif self._turn_started and event_type == 'session.idle':
                            self._idle = True
                        elif self._turn_started and event_type == 'session.status':
                            status = properties.get('status')
                            if isinstance(status, Mapping) and status.get('type') == 'idle':
                                self._idle = True
            except Exception as exc:
                if self._stop.is_set():
                    return
                with self._lock:
                    self._stream_active = False
                    if self._final_complete_seen or self._idle:
                        return
                    self._stream_errors.append(exc)
                    self._record_reconnect_locked()
                    if (not self._ready.is_set()
                            and not isinstance(exc, OpenCodeEventConnectTimeout)):
                        self._startup_error = exc
                        self._ready.set()
                        return
                if isinstance(exc, TimeoutError) or time.monotonic() >= self.deadline:
                    return
                self._stop.wait(min(0.1, max(0, self.deadline - time.monotonic())))
            else:
                if self._stop.is_set():
                    return
                with self._lock:
                    self._stream_active = False
                    if self._final_complete_seen or self._idle:
                        return
                    self._record_reconnect_locked()
                self._stop.wait(min(0.05, max(0, self.deadline - time.monotonic())))

    def _record_reconnect_locked(self) -> None:
        # A connection attempt before the first ready event is not a broken
        # subscription and must not prevent posting after a successful retry.
        if not self._ready.is_set():
            return
        self._reconnect_count += 1
        if (self._turn_posted and not self._final_complete_seen and not self._idle):
            self._turn_gap = True
            self._gap_completed_ids.append(frozenset(self._completed_assistant_ids))

    def mark_turn_posted(self) -> None:
        with self._lock:
            if not self._stream_active or self._reconnect_count:
                raise RuntimeError(
                    'OpenCode SSE subscription was interrupted before this turn was posted')
            self._turn_posted = True

    def expect_final(self, message_id: str) -> None:
        with self._lock:
            self._expected_final_id = message_id
            if message_id in self._completed_assistant_ids:
                self._final_complete_seen = True
                if all(message_id in completed for completed in self._gap_completed_ids):
                    self._turn_gap = False

    @property
    def turn_gap(self) -> bool:
        with self._lock:
            return self._turn_gap

    @property
    def reconnect_count(self) -> int:
        with self._lock:
            return self._reconnect_count

    @property
    def idle(self) -> bool:
        with self._lock:
            return self._idle

    @property
    def assistant_message_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._assistant_order)

    @property
    def part_message_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._part_message_ids))

    def wait_for_final(self, message_id: str) -> None:
        while time.monotonic() < self.deadline:
            with self._lock:
                if message_id in self._completed_assistant_ids or self._idle:
                    return
                if self._turn_gap:
                    raise RuntimeError(
                        'OpenCode SSE disconnected before the current assistant message completed')
            time.sleep(min(0.01, max(0, self.deadline - time.monotonic())))
        raise TimeoutError(
            'OpenCode SSE did not confirm current assistant completion or session idle before deadline')

    def close(self) -> None:
        self._stop.set()
        if self._thread.ident is not None:
            self._thread.join(timeout=3.0)
        if self._thread.is_alive():
            raise RuntimeError('OpenCode event reader did not stop within its bounded close interval')


@contextmanager
def observe_turn_usage(server: OpenCodeServer, *, cwd: Path, session_id: str,
                       message_id: str, deadline: float):
    """Observe native goal usage and child sessions during the blocking POST."""
    reader = _TurnEventReader(server, cwd=cwd, session_id=session_id,
                              message_id=message_id, deadline=deadline)
    try:
        reader.start()
        reader.mark_turn_posted()
        yield
    except BaseException:
        try:
            reader.close()
        except Exception:
            pass  # Preserve the original provider/budget/identity failure.
        raise
    else:
        reader.close()


def _turn_transcript(server: OpenCodeServer, *, response: Mapping[str, Any],
                     session_id: str, user_message_id: str, cwd: Path,
                     deadline: float, observed_ids: tuple[str, ...] = (),
                     observed_part_ids: tuple[str, ...] = ()
                     ) -> tuple[list[Mapping[str, Any]], Mapping[str, Any]]:
    """Fetch each live assistant step and verify it belongs to this user turn."""
    response_info = response.get('info')
    if not isinstance(response_info, Mapping):
        raise RuntimeError('OpenCode response omitted assistant message identity')
    final_id = response_info.get('id')
    if not isinstance(final_id, str) or not final_id.startswith('msg'):
        raise RuntimeError('OpenCode response returned an invalid assistant message ID')
    if response_info.get('role') != 'assistant':
        raise RuntimeError('OpenCode response did not identify an assistant message')
    if response_info.get('sessionID') not in (None, session_id):
        raise RuntimeError('OpenCode response belongs to a different session')

    ordered_ids = list(dict.fromkeys((*observed_ids, *observed_part_ids, final_id)))
    messages: dict[str, Mapping[str, Any]] = {}
    for seed_id in ordered_ids:
        current_id = seed_id
        visited: set[str] = set()
        for _ in range(64):
            if current_id == user_message_id:
                break
            if current_id in visited:
                raise RuntimeError('OpenCode assistant message parent chain contains a cycle')
            visited.add(current_id)
            message = messages.get(current_id)
            if message is None:
                message = server.get_message(session_id, current_id, cwd=cwd,
                                             deadline=deadline)
                info = message.get('info')
                if not isinstance(info, Mapping):
                    raise RuntimeError('OpenCode single-message response omitted message metadata')
                if info.get('id') != current_id or info.get('sessionID') != session_id:
                    raise RuntimeError('OpenCode single-message response identity did not match request')
                if info.get('role') != 'assistant':
                    raise RuntimeError('OpenCode assistant parent chain contains an unexpected role')
                message_time = info.get('time')
                if (not isinstance(message_time, Mapping)
                        or type(message_time.get('completed')) is not int
                        or message_time['completed'] < 0):
                    raise RuntimeError('OpenCode assistant step has no completion timestamp')
                parts = message.get('parts')
                if not isinstance(parts, list):
                    raise RuntimeError('OpenCode single-message response omitted assistant parts')
                messages[current_id] = message
            parent_id = message['info'].get('parentID')
            if parent_id == user_message_id:
                break
            if not isinstance(parent_id, str) or not parent_id.startswith('msg'):
                raise RuntimeError('OpenCode assistant message has no valid parent ID')
            current_id = parent_id
        else:
            raise RuntimeError('OpenCode assistant message parent chain exceeded 64 messages')

    if final_id not in messages:
        raise RuntimeError('OpenCode parent chain did not contain its returned assistant message')
    observed_index = {message_id: index for index, message_id in enumerate(ordered_ids)}

    def message_order(message_id: str) -> tuple[int, int, int, str]:
        depth = 0
        current_id = message_id
        visited: set[str] = set()
        while current_id != user_message_id:
            if current_id in visited:
                raise RuntimeError('OpenCode assistant message parent chain contains a cycle')
            visited.add(current_id)
            parent_id = messages[current_id]['info'].get('parentID')
            if parent_id == user_message_id:
                depth += 1
                break
            if parent_id not in messages:
                raise RuntimeError('OpenCode assistant transcript is missing a parent message')
            current_id = parent_id
            depth += 1
        created = messages[message_id]['info'].get('time', {}).get('created')
        created_key = created if type(created) is int else 0
        return depth, created_key, observed_index.get(message_id, len(ordered_ids)), message_id

    transcript = sorted(messages.values(),
                        key=lambda message: message_order(message['info']['id']))
    final_message = transcript[-1]
    if final_message.get('info', {}).get('id') != final_id:
        # The POST response identity is the authoritative last message even if
        # the provider gave its intermediate step the same creation timestamp.
        transcript.remove(messages[final_id])
        transcript.append(messages[final_id])
        final_message = transcript[-1]
    return transcript, final_message


def _send_turn(server: OpenCodeServer, *, session_id: str, prompt: str, cwd: Path,
               model: str, variant: str, tools: Mapping[str, bool], deadline: float,
               output_format: Mapping[str, Any] | None = None
               ) -> tuple[Mapping[str, Any], list[Mapping[str, Any]]]:
    user_message_id = 'msg' + uuid.uuid4().hex
    reader = _TurnEventReader(server, cwd=cwd, session_id=session_id,
                              message_id=user_message_id, deadline=deadline)
    try:
        reader.start()
        reader.mark_turn_posted()
        response = server.send_message(session_id, prompt, model=model, variant=variant,
                                       tools=tools, output_format=output_format,
                                       message_id=user_message_id, deadline=deadline)
        response_info = response.get('info')
        final_id = response_info.get('id') if isinstance(response_info, Mapping) else None
        if isinstance(final_id, str):
            reader.expect_final(final_id)
            reader.wait_for_final(final_id)
    except BaseException:
        try:
            reader.close()
        except Exception:
            pass
        raise
    else:
        reader.close()
    if reader.turn_gap:
        raise RuntimeError(
            'OpenCode SSE disconnected before this turn had an observed completed assistant '
            f'message or idle event; reconnects={reader.reconnect_count}, '
            f'observed_assistant_messages={len(reader.assistant_message_ids)}')
    transcript, final_message = _turn_transcript(
        server, response=response, session_id=session_id, user_message_id=user_message_id,
        cwd=cwd, deadline=deadline, observed_ids=reader.assistant_message_ids,
        observed_part_ids=reader.part_message_ids)
    final_info = final_message.get('info')
    time_info = final_info.get('time') if isinstance(final_info, Mapping) else None
    completed = (isinstance(time_info, Mapping)
                 and type(time_info.get('completed')) is int
                 and time_info['completed'] >= 0)
    if not completed and not reader.idle:
        raise RuntimeError('OpenCode turn returned without assistant completion or session idle')
    return final_message, transcript


def run_agent(*, prompt: str, cwd: Path, log: Path, model: str, variant: str,
              timeout: float, read_only: bool = False, mcp: Mapping[str, Mapping] | None = None,
              planning_prompt: str | None = None, plan_path: Path | None = None,
              schema_path: Path | None = None, no_tools: bool = False,
              run_root: Path | None = None, command_id: str | None = None,
              session_context_budget: Mapping[str, Any] | None = None,
              auto_context_budget: bool = False,
              token_budget_root: Path | None = None,
              allow_project_commands: bool = True,
              model_policy: Mapping | None = None) -> subprocess.CompletedProcess[str]:
    """Execute one or two turns in the same real OpenCode session."""
    workspace = Path(cwd).absolute()
    if not workspace.is_dir() or workspace.is_symlink() or workspace.resolve() != workspace:
        raise ValueError('OpenCode assignment cwd must be a real directory')
    execution_log = _safe_output_path(log, workspace, 'OpenCode execution log')
    deadline = time.monotonic() + float(timeout)
    if no_tools and (mcp or run_root is not None):
        raise ValueError('tool-free OpenCode assignment cannot register MCP tools')
    mcp_config = dict(mcp or {})
    if run_root is not None:
        if not command_id:
            raise ValueError('OpenCode sandbox tool requires command_id')
        from .opencode_shell_mcp import prepare_sandbox_tool
        mcp_config.update(prepare_sandbox_tool(run_root, workspace, command_id, timeout,
                                               read_only=read_only,
                                               allow_project_commands=allow_project_commands))
    from .opencode_subagents import TASK_PERMISSION, subagent_profiles
    permissions = {'bash': 'deny', 'task': 'deny' if no_tools else TASK_PERMISSION,
                   'question': 'deny',
                   'doom_loop': 'deny', 'external_directory': 'deny'}
    from .workspace import workspace_spec
    if run_root is not None and workspace_spec(run_root):
        from .local_workspace_sandbox import workspace_sensitive_permissions
        permissions.update(workspace_sensitive_permissions(workspace))
    if network_tools_disabled():
        permissions.update({name: 'deny' for name in (
            'modport_sandbox_run_project_command', 'webfetch', 'websearch')})
    if not allow_project_commands:
        permissions['modport_sandbox_run_project_command'] = 'deny'
    if read_only or no_tools:
        permissions['edit'] = 'deny'
    profiles = ({} if no_tools else subagent_profiles(
        model_policy=model_policy, model=model, variant=variant, permissions=permissions))
    with OpenCodeServer.start(cwd=workspace,
                              config=OpenCodeConfig(mcp=mcp_config, permission=permissions,
                                                    agent=profiles,
                                                    provider=managed_provider_config()),
                              xdg_root=execution_log.with_suffix('.opencode-state'),
                              deadline=deadline) as server:
        accounting_root = token_budget_root if token_budget_root is not None else run_root
        if accounting_root is not None:
            from .token_budget import bind_token_budget
            bind_token_budget(server, accounting_root)
        if mcp_config:
            status = server.mcp_status(cwd=workspace, deadline=deadline)
            disconnected = [name for name in mcp_config
                            if not isinstance(status.get(name), Mapping)
                            or status[name].get('status') != 'connected']
            if disconnected:
                raise RuntimeError('OpenCode MCP tools are unavailable: ' + ', '.join(disconnected))
        selected_model = require_model(server, workspace, model, variant, deadline)
        budget_source = 'host_compression_metadata'
        if planning_prompt is not None and auto_context_budget and session_context_budget is None:
            from .prompt_compressor import load_model_profile
            profile = load_model_profile(
                selected_model, catalog=server.providers(cwd=workspace, deadline=deadline))
            session_context_budget = {
                'context_window': profile.context_window,
                'input_token_budget': profile.input_tokens,
                'output_token_reserve': profile.output_tokens,
                'tool_token_reserve': profile.tool_reserve_tokens,
            }
            budget_source = 'OpenCode provider catalog'
        session = server.create_session(cwd=workspace, title='ModPort assignment',
                                        model=selected_model, variant=variant,
                                        deadline=deadline)
        session_id = session.get('id')
        if not isinstance(session_id, str) or not session_id.startswith('ses'):
            raise RuntimeError('OpenCode did not return a durable session ID')
        tools = ({name: False for name in server.tool_ids(cwd=workspace, deadline=deadline)}
                 if no_tools else _tool_policy(server, workspace, read_only=read_only,
                                               deadline=deadline))
        if not allow_project_commands:
            tools['modport_sandbox_run_project_command'] = False
        if planning_prompt is None:
            response, transcript = _send_turn(server, session_id=session_id, prompt=prompt,
                                              cwd=workspace, model=selected_model,
                                              variant=variant, tools=tools, deadline=deadline)
            return _save_turn(execution_log, prompt, session_id, response, transcript)

        if plan_path is None:
            raise ValueError('two-turn OpenCode assignment requires plan_path')
        planning_log = _safe_output_path(_planning_log(execution_log), workspace, 'OpenCode planning log')
        saved_plan = _safe_output_path(plan_path, workspace, 'OpenCode plan')
        if len({planning_log, execution_log, saved_plan}) != 3:
            raise ValueError('OpenCode plan and logs must be distinct')
        from .evidence import atomic_json
        initial = _metadata(turns=0, thread_id=session_id, planning_log=planning_log,
                            execution_log=execution_log, plan_path=saved_plan,
                            planning_prompt=planning_prompt, execution_prompt=prompt,
                            schema_path=str(schema_path) if schema_path else None)
        atomic_json(saved_plan.parent / 'session.json', initial.to_dict())
        try:
            plan_tools = {**tools,
                          'task': False,
                          'modport_rework_request_rework': False,
                          'modport_rework_list_rework_targets': False,
                          'modport_sandbox_run_project_command': False,
                          'edit': False, 'write': False, 'apply_patch': False,
                          'patch': False, 'multiedit': False}
            planning, planning_messages = _send_turn(
                server, session_id=session_id, prompt=planning_prompt, cwd=workspace,
                model=selected_model, variant=variant, tools=plan_tools, deadline=deadline)
        except BaseException as exc:
            setattr(exc, 'metadata', initial.to_dict())
            raise
        first = _save_turn(planning_log, planning_prompt, session_id, planning, planning_messages)
        plan_text = public_last_message(first.stdout)
        initial = _metadata(turns=1, thread_id=session_id, planning_log=planning_log,
                            execution_log=execution_log, plan_path=saved_plan,
                            planning_prompt=planning_prompt, execution_prompt=prompt,
                            schema_path=str(schema_path) if schema_path else None)
        atomic_json(saved_plan.parent / 'session.json', initial.to_dict())
        if not plan_text.strip() or plan_text.startswith('No final agent message was reported;'):
            raise AgentDialogueError('planning_message_missing',
                                     'OpenCode planning turn returned no public plan', initial,
                                     completed=first)
        saved_plan = _safe_output_path(saved_plan, workspace, 'OpenCode plan')
        saved_plan.write_text(plan_text + '\n', encoding='utf-8')
        output_format = None
        if schema_path is not None:
            schema = json.loads(Path(schema_path).read_text(encoding='utf-8'))
            output_format = {'type': 'json_schema', 'schema': schema}
        if session_context_budget is not None:
            from .session_context_budget import (SessionContextBudgetError,
                prior_context_usage, require_next_turn_capacity)
            info = planning.get('info')
            tokens = info.get('tokens') if isinstance(info, Mapping) else None
            usage = prior_context_usage(tokens=tokens, prior_prompt=planning_prompt,
                                        response=planning_messages)
            preflight_path = saved_plan.parent / 'session-context-preflight.json'
            try:
                capacity = require_next_turn_capacity(
                    session_context_budget, usage, prompt, output_format)
            except SessionContextBudgetError as exc:
                atomic_json(preflight_path, {'status': 'exceeded', 'model': selected_model,
                            'profile_source': budget_source, 'budget': dict(session_context_budget),
                            'prior_usage': usage, 'detail': str(exc)})
                raise AgentDialogueError('session_context_budget_exceeded', str(exc),
                                         initial, completed=first) from exc
            atomic_json(preflight_path, {'status': 'passed', 'model': selected_model,
                        'profile_source': budget_source, 'budget': dict(session_context_budget),
                        'capacity': capacity})
        try:
            response, execution_messages = _send_turn(
                server, session_id=session_id, prompt=prompt, cwd=workspace,
                model=selected_model, variant=variant, tools=tools, deadline=deadline,
                output_format=output_format)
        except BaseException as exc:
            setattr(exc, 'metadata', initial.to_dict())
            raise
        second = _save_turn(execution_log, prompt, session_id, response, execution_messages)
        metadata = _metadata(turns=2, thread_id=session_id, planning_log=planning_log,
                             execution_log=execution_log, plan_path=saved_plan,
                             planning_prompt=planning_prompt, execution_prompt=prompt,
                             schema_path=str(schema_path) if schema_path else None)
        second.dialogue_metadata = metadata.to_dict()
        return second
