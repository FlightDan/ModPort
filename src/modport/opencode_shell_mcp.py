"""Credential-free project commands for OpenCode agent sessions.

The MCP process is a host component.  The requested command runs only through
the existing ModPort bubblewrap gate, with its clear environment and bounded
workspace mounts.  A namespace failure is returned as a failure; it never
falls back to a host shell.
"""
from __future__ import annotations

from .workspace import project_path, project_relative, is_project_workspace, is_run_path, workspace_context

from .workspace import git_probe

import argparse
from contextlib import ExitStack
from hashlib import sha256
import json
import math
from .platform_files import file_os as os
from pathlib import Path
import re
import selectors
import shlex
import signal
import stat
import subprocess
import sys
import time
from xml.etree import ElementTree
from typing import Any, Mapping
from uuid import uuid4


PROTOCOL_VERSION = '2025-06-18'
_WORKSPACE_ROOTS = frozenset({'worktree', 'baseline', 'workspaces'})
MAX_COMMAND_SECONDS = 7200
MAX_CANDIDATE_FILES = 100_000
MAX_CANDIDATE_FILE_BYTES = 256 * 1024 * 1024
MAX_CANDIDATE_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_CANDIDATE_INDEX_BYTES = 32 * 1024 * 1024
MAX_RESULT_TREE_ENTRIES = 100_000
MAX_RESULT_TREE_DEPTH = 64
MAX_JUNIT_XML_FILES = 500
MAX_JUNIT_XML_FILE_BYTES = 16 * 1024 * 1024
MAX_JUNIT_XML_TOTAL_BYTES = 128 * 1024 * 1024
MAX_CHARACTERIZATION_INVOCATIONS = 100
_GENERATED_PARTS = frozenset({'.git', '.gradle', 'build', 'out', 'target',
                              'node_modules', '.venv', 'venv', '__pycache__'})
from .retry_policy import HARNESS_HOST_OUTPUTS, HARNESS_OUTPUT_DIRS

_RESULT_PARTS = HARNESS_OUTPUT_DIRS | HARNESS_HOST_OUTPUTS | {'run-characterization'}


class AssertionIdentityError(ValueError):
    """A v29 assertion/source/test binding is malformed or stale."""


def network_tools_disabled() -> bool:
    """A host-only opt-in for assignments that must not offer network tools."""
    return os.environ.get('MODPORT_DENY_NETWORK_TOOLS') == '1'


def _validate_scope(root: Path, workspace: Path, command_id: str) -> None:
    if (not isinstance(command_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]+', command_id)
            or command_id in {'.', '..'}):
        raise ValueError('invalid sandbox command identity')
    if not is_project_workspace(root, workspace) or workspace == root:
        raise ValueError('sandbox workspace must be an isolated project directory')
    relative = project_relative(root, workspace)
    if relative.parts[0] not in _WORKSPACE_ROOTS:
        raise ValueError('sandbox workspace is outside supported project roots')


def prepare_sandbox_tool(root: Path, workspace: Path, command_id: str, timeout: float,
                         *, read_only: bool = False, allow_project_commands: bool = True) -> dict:
    """Return OpenCode MCP config after storing a host-owned execution scope."""
    root = Path(root).resolve(strict=True)
    workspace = Path(workspace).resolve(strict=True)
    _validate_scope(root, workspace, command_id)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('sandbox deadline must be positive and finite')
    from .evidence import atomic_json
    directory = root / 'artifacts' / 'executions' / command_id / 'opencode-shell'
    directory.mkdir(parents=True, exist_ok=True)
    descriptor = directory / 'session.json'
    new = {'root': str(root), 'workspace': str(workspace), 'command_id': command_id,
           'read_only': bool(read_only),
           'allow_project_command': bool(allow_project_commands) and not network_tools_disabled(),
           'deadline_epoch': time.time() + timeout}
    if descriptor.exists():
        previous = json.loads(descriptor.read_text(encoding='utf-8'))
        # Older descriptors did not record this opt-in. Their implicit policy
        # allowed project commands; preserve that identity on recovery.
        previous_allow = previous.get('allow_project_command', True)
        if (type(previous_allow) is not bool
                or previous_allow != new['allow_project_command']
                or any(previous.get(k) != new[k] for k in (
                    'root', 'workspace', 'command_id', 'read_only'))):
            raise ValueError('sandbox tool session identity changed')
        new['deadline_epoch'] = min(previous['deadline_epoch'], new['deadline_epoch'])
    atomic_json(descriptor, new)
    from .mcp_launcher import trusted_mcp_command
    return {'modport_sandbox': {'type': 'local',
            'command': trusted_mcp_command('modport.opencode_shell_mcp', __file__, descriptor),
            'environment': {},
            'enabled': True, 'timeout': max(1000, math.ceil(timeout * 1000))}}


def prepare_artifact_compile_tool(command, workspace: Path, timeout: float) -> dict:
    """Register a diagnostic compiler with host-owned binary-only wiring."""
    from .evidence import atomic_json
    from .handlers import _locked_java_home
    from .mcp_launcher import trusted_mcp_command
    root = Path(command.run_dir).resolve(strict=True)
    workspace = Path(workspace).resolve(strict=True)
    _validate_scope(root, workspace, command.command_id)
    path = root / 'artifacts' / 'executions' / command.command_id / 'artifact-compile-session.json'
    atomic_json(path, {'root': str(root), 'workspace': str(workspace),
        'command_id': command.command_id, 'kind': 'artifact_compile',
        'deadline_epoch': time.time() + timeout,
        'java_home': str(_locked_java_home(root)),
        'operation': {'run_id': command.run_id, 'task_id': command.task_id,
            'stage_id': command.stage_id, 'command_id': command.command_id,
            'run_dir': command.run_dir, 'options': dict(command.options),
            'payload': {'request': dict(command.payload.get('request', command.payload))}}})
    return {'modport_artifact': {'type': 'local',
        'command': trusted_mcp_command('modport.opencode_shell_mcp', __file__, path),
        'environment': {}, 'enabled': True, 'timeout': max(1000, math.ceil(timeout * 1000))}}


def _compile_artifact_harness(session: Mapping[str, Any], arguments: Mapping[str, Any]) -> dict:
    from .contracts import OperationInput
    from .evidence import atomic_json
    from .handlers import _sandboxed_build_command
    from .harness_wiring import characterization_init_scripts
    if set(arguments) - {'timeout_seconds'}:
        raise ValueError('artifact compilation accepts only timeout_seconds')
    timeout = arguments.get('timeout_seconds', MAX_COMMAND_SECONDS)
    if type(timeout) is not int or not 1 <= timeout <= MAX_COMMAND_SECONDS:
        raise ValueError('artifact compiler timeout is outside the host bound')
    remaining = session['deadline_epoch'] - time.time()
    if remaining <= 0:
        raise TimeoutError('assignment deadline is exhausted')
    root, workspace = Path(session['root']), Path(session['workspace'])
    _validate_scope(root, workspace, session['command_id'])
    operation = OperationInput.from_dict(session['operation'])
    # Include wrapper preparation in this call's bound without extending the
    # transported SDK/model deadline.
    from dataclasses import replace
    call_deadline = min(session['deadline_epoch'], time.time() + timeout)
    prior_deadline = operation.options.get('model_deadline_epoch')
    if prior_deadline is not None:
        call_deadline = min(call_deadline, float(prior_deadline))
    operation = replace(operation, options={**operation.options,
        'model_deadline_epoch': call_deadline})
    args = ['bash', '/workspace/gradlew', '--no-daemon']
    if (root / 'artifacts/dependency-repository.init.gradle').is_file():
        args += ['--init-script', '/modport-dependency.init.gradle']
    for script in characterization_init_scripts(workspace,
            workflow_version=operation.options['workflow_version'],
            supplement_directory=root / 'artifacts/harness-wiring'):
        mount = '/workspace/' + script.relative_to(workspace).as_posix() if script.is_relative_to(workspace) else '/modport-wiring/' + script.name
        args += ['--init-script', mount]
    args += ['--init-script', '/modport-wiring/' + Path(operation.options['artifact_init_script']).name,
             'compileJava', 'compileTestJava']
    argv = _sandboxed_build_command(root, workspace, args, operation=operation,
        java_home=Path(session['java_home']), cache_name='target-contract-gradle-cache')
    remaining = call_deadline - time.time()
    if remaining <= 0:
        raise TimeoutError('assignment deadline exhausted during compiler preparation')
    result = _bounded_command(argv, workspace, min(timeout, remaining))
    result.update(acceptance_evidence=False, tasks=['compileJava', 'compileTestJava'])
    path = root / 'artifacts/executions' / session['command_id'] / ('artifact-compile-' + uuid4().hex + '.json')
    atomic_json(path, result)
    return {**result, 'artifact_path': project_relative(root, path).as_posix()}


def prepare_characterization_tool(
    root: Path,
    workspace: Path,
    command_id: str,
    *,
    source_commit: str,
    contract_sha256: str | None,
    contract_path: str | None = None,
    test_cases: Mapping[str, Mapping[str, Any]],
    init_scripts: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
    full_tasks: list[str] | tuple[str, ...],
    timeout: float,
    assertions_reviewed: bool = False,
    prior_case_receipts: Mapping[str, Mapping[str, str]] | None = None,
    dynamic_contract: bool = False,
    read_only: bool = False,
) -> dict[str, Any]:
    """Register a host-defined, typed characterization verifier for OpenCode.

    The MCP arguments choose only registered test IDs or an explicitly
    authorized full diagnostic. Commands, candidate identities, wiring paths,
    nonce and deadlines remain host owned.
    """
    root = Path(root).resolve(strict=True)
    workspace = Path(workspace).resolve(strict=True)
    _validate_scope(root, workspace, command_id)
    if not re.fullmatch(r'[0-9a-f]{40,64}', str(source_commit)):
        raise ValueError('source commit must be a full Git object ID')
    if dynamic_contract:
        if contract_sha256 is not None:
            raise ValueError('dynamic contract identity is computed at verifier invocation')
    elif not isinstance(contract_sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', contract_sha256):
        raise ValueError('contract identity must be a SHA-256 digest')
    if contract_path is None:
        contract_file = workspace / '.modport' / 'functional-contract.json'
    else:
        relative_contract = Path(contract_path)
        if (relative_contract.is_absolute() or not relative_contract.parts
                or '..' in relative_contract.parts or '\\' in contract_path):
            raise ValueError('functional contract path must be a contained host-relative path')
        contract_file = project_path(root, relative_contract)
    if (contract_file.is_symlink()
            or (not dynamic_contract and not contract_file.is_file())
            or (contract_file.exists() and contract_file.resolve() != contract_file.absolute())
            or not is_run_path(root, contract_file)):
        raise ValueError('functional contract source is missing or unsafe')
    if (not dynamic_contract and _safe_file_sha256(contract_file) != contract_sha256):
        raise ValueError('functional contract digest changed before verifier registration')
    if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError('characterization deadline must be positive and finite')
    if not isinstance(test_cases, Mapping):
        raise ValueError('characterization test cases must be a mapping')
    normalized_cases: dict[str, dict[str, Any]] = {}
    for test_id, raw in test_cases.items():
        if not isinstance(test_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]+', test_id):
            raise ValueError('registered test ID is unsafe')
        if not isinstance(raw, Mapping):
            raise ValueError(f'registered test {test_id!r} must be an object')
        identity = raw.get('result_identity')
        if identity is not None and (
                not isinstance(identity, Mapping) or identity.get('kind') != 'junit_xml'
                or not isinstance(identity.get('gradle_task'), str)
                or not re.fullmatch(r':?[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)*', identity['gradle_task'])
                or not isinstance(identity.get('classname'), str)
                or not re.fullmatch(r'[A-Za-z_$][A-Za-z0-9_$.]*', identity['classname'])
                or not isinstance(identity.get('name'), str)
                or not re.fullmatch(r'[A-Za-z_$][A-Za-z0-9_$]*', identity['name'])):
            raise ValueError(f'registered test {test_id!r} has an invalid JUnit result identity')
        assertion_ids = raw.get('assertion_ids', ())
        if (not isinstance(assertion_ids, (list, tuple))
                or any(not isinstance(value, str) or not value.strip() for value in assertion_ids)
                or len(assertion_ids) != len(set(assertion_ids))):
            raise ValueError(f'registered test {test_id!r} has invalid assertion IDs')
        source_files = raw.get('test_source_files', ())
        if (not isinstance(source_files, (list, tuple))
                or (identity is not None and not source_files)
                or len(source_files) != len(set(source_files))
                or any(not isinstance(value, str) or not value.startswith('.modport/')
                       or '\\' in value or '..' in Path(value).parts
                       or Path(value).suffix not in {'.java', '.kt', '.groovy'}
                       for value in source_files)):
            raise ValueError(f'registered test {test_id!r} requires safe harness source paths')
        executor = raw.get('executor', 'junit')
        if executor not in {'junit', 'client_smoke'}:
            raise ValueError(f'registered test {test_id!r} has an unsupported executor')
        if executor == 'junit' and identity is None:
            raise ValueError(f'registered JUnit test {test_id!r} requires a result identity')
        normalized_cases[test_id] = {
            'result_identity': dict(identity) if identity is not None else None,
            'assertion_ids': list(assertion_ids),
            'test_source_files': list(source_files),
            'executor': executor,
        }
    identities = [canonical for canonical in (
        json.dumps(item['result_identity'], sort_keys=True, separators=(',', ':'))
        for item in normalized_cases.values() if item['result_identity'] is not None)]
    if len(identities) != len(set(identities)):
        raise ValueError('JUnit result identities must be unique across registered test IDs')
    if (not isinstance(full_tasks, (list, tuple)) or (not full_tasks and not dynamic_contract)
            or any(not isinstance(task, str) or not _safe_gradle_task(task) for task in full_tasks)
            or len(full_tasks) != len(set(full_tasks))):
        raise ValueError('full diagnostic tasks must be unique Gradle task names')
    normalized_wiring: list[dict[str, str]] = []
    if not isinstance(init_scripts, (list, tuple)):
        raise TypeError('init_scripts must be a sequence')
    for raw in init_scripts:
        if not isinstance(raw, Mapping):
            raise ValueError('init script descriptor must be an object')
        path = raw.get('path')
        digest = raw.get('sha256')
        origin = raw.get('origin')
        if (not isinstance(path, str) or not path or '\x00' in path or '\\' in path
                or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)
                or origin not in {'workspace', 'wiring'}):
            raise ValueError('init script path, digest or origin is invalid')
        base = workspace if origin == 'workspace' else root / 'artifacts' / 'harness-wiring'
        candidate = base / path
        if (candidate.is_symlink() or not candidate.is_file()
                or candidate.resolve() != candidate.absolute()
                or not candidate.resolve().is_relative_to(base.resolve())):
            raise ValueError('init script is missing or unsafe')
        if _safe_file_sha256(candidate) != digest:
            raise ValueError('init script digest changed before verifier registration')
        normalized_wiring.append({'path': path, 'sha256': digest, 'origin': origin})
    normalized_prior: dict[str, dict[str, str]] = {}
    if prior_case_receipts is not None:
        if not isinstance(prior_case_receipts, Mapping):
            raise ValueError('prior_case_receipts must be a mapping')
        for test_id, ref in prior_case_receipts.items():
            if ((not dynamic_contract and test_id not in normalized_cases)
                    or not isinstance(test_id, str)
                    or not re.fullmatch(r'[A-Za-z0-9_.:-]+', test_id)
                    or not isinstance(ref, Mapping)):
                raise ValueError('prior receipt must belong to a registered test ID')
            relative = ref.get('path')
            digest = ref.get('sha256')
            if (not isinstance(relative, str) or not isinstance(digest, str)
                    or not re.fullmatch(r'[0-9a-f]{64}', digest)):
                raise ValueError('prior receipt reference is malformed')
            path = Path(relative)
            if (path.is_absolute() or not path.parts or '..' in path.parts
                    or '\\' in relative):
                raise ValueError('prior receipt path must be contained')
            prior_path = root / path
            if (prior_path.is_symlink() or not prior_path.is_file()
                    or prior_path.resolve() != prior_path.absolute()
                    or not prior_path.resolve().is_relative_to(root)
                    or _safe_file_sha256(prior_path) != digest):
                raise ValueError('prior receipt artifact is missing, unsafe or changed')
            normalized_prior[test_id] = {'path': relative, 'sha256': digest}
    nonce = uuid4().hex
    directory = root / 'artifacts' / 'executions' / command_id / 'opencode-characterization'
    directory.mkdir(parents=True, exist_ok=True)
    descriptor = directory / 'session.json'
    session = {
        'schema_version': 1, 'kind': 'characterization', 'root': str(root),
        'workspace': str(workspace), 'command_id': command_id,
        'read_only': bool(read_only), 'allow_project_command': False,
        'source_commit': str(source_commit), 'contract_sha256': contract_sha256,
        'contract_path': project_relative(root, contract_file).as_posix(),
        'test_cases': normalized_cases, 'init_scripts': normalized_wiring,
        'prior_case_receipts': normalized_prior,
        'full_tasks': list(full_tasks), 'dynamic_contract': bool(dynamic_contract),
        'execution_nonce': nonce,
        'assertions_reviewed': bool(assertions_reviewed),
        'deadline_epoch': time.time() + timeout,
        'max_invocation_seconds': min(float(timeout), 7200.0),
    }
    from .evidence import atomic_json
    atomic_json(descriptor, session)
    from .mcp_launcher import trusted_mcp_command
    return {'modport_characterization': {
        'type': 'local',
        'command': trusted_mcp_command('modport.opencode_shell_mcp', __file__, descriptor),
        'environment': {}, 'enabled': True,
        'timeout': max(1000, math.ceil(timeout * 1000)),
    }}


def _safe_gradle_task(value: str) -> bool:
    return (isinstance(value, str) and bool(re.fullmatch(
        r':?[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)*', value))
        and value.split(':')[-1] not in {'help', 'tasks', 'properties'})


def _safe_file_sha256(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > 32 * 1024 * 1024:
            raise ValueError('registered input must be a bounded regular file')
        digest = sha256()
        while block := os.read(descriptor, 1024 * 1024):
            digest.update(block)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError('registered input changed while it was hashed')
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _workspace_candidate_identity(workspace: Path) -> dict[str, Any]:
    """Hash a bounded Git candidate without generated build/runtime outputs."""
    workspace = Path(workspace)
    if (not workspace.is_dir() or workspace.is_symlink()
            or workspace.resolve() != workspace.absolute()):
        raise ValueError('candidate workspace is missing or unsafe')
    env = {'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent',
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
           'GIT_OPTIONAL_LOCKS': '0', 'GIT_TERMINAL_PROMPT': '0',
           'GIT_NO_REPLACE_OBJECTS': '1'}

    def git_bytes(arguments: list[str], limit: int) -> bytes:
        result = git_probe(
            ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
             '-c', 'core.untrackedCache=false', '-C', str(workspace), *arguments],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
            timeout=30, check=False,
        )
        if result.returncode or len(result.stdout) > limit:
            raise ValueError(f'candidate Git identity unavailable for {arguments[0]}')
        return result.stdout

    head = git_bytes(['rev-parse', 'HEAD'], 256).decode('ascii', errors='strict').strip()
    if not re.fullmatch(r'[0-9a-f]{40,64}', head):
        raise ValueError('candidate HEAD is not a full Git object ID')
    index_records = git_bytes(['ls-files', '--stage', '-z'], MAX_CANDIDATE_INDEX_BYTES)
    selected_index: list[bytes] = []
    for raw in index_records.split(b'\0'):
        if not raw:
            continue
        header, separator, path = raw.partition(b'\t')
        if not separator:
            raise ValueError('candidate Git index record is malformed')
        try:
            relative = path.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise ValueError('candidate Git index has a non-UTF-8 source path') from exc
        parts = relative.split('/')
        if any(part in _GENERATED_PARTS for part in parts):
            continue
        if len(parts) >= 2 and parts[0] == '.modport' and (
                parts[1] in _RESULT_PARTS or parts[1] == 'goal-reports'):
            continue
        selected_index.append(header + b'\t' + path)
    index_sha256 = sha256(b'\0'.join(sorted(selected_index))).hexdigest()
    listed = git_bytes(['ls-files', '-z', '--cached', '--others', '--exclude-standard'],
                       MAX_CANDIDATE_INDEX_BYTES)
    try:
        names = [item.decode('utf-8') for item in listed.split(b'\0') if item]
    except UnicodeDecodeError as exc:
        raise ValueError('candidate has a non-UTF-8 source path') from exc
    if len(names) > MAX_CANDIDATE_FILES:
        raise ValueError('candidate exceeds the source file count limit')
    records: list[dict[str, Any]] = []
    total = 0
    seen: set[str] = set()
    for relative in sorted(names):
        parts = relative.split('/')
        if (relative.startswith('/') or '\\' in relative
                or any(part in {'', '.', '..'} for part in parts)):
            raise ValueError('candidate contains an unsafe source path')
        if any(part in _GENERATED_PARTS for part in parts):
            continue
        if (len(parts) >= 2 and parts[0] == '.modport'
                and (parts[1] in _RESULT_PARTS or parts[1] == 'goal-reports')):
            continue
        if relative in seen:
            continue
        seen.add(relative)
        source = workspace.joinpath(*parts)
        if not source.exists() and not source.is_symlink():
            records.append({'path': relative, 'deleted': True})
            continue
        if source.resolve() != source.absolute() or not source.resolve().is_relative_to(workspace):
            raise ValueError('candidate source path crosses a symbolic link')
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
        try:
            descriptor = os.open(source, flags)
        except OSError as exc:
            raise ValueError(f'candidate source is unreadable: {relative}') from exc
        try:
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or before.st_size > MAX_CANDIDATE_FILE_BYTES):
                raise ValueError(f'candidate source is not a bounded regular file: {relative}')
            total += before.st_size
            if total > MAX_CANDIDATE_TOTAL_BYTES:
                raise ValueError('candidate exceeds the total source byte limit')
            digest = sha256()
            count = 0
            while block := os.read(descriptor, 1024 * 1024):
                count += len(block)
                if count > MAX_CANDIDATE_FILE_BYTES:
                    raise ValueError(f'candidate source exceeds its byte limit: {relative}')
                digest.update(block)
            after = os.fstat(descriptor)
            before_identity = (before.st_dev, before.st_ino, before.st_size,
                               before.st_mtime_ns, before.st_ctime_ns, before.st_mode)
            after_identity = (after.st_dev, after.st_ino, after.st_size,
                              after.st_mtime_ns, after.st_ctime_ns, after.st_mode)
            if before_identity != after_identity or count != before.st_size:
                raise ValueError(f'candidate source changed while it was hashed: {relative}')
            records.append({'path': relative, 'sha256': digest.hexdigest(),
                            'size': count, 'mode': before.st_mode & 0o111})
        finally:
            os.close(descriptor)
    material = {'schema_version': 1, 'head': head,
                'index_sha256': index_sha256, 'files': records}
    identity = sha256(json.dumps(material, ensure_ascii=False, sort_keys=True,
                                 separators=(',', ':')).encode('utf-8')).hexdigest()
    return {'candidate_id': identity, 'head': head, 'index_sha256': index_sha256,
            'file_count': len(records), 'total_bytes': total}


def _current_case_binding(session: Mapping[str, Any], test_id: str) -> dict[str, Any]:
    root = Path(session['root'])
    workspace = Path(session['workspace'])
    contract_path = project_path(root, session['contract_path'])
    if (contract_path.is_symlink() or not contract_path.is_file()
            or contract_path.resolve() != contract_path.absolute()
            or not is_run_path(root, contract_path)):
        raise AssertionIdentityError('registered functional contract is missing or unsafe')
    contract_sha256 = _safe_file_sha256(contract_path)
    if (not session.get('dynamic_contract')
            and contract_sha256 != session['contract_sha256']):
        raise AssertionIdentityError('registered functional contract changed; create a fresh verifier session')
    from .author_contracts import normalize_characterization_contract
    try:
        document = normalize_characterization_contract(
            json.loads(contract_path.read_text(encoding='utf-8')))
    except (ValueError, TypeError) as exc:
        raise AssertionIdentityError('registered functional contract mappings are invalid: ' + str(exc)) from exc
    declarations = document.get('test_evidence') if isinstance(document, Mapping) else None
    declaration = declarations.get(test_id) if isinstance(declarations, Mapping) else None
    expected = session['test_cases'][test_id]
    if (not isinstance(declaration, Mapping)
            or declaration.get('result_identity') != expected['result_identity']
            or declaration.get('executor', 'junit') != expected['executor']
            or declaration.get('test_source_files', []) != expected['test_source_files']):
        raise AssertionIdentityError(f'test declaration changed after verifier registration: {test_id}')
    assertion_rows: dict[str, Mapping[str, Any]] = {}
    for behavior in document.get('behaviors', document.get('entries', ())):
        if not isinstance(behavior, Mapping):
            continue
        for assertion in behavior.get('assertion_contracts', ()):
            if not isinstance(assertion, Mapping) or test_id not in assertion.get('test_ids', ()):
                continue
            assertion_id = assertion.get('assertion_id')
            if assertion_id in expected['assertion_ids']:
                assertion_rows[assertion_id] = assertion
    if set(assertion_rows) != set(expected['assertion_ids']):
        raise AssertionIdentityError(f'assertion to test mapping changed after verifier registration: {test_id}')
    resolved_anchors: dict[str, Any] = {}
    source_root = root / 'baseline'
    if _git_head(source_root) != session['source_commit']:
        raise AssertionIdentityError('original source checkout does not match the registered source commit')
    for assertion_id, assertion in assertion_rows.items():
        anchor = assertion.get('source_anchor')
        if not isinstance(anchor, Mapping):
            raise AssertionIdentityError(f'assertion source anchor is missing: {assertion_id}')
        path = anchor.get('path')
        start_line = anchor.get('start_line')
        end_line = anchor.get('end_line')
        if (not isinstance(path, str) or path.startswith('/') or '\\' in path
                or any(part in {'', '.', '..', '.git'} for part in path.split('/'))
                or type(start_line) is not int or type(end_line) is not int
                or start_line < 1 or end_line < start_line):
            raise AssertionIdentityError(f'assertion source anchor is malformed: {assertion_id}')
        source = source_root.joinpath(*path.split('/'))
        if (source.is_symlink() or not source.is_file()
                or source.resolve() != source.absolute()
                or not source.resolve().is_relative_to(source_root)):
            raise AssertionIdentityError(f'assertion source anchor is missing or unsafe: {assertion_id}')
        data = _read_bounded_file(source, 16 * 1024 * 1024)
        try:
            lines = data.decode('utf-8').splitlines(keepends=True)
        except UnicodeDecodeError as exc:
            raise AssertionIdentityError(
                f'assertion source anchor is not UTF-8: {assertion_id}',
            ) from exc
        if end_line > len(lines):
            raise AssertionIdentityError(f'assertion source anchor line range is invalid: {assertion_id}')
        source_sha256 = sha256(data).hexdigest()
        range_sha256 = sha256(''.join(lines[start_line - 1:end_line]).encode('utf-8')).hexdigest()
        resolved = {'path': path, 'start_line': start_line, 'end_line': end_line,
                    'source_commit': session['source_commit'],
                    'source_file_sha256': source_sha256, 'range_sha256': range_sha256}
        for field in ('source_commit', 'source_file_sha256', 'range_sha256'):
            if anchor.get(field) is not None and anchor.get(field) != resolved[field]:
                raise AssertionIdentityError(f'assertion source anchor identity mismatch: {assertion_id}')
        resolved_anchors[assertion_id] = {'text': str(assertion.get('text', '')),
                                           'source_anchor': resolved,
                                           'assertion_id': assertion_id}
    harness_files: dict[str, str] = {}
    for relative in expected['test_source_files']:
        source = workspace / relative
        if (source.is_symlink() or not source.is_file()
                or source.resolve() != source.absolute()
                or not source.resolve().is_relative_to(workspace)):
            raise ValueError(f'test harness source is missing or unsafe: {relative}')
        harness_files[relative] = _safe_file_sha256(source)
    wiring = []
    for item in session['init_scripts']:
        base = workspace if item['origin'] == 'workspace' else root / 'artifacts' / 'harness-wiring'
        source = base / item['path']
        if (source.is_symlink() or not source.is_file()
                or source.resolve() != source.absolute()
                or not source.resolve().is_relative_to(base.resolve())
                or _safe_file_sha256(source) != item['sha256']):
            raise ValueError('registered characterization init wiring changed')
        wiring.append(item)
    binding = {
        'schema_version': 1, 'test_id': test_id,
        'source_commit': session['source_commit'],
        'contract_sha256': contract_sha256,
        'declaration': dict(declaration),
        'declaration_sha256': sha256(json.dumps(dict(declaration), sort_keys=True,
            separators=(',', ':')).encode()).hexdigest(),
        'source_anchors': resolved_anchors,
        'test_source_files': harness_files,
        'init_scripts': wiring,
    }
    identity_material = {key: value for key, value in binding.items()
                         if key != 'contract_sha256'}
    binding['case_identity'] = sha256(json.dumps(identity_material, ensure_ascii=False,
        sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
    return binding


def _verified_prior_case_receipt(session: Mapping[str, Any], test_id: str,
                                 binding: Mapping[str, Any], candidate: Mapping[str, Any]
                                 ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return exact-candidate reusable evidence and preserve prior-version refs."""
    ref = session.get('prior_case_receipts', {}).get(test_id)
    if not isinstance(ref, Mapping):
        return None, None
    path = Path(session['root']) / str(ref.get('path', ''))
    if (path.is_symlink() or not path.is_file() or path.resolve() != path.absolute()
            or not path.resolve().is_relative_to(Path(session['root']).resolve())
            or _safe_file_sha256(path) != ref.get('sha256')):
        raise AssertionIdentityError('registered prior case receipt changed or became unsafe')
    data = _read_bounded_file(path, 32 * 1024 * 1024)
    try:
        previous = json.loads(data.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AssertionIdentityError('registered prior case receipt is malformed') from exc
    if not isinstance(previous, Mapping):
        raise AssertionIdentityError('registered prior case receipt is not an object')
    digest = previous.get('receipt_sha256')
    unsigned = dict(previous)
    unsigned.pop('receipt_sha256', None)
    calculated = sha256(json.dumps(unsigned, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode('utf-8')).hexdigest()
    test_results = previous.get('test_results')
    bindings = previous.get('case_bindings')
    registered_identity = session['test_cases'][test_id]['result_identity']
    prior_result = test_results[0] if isinstance(test_results, list) and len(test_results) == 1 else {}
    xml_ref = prior_result.get('xml_artifact_ref') if isinstance(prior_result, Mapping) else None
    xml_valid = False
    if isinstance(xml_ref, Mapping):
        xml_path = Path(session['root']) / str(xml_ref.get('path', ''))
        try:
            xml_valid = (
                not xml_path.is_symlink() and xml_path.is_file()
                and xml_path.resolve() == xml_path.absolute()
                and xml_path.resolve().is_relative_to(Path(session['root']).resolve())
                and _safe_file_sha256(xml_path) == xml_ref.get('sha256')
            )
        except (OSError, ValueError, TypeError):
            xml_valid = False
    valid_pass = (
        previous.get('receipt_type') == 'modport.characterization_verification.v1'
        and previous.get('outcome') == 'passed'
        and previous.get('category') == 'none'
        and previous.get('scope') == 'selected'
        and previous.get('test_ids') == [test_id]
        and previous.get('case_execution_evidence') is True
        and previous.get('candidate_unchanged') is True
        and isinstance(test_results, list) and len(test_results) == 1
        and test_results[0].get('test_id') == test_id
        and test_results[0].get('outcome') == 'passed'
        and test_results[0].get('task') == registered_identity.get('gradle_task')
        and test_results[0].get('classname') == registered_identity.get('classname')
        and test_results[0].get('name') == registered_identity.get('name')
        and xml_valid
        and isinstance(bindings, list) and len(bindings) == 1
        and bindings[0].get('case_identity') == binding.get('case_identity')
        and isinstance(previous.get('candidate_after'), Mapping)
        and previous['candidate_after'].get('candidate_id') == candidate.get('candidate_id')
        and isinstance(digest, str) and digest == calculated
    )
    historical = {
        'path': str(ref['path']), 'sha256': ref['sha256'],
        'receipt_sha256': digest if isinstance(digest, str) else None,
        'candidate_id': (previous.get('candidate_after', {}).get('candidate_id')
                         if isinstance(previous.get('candidate_after'), Mapping) else None),
        'case_identity': (bindings[0].get('case_identity')
                          if isinstance(bindings, list) and len(bindings) == 1
                          and isinstance(bindings[0], Mapping) else None),
        'reused_for_current_candidate': bool(valid_pass),
    }
    if not valid_pass:
        return None, historical
    return dict(previous), historical


def _dynamic_contract_session(session: Mapping[str, Any], test_ids: list[str],
                              scope: str) -> dict[str, Any]:
    """Resolve authored contract identities at invocation, never from tool args."""
    if not session.get('dynamic_contract'):
        return dict(session)
    root = Path(session['root'])
    contract_path = project_path(root, session['contract_path'])
    if (contract_path.is_symlink() or not contract_path.is_file()
            or contract_path.resolve() != contract_path.absolute()
            or not is_run_path(root, contract_path)):
        raise AssertionIdentityError('current functional contract is missing or unsafe')
    raw = _read_bounded_file(contract_path, 32 * 1024 * 1024)
    try:
        contract = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AssertionIdentityError('current functional contract is malformed') from exc
    if not isinstance(contract, Mapping):
        raise AssertionIdentityError('current functional contract is not an object')
    from .author_contracts import normalize_characterization_contract
    try:
        contract = normalize_characterization_contract(contract)
    except (ValueError, TypeError) as exc:
        raise AssertionIdentityError('current functional contract mappings are invalid: ' + str(exc)) from exc
    task_values = contract.get('baseline_gradle_tasks')
    if (not isinstance(task_values, list) or not task_values
            or any(not _safe_gradle_task(task) for task in task_values)
            or len(task_values) != len(set(task_values))):
        raise AssertionIdentityError('current contract has no safe unique full diagnostic task list')
    task_paths = {':' + task.lstrip(':') for task in task_values}
    if scope == 'full_diagnostic':
        # This route supports legacy/custom launchers that cannot isolate one
        # JUnit assertion. It is diagnostic only and never produces case pass evidence.
        return {
            **dict(session), 'contract_sha256': sha256(raw).hexdigest(),
            'test_cases': {}, 'prior_case_receipts': {},
            'full_tasks': list(task_values),
        }
    declarations = contract.get('test_evidence')
    behaviors = contract.get('behaviors', contract.get('entries', ()))
    if not isinstance(declarations, Mapping) or not isinstance(behaviors, list):
        raise AssertionIdentityError('current contract has no test and assertion mappings')
    assertion_ids_by_test: dict[str, list[str]] = {}
    mapped_test_ids: list[str] = []
    seen_assertion_ids: set[str] = set()
    for behavior in behaviors:
        if not isinstance(behavior, Mapping):
            raise AssertionIdentityError('current behavior mapping is malformed')
        behavior_test_ids = behavior.get('test_mapping')
        assertion_texts = behavior.get('assertions')
        assertion_contracts = behavior.get('assertion_contracts')
        if (not isinstance(behavior_test_ids, list) or not behavior_test_ids
                or any(not isinstance(test_id, str) for test_id in behavior_test_ids)
                or len(behavior_test_ids) != len(set(behavior_test_ids))
                or not isinstance(assertion_texts, list) or not assertion_texts
                or any(not isinstance(text, str) or not text.strip() for text in assertion_texts)
                or len(assertion_texts) != len(set(assertion_texts))
                or not isinstance(assertion_contracts, list)
                or len(assertion_contracts) != len(assertion_texts)):
            raise AssertionIdentityError('current behavior has invalid assertion/test mappings')
        mapped_test_ids.extend(behavior_test_ids)
        local_assertion_texts: list[str] = []
        for assertion in assertion_contracts:
            if not isinstance(assertion, Mapping):
                raise AssertionIdentityError('current assertion mapping is malformed')
            assertion_id = assertion.get('assertion_id')
            assertion_text = assertion.get('text')
            mapped_ids = assertion.get('test_ids')
            if (not isinstance(assertion_id, str) or not assertion_id.strip()
                    or assertion_id in seen_assertion_ids
                    or not isinstance(assertion_text, str) or not assertion_text.strip()
                    or assertion_text not in assertion_texts
                    or not isinstance(mapped_ids, list) or not mapped_ids
                    or any(not isinstance(test_id, str) for test_id in mapped_ids)
                    or len(mapped_ids) != len(set(mapped_ids))
                    or any(test_id not in behavior_test_ids for test_id in mapped_ids)):
                raise AssertionIdentityError('current assertion identity or test mapping is invalid')
            seen_assertion_ids.add(assertion_id)
            local_assertion_texts.append(assertion_text)
            for test_id in mapped_ids:
                if not isinstance(test_id, str):
                    raise AssertionIdentityError('current assertion test ID is invalid')
                assertion_ids_by_test.setdefault(test_id, []).append(assertion_id)
        if len(local_assertion_texts) != len(set(local_assertion_texts)):
            raise AssertionIdentityError('current behavior has duplicate assertion text mappings')
        if set(test_id for assertion in assertion_contracts
               for test_id in assertion.get('test_ids', ())) != set(behavior_test_ids):
            raise AssertionIdentityError('current assertion test IDs do not exactly equal test_mapping')
    if (any(not isinstance(test_id, str) for test_id in mapped_test_ids)
            or len(mapped_test_ids) != len(set(mapped_test_ids))
            or set(declarations) != set(mapped_test_ids)
            or any(not re.fullmatch(r'[A-Za-z0-9_.:-]+', test_id)
                   for test_id in mapped_test_ids)):
        raise AssertionIdentityError('current contract test IDs are duplicated or declarations do not match mappings')
    cases: dict[str, dict[str, Any]] = {}
    identities: set[str] = set()
    for test_id in mapped_test_ids:
        declaration = declarations.get(test_id)
        identity = declaration.get('result_identity') if isinstance(declaration, Mapping) else None
        if (not isinstance(declaration, Mapping) or declaration.get('executor', 'junit') != 'junit'
                or not isinstance(identity, Mapping) or identity.get('kind') != 'junit_xml'
                or set(identity) != {'kind', 'gradle_task', 'classname', 'name'}
                or not _safe_gradle_task(identity.get('gradle_task'))
                or ':' + identity['gradle_task'].lstrip(':') not in task_paths
                or not isinstance(identity.get('classname'), str)
                or not re.fullmatch(r'[A-Za-z_$][A-Za-z0-9_$.]*', identity['classname'])
                or not isinstance(identity.get('name'), str)
                or not re.fullmatch(r'[A-Za-z_$][A-Za-z0-9_$]*', identity['name'])):
            raise AssertionIdentityError(f'current test {test_id!r} has no exact safe JUnit identity')
        canonical_identity = json.dumps({**identity,
            'gradle_task': ':' + identity['gradle_task'].lstrip(':')},
            sort_keys=True, separators=(',', ':'))
        if canonical_identity in identities:
            raise AssertionIdentityError('current contract reuses a JUnit result identity')
        identities.add(canonical_identity)
        source_files = declaration.get('test_source_files')
        if (not isinstance(source_files, list) or not source_files
                or any(not isinstance(path, str) or not path.startswith('.modport/')
                       or '\\' in path or '..' in Path(path).parts
                       or Path(path).suffix not in {'.java', '.kt', '.groovy'}
                       for path in source_files)):
            raise AssertionIdentityError(f'current test {test_id!r} has unsafe test source paths')
        cases[test_id] = {
            'result_identity': dict(identity),
            'assertion_ids': sorted(assertion_ids_by_test.get(test_id, [])),
            'test_source_files': list(source_files), 'executor': 'junit',
        }
    if any(test_id not in cases for test_id in test_ids):
        raise AssertionIdentityError('requested test ID is not mapped by the current contract')
    return {
        **dict(session), 'contract_sha256': sha256(raw).hexdigest(),
        'test_cases': {test_id: cases[test_id] for test_id in test_ids},
        'prior_case_receipts': {
            test_id: session.get('prior_case_receipts', {})[test_id]
            for test_id in test_ids if test_id in session.get('prior_case_receipts', {})
        },
        'full_tasks': list(task_values),
    }


def _refresh_characterization_wiring(session: Mapping[str, Any]) -> dict[str, Any]:
    root = Path(session['root'])
    workspace = Path(session['workspace'])
    from .harness_wiring import characterization_init_scripts
    wiring_directory = root / 'artifacts' / 'harness-wiring'
    refreshed = []
    for path in characterization_init_scripts(
            workspace, workflow_version=29, supplement_directory=wiring_directory):
        host_owned = path.parent == wiring_directory
        origin = 'wiring' if host_owned else 'workspace'
        base = wiring_directory if host_owned else workspace
        refreshed.append({
            'path': path.relative_to(base).as_posix(),
            'sha256': _safe_file_sha256(path), 'origin': origin,
        })
    if any(task.split(':')[-1] in {'runClient', 'runServer'}
           for task in session.get('full_tasks', ())) and not refreshed:
        raise ValueError('client or server full diagnostic requires host-registered init wiring')
    return {**dict(session), 'init_scripts': refreshed}


def _wiring_unchanged(session: Mapping[str, Any]) -> bool:
    root = Path(session['root'])
    workspace = Path(session['workspace'])
    try:
        for item in session['init_scripts']:
            base = workspace if item['origin'] == 'workspace' else root / 'artifacts' / 'harness-wiring'
            source = base / item['path']
            if (source.is_symlink() or not source.is_file()
                    or source.resolve() != source.absolute()
                    or not source.resolve().is_relative_to(base.resolve())
                    or _safe_file_sha256(source) != item['sha256']):
                return False
    except (OSError, ValueError, TypeError, KeyError):
        return False
    return True


def _git_head(workspace: Path) -> str:
    result = git_probe(
        ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
         '-C', str(workspace), 'rev-parse', 'HEAD'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent',
             'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
             'GIT_OPTIONAL_LOCKS': '0', 'GIT_TERMINAL_PROMPT': '0',
             'GIT_NO_REPLACE_OBJECTS': '1'},
        timeout=30, check=False,
    )
    head = result.stdout.decode('ascii', errors='ignore').strip()
    if result.returncode or not re.fullmatch(r'[0-9a-f]{40,64}', head):
        raise AssertionIdentityError('cannot authenticate the original source commit')
    return head


def _read_bounded_file(path: Path, limit: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise AssertionIdentityError('assertion source anchor exceeds the file size limit')
        chunks = []
        total = 0
        while block := os.read(descriptor, min(1024 * 1024, limit + 1 - total)):
            total += len(block)
            if total > limit:
                raise AssertionIdentityError('assertion source anchor exceeds the file size limit')
            chunks.append(block)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise AssertionIdentityError('assertion source anchor changed while it was read')
        return b''.join(chunks)
    finally:
        os.close(descriptor)


def _result_tree_entries(workspace: Path):
    """Yield bounded entries under workspace and Gradle test-result trees."""
    stack: list[tuple[Path, int, str]] = [(workspace, 0, 'workspace')]
    entry_count = 0
    while stack:
        current, depth, mode = stack.pop()
        if depth > MAX_RESULT_TREE_DEPTH:
            raise ValueError('test-result directory tree exceeds the depth limit')
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    entry_count += 1
                    if entry_count > MAX_RESULT_TREE_ENTRIES:
                        raise ValueError('test-result directory tree exceeds the entry limit')
                    path = Path(entry.path)
                    if entry.is_symlink():
                        raise ValueError('candidate contains a symbolic link while locating test results')
                    is_directory = entry.is_dir(follow_symlinks=False)
                    relative = path.relative_to(workspace)
                    if mode == 'workspace':
                        if not is_directory:
                            continue
                        if entry.name in _GENERATED_PARTS and entry.name != 'build':
                            continue
                        if entry.name == 'build':
                            stack.append((path, depth + 1, 'build'))
                        else:
                            stack.append((path, depth + 1, 'workspace'))
                    elif mode == 'build':
                        if is_directory and entry.name == 'test-results':
                            stack.append((path, depth + 1, 'results'))
                    else:
                        yield path, relative.as_posix(), is_directory
                        if is_directory:
                            stack.append((path, depth + 1, 'results'))
        except OSError as exc:
            raise ValueError(f'test-result directory could not be inspected: {exc}') from exc


def _clear_junit_results(
    workspace: Path, *, validated_tasks: list[str] | tuple[str, ...],
) -> list[str]:
    """Clear only after the exact Gradle task selection is host validated."""
    if (not isinstance(validated_tasks, (list, tuple)) or not validated_tasks
            or any(not _safe_gradle_task(task) for task in validated_tasks)):
        raise ValueError('JUnit cleanup requires validated Gradle task selection')
    workspace = Path(workspace)
    if workspace.is_symlink() or not workspace.is_dir() or workspace.resolve() != workspace.absolute():
        raise ValueError('candidate workspace is unsafe while locating test results')
    candidates: list[Path] = []
    for path, relative, is_directory in _result_tree_entries(workspace):
        if not is_directory and path.suffix.lower() == '.xml':
            if len(candidates) >= MAX_JUNIT_XML_FILES:
                raise ValueError('JUnit result output exceeds the file count limit')
            if path.is_symlink() or not path.is_file():
                raise ValueError('JUnit result output is not a regular file')
            candidates.append(path)
    removed = [path.relative_to(workspace).as_posix() for path in candidates]
    for path in candidates:
        path.unlink()
    return removed


def _read_junit_result(workspace: Path, test_id: str,
                       identity: Mapping[str, Any], *,
                       require_isolated: bool = True,
                       include_xml_digest: bool = True) -> dict[str, Any]:
    task_name = str(identity['gradle_task']).split(':')[-1]
    project_segments = [segment for segment in str(identity['gradle_task']).split(':') if segment]
    if len(project_segments) != 1:
        raise ValueError('JUnit result binding for a subproject task is unsupported without a project-directory map')
    matches: list[dict[str, Any]] = []
    task_case_count = 0
    scanned = 0
    total = 0
    for path, relative, is_directory in _result_tree_entries(workspace):
        if is_directory or path.suffix.lower() != '.xml':
            continue
        if Path(relative).parts[:-1] != ('build', 'test-results', task_name):
            continue
        scanned += 1
        if scanned > MAX_JUNIT_XML_FILES:
            raise ValueError('JUnit result output exceeds the file count limit')
        if path.is_symlink() or not path.is_file():
            raise ValueError('JUnit result output is unsafe')
        try:
            data = _read_bounded_file(path, MAX_JUNIT_XML_FILE_BYTES)
        except OSError as exc:
            raise ValueError('JUnit result output could not be read safely') from exc
        total += len(data)
        if total > MAX_JUNIT_XML_TOTAL_BYTES:
            raise ValueError('JUnit result output exceeds the size limit')
        if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
            raise ValueError('JUnit result output contains a forbidden XML declaration')
        try:
            root_element = ElementTree.fromstring(data)
        except ElementTree.ParseError as exc:
            raise ValueError(f'JUnit result XML is malformed: {exc}') from exc
        if root_element.tag.rsplit('}', 1)[-1] not in {'testsuite', 'testsuites'}:
            raise ValueError('JUnit result XML must have a testsuite or testsuites root')
        for case in root_element.iter():
            if case.tag.rsplit('}', 1)[-1] != 'testcase':
                continue
            task_case_count += 1
            if (case.get('classname') != identity['classname']
                    or case.get('name') != identity['name']):
                continue
            failure = next((child for child in case if child.tag.rsplit('}', 1)[-1] == 'failure'), None)
            error = next((child for child in case if child.tag.rsplit('}', 1)[-1] == 'error'), None)
            skipped = next((child for child in case if child.tag.rsplit('}', 1)[-1] == 'skipped'), None)
            outcome = ('error' if error is not None else
                       'failed' if failure is not None else
                       'skipped' if skipped is not None else 'passed')
            matches.append({
                'test_id': test_id, 'outcome': outcome,
                'classname': case.get('classname'), 'name': case.get('name'),
                'task': identity['gradle_task'],
                'xml_path': relative,
                **({'xml_sha256': sha256(data).hexdigest()} if include_xml_digest else {}),
                'failure_type': failure.get('type') if failure is not None else None,
                'failure_message': (failure.get('message') or '')[:2000] if failure is not None else None,
                'error_type': error.get('type') if error is not None else None,
                'error_message': (error.get('message') or '')[:2000] if error is not None else None,
            })
    if len(matches) != 1:
        raise ValueError(f'exact JUnit result identity for {test_id!r} matched {len(matches)} results')
    if require_isolated and task_case_count != 1:
        raise ValueError(f'selected Gradle task emitted {task_case_count} test results; selection was not isolated')
    matches[0]['task_testcase_count'] = task_case_count
    return matches[0]


def _archive_junit_result(root: Path, workspace: Path, result: Mapping[str, Any],
                          receipt_path: Path) -> dict[str, str]:
    relative = result.get('xml_path')
    expected_digest = result.get('xml_sha256')
    if (not isinstance(relative, str) or not isinstance(expected_digest, str)
            or not re.fullmatch(r'[0-9a-f]{64}', expected_digest)):
        raise ValueError('JUnit XML result has no exact source identity')
    source = workspace / relative
    if (source.is_symlink() or not source.is_file()
            or source.resolve() != source.absolute()
            or not source.resolve().is_relative_to(workspace)):
        raise ValueError('JUnit XML result path is unsafe')
    data = _read_bounded_file(source, MAX_JUNIT_XML_FILE_BYTES)
    actual = sha256(data).hexdigest()
    if actual != expected_digest:
        raise ValueError('JUnit XML result changed before host archival')
    directory = receipt_path.parent / 'junit-results'
    if directory.is_symlink() or directory.resolve() != directory.absolute():
        raise ValueError('JUnit result archive directory is unsafe')
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / (receipt_path.stem + '.xml')
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return {'path': project_relative(root, destination).as_posix(),
            'sha256': actual}


def _run_characterization(session: Mapping[str, Any], arguments: Mapping[str, Any]) -> dict[str, Any]:
    root = Path(session['root'])
    workspace = Path(session['workspace'])
    scope = arguments.get('scope', 'selected')
    if scope not in {'selected', 'full_diagnostic'}:
        raise ValueError('scope must be selected or full_diagnostic')
    if scope == 'selected':
        ids = arguments.get('test_ids')
        if (not isinstance(ids, list) or len(ids) != 1
                or not isinstance(ids[0], str)
                or (not session.get('dynamic_contract') and ids[0] not in session['test_cases'])
                or not re.fullmatch(r'[A-Za-z0-9_.:-]+', ids[0])):
            raise ValueError('selected scope requires exactly one registered test ID')
    else:
        ids = [] if session.get('dynamic_contract') else list(session['test_cases'])
    invocation_id = uuid4().hex
    execution_nonce = sha256(
        f"{session['execution_nonce']}:{invocation_id}".encode('utf-8')
    ).hexdigest()
    receipt_path = root / 'artifacts' / 'executions' / session['command_id'] \
        / 'opencode-characterization' / (invocation_id + '.json')
    receipt_directory = receipt_path.parent
    if receipt_directory.is_symlink() or receipt_directory.resolve() != receipt_directory.absolute():
        raise ValueError('characterization receipt directory is unsafe')
    receipt_directory.mkdir(parents=True, exist_ok=True)
    prior_receipts = [path for path in receipt_directory.glob('*.json')
                      if path.name not in {'session.json', 'latest-receipt.json', 'contract-input.json'}]
    if len(prior_receipts) >= MAX_CHARACTERIZATION_INVOCATIONS:
        raise ValueError('characterization session reached its invocation limit')
    started_at = time.time()
    receipt: dict[str, Any] = {
        'schema_version': 1, 'receipt_type': 'modport.characterization_verification.v1',
        'command_id': session['command_id'], 'invocation_id': invocation_id,
        'scope': scope, 'test_ids': list(ids), 'source_commit': session['source_commit'],
        'contract_sha256': session.get('contract_sha256'),
        'execution_nonce': execution_nonce, 'started_at': started_at,
        'timeout_seconds': session['max_invocation_seconds'],
        'case_execution_evidence': False,
    }
    try:
        # Authoring contracts are created after the OpenCode session starts.
        # Resolve them at the host tool invocation, inside the receipt boundary,
        # so malformed declarations still produce an immutable typed result.
        session = _dynamic_contract_session(session, ids, scope)
        session = _refresh_characterization_wiring(session)
        before = _workspace_candidate_identity(workspace)
        receipt['candidate_before'] = before
        if scope == 'full_diagnostic':
            ids = []
        receipt['contract_sha256'] = session['contract_sha256']
        receipt['test_ids'] = list(ids)
        bindings = []
        prior_evidence: list[dict[str, Any]] = []
        reusable_prior: dict[str, Any] | None = None
        for test_id in ids:
            if scope == 'selected':
                if session['test_cases'][test_id].get('executor') != 'junit':
                    receipt.update({'outcome': 'selection_unsupported',
                                    'category': 'test_infrastructure',
                                    'diagnostic': 'selected verification currently requires a JUnit XML executor'})
                    break
                identity = session['test_cases'][test_id]['result_identity']
                if identity['gradle_task'].split(':')[-1] in {'runClient', 'runServer'}:
                    receipt.update({'outcome': 'selection_unsupported',
                                    'category': 'test_infrastructure',
                                    'diagnostic': 'client/server launch tasks cannot isolate one testcase'})
                    break
                project_segments = [segment for segment in identity['gradle_task'].split(':') if segment]
                if len(project_segments) != 1:
                    receipt.update({'outcome': 'selection_unsupported',
                                    'category': 'test_infrastructure',
                                    'diagnostic': 'subproject JUnit selection requires a host project-directory map'})
                    break
                bindings.append(_current_case_binding(session, test_id))
                try:
                    previous, historical = _verified_prior_case_receipt(
                        session, test_id, bindings[-1], before,
                    )
                except AssertionIdentityError as exc:
                    previous = None
                    historical = {
                        'path': session.get('prior_case_receipts', {}).get(test_id, {}).get('path'),
                        'sha256': session.get('prior_case_receipts', {}).get(test_id, {}).get('sha256'),
                        'reused_for_current_candidate': False,
                        'diagnostic': str(exc)[:1000],
                    }
                if historical is not None:
                    prior_evidence.append(historical)
                if previous is not None:
                    reusable_prior = previous
        if receipt.get('outcome') == 'selection_unsupported':
            pass
        elif reusable_prior is not None:
            receipt.update({
                'candidate_after': before,
                'candidate_unchanged': True,
                'case_bindings': bindings,
                'test_results': reusable_prior['test_results'],
                'prior_version_evidence': prior_evidence,
                'outcome': 'passed', 'category': 'none',
                'case_execution_evidence': True,
                'evidence_source': 'authenticated_exact_candidate_carry_forward',
            })
        else:
            if time.time() >= session['deadline_epoch']:
                raise TimeoutError('assignment deadline is exhausted')
            tasks: list[str] = []
            selected_task: str | None = None
            if scope == 'selected':
                identity = session['test_cases'][ids[0]]['result_identity']
                selected_task = identity['gradle_task']
                tasks.append(selected_task)
            else:
                tasks.extend(session['full_tasks'])
            if scope == 'selected' or not session.get('dynamic_contract'):
                # A game harness may be JavaExec rather than Gradle Test.
                # Test-only CLI flags cannot select those tasks. Use the same
                # host filter as formal verification plus the harness ID env.
                from .test_selection_execution import build_selected_test_execution
                selection_ids = list(ids) if scope == 'selected' else list(session['test_cases'])
                execution = build_selected_test_execution({'test_evidence': {
                    test_id: {'evidence_kind': 'runtime', **case}
                    for test_id, case in session['test_cases'].items()}}, selection_ids)
                tasks = list(execution.gradle_tasks)
                wiring_directory = root / 'artifacts' / 'harness-wiring'
                if wiring_directory.resolve() != wiring_directory.absolute():
                    raise ValueError('selection wiring directory is unsafe')
                wiring_directory.mkdir(parents=True, exist_ok=True)
                selector = wiring_directory / (session['command_id'] + '-' + invocation_id + '-selected-tests.init.gradle')
                with selector.open('x', encoding='utf-8') as output:
                    output.write(execution.gradle_init_script)
                selector_digest = _safe_file_sha256(selector)
                session = {**session, 'init_scripts': [*session['init_scripts'], {
                    'path': selector.name, 'sha256': selector_digest, 'origin': 'wiring'}]}
                receipt['selection_wiring_ref'] = {
                    'path': project_relative(root, selector).as_posix(), 'sha256': selector_digest}
            removed = _clear_junit_results(
                workspace, validated_tasks=tasks,
            )
            receipt['cleared_prior_junit_results'] = removed
            receipt['prior_version_evidence'] = prior_evidence
            from .handlers import _sandboxed_build_command
            args = ['/workspace/gradlew', '--no-daemon', '--rerun-tasks']
            for init in session['init_scripts']:
                sandbox_path = ('/modport-wiring/' + init['path'] if init['origin'] == 'wiring'
                                else '/workspace/' + init['path'])
                args.extend(['--init-script', sandbox_path])
            args.extend(['-Pmodport.characterization=true', '-Dmodport.characterization=true'])
            gradle_args = ['bash', *args, *tasks]
            actual_timeout = min(float(session['max_invocation_seconds']),
                                 max(0.0, session['deadline_epoch'] - time.time()))
            if actual_timeout <= 0:
                raise TimeoutError('assignment deadline is exhausted')
            sandbox_argv = _sandboxed_build_command(
                root, workspace, gradle_args,
                cache_name='characterization-cache',
                environment={
                    'MODPORT_EXECUTION_ID': session['command_id'],
                    'MODPORT_EVIDENCE_NONCE': execution_nonce,
                    **({'MODPORT_SELECTED_TEST_IDS': json.dumps(
                        ids if scope == 'selected' else list(session['test_cases']))}
                       if scope == 'selected' or not session.get('dynamic_contract') else {}),
                },
            )
            output = _bounded_command(sandbox_argv, workspace, actual_timeout)
            receipt.update({
                'argv': gradle_args,
                'exit_code': output.get('exit_code'),
                'timed_out': bool(output.get('timed_out')),
                'stdout': output.get('stdout', ''), 'stderr': output.get('stderr', ''),
                'stdout_truncated': output.get('stdout_truncated', False),
                'stderr_truncated': output.get('stderr_truncated', False),
            })
            results = []
            if scope == 'selected':
                try:
                    result = _read_junit_result(workspace, ids[0],
                        session['test_cases'][ids[0]]['result_identity'])
                    result['xml_artifact_ref'] = _archive_junit_result(
                        root, workspace, result, receipt_path,
                    )
                    results.append(result)
                except (OSError, ValueError, ElementTree.ParseError) as exc:
                    receipt['result_error'] = str(exc)
            after = _workspace_candidate_identity(workspace)
            receipt['candidate_after'] = after
            receipt['wiring_unchanged'] = _wiring_unchanged(session)
            receipt['candidate_unchanged'] = (
                before['candidate_id'] == after['candidate_id']
                and receipt['wiring_unchanged'] is True
            )
            receipt['case_bindings'] = bindings
            receipt['test_results'] = results
            if not receipt['candidate_unchanged']:
                receipt.update({'outcome': 'candidate_changed_during_verification', 'category': 'unknown'})
            elif output.get('timed_out') or output.get('exit_code') is None:
                receipt.update({'outcome': 'timeout', 'category': 'test_infrastructure'})
            elif scope == 'full_diagnostic':
                receipt.update({'outcome': 'diagnostic_complete' if output.get('exit_code') == 0 else 'diagnostic_failed',
                                'category': 'unknown'})
            elif output.get('exit_code') != 0:
                failure_xml = bool(results and results[0].get('outcome') == 'failed')
                if failure_xml and bindings and session.get('assertions_reviewed') is True and workspace != root / 'baseline':
                    receipt.update({'outcome': 'failed_assertion', 'category': 'mod_behavior'})
                elif failure_xml and bindings:
                    receipt.update({'outcome': 'failed_assertion_expectation_unverified',
                                    'category': 'unknown'})
                else:
                    receipt.update({'outcome': 'execution_failed', 'category': 'test_infrastructure'})
            elif results and results[0].get('outcome') == 'passed' and bindings:
                receipt.update({'outcome': 'passed', 'category': 'none',
                                'case_execution_evidence': True})
            elif results and results[0].get('outcome') == 'failed' and bindings:
                if session.get('assertions_reviewed') is True and workspace != root / 'baseline':
                    receipt.update({'outcome': 'failed_assertion', 'category': 'mod_behavior'})
                else:
                    receipt.update({'outcome': 'failed_assertion_expectation_unverified',
                                    'category': 'unknown'})
            else:
                receipt.update({'outcome': 'missing_or_skipped_test_result',
                                'category': 'test_infrastructure'})
        receipt.setdefault('candidate_unchanged', None)
    except AssertionIdentityError as exc:
        receipt.update({'outcome': 'invalid_assertion_or_source_anchor',
                        'category': 'assertion_invalid', 'diagnostic': str(exc)[:2000]})
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, json.JSONDecodeError,
            TimeoutError, subprocess.SubprocessError) as exc:
        receipt.update({'outcome': 'verification_unavailable',
                        'category': 'test_infrastructure',
                        'diagnostic': f'{type(exc).__name__}: {str(exc)[:2000]}'})
    receipt['finished_at'] = time.time()
    receipt['receipt_sha256'] = sha256(json.dumps(receipt, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode('utf-8')).hexdigest()
    from .evidence import atomic_json
    atomic_json(receipt_path, receipt)
    relative = project_relative(root, receipt_path).as_posix()
    try:
        atomic_json(receipt_path.parent / 'latest-receipt.json', {
            'schema_version': 1, 'command_id': session['command_id'], 'receipt': receipt_path.name,
            'receipt_sha256': receipt['receipt_sha256'],
        })
        marker_written = True
    except OSError:
        marker_written = False
    return {'receipt_path': relative, 'receipt_sha256': receipt['receipt_sha256'],
            'activity_marker_written': marker_written, **receipt}


def _response(rpc_id: Any, *, result: Any = None, error: Mapping | None = None) -> None:
    value = {'jsonrpc': '2.0', 'id': rpc_id}
    if error is None:
        value['result'] = result
    else:
        value['error'] = dict(error)
    sys.stdout.write(json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n')
    sys.stdout.flush()


def _tool_result(value: Mapping[str, Any], *, failed: bool = False) -> dict:
    return {'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False)}],
            'structuredContent': dict(value), 'isError': failed}


def _direct_gradle_wrapper(command: str) -> bool:
    """Recognize plain Wrapper argv; complex shell programs use the old path."""
    if re.search(r'[;&|<>`$\r\n]', command):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if not words:
        return False
    wrapper = {'./gradlew', '/workspace/gradlew'}
    if words[0] in wrapper:
        return True
    return (len(words) >= 2 and words[0] in {
        'sh', '/bin/sh', 'bash', '/bin/bash',
    } and words[1] in wrapper | {'gradlew'})


def _seed_wrapper_for_project_command(session: Mapping[str, Any], command: str,
                                      timeout_seconds: float) -> dict[str, Any] | None:
    """Prepare an official Wrapper ZIP before a v26+ sandboxed Gradle call.

    The shell text only decides whether to try the optimization. The URL and
    cache root come from the project Wrapper and the host's frozen Run input;
    the existing cache verifies the official checksum before materialization.
    """
    if not _direct_gradle_wrapper(command):
        return None
    root = Path(session['root'])
    from .artifact_handoff import _read_json_snapshot
    header, _ = _read_json_snapshot(root / 'run.json', limit=16 * 1024 * 1024)
    if (header.get('format_version') != 2
            or header.get('run_dir') != str(root)
            or not isinstance(header.get('run_id'), str)
            or not session['command_id'].startswith(header['run_id'] + ':')):
        raise ValueError('frozen Run identity does not match the sandbox command')
    definition = header.get('definition')
    if not isinstance(definition, Mapping):
        raise ValueError('frozen workflow definition is unavailable')
    version = definition.get('workflow_version')
    if type(version) is not int:
        raise ValueError('frozen workflow version is unavailable')
    if version < 26:
        return None
    request = header.get('request')
    if not isinstance(request, Mapping):
        raise ValueError('frozen Run request is unavailable')
    from .handlers import _seed_gradle_wrapper_from_request
    return _seed_gradle_wrapper_from_request(
        request, root, Path(session['workspace']), 'gradle-cache',
        timeout_seconds=min(300.0, timeout_seconds),
    )


def _run(session: Mapping[str, Any], command: str, timeout_seconds: int) -> dict:
    root = Path(session['root'])
    workspace = Path(session['workspace'])
    if (not root.is_dir() or root.is_symlink() or root.resolve() != root
            or not workspace.is_dir() or workspace.is_symlink() or workspace.resolve() != workspace
            or not is_project_workspace(root, workspace)):
        raise ValueError('sandbox tool workspace identity is invalid')
    _validate_scope(root, workspace, session['command_id'])
    if not isinstance(command, str) or not command or '\x00' in command or len(command) > 20000:
        raise ValueError('command must be a bounded non-empty string')
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_COMMAND_SECONDS:
        raise ValueError(f'timeout_seconds must be between 1 and {MAX_COMMAND_SECONDS}')
    remaining = session['deadline_epoch'] - time.time()
    if remaining <= 0:
        raise TimeoutError('assignment deadline is exhausted')
    actual_timeout = min(timeout_seconds, remaining)
    command_deadline = time.monotonic() + actual_timeout
    wrapper_cache = _seed_wrapper_for_project_command(session, command, actual_timeout)
    actual_timeout = min(command_deadline - time.monotonic(),
                         session['deadline_epoch'] - time.time())
    if actual_timeout <= 0:
        raise TimeoutError('project command deadline exhausted before sandbox execution')
    from .handlers import _sandboxed_build_command
    argv = _sandboxed_build_command(root, workspace, ['/bin/sh', '-lc', command],
                                    readonly_workspace=bool(session.get('read_only')),
                                    timeout_seconds=actual_timeout)
    actual_timeout = min(command_deadline - time.monotonic(),
                         session['deadline_epoch'] - time.time())
    if actual_timeout <= 0:
        raise TimeoutError('project command deadline exhausted before sandbox execution')
    execution_id = uuid4().hex
    output = _bounded_command(argv, workspace, actual_timeout)
    from .telemetry import redact
    redacted_command = redact(command)
    record = {'execution_id': execution_id,
              'request_command_id': session['command_id'],
              'command_sha256': sha256(command.encode()).hexdigest(),
              'command_redacted': redacted_command[:4000],
              'command_truncated': len(redacted_command) > 4000,
              'workspace': str(project_relative(root, workspace)), 'timeout_seconds': actual_timeout,
              **output}
    if wrapper_cache is not None:
        record['wrapper_distribution_cache'] = wrapper_cache
    from .evidence import atomic_json
    artifact = root / 'artifacts' / 'executions' / session['command_id'] / 'opencode-shell' / (execution_id + '.json')
    atomic_json(artifact, record)
    # A fixed-size pointer lets the independent monitor observe the newest
    # completed tool call without scanning an unbounded execution directory.
    try:
        atomic_json(artifact.parent / 'latest-receipt.json', {
            'schema_version': 1, 'command_id': session['command_id'],
            'receipt': artifact.name,
        })
        activity_marker_written = True
    except OSError:
        # The command already ran and its full receipt is durable. Do not make
        # the agent repeat a side effect only because observation metadata failed.
        activity_marker_written = False
    return {'artifact_path': project_relative(root, artifact).as_posix(),
            'activity_marker_written': activity_marker_written, **record}


def _read_run_artifact(session: Mapping[str, Any], path: str,
                       offset: int = 0, limit: int = 65536,
                       expected_sha256: str | None = None) -> dict[str, Any]:
    """Read a bounded slice of host evidence without granting file tool access."""
    root = Path(session['root'])
    artifacts = root / 'artifacts'
    if (not root.is_dir() or root.is_symlink() or root.resolve() != root
            or not artifacts.is_dir() or artifacts.is_symlink()
            or artifacts.resolve() != artifacts):
        raise ValueError('Run artifact root is unsafe')
    if not isinstance(path, str) or not path or '\x00' in path or len(path) > 8192:
        raise ValueError('artifact path must be a bounded non-empty string')
    if type(offset) is not int or offset < 0:
        raise ValueError('artifact offset must be nonnegative')
    if type(limit) is not int or not 1 <= limit <= 131072:
        raise ValueError('artifact limit must be between 1 and 131072 bytes')
    if (expected_sha256 is not None
            and (not isinstance(expected_sha256, str)
                 or not re.fullmatch(r'[0-9a-fA-F]{64}', expected_sha256))):
        raise ValueError('expected artifact SHA-256 must be 64 hex characters')
    target = Path(path) if Path(path).is_absolute() else root / path
    if not target.is_relative_to(artifacts):
        raise ValueError('artifact path is outside the Run evidence')
    relative = target.relative_to(artifacts)
    if not relative.parts or any(part in {'.', '..'} for part in relative.parts):
        raise ValueError('artifact path is unsafe')
    if time.time() >= session['deadline_epoch']:
        raise TimeoutError('assignment deadline is exhausted')
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    with ExitStack() as stack:
        descriptor = os.open(root, directory_flags)
        stack.callback(os.close, descriptor)
        descriptor = os.open('artifacts', directory_flags, dir_fd=descriptor)
        stack.callback(os.close, descriptor)
        for component in relative.parts[:-1]:
            descriptor = os.open(component, directory_flags, dir_fd=descriptor)
            stack.callback(os.close, descriptor)
        source = os.open(relative.parts[-1], file_flags, dir_fd=descriptor)
        stack.callback(os.close, source)
        details = os.fstat(source)
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise ValueError('artifact must be a regular, unlinked file')
        total = details.st_size
        if offset > total:
            raise ValueError('artifact offset exceeds file size')
        if expected_sha256 is not None:
            digest = sha256()
            position = 0
            while position < total:
                if time.time() >= session['deadline_epoch']:
                    raise TimeoutError('assignment deadline exhausted while verifying artifact')
                block = os.pread(source, min(131072, total - position), position)
                if not block:
                    raise OSError('artifact changed while verifying SHA-256')
                digest.update(block)
                position += len(block)
            if digest.hexdigest() != expected_sha256.lower():
                raise ValueError('artifact SHA-256 does not match expected digest')
        chunk = os.pread(source, limit, offset)
        if expected_sha256 is not None:
            after = os.fstat(source)
            if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
                    details.st_size, details.st_mtime_ns, details.st_ctime_ns):
                raise OSError('artifact changed while verifying SHA-256')
        try:
            content = chunk.decode('utf-8')
        except UnicodeDecodeError as exc:
            if (offset + len(chunk) < total and exc.reason == 'unexpected end of data'
                    and exc.end == len(chunk)):
                if exc.start == 0:
                    raise ValueError('artifact limit is too small for the next UTF-8 character')
                chunk = chunk[:exc.start]
                content = chunk.decode('utf-8')
            else:
                if expected_sha256 is not None:
                    raise ValueError('verified artifact slice is not valid UTF-8 at this offset')
                content = chunk.decode('utf-8', errors='replace')
    if time.time() >= session['deadline_epoch']:
        raise TimeoutError('assignment deadline exhausted while reading artifact')
    return {'path': project_relative(root, target).as_posix(),
            'chunk_sha256': sha256(chunk).hexdigest(),
            'offset': offset, 'next_offset': offset + len(chunk),
            'total_bytes': total, 'content_utf8': content,
            **({'verified_sha256': expected_sha256.lower()}
               if expected_sha256 is not None else {})}


def _bounded_command(argv: list[str], workspace: Path, timeout: float) -> dict[str, Any]:
    """Drain output with fixed memory and terminate the whole command group."""
    limit = 32768
    if os.name == 'nt':
        from .platform_runtime import capture_process, terminate_tree
        capture = capture_process(argv, cwd=workspace, timeout=timeout,
                                  max_output_bytes=limit)
        return {'exit_code': None if capture.timed_out else capture.returncode,
                'stdout': capture.stdout.decode('utf-8', errors='replace'),
                'stderr': capture.stderr.decode('utf-8', errors='replace'),
                'stdout_truncated': capture.stdout_truncated,
                'stderr_truncated': capture.stderr_truncated,
                **({'timed_out': True} if capture.timed_out else {})}
    tails = {'stdout': bytearray(), 'stderr': bytearray()}
    sizes = {'stdout': 0, 'stderr': 0}
    process = subprocess.Popen(argv, cwd=workspace, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    deadline = time.monotonic() + timeout
    timed_out = False
    with selectors.DefaultSelector() as selector:
        assert process.stdout is not None and process.stderr is not None
        selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
        selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                for key, _ in selector.select(timeout=min(remaining, 0.5)):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    name = key.data
                    sizes[name] += len(chunk)
                    tails[name].extend(chunk)
                    if len(tails[name]) > limit:
                        del tails[name][:-limit]
            if not timed_out:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                else:
                    try:
                        process.wait(timeout=remaining)
                    except subprocess.TimeoutExpired:
                        timed_out = True
        finally:
            # An exited shell can leave descendants with inherited pipe FDs.
            # Kill its process group even after the parent exits.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            for key in list(selector.get_map().values()):
                selector.unregister(key.fileobj)
                key.fileobj.close()
    result = {'exit_code': None if timed_out else process.returncode,
              'stdout': bytes(tails['stdout']).decode('utf-8', errors='replace'),
              'stderr': bytes(tails['stderr']).decode('utf-8', errors='replace'),
              'stdout_truncated': sizes['stdout'] > limit,
              'stderr_truncated': sizes['stderr'] > limit}
    if timed_out:
        result['timed_out'] = True
    return result


def _dispatch(session: Mapping[str, Any], request: Mapping[str, Any]) -> None:
    method = request.get('method')
    rpc_id = request.get('id')
    if method == 'notifications/initialized' or rpc_id is None:
        return
    if method == 'initialize':
        requested = request.get('params', {}).get('protocolVersion')
        _response(rpc_id, result={'protocolVersion': requested or PROTOCOL_VERSION,
                                  'capabilities': {'tools': {'listChanged': False}},
                                  'serverInfo': {'name': ('modport_characterization'
                                      if session.get('kind') == 'characterization'
                                      else 'modport_sandbox'), 'version': '1'}})
        return
    if method == 'ping':
        _response(rpc_id, result={})
        return
    if method == 'tools/list':
        available = [{
            'name': 'run_project_command',
            'description': 'Run a project command in the credential-free ModPort sandbox. '
                           'Only /workspace and declared support mounts are visible. '
                           'Read host Run artifacts with read_run_artifact before invoking this tool; '
                           'host absolute paths are unavailable to project commands. '
                           'Returns the exit code and an artifact path; namespace errors are failures.',
            'inputSchema': {'type': 'object', 'properties': {
                'command': {'type': 'string'},
                'timeout_seconds': {'type': 'integer', 'minimum': 1,
                                    'maximum': MAX_COMMAND_SECONDS}},
                'required': ['command'], 'additionalProperties': False},
        }, {
            'name': 'read_run_artifact',
            'description': 'Read a bounded UTF-8 slice of a host-owned Run artifact under '
                           'artifacts/. Use this for task-instructions.json, input.json, rules, '
                           'and other host evidence. Pass expected_sha256 to make the host verify '
                           'the complete file before returning each slice; continue at next_offset. '
                           'This tool cannot write files.',
            'inputSchema': {'type': 'object', 'properties': {
                'path': {'type': 'string'},
                'offset': {'type': 'integer', 'minimum': 0},
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 131072},
                'expected_sha256': {'type': 'string', 'pattern': '^[0-9a-fA-F]{64}$'}},
                'required': ['path'], 'additionalProperties': False},
        }]
        if session.get('allow_project_command') is False:
            available = [tool for tool in available
                         if tool['name'] != 'run_project_command']
        if session.get('kind') == 'characterization':
            test_id_schema = ({'type': 'string', 'pattern': '^[A-Za-z0-9_.:-]+$'}
                              if session.get('dynamic_contract') else
                              {'type': 'string', 'enum': sorted(session['test_cases'])})
            available = [{
                'name': 'verify_characterization',
                'description': 'Run one host-registered JUnit testcase with the current candidate identity, '
                               'registered characterization init wiring, host nonce and deadline. The host '
                               'writes a typed immutable receipt. A full_diagnostic scope runs the registered '
                               'suite through the same wiring but never creates per-case acceptance evidence. '
                               'No shell command or task name is accepted from the caller.',
                'inputSchema': {'type': 'object', 'properties': {
                    'scope': {'type': 'string', 'enum': ['selected', 'full_diagnostic']},
                    'test_ids': {'type': 'array', 'minItems': 1, 'maxItems': 1,
                                 'items': test_id_schema}},
                    'required': ['scope'], 'additionalProperties': False},
            }]
        if session.get('kind') == 'artifact_compile':
            available = [{'name': 'compile_artifact_harness',
                'description': 'Compile harness and JUnit sources against the delivered binary with host-owned '
                    'target Java, dependency and binary-only init wiring in the credential-free sandbox. '
                    'Return raw compiler diagnostics; no gameplay executes and no acceptance is granted.',
                'inputSchema': {'type': 'object', 'properties': {
                    'timeout_seconds': {'type': 'integer', 'minimum': 1, 'maximum': MAX_COMMAND_SECONDS}},
                    'additionalProperties': False}}]
        _response(rpc_id, result={'tools': available})
        return
    if method != 'tools/call':
        _response(rpc_id, error={'code': -32601, 'message': 'Method not found'})
        return
    params = request.get('params')
    if not isinstance(params, dict) or params.get('name') not in {
            'run_project_command', 'read_run_artifact', 'verify_characterization', 'compile_artifact_harness'}:
        _response(rpc_id, error={'code': -32602, 'message': 'Invalid tool call'})
        return
    if (params['name'] == 'verify_characterization'
            and session.get('kind') != 'characterization'):
        _response(rpc_id, error={'code': -32602, 'message': 'Characterization verification is disabled by host policy'})
        return
    if ((params['name'] == 'compile_artifact_harness') != (session.get('kind') == 'artifact_compile')):
        _response(rpc_id, error={'code': -32602, 'message': 'Artifact compiler tool is disabled by host policy'})
        return
    if (params['name'] in {'run_project_command', 'read_run_artifact'}
            and session.get('kind') == 'characterization'):
        _response(rpc_id, error={'code': -32602, 'message': 'Only registered characterization verification is available'})
        return
    if (params['name'] == 'run_project_command'
            and session.get('allow_project_command') is False):
        _response(rpc_id, error={'code': -32602,
                                 'message': 'Project commands disabled by host policy'})
        return
    arguments = params.get('arguments') or {}
    if not isinstance(arguments, dict):
        _response(rpc_id, error={'code': -32602, 'message': 'Invalid arguments'})
        return
    try:
        if params['name'] == 'compile_artifact_harness':
            value = _compile_artifact_harness(session, arguments)
        elif params['name'] == 'verify_characterization':
            value = _run_characterization(session, arguments)
        elif params['name'] == 'read_run_artifact':
            value = _read_run_artifact(session, arguments.get('path'),
                                       arguments.get('offset', 0), arguments.get('limit', 65536),
                                       arguments.get('expected_sha256'))
        else:
            default_timeout = min(MAX_COMMAND_SECONDS,
                                  max(1, math.ceil(session['deadline_epoch'] - time.time())))
            value = _run(session, arguments.get('command'),
                         arguments.get('timeout_seconds', default_timeout))
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, TimeoutError) as exc:
        _response(rpc_id, result=_tool_result({'error': type(exc).__name__, 'detail': str(exc)}, failed=True))
        return
    failed = ((value.get('exit_code') != 0) if params['name'] in {'run_project_command', 'compile_artifact_harness'}
              else (value.get('category') in {'test_infrastructure', 'assertion_invalid', 'mod_behavior'}
                    if params['name'] == 'verify_characterization' and value.get('scope') == 'selected'
                    else False))
    _response(rpc_id, result=_tool_result(value, failed=failed))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--session', type=Path, required=True)
    args = parser.parse_args(argv)
    path = args.session
    if path.is_symlink() or not path.is_file() or path.resolve() != path.absolute():
        raise SystemExit('invalid sandbox tool session path')
    session = json.loads(path.read_text(encoding='utf-8'))
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError('request must be an object')
        except ValueError:
            _response(None, error={'code': -32700, 'message': 'Parse error'})
            continue
        with workspace_context(session['root']):
            _dispatch(session, request)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
