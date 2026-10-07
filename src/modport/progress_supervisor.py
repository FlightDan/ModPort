"""Read-only investigation of an assignment's host-observed idle windows.

Only a valid, execution-bound agent decision can request termination. Missing,
malformed or failed reports remain diagnostics; this module never stops work,
extends a deadline, dispatches repairs or changes the assignment budget.
"""
from __future__ import annotations

from dataclasses import replace
import json
import math
import os
from pathlib import Path
import stat
from typing import Any, Callable, Mapping

from .contracts import OperationInput, OperationResult


PROGRESS_SUPERVISOR_PROMPT = """Investigate the supplied assignment after three or
more observation windows without measured useful work. Decide whether that exact
execution should continue or terminate. Observation counters are an index: inspect
the bounded original input, log, tool request/response, verification and workspace
references to explain the actual dependency and whether useful work can resume.
Use modport_sandbox_read_run_artifact for observation.snapshot_paths and
observation.evidence_paths outside your isolated workspace. Read bounded slices
and follow next_offset when needed. These are host-supplied evidence references;
do not add expected hash arguments or identity comparisons to this investigation.
Code or artifact changes, resolved errors, completed useful tool operations and
fresh verification can demonstrate useful work. Heartbeats, log volume, repeated
reports or a live process alone cannot. Missing observations remain unknown.
Trace raw errors through inputs, dispatch, execution and consumption. Separate
confirmed causes from hypotheses; an exit code, timeout or large input is not a
cause. When evidence is inconclusive, choose continue and state what is unknown.

Inspect active target workspaces and evidence read-only. When the host supplies
an isolated diagnostic repair workspace, follow its edit instructions to correct
confirmed small source errors there. Otherwise do not edit product or harness.
Do not edit goals or frozen inputs, run project code, dispatch work or invoke upstream repair.
The host enforces the original Run deadline and assignment budget; neither decision
extends them. Terminate requests only the supplied target execution, never peers
or the whole Run. Return exactly one JSON object with review_id, target_task_id,
target_execution_id, decision (continue or terminate), and a nonempty reason.
Copy those three identity fields from the host request. Explain the causal evidence
in reason; no extra approval, schema, hash or fingerprint checks are required.
"""


def _text(value: Any, name: str, *, limit: int = 16_000) -> str:
    if (not isinstance(value, str) or not value.strip()
            or len(value) > limit or '\x00' in value):
        raise ValueError(f'{name} must be nonempty text of at most {limit} characters')
    return value


def progress_supervision_request(value: Any) -> dict[str, Any]:
    """Detach the host's causal evidence without adding provenance checks."""
    if not isinstance(value, Mapping):
        raise ValueError('progress_supervision must be a host observation object')
    request = dict(value)
    for key in ('review_id', 'target_task_id', 'target_execution_id'):
        _text(request.get(key), key, limit=512)
    if not isinstance(request.get('observation'), Mapping):
        raise ValueError('progress_supervision observation must be an object')
    if type(request.get('idle_windows')) is not int or request['idle_windows'] < 1:
        raise ValueError('progress_supervision idle_windows must be positive')
    interval = request.get('interval_seconds')
    if type(interval) not in (int, float) or not math.isfinite(interval) or interval <= 0:
        raise ValueError('progress_supervision interval_seconds must be positive and finite')
    return json.loads(json.dumps(request, ensure_ascii=False, allow_nan=False))


def progress_supervisor_schema() -> dict[str, Any]:
    """The same five fields are declared to the producer and read by the host."""
    properties = {key: {'type': 'string', 'minLength': 1, 'maxLength': 512}
                  for key in ('review_id', 'target_task_id', 'target_execution_id')}
    properties.update(decision={'type': 'string', 'enum': ['continue', 'terminate']},
                      reason={'type': 'string', 'minLength': 1, 'maxLength': 16_000})
    return {'type': 'object', 'properties': properties,
            'required': list(properties), 'additionalProperties': False}


def validate_progress_supervisor_decision(
    document: Any, request: Mapping[str, Any],
) -> dict[str, str]:
    """Bind termination authority to the exact review and still-live execution."""
    expected = progress_supervision_request(request)
    required = {'review_id', 'target_task_id', 'target_execution_id', 'decision', 'reason'}
    if not isinstance(document, Mapping) or set(document) != required:
        raise ValueError('progress supervisor decision must contain exactly the five protocol fields')
    for key in ('review_id', 'target_task_id', 'target_execution_id'):
        _text(document[key], key, limit=512)
        if document[key] != expected[key]:
            raise ValueError(f'progress supervisor {key} does not match the host request')
    if document['decision'] not in ('continue', 'terminate'):
        raise ValueError('progress supervisor decision must be continue or terminate')
    _text(document['reason'], 'reason')
    return {key: document[key] for key in ('review_id', 'target_task_id',
                                         'target_execution_id', 'decision', 'reason')}


def recorded_progress_termination(snapshot: Mapping[str, Any], operation: OperationInput,
                                  recovery_reason: Any) -> dict[str, Any] | None:
    """Bind cancellation settlement to its committed review and SDK result."""
    try:
        state = snapshot['application_state']['progress_supervision']
        termination = state['terminated_executions'][operation.command_id]
        review_id = termination['review_id']
        review = state['reviews'][review_id]
        if (termination['task_id'] != operation.task_id
                or review['status'] != 'termination_requested'
                or review['target_task_id'] != operation.task_id
                or review['target_execution_id'] != operation.command_id):
            return None
        target = snapshot['tasks'][operation.task_id]['attempts'][-1]
        if target['command']['execution_id'] != operation.command_id:
            return None
        supervisor_task_id = review['supervisor_task_id']
        supervisor = snapshot['tasks'][supervisor_task_id]['attempts'][-1]
        if (supervisor['state'] != 'succeeded'
                or supervisor['command']['execution_id'] != review['supervisor_execution_id']):
            return None
        supervisor_input = OperationInput.from_dict(supervisor['command']['payload'])
        outcome = OperationResult.from_dict(supervisor['result']['value'])
        outcome.validate_for(supervisor_input)
        if (supervisor_input.stage_id != 'supervisor' or outcome.status != 'completed'
                or supervisor_input.run_id != operation.run_id
                or supervisor_input.task_id != supervisor_task_id
                or supervisor_input.payload.get('progress_supervision') != review['request']):
            return None
        decision = validate_progress_supervisor_decision(
            outcome.outputs.get('progress_supervisor_decision'), review['request'])
        if (decision['decision'] != 'terminate' or decision != review['decision']
                or decision['review_id'] != review_id
                or decision['target_task_id'] != operation.task_id
                or decision['target_execution_id'] != operation.command_id
                or termination['reason'] != decision['reason']
                or recovery_reason != 'progress_supervisor: ' + decision['reason']):
            return None
        return {'schema': 'modport.progress-supervisor-termination.v1',
                'run_id': operation.run_id, 'review_id': review_id,
                'supervisor_task_id': supervisor_task_id,
                'supervisor_execution_id': supervisor_input.command_id,
                'request': review['request'], 'decision': decision,
                'recovery_reason': recovery_reason}
    except (KeyError, TypeError, ValueError, IndexError):
        return None


def _bounded_references(command: OperationInput) -> dict[str, Any]:
    # Copy host-owned provenance as provided; do not compute or check digests.
    rows = list(command.artifact_refs.items())[:32]
    return {str(key): value for key, value in rows if isinstance(value, Mapping)}


def _raw_report(root: Path, outputs: Mapping[str, Any]) -> tuple[str, bool]:
    relative = outputs.get('last_message')
    if not isinstance(relative, str) or not relative:
        return '', False
    path = Path(relative)
    path = path if path.is_absolute() else root / path
    if path.resolve() != path.absolute() or not path.resolve().is_relative_to(root):
        raise ValueError('progress supervisor report path leaves the Run evidence directory')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError('progress supervisor report must be a regular file')
        with os.fdopen(descriptor, 'rb', closefd=False) as source:
            data = source.read(1024 * 1024 + 1)
    finally:
        os.close(descriptor)
    truncated = len(data) > 1024 * 1024
    return data[:1024 * 1024].decode('utf-8', errors='replace'), truncated


def archive_interrupted_progress_supervisor(command: OperationInput) -> dict[str, Any]:
    """Retain cancelled investigation evidence without accepting its decision.

    The recovery caller must first prove exact cancellation custody and cleanup.
    Partial response text is retained even if it happens to contain valid JSON;
    recovering an interrupted assignment never creates termination authority.
    """
    root = Path(command.run_dir).resolve()
    execution_directory = root / 'artifacts' / 'executions' / command.command_id
    directory = execution_directory / 'progress-supervision'
    if directory.resolve() != directory.absolute():
        raise ValueError('unsafe interrupted progress supervisor archive path')
    directory.mkdir(parents=True, exist_ok=True)
    request_path = directory / 'interrupted-request.json'
    report_path = directory / 'interrupted-raw-report.txt'
    if request_path.is_symlink() or report_path.is_symlink():
        raise ValueError('unsafe interrupted progress supervisor evidence archive')
    packet = {'progress_supervision': command.payload.get('progress_supervision'),
              'artifact_refs': _bounded_references(command)}
    request_path.write_text(json.dumps(packet, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    assignment = command.options.get('agent_assignment', command.attempt)
    candidates = (directory / 'raw-report.txt',
                  execution_directory / 'dialogue' / 'final-report.txt',
                  root / 'logs' / f'agent-{command.task_id}-{assignment}.txt',
                  root / 'logs' / f'agent-{command.task_id}-{assignment}.log')
    diagnostics = ['progress supervisor was interrupted; no decision is accepted']
    raw = ''
    source = None
    truncated = False
    for path in candidates:
        try:
            raw, truncated = _raw_report(root, {'last_message': str(path)})
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            diagnostics.append(str(exc))
            continue
        source = path.relative_to(root).as_posix()
        break
    if source is None:
        diagnostics.append('no partial supervisor response was available; original execution artifacts retained')
    if truncated:
        diagnostics.append('partial report archive is limited to 1 MiB; original report remains at its source path')
    report_path.write_text(raw, encoding='utf-8')
    return {'disposition': 'partial progress investigation retained; no decision accepted',
            'request_ref': {'path': request_path.relative_to(root).as_posix()},
            'raw_report_ref': {'path': report_path.relative_to(root).as_posix()},
            'raw_report_source': source, 'raw_report_truncated': truncated,
            'progress_supervisor_decision': None, 'diagnostics': diagnostics}


def invoke_progress_supervisor(
    command: OperationInput,
    stage_handler: Callable[[str], Callable[[OperationInput], OperationResult]],
) -> OperationResult:
    """Use the normal assignment transport and retain all invalid raw reports."""
    root = Path(command.run_dir).resolve()
    directory = root / 'artifacts' / 'executions' / command.command_id / 'progress-supervision'
    diagnostics: list[str] = []
    repair_preparation_diagnostic = None
    from . import diagnostic_repairs
    if diagnostic_repairs.enabled(command) and command.payload.get('diagnostic_repair_targets'):
        try:
            command = diagnostic_repairs.prepare(command)
        except (OSError, ValueError, TypeError, KeyError) as error:
            # Optional edits must not suppress the supervisor's investigation.
            # Keep an interrupted snapshot intact and use the original private
            # observation workspace with no write permission.
            repair_preparation_diagnostic = str(error)
            command = replace(command, payload={key: value for key, value in command.payload.items()
                                                if key != 'diagnostic_repair_manifest'})
    try:
        if directory.resolve() != directory.absolute():
            raise ValueError('unsafe progress supervisor archive path')
        directory.mkdir(parents=True, exist_ok=True)
        from .kernel_runtime import operation_workspace
        workspace = operation_workspace(root, command)
        if workspace is not None:
            workspace.mkdir(parents=True, exist_ok=True)
        request = progress_supervision_request(command.payload.get('progress_supervision'))
        packet = {'progress_supervision': request, 'artifact_refs': _bounded_references(command)}
        input_path = directory / 'input.json'
        if input_path.is_symlink():
            raise ValueError('unsafe progress supervisor input archive')
        input_path.write_text(json.dumps(packet, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    except (OSError, TypeError, ValueError) as exc:
        return OperationResult('failed', command.run_id, command.task_id, command.stage_id,
                               command.command_id,
                               outputs={'progress_supervisor_decision': None},
                               detail=str(exc), error_code='progress_supervision_input_invalid')
    prompt = (PROGRESS_SUPERVISOR_PROMPT + '\nHost observation and bounded artifact references: '
              + json.dumps(packet, ensure_ascii=False, sort_keys=True))
    if command.payload.get('diagnostic_repair_manifest'):
        prompt += '\n' + diagnostic_repairs.instructions(command)
    elif repair_preparation_diagnostic is not None:
        prompt += ('\nOptional isolated repair snapshot is unavailable: '
                   + repair_preparation_diagnostic + '. Continue the investigation read-only.')
    result = stage_handler(prompt)(command)
    if command.payload.get('diagnostic_repair_manifest'):
        result = diagnostic_repairs.collect(command, result)
    outputs = {**result.outputs, 'progress_supervisor_decision': None,
               'progress_supervision_input': input_path.relative_to(root).as_posix()}
    if repair_preparation_diagnostic is not None:
        outputs['diagnostic_repair_preparation_diagnostic'] = repair_preparation_diagnostic
    raw_report = ''
    try:
        raw_report, truncated = _raw_report(root, result.outputs)
        archive = directory / 'raw-report.txt'
        if archive.is_symlink():
            raise ValueError('unsafe progress supervisor raw report archive')
        archive.write_text(raw_report, encoding='utf-8')
        outputs['progress_supervisor_raw_report'] = archive.relative_to(root).as_posix()
        if truncated:
            raise ValueError('progress supervisor report exceeds the 1 MiB decision limit; original log retained')
        if result.status == 'completed':
            outputs['progress_supervisor_decision'] = validate_progress_supervisor_decision(
                json.loads(raw_report), request)
        else:
            diagnostics.append('progress supervisor execution did not complete; any decision is ignored')
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        diagnostics.append(str(exc))
    if diagnostics:
        outputs['progress_supervisor_diagnostics'] = diagnostics
    return replace(result, outputs=outputs,
                   detail=('progress supervisor decision recorded'
                           if outputs['progress_supervisor_decision'] is not None else result.detail))
