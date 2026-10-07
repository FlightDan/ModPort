"""Independent test authorship and deterministic execution on a separate clone.

The frozen characterization verifier remains a separate acceptance gate. This
stage adds tests without giving their author write access to the main candidate.
"""
from __future__ import annotations

from .workspace import project_path, is_run_path
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import secrets
import stat
import subprocess
from typing import Mapping
from xml.etree import ElementTree

from . import handlers
from .characterization import CharacterizationContract
from .contracts import OperationInput, OperationResult
from .evidence import digest, file_digest, verified_path
from .manifest import canonical_json
from .review_contracts import REJECTED_FINDINGS_PROMPT, validate_review_findings
from .business_policy import business_gates_disabled
from .local_workspace_sandbox import is_sensitive_name


_SUITE = '.modport/independent-tests'
_DECLARATION = _SUITE + '/suite.json'
_OUTPUTS = frozenset({'.gradle', 'build', 'run', 'runs', 'logs', '__pycache__'})
_EXCLUDED = _OUTPUTS | {'.git'}
_TASK = re.compile(r':?[A-Za-z][A-Za-z0-9_-]*(?::[A-Za-z][A-Za-z0-9_-]*)*\Z')
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z')
_PROMPT = """Independently author new executable acceptance assertions for the reviewed migration.
You are the independent-test-agent. The isolated clone contains the approved
candidate and frozen characterization tests. Read the frozen functional contract,
source, baseline evidence, and rubric. Do not execute Gradle, tests, mod code or
other project code; the host runs the declared tasks after validation.
Change or add files ONLY under .modport/independent-tests/. Do not modify any
existing product, characterization test, build, settings, config or contract file.
Do not edit the main worktree or its Git repository. No commits are needed.
Write .modport/independent-tests/suite.json with schema_version=1, init_script as
a relative Gradle init script path in this suite, and a nonempty tests array.
Each test id must match [A-Za-z0-9][A-Za-z0-9_.:-]{0,159} (1–160 characters)
and be unique across the tests array. Every behavior_ids, sources, and report_paths
array must contain unique nonblank strings. init_script must end in .gradle or .kts.
Each test has a unique id, nonempty behavior_ids referencing actual frozen
contract entry IDs, nonempty sources listing relative .java/.kt/.groovy files in
this suite, task as one Gradle task identifier, and nonempty report_paths listing
explicit workspace-relative JUnit XML files under the root build/ directory.
Use the init script to add isolated test source sets/tasks without editing the
candidate build. Compile/run against real candidate classes and assert observable
behavior. The init script must not rewrite source or tests. Assertions must fail
when the behavior is broken. Never manufacture XML, assert source substrings or
hard-coded facts, skip the tests, or replace runtime tests with static inspection.
You may focus on a subset of behaviors; the host records uncovered IDs, and the
full frozen characterization gate and client smoke gate still run separately.
For multi-project builds, explicitly configure each declared Test task to write
its JUnit XML into the ROOT project build/ directory, not a subproject build/.
Only conventional root build/cache/run/log directories are writable during host
execution; the product and this test suite are mounted read-only.
"""


def _relative(value):
    if (not isinstance(value, str) or not value or '\\' in value
            or any(ord(c) < 32 for c in value)):
        raise ValueError('paths must be nonempty contained relative paths')
    path = PurePosixPath(value)
    if (path.is_absolute() or any(p in ('', '.', '..', '.git') for p in value.split('/'))
            or any(':' in part for part in value.split('/'))):
        raise ValueError('paths must be contained relative paths without traversal')
    return path.as_posix()


def _contained(root, relative, *, regular=False, project=False):
    relative = _relative(relative)
    path = project_path(root, relative) if project else root / relative
    contained = is_run_path(root, path) if project else path.is_relative_to(root)
    if path.resolve() != path.absolute() or not contained:
        raise ValueError('symlinks are not allowed in independent test paths')
    if regular and (not path.is_file() or not stat.S_ISREG(path.lstat().st_mode)):
        raise ValueError(f'expected a regular file: {relative}')
    return path


def _workspace(root, command):
    relative = _relative(command.options.get('workspace'))
    parts = PurePosixPath(relative).parts
    if len(parts) != 3 or parts[:2] != ('workspaces', 'tests'):
        raise ValueError('independent tests require workspaces/tests/<assignment>')
    path = _contained(root, relative)
    return relative, path


def _tree(root, *, protected=False, outputs=False):
    """Hash all regular files; excluded names apply at the root, never in src/."""
    if root.resolve() != root.absolute() or not root.is_dir():
        raise ValueError('test workspace is missing or unsafe')
    files = {}
    for directory, names, filenames in os.walk(root, followlinks=False):
        parent = Path(directory)
        prefix = parent.relative_to(root)
        if not prefix.parts and outputs:
            names[:] = [name for name in names if name not in _EXCLUDED]
            filenames = [name for name in filenames if name not in _EXCLUDED]
        if protected and prefix.as_posix() == '.modport':
            names[:] = [name for name in names if name != 'independent-tests']
        for name in names:
            path = parent / name
            if path.is_symlink() or not path.is_dir() or name == '.git':
                raise ValueError('symlink or Git directory in test clone')
        for name in sorted(filenames):
            path = parent / name
            relative = path.relative_to(root).as_posix()
            mode = path.lstat().st_mode
            if path.is_symlink() or not stat.S_ISREG(mode) or name == '.git':
                raise ValueError(f'non-regular independent test file: {relative}')
            if path.stat().st_nlink != 1:
                raise ValueError(f'hard-linked independent test file: {relative}')
            files[relative] = {'sha256': file_digest(path), 'mode': stat.S_IMODE(mode)}
    return dict(sorted(files.items()))


def _contract(command, root):
    rubric = handlers._acceptance_rubric_for(command, root)
    ref = command.artifact_refs.get('functional_contract_lock')
    if not isinstance(ref, Mapping):
        raise ValueError('frozen functional contract ref is required')
    path = verified_path(root, ref)
    lock = json.loads(path.read_text())
    if not isinstance(lock, Mapping):
        raise ValueError('functional contract lock must be an object')
    bound_rubric = lock.get('acceptance_rubric', {})
    if any(bound_rubric.get(key) != rubric[key] for key in ('rubric_id', 'rubric_version')):
        raise ValueError('frozen functional contract rubric mismatch')
    contract = CharacterizationContract.from_mapping(lock.get('contract', {}))
    ids = {entry.entry_id for entry in contract.entries}
    if not ids:
        raise ValueError('frozen functional contract has no behaviors')
    from .regression import frozen_static_behavior_ids
    static_ids = frozen_static_behavior_ids(lock)
    if not static_ids <= ids:
        raise ValueError('frozen static classification references unknown contract behavior IDs')
    return ids, {'contract_id': contract.contract_id,
                 'contract_schema_version': contract.schema_version,
                 'rubric_id': rubric['rubric_id'],
                 'rubric_version': rubric['rubric_version']}, static_ids


def _strings(value, name):
    if (not isinstance(value, list) or not value
            or any(not isinstance(item, str) or not item.strip() for item in value)
            or len(set(value)) != len(value)):
        raise ValueError(f'{name} requires a nonempty unique string array')
    return value


def validate_suite(suite, workspace, behavior_ids, *, scope=None,
                   contract_static_behavior_ids=None):
    """Validate declarations and existing test sources, without running the project."""
    if not isinstance(suite, Mapping) or type(suite.get('schema_version')) is not int or suite['schema_version'] != 1:
        raise ValueError('independent suite requires schema_version 1')
    directory = _contained(workspace, _SUITE)
    assigned_static_ids = set(scope.get('static_behavior_ids', [])) if scope else set()
    static_ids = (set(contract_static_behavior_ids)
                  if contract_static_behavior_ids is not None else assigned_static_ids)
    if not static_ids <= behavior_ids or not assigned_static_ids <= static_ids:
        raise ValueError('static behavior classification differs from the frozen contract')
    static = suite.get('static_behavior_ids', [])
    evidence = suite.get('static_evidence', [])
    if (not isinstance(static, list) or len(static) != len(set(static))
            or set(static) != assigned_static_ids
            or not isinstance(evidence, list) or len(evidence) != len(assigned_static_ids)):
        raise ValueError('static coverage must exactly match the frozen assigned classification')
    seen_static = set()
    for row in evidence:
        if (not isinstance(row, dict) or row.get('behavior_id') not in assigned_static_ids
                or row['behavior_id'] in seen_static or not isinstance(row.get('reason'), str)
                or not row['reason'].strip() or row.get('acceptance_gates') != ['client_smoke']):
            raise ValueError('static evidence requires a unique behavior, reason and retained client_smoke gate')
        _strings(row.get('evidence'), 'static evidence observations')
        seen_static.add(row['behavior_id'])
    assigned_runtime_ids = (set(scope.get('runtime_behavior_ids', scope['behavior_ids']))
                            if scope else set(behavior_ids)) - assigned_static_ids
    runtime_ids = behavior_ids - static_ids
    if assigned_static_ids and not assigned_runtime_ids and suite.get('tests') == []:
        if 'init_script' in suite:
            raise ValueError('static-only suites must omit init_script')
        return {'covered_behavior_ids': sorted(assigned_static_ids),
                'uncovered_behavior_ids': sorted(behavior_ids - assigned_static_ids),
                'runtime_behavior_ids': [], 'static_behavior_ids': sorted(assigned_static_ids),
                'static_evidence': evidence}
    init = _relative(suite.get('init_script'))
    if PurePosixPath(init).suffix not in ('.gradle', '.kts'):
        raise ValueError('init_script must be a Gradle script within the suite')
    init_path = _contained(directory, init, regular=True)
    if not init_path.read_bytes().strip():
        raise ValueError('init_script is empty')
    tests = suite.get('tests')
    if not isinstance(tests, list) or not tests:
        raise ValueError('independent suite requires nonempty tests')
    seen, covered = set(), set()
    for test in tests:
        if not isinstance(test, Mapping):
            raise ValueError('test must be an object')
        identifier = test.get('id')
        if not isinstance(identifier, str) or not _ID.fullmatch(identifier) or identifier in seen:
            raise ValueError('test ids must be safe and unique')
        seen.add(identifier)
        mapped = _strings(test.get('behavior_ids'), 'behavior_ids')
        if not set(mapped) <= runtime_ids:
            raise ValueError('test references unknown or non-runtime frozen behavior IDs')
        covered.update(mapped)
        for relative in _strings(test.get('sources'), 'sources'):
            path = _contained(directory, relative, regular=True)
            if path.suffix not in ('.java', '.kt', '.groovy') or not path.read_bytes().strip():
                raise ValueError('test sources must be real nonempty Java/Kotlin/Groovy files')
        task = test.get('task')
        if not isinstance(task, str) or not _TASK.fullmatch(task):
            raise ValueError('task must be a Gradle identifier, not shell code or options')
        for relative in _strings(test.get('report_paths'), 'report_paths'):
            path = _relative(relative)
            if (not path.startswith('build/') or PurePosixPath(path).suffix != '.xml'
                    or any(character in path for character in '*?[]')):
                raise ValueError('report_paths must be explicit XML files under root build/')
            _contained(workspace, path)
    result = {'covered_behavior_ids': sorted(covered | assigned_static_ids),
              'uncovered_behavior_ids': sorted(behavior_ids - covered - assigned_static_ids)}
    if scope is not None and 'runtime_behavior_ids' in scope:
        result.update(runtime_behavior_ids=sorted(covered), static_behavior_ids=sorted(assigned_static_ids),
                      static_evidence=evidence)
    return result


def _scope(command, behavior_ids, contract_static_behavior_ids=None):
    scope = command.payload.get('regression_scope')
    if scope is None:
        return None
    if (not isinstance(scope, dict) or not isinstance(scope.get('scope_id'), str)
            or not re.fullmatch(r'scope-[0-9]{3,}', scope['scope_id'])):
        raise ValueError('invalid regression scope identity')
    ids = _strings(scope.get('behavior_ids'), 'scope behavior_ids')
    if not set(ids) <= behavior_ids:
        raise ValueError('regression scope references unknown frozen behavior IDs')
    if _review_required(command):
        static_all = contract_static_behavior_ids
        if static_all is None:
            from .regression import frozen_static_behavior_ids
            lock = json.loads(verified_path(handlers._run_root(command),
                              command.artifact_refs['functional_contract_lock']).read_text())
            static_all = frozen_static_behavior_ids(lock)
        static = set(static_all).intersection(ids)
        if (set(scope.get('static_behavior_ids', [])) != static
                or set(scope.get('runtime_behavior_ids', [])) != set(ids) - static):
            raise ValueError('regression scope runtime/static classification differs from frozen contract')
    return scope


def _scope_coverage(scope, coverage):
    diagnostics = _scope_coverage_diagnostics(scope, coverage)
    if diagnostics:
        raise ValueError(diagnostics[0])


def _scope_coverage_diagnostics(scope, coverage):
    if scope is None:
        return []
    missing = sorted(set(scope['behavior_ids']) - set(coverage['covered_behavior_ids']))
    extra = sorted(set(coverage['covered_behavior_ids']) - set(scope['behavior_ids']))
    if not missing and not extra:
        return []
    details = []
    if missing:
        details.append('uncovered assigned behavior IDs: ' + ', '.join(missing))
    if extra:
        details.append('unexpected behavior IDs: ' + ', '.join(extra))
    return ['independent suite coverage differs from its assigned regression scope ('
            + '; '.join(details) + ')']


def _upstream_diagnostics(command, *stages):
    diagnostics = []
    for stage in stages:
        producer = command.upstream_results.get(stage, {})
        outputs = producer.get('outputs', {}) if isinstance(producer, Mapping) else {}
        observed = outputs.get('business_diagnostics', []) if isinstance(outputs, Mapping) else []
        if isinstance(observed, list):
            diagnostics.extend(str(item) for item in observed if str(item))
    return list(dict.fromkeys(diagnostics))


def _scope_tasks(scope, suite):
    if scope is not None and any(not test['task'].startswith(':') for test in suite['tests']):
        raise ValueError('scoped regression requires fully qualified Gradle Test task paths')


def _artifact(root, command, name, data, *, source_path=None):
    path = _contained(root, f'artifacts/executions/{command.command_id}/{name}')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as output:
        output.write(data)
    return {'path': path.relative_to(root).as_posix(), 'sha256': sha256(data).hexdigest(),
            'media_type': 'application/json' if name.endswith('.json') else 'application/octet-stream',
            'metadata': {'source_path': source_path or path.relative_to(root).as_posix()}}


def _json_artifact(root, command, name, data):
    return _artifact(root, command, name, (canonical_json(data) + '\n').encode())


def _main_unchanged(root, candidate=None, protected=None):
    """Compatibility hook retained for callers of older run records.

    The test author and executor run in isolated workspaces.  A later source
    or test update is ordinary workflow progress, so candidate and protected
    tree digests are no longer compared here.
    """
    worktree = project_path(Path(root), 'worktree')
    if not worktree.exists() or not worktree.is_dir() or worktree.is_symlink():
        raise ValueError('main candidate workspace is missing or unsafe')



def _review_required(command):
    return int(command.options.get('workflow_version', 12)) >= 13


def _safe_copy(source, destination, **kwargs):
    # Check shape without hashing the product or interpreting checkout changes.
    if source.is_symlink() or not source.is_dir():
        raise ValueError('candidate snapshot requires a regular directory')
    def sensitive(name):
        return is_sensitive_name(name)

    for directory, names, files in os.walk(source, followlinks=False):
        names[:] = [name for name in names if not sensitive(name)]
        files = [name for name in files if not sensitive(name)]
        for name in names + files:
            path = Path(directory) / name
            if path.is_symlink() or (not path.is_dir() and not path.is_file()):
                raise ValueError('candidate snapshot cannot contain symlinks or special files')
    caller_ignore = kwargs.pop('ignore', None)
    def ignore(directory, names):
        ignored = {name for name in names if sensitive(name)}
        if caller_ignore is not None:
            ignored.update(caller_ignore(directory, names))
        return sorted(ignored)
    shutil.copytree(source, destination, ignore=ignore, **kwargs)


def _immutable_path(root, ref):
    path = verified_path(root, ref)
    if file_digest(path) != ref.get('sha256'):
        raise ValueError('independent test handoff artifact changed; create a new design and review')
    return path


def _bound_snapshot(command, root):
    snapshot = json.loads(_immutable_path(root, command.artifact_refs['independent_test_snapshot']).read_text())
    if not isinstance(snapshot, dict) or snapshot.get('schema_version') != 2:
        raise ValueError('reviewed execution requires a pristine candidate snapshot')
    candidate_ref = command.artifact_refs.get('independent_test_candidate')
    if candidate_ref != snapshot.get('candidate_ref'):
        # SDK sealing may add metadata; bind the actual artifact identity.
        if not isinstance(candidate_ref, dict) or any(candidate_ref.get(k) != snapshot.get('candidate_ref', {}).get(k)
                                                    for k in ('path', 'sha256')):
            raise ValueError('independent candidate reference mismatch')
    return snapshot


def _assemble(command, root, design_workspace):
    snapshot = _bound_snapshot(command, root)
    if snapshot.get('workspace') != design_workspace:
        raise ValueError('independent test snapshot workspace mismatch')
    descriptor = json.loads(_immutable_path(root, command.artifact_refs['independent_test_candidate']).read_text())
    if (descriptor.get('schema_version') != 1
            or descriptor.get('design_command_id') != snapshot.get('design_command_id')
            or descriptor.get('candidate_workspace') != f"artifacts/test-candidates/{snapshot.get('design_command_id')}"):
        raise ValueError('pristine candidate identity mismatch')
    pristine = _contained(root, descriptor['candidate_workspace'])
    assembled = _contained(root, f'workspaces/tests/{command.stage_id}-{command.command_id}')
    _safe_copy(pristine, assembled)
    directory = _contained(assembled, _SUITE)
    directory.mkdir(parents=True, exist_ok=False)
    files = snapshot.get('suite_files')
    if not isinstance(files, dict) or not files:
        raise ValueError('independent suite file manifest is missing')
    for relative, expected in files.items():
        key = 'independent_test_suite' if relative == 'suite.json' else 'independent_test_source:' + relative
        ref = command.artifact_refs[key]
        data = _immutable_path(root, ref).read_bytes()
        if sha256(data).hexdigest() != expected.get('sha256'):
            raise ValueError('independent suite source does not match its design')
        target = _contained(directory, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(expected['mode'])
    if digest({'schema_version': 1, 'files': _tree(directory)}) != snapshot.get('suite_sha256'):
        raise ValueError('independent suite manifest does not match its design')
    return assembled


def _review_binding(snapshot):
    return {'design_command_id': snapshot['design_command_id'],
            'suite_sha256': snapshot['suite_sha256'],
            'candidate_sha256': snapshot['candidate_ref']['sha256'],
            'regression_scope': snapshot.get('regression_scope')}


def _validate_reworked_review_workspace(command, root, workspace, snapshot):
    """Bind an in-place suite refresh to the source the reviewer inspected."""
    suite_digest = digest({'schema_version': 1, 'files': _tree(_contained(workspace, _SUITE))})
    if suite_digest != snapshot.get('suite_sha256'):
        raise ValueError('reworked independent suite was not installed in the reviewer workspace')
    bound = _bound_snapshot(command, root)
    if bound.get('design_command_id') != snapshot.get('design_command_id'):
        raise ValueError('reworked independent test snapshot identity mismatch')
    descriptor = json.loads(_immutable_path(root, command.artifact_refs['independent_test_candidate']).read_text())
    if (descriptor.get('schema_version') != 1
            or descriptor.get('design_command_id') != snapshot.get('design_command_id')
            or descriptor.get('candidate_workspace')
               != f"artifacts/test-candidates/{snapshot.get('design_command_id')}"):
        raise ValueError('reworked independent candidate identity mismatch')
    pristine = _contained(root, descriptor.get('candidate_workspace'))
    if (_tree(workspace, protected=True, outputs=True)
            != _tree(pristine, protected=True, outputs=True)):
        raise ValueError('reworked independent design changed the source under review')


def _validate_review(document, snapshot):
    if (not isinstance(document, dict) or type(document.get('schema_version')) is not int
            or document['schema_version'] != 1
            or document.get('reviewer_id') != 'independent-test-review-agent'
            or document.get('verdict') not in ('approved', 'rejected')):
        raise ValueError('invalid independent test review schema, identity or verdict')
    if any(document.get(key) != snapshot.get(key) for key in ('design_command_id', 'regression_scope')):
        raise ValueError('independent test review design/suite/candidate binding mismatch')


def _review_observation(command, root, snapshot):
    ref = command.artifact_refs.get('independent_test_review')
    if not isinstance(ref, Mapping):
        return None
    document = json.loads(verified_path(root, ref).read_text())
    _validate_review(document, snapshot)
    producer = command.upstream_results.get('test_review', {})
    produced_ref = producer.get('outputs', {}).get('artifact_refs', {}).get('independent_test_review', {})
    if (producer.get('status') not in ('completed', 'failed')
            or producer.get('stage_id') != 'test_review' or producer.get('run_id') != command.run_id
            or producer.get('command_id') != document.get('review_command_id')
            or produced_ref.get('path') != ref.get('path')):
        raise ValueError('independent test review lacks a matching reviewer producer')
    return document


def _approved_review(command, root, snapshot):
    document = _review_observation(command, root, snapshot)
    if document is None or document['verdict'] != 'approved':
        raise ValueError('approved independent test review is required before execution')
    producer = command.upstream_results.get('test_review', {})
    if producer.get('status') != 'completed':
        raise ValueError('independent test review lacks a matching completed reviewer producer')
    return document


_REVIEW_PROMPT = """You are independent-test-review-agent, a separate read-only reviewer.
Inspect the pristine reviewed candidate and authored suite in this workspace.
Do not write files or execute Gradle, tests, mod code, scripts, or other project code.
Review assertion quality, actual candidate class use, frozen behavior coverage,
Gradle task wiring, and JUnit report configuration. Reject vacuous assertions,
fabricated reports, source-text tests, skipped cases, or claims not established by
runtime assertions. For static_behavior_ids independently verify the frozen
static_client declaration and the submitted reason/evidence, retaining client_smoke.
Static coverage is explicitly separate from runtime assertions and cannot prove
visual rendering. Do not infer visual rendering from successful compilation.
Report actionable fixes freely for the test author. Finish with a standalone
MODPORT_DECISION: approved or MODPORT_DECISION: rejected line.
No report field schema, identity or hash echo is required. Host context for reference:

""" + REJECTED_FINDINGS_PROMPT + "\n"


class TestReviewHandler:
    def __call__(self, command: OperationInput) -> OperationResult:
        root = handlers._run_root(command)
        outputs = {}
        business_diagnostics = _upstream_diagnostics(command, 'test_design')
        try:
            try:
                relative, _ = _workspace(root, command)
                workspace = _assemble(command, root, relative)
                suite, snapshot, coverage = _load_snapshot(command, root, workspace, relative)
                business_diagnostics.extend(coverage.get('business_diagnostics', []))
            except (OSError, ValueError, TypeError, KeyError) as exc:
                if not business_gates_disabled(command):
                    raise
                business_diagnostics.append(str(exc))
                relative, workspace = _workspace(root, command)
                if not workspace.is_dir() or workspace.is_symlink():
                    raise ValueError('independent test review workspace is unavailable')
                suite, snapshot, coverage = {}, {}, {}
            review_command = replace(command, options={**command.options,
                'workspace': workspace.relative_to(root).as_posix()})
            binding = _review_binding(snapshot) if snapshot else {}
            review_context = {**binding,
                              'business_diagnostics': list(dict.fromkeys(business_diagnostics))}
            result = handlers.CodexStageHandler(_REVIEW_PROMPT + canonical_json(review_context),
                                                read_only=True)(review_command)
            outputs = result.outputs
            if result.status != 'completed':
                if business_gates_disabled(command):
                    return handlers._unverified_result(command, status=result.status,
                        outputs=outputs, diagnostics=[result.detail], detail=result.detail,
                        error_code=result.error_code)
                return handlers._result(command, 'failed', outputs=outputs, detail=result.detail,
                                        error_code='independent_test_review_failed')
            message = result.outputs['last_message']
            message_path = Path(message)
            if message_path.is_absolute():
                message = message_path.relative_to(root).as_posix()
            from .agent_reports import review_decision, report_findings
            from .rework_tools import refresh_review_command, responses
            if responses(command):
                command = refresh_review_command(command)
                relative = command.options['workspace']
                suite, snapshot, coverage = _load_snapshot(command, root, workspace, relative)
                business_diagnostics.extend(coverage.get('business_diagnostics', []))
                _validate_reworked_review_workspace(command, root, workspace, snapshot)
            document = review_decision(_contained(root, message, regular=True).read_text())
            document.update(schema_version=1, reviewer_id='independent-test-review-agent', **_review_binding(snapshot))
            document['findings'] = report_findings(document)
            _validate_review(document, snapshot)
            document['review_command_id'] = command.command_id
            refs = {**result.outputs.get('artifact_refs', {}),
                    'independent_test_review': _json_artifact(root, command, 'independent-test-review.json', document)}
            outputs = {**outputs, **document, **coverage, 'artifact_refs': refs, 'workspace': relative}
            business_diagnostics = list(dict.fromkeys(business_diagnostics))
            if business_gates_disabled(command) and document['verdict'] != 'approved':
                return handlers._unverified_result(command, status='failed', outputs=outputs,
                    diagnostics=['independent test review observed verdict=' + document['verdict']],
                    detail='independent test review observations recorded',
                    error_code='independent_test_review_rejected')
            if business_gates_disabled(command) and business_diagnostics:
                return handlers._unverified_result(command, outputs=outputs,
                    diagnostics=business_diagnostics,
                    detail='independent test review recorded with coverage diagnostics')
            return handlers._result(command, 'completed' if document['verdict'] == 'approved' else 'failed',
                outputs=outputs, detail='independent test review ' + document['verdict'],
                error_code=None if document['verdict'] == 'approved' else 'independent_test_review_rejected')
        except (OSError, ValueError, TypeError, KeyError) as exc:
            if business_gates_disabled(command) and outputs:
                return handlers._unverified_result(command, outputs=outputs,
                    diagnostics=[*business_diagnostics, str(exc)],
                    detail='independent test review retained without schema-gated acceptance')
            return handlers._result(command, 'failed', outputs=outputs, detail=str(exc),
                                    error_code='independent_test_review_failed')


class TestDesignHandler:
    def __call__(self, command: OperationInput) -> OperationResult:
        root = handlers._run_root(command)
        outputs = {}
        try:
            relative, workspace = _workspace(root, command)
            ids, bindings, contract_static_ids = _contract(command, root)
            scope = _scope(command, ids, contract_static_ids)
            source = _contained(root, 'worktree', project=True)
            if (source / _SUITE).exists() or (source / _SUITE).is_symlink():
                raise ValueError('main candidate already contains an independent suite; author a new suite in its clone')
            if workspace.exists() and (not workspace.is_dir() or any(workspace.iterdir())):
                raise ValueError('independent test design requires a fresh empty workspace')
            def ignore(directory, names):
                return sorted(set(names) & _EXCLUDED) if Path(directory) == source else []
            candidate_ref = None
            if _review_required(command):
                pristine_relative = f'artifacts/test-candidates/{command.command_id}'
                pristine = _contained(root, pristine_relative)
                _safe_copy(source, pristine, ignore=ignore)
                candidate_ref = _json_artifact(root, command, 'independent-test-candidate.json', {
                    'schema_version': 1, 'design_command_id': command.command_id,
                    'candidate_workspace': pristine_relative})
                source = pristine
            _safe_copy(source, workspace, ignore=ignore, dirs_exist_ok=True)
            prompt = _PROMPT
            if scope is not None:
                prompt += ('\nThis assignment owns only the following regression scope. '
                           'Cover every assigned behavior ID with executable assertions, and no other IDs. '
                           'Declare fully qualified Gradle Test task paths such as :independentTest. '
                           'Address its verification obligations with real runtime evidence.\n'
                           + canonical_json(scope))
                if _review_required(command):
                    prompt += ('\nExecutable tests must map exactly runtime_behavior_ids. '
                        'For assigned static_behavior_ids, declare static_behavior_ids in suite.json '
                        'and static_evidence rows {behavior_id, reason, evidence:[nonblank observations], '
                        'acceptance_gates:["client_smoke"]}. Preserve the frozen static_client exception '
                        'and explain its evidence; do not invent runtime or visual assertions. '
                        'When runtime_behavior_ids is empty, use tests:[] and omit init_script. '
                        'Static observations never replace frozen characterization or client_smoke.')
            result = handlers.CodexStageHandler(prompt, required_paths=(_DECLARATION,))(command)
            outputs = result.outputs
            if result.status != 'completed':
                if business_gates_disabled(command):
                    return handlers._unverified_result(command, status=result.status,
                        outputs=outputs, diagnostics=[result.detail], detail=result.detail,
                        error_code=result.error_code)
                return handlers._result(command, 'failed', outputs=result.outputs,
                                        detail=result.detail, error_code='independent_test_failed')
            declaration = _contained(workspace, _DECLARATION, regular=True)
            suite = json.loads(declaration.read_text())
            coverage = validate_suite(suite, workspace, ids,
                scope=scope if _review_required(command) else None,
                contract_static_behavior_ids=(contract_static_ids if _review_required(command) else None))
            coverage_diagnostics = _scope_coverage_diagnostics(scope, coverage)
            if coverage_diagnostics and not business_gates_disabled(command):
                _scope_coverage(scope, coverage)
            _scope_tasks(scope, suite)
            suite_files = _tree(_contained(workspace, _SUITE))
            suite_sha = digest({'schema_version': 1, 'files': suite_files})
            snapshot = {'schema_version': 1, 'workspace': relative,
                        'suite_sha256': suite_sha, 'suite_json_sha256': file_digest(declaration),
                        'suite_files': suite_files, 'design_command_id': command.command_id,
                        'excluded_output_roots': sorted(_OUTPUTS), **bindings, **coverage}
            if candidate_ref is not None:
                snapshot['schema_version'] = 2
                snapshot['candidate_ref'] = candidate_ref
            if scope is not None:
                snapshot['regression_scope'] = scope
            refs = dict(result.outputs.get('artifact_refs', {}))
            if candidate_ref is not None:
                refs['independent_test_candidate'] = candidate_ref
            refs['independent_test_suite'] = _artifact(root, command, 'independent-test-suite.json', declaration.read_bytes(),
                                                      source_path=relative + '/' + _DECLARATION)
            refs['independent_test_snapshot'] = _json_artifact(root, command, 'independent-test-snapshot.json', snapshot)
            for path in suite_files:
                if path != 'suite.json':
                    refs['independent_test_source:' + path] = _artifact(root, command, 'suite-files/' + path,
                        (workspace / _SUITE / path).read_bytes(), source_path=relative + '/' + _SUITE + '/' + path)
            outputs = {**result.outputs, 'artifact_refs': refs, 'workspace': relative,
                       'suite_sha256': suite_sha, **coverage}
            if scope is not None:
                outputs['regression_scope'] = scope
            if business_gates_disabled(command) and coverage_diagnostics:
                return handlers._unverified_result(command, outputs=outputs,
                    diagnostics=coverage_diagnostics,
                    detail='independent test suite snapshot recorded with incomplete coverage')
            return handlers._result(command, 'completed', outputs=outputs, detail='independent test suite designed and source clone recorded')
        except (OSError, ValueError, TypeError, KeyError) as exc:
            if business_gates_disabled(command):
                declaration = locals().get('declaration')
                retained = dict(outputs)
                if isinstance(declaration, Path) and declaration.is_file() and not declaration.is_symlink():
                    retained['raw_report'] = declaration.read_text(encoding='utf-8', errors='replace')
                return handlers._unverified_result(command, outputs=retained,
                    diagnostics=[str(exc)],
                    detail='independent test design retained without schema-gated acceptance')
            return handlers._result(command, 'failed', outputs=outputs, detail=str(exc), error_code='independent_test_failed')


def _load_snapshot(command, root, workspace, relative):
    snapshot_ref = command.artifact_refs.get('independent_test_snapshot')
    suite_ref = command.artifact_refs.get('independent_test_suite')
    if not isinstance(snapshot_ref, Mapping) or not isinstance(suite_ref, Mapping):
        raise ValueError('independent suite and snapshot refs are required')
    snapshot = json.loads(verified_path(root, snapshot_ref).read_text())
    # The suite reference establishes that design produced the expected
    # artifact.  Execute the current workspace declaration so a repaired or
    # regenerated suite can move forward without a stale content lock.
    verified_path(root, suite_ref)
    if (not isinstance(snapshot, Mapping) or type(snapshot.get('schema_version')) is not int
            or snapshot.get('schema_version') not in (1, 2)
            or snapshot.get('workspace') != relative):
        raise ValueError('independent test snapshot workspace mismatch')
    if not isinstance(snapshot.get('design_command_id'), str) or not snapshot['design_command_id'].strip():
        raise ValueError('independent test snapshot design command identity missing')
    ids, bindings, contract_static_ids = _contract(command, root)
    scope = _scope(command, ids, contract_static_ids)
    if snapshot.get('regression_scope') != scope:
        raise ValueError('independent test snapshot regression scope mismatch')
    # Semantic contract and rubric version links remain useful.  Historical
    # digest fields in snapshots are accepted for compatibility and ignored.
    for key in ('contract_id', 'contract_schema_version', 'rubric_id', 'rubric_version'):
        if key in snapshot and snapshot.get(key) != bindings.get(key):
            raise ValueError('independent test suite is bound to a different contract version')
    _check_clone(workspace, snapshot)
    declaration = _contained(workspace, _DECLARATION, regular=True)
    suite = json.loads(declaration.read_text())
    coverage = validate_suite(suite, workspace, ids,
        scope=scope if _review_required(command) else None,
        contract_static_behavior_ids=(contract_static_ids if _review_required(command) else None))
    coverage_diagnostics = _scope_coverage_diagnostics(scope, coverage)
    if coverage_diagnostics and not business_gates_disabled(command):
        _scope_coverage(scope, coverage)
    _scope_tasks(scope, suite)
    if scope is not None:
        # The aggregate rechecks the exact contract/rubric identities observed
        # by this branch, so carry them into its execution record as well.
        coverage = {**coverage, **bindings}
    if coverage_diagnostics:
        coverage = {**coverage, 'business_diagnostics': coverage_diagnostics}
    return suite, snapshot, coverage


def _check_clone(workspace, snapshot):
    _contained(workspace, _SUITE)
    if (workspace / '.git').exists() or (workspace / '.git').is_symlink():
        raise ValueError('independent test clone must not share a Git repository')


def _readonly_build_command(root, workspace, args, *, cache_name='independent-test-gradle-cache', operation=None):
    if os.name == 'nt':
        writable = []
        for name in sorted(_OUTPUTS):
            output = _contained(workspace, name)
            output.mkdir(exist_ok=True)
            writable.append(name)
        return handlers._sandboxed_build_command(
            root, workspace, args, cache_name=cache_name,
            java_home=handlers._locked_java_home(root), operation=operation,
            readonly_workspace=True, writable_workspace_paths=tuple(writable))
    command = handlers._sandboxed_build_command(root, workspace, args,
        cache_name=cache_name, java_home=handlers._locked_java_home(root), operation=operation)
    mounts = [i for i in range(len(command) - 2)
              if command[i:i + 3] == ['--bind', str(workspace), '/workspace']]
    if len(mounts) != 1 or '--chdir' not in command:
        raise ValueError('sandbox does not expose the expected isolated workspace mount')
    command[mounts[0]] = '--ro-bind'
    writable = []
    for name in sorted(_OUTPUTS):
        output = _contained(workspace, name)
        output.mkdir(exist_ok=True)
        writable += ['--bind', str(output), '/workspace/' + name]
    boundary = command.index('--chdir')
    command[boundary:boundary] = writable
    return command


def _xml_report(path, *, allow_skipped=True):
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('JUnit report exceeds 16 MiB')
    data = path.read_bytes()
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ValueError('JUnit reports must not declare XML entities')
    document = ElementTree.fromstring(data)
    if document.tag not in ('testsuite', 'testsuites'):
        raise ValueError('expected a JUnit testsuite or testsuites report')
    cases = list(document.iter('testcase'))
    if not cases:
        raise ValueError('JUnit report has no actual test cases')
    def totals(node):
        cases = list(node.iter('testcase'))
        return {'tests': len(cases), 'failures': sum(c.find('failure') is not None for c in cases),
                'errors': sum(c.find('error') is not None for c in cases),
                'skipped': sum(c.find('skipped') is not None for c in cases)}
    for node in document.iter():
        if node.tag not in ('testsuite', 'testsuites'):
            continue
        actual = totals(node)
        for key, count in actual.items():
            value = node.get(key)
            if value is None:
                if node.tag == 'testsuite' and key in ('tests', 'failures', 'errors'):
                    raise ValueError(f'JUnit report lacks {key} count')
                continue
            if not re.fullmatch(r'[0-9]+', value) or int(value) != count:
                raise ValueError(f'JUnit {key} count disagrees with actual cases')
    counts = totals(document)
    if counts['failures'] or counts['errors'] or counts['tests'] <= counts['skipped']:
        raise ValueError('independent tests failed, errored or were all skipped')
    if not allow_skipped and counts['skipped']:
        raise ValueError('scoped independent regression must execute every test without skips')
    return counts


def _join_regression(command, root):
    """Validate branch artifacts and emit one SDK-authenticated aggregate result."""
    ids, bindings, contract_static_ids = _contract(command, root)
    contract_runtime_ids = ids - contract_static_ids
    scopes = command.payload.get('regression_scopes')
    results = command.payload['regression_results']
    designs = command.payload.get('regression_designs')
    generation = command.payload.get('regression_generation')
    reviews = command.payload.get('regression_reviews')
    diagnostic_only = business_gates_disabled(command)
    diagnostics = []
    if (_review_required(command) and not diagnostic_only
            and (not isinstance(reviews, list) or len(reviews) != len(scopes or []))):
        raise ValueError('regression join requires one independent review per scope')
    if (not isinstance(scopes, list) or not scopes or not isinstance(results, list)
            or not isinstance(designs, list) or len(results) != len(scopes) or len(designs) != len(scopes)
            or type(generation) is not int or generation < 1):
        raise ValueError('regression join requires one design and execution per scope')
    if _review_required(command) and diagnostic_only and (
            not isinstance(reviews, list) or len(reviews) != len(scopes)):
        diagnostics.append('independent review result is missing or malformed for one or more scopes')
        reviews = [None] * len(scopes)
    assigned_all, covered, seen_scopes, commands, refs, branches = set(), set(), set(), set(), {}, []
    runtime_covered, static_covered = set(), set()
    for index, (scope, design, result) in enumerate(zip(scopes, designs, results)):
        if not isinstance(scope, dict):
            raise ValueError('invalid regression join scope')
        if _review_required(command):
            _scope(replace(command, payload={**command.payload, 'regression_scope': scope}),
                   ids, contract_static_ids)
        name = scope.get('scope_id')
        assigned = set(_strings(scope.get('behavior_ids'), 'scope behavior_ids'))
        if (not isinstance(name, str) or not re.fullmatch(r'scope-[0-9]{3,}', name)
                or name in seen_scopes or assigned_all.intersection(assigned) or not assigned <= ids):
            raise ValueError('regression scopes must have unique identities and disjoint known behaviors')
        seen_scopes.add(name)
        assigned_all.update(assigned)
        producers = [('test_design', design), ('test_execute', result)]
        review_producer = reviews[index] if _review_required(command) else None
        if review_producer is not None:
            valid_review_identity = (
                isinstance(review_producer, dict)
                and review_producer.get('status') in (('completed', 'failed') if diagnostic_only else ('completed',))
                and review_producer.get('stage_id') == 'test_review'
                and review_producer.get('run_id') == command.run_id
                and review_producer.get('task_id') == f'test_review.g{generation}.{name}'
                and isinstance(review_producer.get('command_id'), str)
                and isinstance(review_producer.get('outputs'), dict)
                and review_producer.get('outputs', {}).get('regression_scope') == scope)
            if valid_review_identity:
                producers.append(('test_review', review_producer))
            elif diagnostic_only:
                diagnostics.append(f'{name}: independent review producer identity or status is unavailable')
                review_producer = None
            else:
                raise ValueError('regression join contains a failed, duplicate or mismatched branch')
        for stage, producer in producers:
            allowed_statuses = ('completed', 'failed') if diagnostic_only else ('completed',)
            if (not isinstance(producer, dict) or producer.get('status') not in allowed_statuses
                    or producer.get('stage_id') != stage or producer.get('run_id') != command.run_id
                    or producer.get('task_id') != f'{stage}.g{generation}.{name}'
                    or not isinstance(producer.get('command_id'), str)
                    or producer['command_id'] in commands
                    or producer.get('outputs', {}).get('regression_scope') != scope):
                raise ValueError('regression join contains a failed, duplicate or mismatched branch')
            commands.add(producer['command_id'])
            if diagnostic_only:
                observed = producer.get('outputs', {}).get('business_diagnostics', [])
                if isinstance(observed, list):
                    diagnostics.extend(f'{stage}/{name}: {item}' for item in observed
                                       if isinstance(item, str) and item)
            for key, ref in producer['outputs'].get('artifact_refs', {}).items():
                verified_path(root, ref)
                refs[f'regression:{producer["task_id"]}:{key}'] = ref
        execution_ref = result['outputs']['artifact_refs']['independent_test_result']
        record = json.loads(verified_path(root, execution_ref).read_text())
        snapshot_ref = design['outputs']['artifact_refs']['independent_test_snapshot']
        snapshot = json.loads(verified_path(root, snapshot_ref).read_text())
        review_ref = None
        reviewed = None
        if _review_required(command):
            if review_producer is not None:
                review_ref = review_producer.get('outputs', {}).get('artifact_refs', {}).get(
                    'independent_test_review')
                if isinstance(review_ref, Mapping):
                    review_command = replace(command,
                        upstream_results={'test_review': review_producer},
                        artifact_refs={'independent_test_review': review_ref})
                    if diagnostic_only:
                        try:
                            reviewed = _review_observation(review_command, root, snapshot)
                        except (OSError, ValueError, TypeError, KeyError) as exc:
                            diagnostics.append(f'{name}: independent review observation is unusable: {exc}')
                            reviewed = None
                        if reviewed is None:
                            if not any(item.startswith(f'{name}: independent review observation is unusable:')
                                       for item in diagnostics):
                                diagnostics.append(f'{name}: independent review artifact is unavailable')
                        elif reviewed.get('verdict') != 'approved':
                            diagnostics.append(f'{name}: independent review observed verdict={reviewed.get("verdict")}')
                    else:
                        reviewed = _approved_review(review_command, root, snapshot)
                    if reviewed is not None and record.get('review_command_id') != reviewed['review_command_id']:
                        if not diagnostic_only:
                            raise ValueError('regression execution differs from reviewed design')
                        diagnostics.append(f'{name}: execution record is not bound to the observed review')
                elif not diagnostic_only:
                    raise ValueError('regression join review artifact is missing')
                else:
                    diagnostics.append(f'{name}: independent review artifact is unavailable')
            elif not diagnostic_only:
                raise ValueError('regression join requires one independent review per scope')
            if (record.get('suite_sha256') != snapshot.get('suite_sha256')
                    or record.get('candidate_ref') != snapshot.get('candidate_ref')):
                if not diagnostic_only:
                    raise ValueError('regression execution differs from reviewed design')
                diagnostics.append(f'{name}: execution record differs from the reviewed design snapshot')
            runtime = set(scope.get('runtime_behavior_ids', scope['behavior_ids']))
            static = set(scope.get('static_behavior_ids', []))
            observed_runtime = set(record.get('runtime_behavior_ids', []))
            observed_static = set(record.get('static_behavior_ids', []))
            if runtime & static or runtime | static != assigned:
                raise ValueError('regression runtime/static evidence coverage mismatch')
            if not observed_runtime <= contract_runtime_ids or observed_static != static:
                raise ValueError('regression runtime/static evidence identity mismatch')
            if runtime - observed_runtime:
                diagnostics.append(f'{name}: independent runtime coverage is incomplete')
            if observed_runtime - runtime:
                diagnostics.append(f'{name}: independent tests cover runtime IDs outside the assigned scope: '
                                   + ', '.join(sorted(observed_runtime - runtime)))
            runtime_covered.update(observed_runtime)
            static_covered.update(static)
        if any(document.get(key) != value for document in (record, snapshot)
               for key, value in bindings.items()):
            raise ValueError('regression branch contract or rubric identity mismatch')
        if (record.get('regression_scope') != scope
                or snapshot.get('regression_scope') != scope
                or record.get('execution_command_id') != result['command_id']
                or record.get('design_command_id') != design['command_id']
                or snapshot.get('design_command_id') != design['command_id']
                or record.get('workspace') != snapshot.get('workspace')
                or record.get('workspace') != design['outputs'].get('workspace')):
            raise ValueError('regression branch evidence identity mismatch')
        execution_covered = set(record.get('covered_behavior_ids', []))
        snapshot_covered = set(snapshot.get('covered_behavior_ids', []))
        execution_uncovered = set(record.get('uncovered_behavior_ids', []))
        if (not execution_covered <= ids or execution_covered != snapshot_covered
                or execution_uncovered != ids - execution_covered):
            raise ValueError('regression branch coverage identity mismatch')
        covered.update(execution_covered)
        if execution_covered != assigned or snapshot_covered != assigned:
            diagnostics.append(f'{name}: independent suite coverage differs from its assigned scope')
        if record.get('status') != 'passed':
            errors = record.get('errors', [])
            diagnostics.extend(f'{name}: {error}' for error in errors if isinstance(error, str) and error)
            if not errors:
                diagnostics.append(f'{name}: independent test execution status={record.get("status")}')
        branches.append({'scope': scope, 'design_task_id': design['task_id'],
                         'design_command_id': design['command_id'],
                         'execution_task_id': result['task_id'],
                         'execution_command_id': result['command_id'],
                         'design_snapshot': snapshot_ref, 'execution_result': execution_ref,
                         'covered_behavior_ids': sorted(execution_covered),
                         'uncovered_behavior_ids': sorted(assigned - execution_covered)})
        if review_producer is not None and isinstance(review_producer, dict):
            branches[-1].update(review_task_id=review_producer.get('task_id'),
                                review_command_id=review_producer.get('command_id'))
            if review_ref is not None:
                branches[-1]['review_result'] = review_ref
    if assigned_all != ids:
        diagnostics.append('regression scope assignments do not cover every frozen behavior ID')
    if covered != ids:
        diagnostics.append('independent test results leave frozen behavior IDs uncovered: '
                           + ', '.join(sorted(ids - covered)))
    if diagnostics and not diagnostic_only:
        raise ValueError('regression join does not cover every frozen behavior')
    diagnostics = list(dict.fromkeys(diagnostics))
    record = {'schema_version': 1, 'status': 'failed' if diagnostics else 'passed',
              'execution_command_id': command.command_id,
              'regression_generation': command.payload.get('regression_generation'),
              'scopes': branches, 'covered_behavior_ids': sorted(covered),
              'uncovered_behavior_ids': sorted(ids - covered),
              'frozen_characterization_gate_replaced': False, **bindings}
    if _review_required(command):
        record.update(runtime_behavior_ids=sorted(runtime_covered), static_behavior_ids=sorted(static_covered))
    refs['independent_test_result'] = _json_artifact(root, command, 'independent-test-result.json', record)
    outputs = {**record, 'artifact_refs': refs}
    if diagnostic_only and diagnostics:
        return handlers._unverified_result(command, outputs=outputs, diagnostics=diagnostics,
            detail='scoped independent test outcomes retained with incomplete coverage')
    return handlers._result(command, 'completed', outputs={**record, 'artifact_refs': refs},
                            detail='all scoped independent regressions passed and joined')


class TestExecuteHandler:
    def __call__(self, command: OperationInput) -> OperationResult:
        root = handlers._run_root(command)
        upstream_diagnostics = _upstream_diagnostics(command, 'test_design', 'test_review')
        try:
            if 'regression_results' in command.payload:
                return _join_regression(command, root)
            relative, workspace = _workspace(root, command)
            if _review_required(command):
                workspace = _assemble(command, root, relative)
            suite, snapshot, coverage = _load_snapshot(command, root, workspace, relative)
            business_diagnostics = list(upstream_diagnostics)
            business_diagnostics.extend(coverage.get('business_diagnostics', []))
            review = None
            if _review_required(command):
                if business_gates_disabled(command):
                    try:
                        review = _review_observation(command, root, snapshot)
                    except (OSError, ValueError, TypeError, KeyError) as exc:
                        business_diagnostics.append(str(exc))
                    if review is None:
                        business_diagnostics.append('independent review observation is unavailable')
                    elif review.get('verdict') != 'approved':
                        business_diagnostics.append(
                            'independent test review observed verdict=' + str(review.get('verdict')))
                else:
                    review = _approved_review(command, root, snapshot)
            business_diagnostics = list(dict.fromkeys(business_diagnostics))
            scope = snapshot.get('regression_scope')
            tasks = list(dict.fromkeys(test['task'] for test in suite['tests']))
            if not tasks and 'init_script' not in suite:
                record = {'schema_version': 1, 'workspace': relative, 'status': 'passed',
                    'design_command_id': snapshot['design_command_id'], 'execution_command_id': command.command_id,
                    'suite_sha256': snapshot['suite_sha256'], 'regression_scope': scope,
                    'execution_kind': 'static_only', 'tasks': [], 'reports': {},
                    'required_acceptance_gates': ['client_smoke'],
                    'frozen_characterization_gate_replaced': False, **coverage}
                if review is not None:
                    record['review_command_id'] = review['review_command_id']
                    record['candidate_ref'] = snapshot['candidate_ref']
                ref = _json_artifact(root, command, 'independent-test-result.json', record)
                result_outputs = {**record, 'artifact_refs': {'independent_test_result': ref}}
                if business_gates_disabled(command):
                    return handlers._unverified_result(command, outputs=result_outputs,
                        diagnostics=[*business_diagnostics,
                            'client smoke remains required for static-only observations'],
                        detail='static-only observations recorded; client smoke remains required')
                return handlers._result(command, 'completed', outputs=result_outputs,
                    detail='reviewed static-only observations recorded; client smoke remains required')
            args = ['bash', '/workspace/gradlew', '--no-daemon', '--rerun-tasks', '--no-build-cache', '--init-script',
                    '/workspace/' + _SUITE + '/' + suite['init_script'], *tasks]
            execution_report, execution_nonce = None, None
            if scope is not None:
                from .regression_evidence import install_gradle_test_listener
                execution_nonce = secrets.token_hex(32)
                listener, execution_report = install_gradle_test_listener(workspace, execution_nonce,
                    script_root='.modport/host-regression')
                args += ['--console=plain', '--init-script', '/workspace/' + listener]
            # Validate every declared destination before removing any stale report.
            reports = sorted({path for test in suite['tests'] for path in test['report_paths']})
            destinations = [_contained(workspace, path) for path in reports]
            for path in destinations:
                if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
                    raise ValueError('JUnit destination must be an unlinked regular file')
            cache_name = 'independent-test-gradle-cache'
            if scope is not None:
                cache_name += '-' + sha256(relative.encode()).hexdigest()[:16]
            sandbox = _readonly_build_command(root, workspace, args, cache_name=cache_name, operation=command)
            for path in destinations:
                path.unlink(missing_ok=True)
            log = root / 'logs' / f'independent-tests-{command.command_id}.log'
            errors, exit_code, timed_out = [], None, False
            dependency_failure = None
            try:
                executed = handlers._exec(sandbox, cwd=workspace, log=log,
                                         timeout=handlers._remaining_timeout(command, 7200))
                exit_code = executed.returncode
                if exit_code:
                    errors.append(f'independent Gradle tasks exited with {exit_code}')
                    from .dependency_build import gradle_failure_kind
                    kind = gradle_failure_kind(executed.stdout)
                    if kind != 'gradle_failed':
                        dependency_failure = kind
            except (TimeoutError, subprocess.TimeoutExpired) as exc:
                timed_out = True
                errors.append(f'independent Gradle execution timed out: {exc}')
            except OSError as exc:
                errors.append(f'independent Gradle execution failed to start: {exc}')
            try:
                _check_clone(workspace, snapshot)
                _contract(command, root)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                errors.append(str(exc))
            report_results, refs = {}, {}
            for index, relative_report in enumerate(reports):
                try:
                    path = _contained(workspace, relative_report, regular=True)
                    if path.stat().st_nlink != 1:
                        raise ValueError('JUnit report must not be hard-linked')
                    ref = _artifact(root, command, f'reports/{index}.xml', path.read_bytes(),
                                    source_path=relative + '/' + relative_report)
                    refs['independent_test_report:' + relative_report] = ref
                    report_results[relative_report] = {'sha256': ref['sha256']}
                    report_results[relative_report].update(_xml_report(path, allow_skipped=scope is None))
                except (OSError, ValueError, ElementTree.ParseError) as exc:
                    errors.append(f'{relative_report}: {exc}')
                    report_results.setdefault(relative_report, {}).update(error=str(exc))
            task_execution = None
            if scope is not None:
                from .regression_evidence import validate_gradle_test_execution
                try:
                    task_execution = validate_gradle_test_execution(workspace,
                        {'tasks': tasks, 'reports': reports}, execution_report, execution_nonce)
                    for test in suite['tests']:
                        observed = task_execution['tasks'][test['task']]['reports']
                        if not set(test['report_paths']) <= set(observed):
                            raise ValueError('independent report is not produced by its declared Gradle Test task')
                    for task, observed in task_execution['tasks'].items():
                        reported_count = sum(report_results[report].get('tests', 0) for report in observed['reports'])
                        if reported_count != observed['tests']:
                            raise ValueError(f'{task}: JUnit cases disagree with observed Gradle Test execution')
                    refs['independent_test_execution'] = _artifact(root, command, 'test-task-execution.json',
                        _contained(workspace, execution_report, regular=True).read_bytes(),
                        source_path=relative + '/' + execution_report)
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    errors.append(str(exc))
            if log.is_file() and not log.is_symlink():
                refs['independent_test_log'] = {'path': log.relative_to(root).as_posix(),
                                               'sha256': file_digest(log), 'media_type': 'text/plain'}
            record = {'schema_version': 1, 'workspace': relative,
                      'design_command_id': snapshot['design_command_id'], 'execution_command_id': command.command_id,
                      'command': args, 'sandbox_command_sha256': digest(sandbox), 'tasks': tasks,
                      'exit_code': exit_code, 'timed_out': timed_out, 'reports': report_results,
                      'status': 'failed' if errors else 'passed', 'errors': errors, **coverage,
                      'frozen_characterization_gate_replaced': False}
            if review is not None:
                record['review_command_id'] = review['review_command_id']
                record['candidate_ref'] = snapshot['candidate_ref']
            if scope is not None:
                record['regression_scope'] = scope
                record['task_execution'] = task_execution
            # Retain historical hashes when an older snapshot carries them,
            # but do not require or compare them for a current execution.
            for key in ('candidate_sha256', 'suite_sha256'):
                if key in snapshot:
                    record[key] = snapshot[key]
            refs['independent_test_result'] = _json_artifact(root, command, 'independent-test-result.json', record)
            result_outputs = {**record, 'artifact_refs': refs}
            if business_gates_disabled(command) and errors:
                return handlers._unverified_result(command, status='failed', outputs=result_outputs,
                    diagnostics=[*business_diagnostics, *errors],
                    detail='independent tests executed; adverse observations retained',
                    error_code=(dependency_failure or 'independent_test_failed'))
            if business_gates_disabled(command) and business_diagnostics:
                return handlers._unverified_result(command, outputs=result_outputs,
                    diagnostics=business_diagnostics,
                    detail='independent tests executed with coverage diagnostics')
            return handlers._result(command, 'failed' if errors else 'completed',
                outputs=result_outputs,
                detail='; '.join(errors) if errors else 'independent tests produced fresh passing JUnit evidence',
                error_code=(dependency_failure or 'independent_test_failed') if errors else None)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
            if business_gates_disabled(command):
                return handlers._unverified_result(command, status='failed',
                    outputs={'observed_test_input': {
                        'regression_results': command.payload.get('regression_results', {}),
                        'workspace': command.options.get('workspace'),
                        'upstream': [{
                            'stage_id': stage,
                            'task_id': producer.get('task_id'),
                            'command_id': producer.get('command_id'),
                            'status': producer.get('status'),
                            'detail': producer.get('detail'),
                            'error_code': producer.get('error_code'),
                            'business_diagnostics': producer.get('outputs', {}).get('business_diagnostics', []),
                        } for stage in ('test_design', 'test_review')
                            if isinstance((producer := command.upstream_results.get(stage)), Mapping)]}},
                    diagnostics=[*upstream_diagnostics, str(exc)],
                    detail='independent test evidence retained without business gating',
                    error_code='independent_test_failed')
            return handlers._result(command, 'failed', detail=str(exc), error_code='independent_test_failed')


def build_test_registry():
    """Return unwrapped stages; the host adds ApprovedCandidateHandler gates."""
    return {'test_design': TestDesignHandler(), 'test_review': TestReviewHandler(), 'test_execute': TestExecuteHandler()}
