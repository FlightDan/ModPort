"""Bounded watchdog investigation through the normal SDK agent assignment."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
import json
import os
from pathlib import Path
import stat
from typing import Any, Callable

from .contracts import OperationInput, OperationResult
from .platform_files import safe_open


WATCHDOG_SUPERVISOR_PROMPT = """Investigate the host watchdog incident and choose
the next action for this migration under its original deadline and cumulative
assignment and token budget. Inspect raw errors, bounded original inputs, tool
requests and responses, process/SDK observations and source evidence to identify
the root cause. Separate confirmed causes from hypotheses. An exit code, timeout,
quiet output, heartbeat or live process alone is not a cause or useful progress.
Missing evidence remains unknown. Use modport_sandbox_read_run_artifact for
host-supplied evidence_refs outside your workspace; read bounded slices and follow
next_offset. Copy supplied provenance; do not add hashes or identity comparisons.

Investigate active source and evidence read-only. When the host supplies an
isolated diagnostic repair manifest, correct confirmed small source errors only
in those copies and preserve their boundaries. Never overwrite a running author,
frozen input, source contract, delivered artifact, credential or .modport evidence.
Do not run project code. Broader upstream work requires the explicit request_rework
tool when the host supplies eligible targets; requesting it in prose is not a
dispatch. Inspect returned failures and actual downstream consumption. Explain
what the host must repair and freshly verify; diagnostic publication alone does
not prove that a repair worked. The host controls integration, verification,
cancellation, settlement and resumption.

Choose continue when useful work can proceed without intervention; repair_resume
when a concrete repair or diagnostic instruction can restore execution; wait for
a known task or an explicitly explained external prerequisite. Stopping one stuck
execution does not abandon the Run: for repair_resume the host cancels and settles
that exact attempt before a fresh attempt under unchanged deadline and usage.
Choose stop only for budget exhaustion or a conclusively unrecoverable cause.
An unsuccessful verification, missing report or invalid interface is unfinished
work, not proof that recovery is impossible. Diagnose its producer and consumer
and use the available repair or prerequisite-wait route. The host also sends
failed tasks and proposed Run failures here when the independent liveness
watchdog is paused; the original deadline and cumulative usage still apply.
For a settled task_failure, continue may explicitly retain an advisory failure
and let the existing consumer advance. It never changes that result to passed
or waives required inputs, integration, target execution or final acceptance.
Inconclusive observations do not establish unrecoverability. The host confirms
budget exhaustion and rejects stale decisions. Neither action extends any budget.

Return one JSON object with exactly action, reason, instruction, wait_for and
stop_category. The host binds your response to its incident and execution; do
not return incident_id or execution identity. action is
continue, repair_resume, wait or stop. reason explains causal evidence. instruction
is concrete and nonempty for repair_resume; when wait_for is empty for wait it
must explicitly describe the external prerequisite. Otherwise instruction may be
null. wait_for lists only known_task_ids and is empty except for wait. stop_category
is null except for stop, when it is budget_exhausted or unrecoverable. Supply no
Run identity, hash or fingerprint fields.
"""


def _text(value: Any, name: str, *, limit: int = 16_000) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or '\x00' in value):
        raise ValueError(f'{name} must be nonempty text of at most {limit} characters')
    return value


def watchdog_incident_request(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError('watchdog_incident must be a host request object')
    for name in ('incident_id', 'kind', 'reason'):
        _text(value.get(name), name, limit=512 if name != 'reason' else 16_000)
    for name in ('target_task_id', 'target_execution_id'):
        if value.get(name) is not None:
            _text(value[name], name, limit=512)
    known = value.get('known_task_ids')
    if not isinstance(known, list) or len(known) > 4096:
        raise ValueError('known_task_ids must be a bounded list')
    for task_id in known:
        _text(task_id, 'known task ID', limit=512)
    if len(set(known)) != len(known):
        raise ValueError('known_task_ids must be unique')
    if not isinstance(value.get('budget_context'), Mapping):
        raise ValueError('budget_context must contain host budget observations')
    return json.loads(json.dumps(dict(value), ensure_ascii=False, allow_nan=False))


def watchdog_supervisor_schema() -> dict[str, Any]:
    properties = {
        'action': {'type': 'string', 'enum': ['continue', 'repair_resume', 'wait', 'stop']},
        'reason': {'type': 'string', 'minLength': 1, 'maxLength': 16_000},
        'instruction': {'type': ['string', 'null'], 'minLength': 1, 'maxLength': 32_000},
        'wait_for': {'type': 'array', 'items': {'type': 'string', 'minLength': 1,
                                             'maxLength': 512},
                     'uniqueItems': True, 'maxItems': 4096},
        'stop_category': {'type': ['string', 'null'],
                          'enum': [None, 'budget_exhausted', 'unrecoverable']},
    }
    return {'type': 'object', 'properties': properties,
            'required': list(properties), 'additionalProperties': False}


def validate_watchdog_decision(document: Any, request: Mapping[str, Any]) -> dict[str, Any]:
    expected = watchdog_incident_request(request)
    fields = set(watchdog_supervisor_schema()['required']) | {'incident_id'}
    if not isinstance(document, Mapping) or set(document) != fields:
        raise ValueError('watchdog decision must contain exactly the six protocol fields')
    if document['incident_id'] != expected['incident_id']:
        raise ValueError('watchdog decision incident_id does not match the host request')
    action = document['action']
    if action not in ('continue', 'repair_resume', 'wait', 'stop'):
        raise ValueError('watchdog action is invalid')
    _text(document['reason'], 'reason')
    instruction = document['instruction']
    if instruction is not None:
        _text(instruction, 'instruction', limit=32_000)
    waits = document['wait_for']
    if not isinstance(waits, list) or len(waits) > 4096:
        raise ValueError('watchdog wait_for must be a bounded list')
    for task_id in waits:
        _text(task_id, 'wait_for task ID', limit=512)
    if len(set(waits)) != len(waits) or any(task_id not in expected['known_task_ids'] for task_id in waits):
        raise ValueError('watchdog wait_for must contain unique known task IDs')
    if action != 'wait' and waits:
        raise ValueError('watchdog wait_for is only valid for wait')
    if action == 'repair_resume' or action == 'wait' and not waits:
        _text(instruction, 'repair or external prerequisite instruction', limit=32_000)
    if action == 'stop':
        if document['stop_category'] not in ('budget_exhausted', 'unrecoverable'):
            raise ValueError('watchdog stop requires budget_exhausted or unrecoverable')
    elif document['stop_category'] is not None:
        raise ValueError('watchdog stop_category must be null unless stopping')
    return json.loads(json.dumps(dict(document), ensure_ascii=False, allow_nan=False))


def bind_watchdog_report(document: Any, request: Mapping[str, Any]) -> dict[str, Any]:
    """Bind model action fields to the request of this exact host invocation."""
    if not isinstance(document, Mapping) or set(document) != set(watchdog_supervisor_schema()['required']):
        raise ValueError('watchdog report must contain exactly action, reason, instruction, wait_for and stop_category')
    expected = watchdog_incident_request(request)
    return validate_watchdog_decision({**document, 'incident_id': expected['incident_id']}, expected)


def _raw_report(root: Path, outputs: Mapping[str, Any]) -> tuple[str, bool]:
    relative = outputs.get('last_message')
    if not isinstance(relative, str) or not relative:
        return '', False
    path = Path(relative)
    path = path if path.is_absolute() else root / path
    if path.resolve() != path.absolute() or not path.resolve().is_relative_to(root):
        raise ValueError('watchdog supervisor report path leaves the Run evidence directory')
    descriptor = safe_open(root, path.relative_to(root).as_posix(), os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError('watchdog supervisor report must be a regular file')
        data = source.read(1024 * 1024 + 1)
    return data[:1024 * 1024].decode('utf-8', errors='replace'), len(data) > 1024 * 1024


def invoke_watchdog_supervisor(
    command: OperationInput,
    stage_handler: Callable[[str], Callable[[OperationInput], OperationResult]],
) -> OperationResult:
    """Preserve malformed/failed reports without giving them recovery authority."""
    from . import diagnostic_repairs
    root = Path(command.run_dir).resolve()
    diagnostics = []
    preparation_diagnostic = None
    if diagnostic_repairs.enabled(command) and command.payload.get('diagnostic_repair_targets'):
        try:
            command = diagnostic_repairs.prepare(command)
        except (OSError, ValueError, TypeError, KeyError) as error:
            preparation_diagnostic = str(error)
            command = replace(command, payload={key: value for key, value in command.payload.items()
                                                if key != 'diagnostic_repair_manifest'})
    directory = root / 'artifacts' / 'executions' / command.command_id / 'watchdog-supervision'
    try:
        if (directory.resolve() != directory.absolute()
                or not directory.resolve().is_relative_to(root)):
            raise ValueError('unsafe watchdog supervisor archive path')
        directory.mkdir(parents=True, exist_ok=True)
        from .kernel_runtime import operation_workspace
        workspace = operation_workspace(root, command)
        if workspace is not None:
            workspace.mkdir(parents=True, exist_ok=True)
        request = watchdog_incident_request(command.payload.get('watchdog_incident'))
        packet = {'watchdog_incident': request,
                  'artifact_refs': dict(list(command.artifact_refs.items())[:32]),
                  'review_rework_targets': command.payload.get('review_rework_targets', [])}
        input_path = directory / 'input.json'
        if input_path.is_symlink():
            raise ValueError('unsafe watchdog supervisor input archive')
        input_path.write_text(json.dumps(packet, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    except (OSError, TypeError, ValueError) as error:
        return OperationResult('failed', command.run_id, command.task_id, command.stage_id,
                               command.command_id, outputs={'watchdog_decision': None},
                               detail=str(error), error_code='watchdog_supervision_input_invalid')
    prompt = WATCHDOG_SUPERVISOR_PROMPT + '\nHost incident and supplied references: ' + json.dumps(packet, ensure_ascii=False, sort_keys=True)
    if command.payload.get('diagnostic_repair_manifest'):
        prompt += '\n' + diagnostic_repairs.instructions(command)
    elif preparation_diagnostic is not None:
        prompt += '\nOptional repair copy is unavailable: ' + preparation_diagnostic + '. Investigate read-only.'
    result = stage_handler(prompt)(command)
    if command.payload.get('diagnostic_repair_manifest'):
        result = diagnostic_repairs.collect(command, result)
    outputs = {**result.outputs, 'watchdog_decision': None,
               'watchdog_supervision_input': input_path.relative_to(root).as_posix()}
    if preparation_diagnostic is not None:
        outputs['diagnostic_repair_preparation_diagnostic'] = preparation_diagnostic
    try:
        raw, truncated = _raw_report(root, result.outputs)
        archive = directory / 'raw-report.txt'
        if archive.is_symlink():
            raise ValueError('unsafe watchdog supervisor raw report archive')
        archive.write_text(raw, encoding='utf-8')
        outputs['watchdog_supervisor_raw_report'] = archive.relative_to(root).as_posix()
        if truncated:
            raise ValueError('watchdog supervisor report exceeds 1 MiB; original log retained')
        if result.status == 'completed':
            result.validate_for(command)
            outputs['watchdog_decision'] = bind_watchdog_report(json.loads(raw), request)
        else:
            diagnostics.append('watchdog supervisor did not complete; decision ignored')
    except (OSError, ValueError, TypeError, KeyError) as error:
        diagnostics.append(str(error))
    if diagnostics:
        outputs['watchdog_supervisor_diagnostics'] = diagnostics
    return replace(result, outputs=outputs,
                   detail='watchdog decision recorded' if outputs['watchdog_decision'] is not None else result.detail)
