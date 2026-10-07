"""Host verification of a coder's frozen task checks, not overall migration proof."""
from __future__ import annotations

from .author_contracts import acceptance_report_contract, validate_report_shape

from hashlib import sha256
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
from typing import Mapping
from xml.etree import ElementTree

from .goal_planning import owned_path, relative_path, validate_goal
from .regression_evidence import install_gradle_test_listener, validate_gradle_test_execution
from .business_policy import business_gates_disabled
from .local_workspace_sandbox import is_sensitive_name
from .workspace import is_project_workspace

def _sensitive_copy_name(name):
    return is_sensitive_name(name)


def export_goal_evidence(root, evidence):
    """Expose the proof files to retry copying without reopening the summary."""
    from .contracts import json_copy
    from .repair_evidence import is_repair_artifact_ref
    proof = json_copy(evidence)
    candidate = relative_path(proof['candidate']['workspace'])
    if isinstance(proof.get('acceptance_report'), dict) and isinstance(
            proof['acceptance_report'].get('path'), str):
        proof['acceptance_report']['path'] = candidate + '/' + relative_path(
            proof['acceptance_report']['path'])
    for check in proof.get('checks', {}).values():
        if not isinstance(check, dict):
            continue
        if check.get('type') in {'gradle_tasks', 'gradle_regression'}:
            workspace = relative_path(check['workspace'])
            for report in check.get('reports', []):
                report['path'] = workspace + '/' + relative_path(report['path'])
        elif isinstance(check.get('path'), str):
            check['path'] = candidate + '/' + relative_path(check['path'])
    refs = {}
    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            if is_repair_artifact_ref(value):
                contained_file(Path(root), value['path'])
                alias = 'goal_host_evidence:' + sha256(value['path'].encode()).hexdigest()
                refs[alias] = {'path': value['path'], 'sha256': value['sha256']}
            else:
                for item in value.values():
                    visit(item)
    visit(proof)
    return proof, refs


def _check_snapshot(command, workspace):
    """Copy candidate files into a fresh build workspace without Git metadata."""
    from .handlers import _remaining_timeout
    from .evidence import file_digest
    root = Path(command.run_dir).absolute()
    workspace = Path(workspace).absolute()
    if root.resolve() != root or workspace.resolve() != workspace:
        raise ValueError('goal snapshot roots must not traverse symlinks')
    if not workspace.is_relative_to(root) and not is_project_workspace(root, workspace):
        raise ValueError('goal snapshot source is outside the Run and registered workspace')
    storage = root / 'artifacts' / 'goal-checks'
    if storage.resolve() != storage.absolute():
        raise ValueError('unsafe goal check storage')
    storage.mkdir(parents=True, exist_ok=True)
    identity = sha256(str(command.command_id).encode()).hexdigest()[:24]
    directory = Path(tempfile.mkdtemp(prefix=identity + '-', dir=storage))
    snapshot = directory / 'workspace'
    snapshot.mkdir()
    files = {}
    for folder, dirs, names in os.walk(workspace, followlinks=False):
        _remaining_timeout(command, 600)
        folder = Path(folder)
        # Exclude every nested Git repository and our own host storage when
        # the caller supplies the Run root as its candidate workspace.
        dirs[:] = [name for name in dirs
                   if not _sensitive_copy_name(name) and folder / name != storage]
        names = [name for name in names if not _sensitive_copy_name(name)]
        for name in [*dirs, *names]:
            source = folder / name
            relative = source.relative_to(workspace)
            mode = source.lstat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise ValueError('goal snapshot rejects symlinks and special files: ' + relative.as_posix())
            destination = snapshot / relative
            if stat.S_ISDIR(mode):
                destination.mkdir(exist_ok=True)
            else:
                _remaining_timeout(command, 600)
                shutil.copy2(source, destination)
                files[relative.as_posix()] = {'sha256': file_digest(destination), 'mode': stat.S_IMODE(mode)}
    manifest = directory / 'candidate-files.json'
    manifest.write_text(json.dumps(files, sort_keys=True) + '\n')
    return snapshot, {'workspace': snapshot.relative_to(root).as_posix(),
                      'candidate_manifest': {'path': manifest.relative_to(root).as_posix(), 'sha256': file_digest(manifest)}}


def contained_file(workspace: Path, relative: str) -> Path:
    relative_path(relative)
    root = Path(workspace).absolute()
    if root.resolve() != root:
        raise ValueError('workspace traverses a symlink')
    target = root
    for part in relative.split('/'):
        target = target / part
        if target.is_symlink():
            raise ValueError('goal path traverses a symlink')
    if not target.resolve().is_relative_to(root) or not target.is_file():
        raise ValueError(f'goal file missing or unsafe: {relative}')
    return target


def _regression_report(workspace, relative):
    """Require actual passing cases; declared totals alone are not execution proof."""
    path = contained_file(workspace, relative)
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('JUnit report exceeds 16 MiB')
    data = path.read_bytes()
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ValueError('JUnit reports must not declare XML entities')
    try:
        document = ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise ValueError('invalid JUnit XML report') from exc
    if document.tag not in {'testsuite', 'testsuites'}:
        raise ValueError('expected JUnit testsuite or testsuites')
    counts = None
    for node in document.iter():
        if node.tag not in {'testsuite', 'testsuites'}:
            continue
        cases = list(node.iter('testcase'))
        actual = {'tests': len(cases),
                  'failures': sum(case.find('failure') is not None for case in cases),
                  'errors': sum(case.find('error') is not None for case in cases),
                  'skipped': sum(case.find('skipped') is not None for case in cases)}
        if counts is None:
            counts = actual
        for key, count in actual.items():
            value = node.get(key)
            if value is None:
                if node.tag == 'testsuite' and key in {'tests', 'failures', 'errors'}:
                    raise ValueError('JUnit report lacks ' + key + ' count')
            elif not re.fullmatch(r'[0-9]+', value) or int(value) != count:
                raise ValueError('JUnit ' + key + ' count disagrees with actual cases')
    if not counts['tests'] or counts['failures'] or counts['errors'] or counts['skipped']:
        raise ValueError('regression requires actual passing tests without failures, errors or skipped cases')
    return {'path': relative, 'sha256': sha256(data).hexdigest(), **counts}


def _check(command, workspace, check):
    kind = check['type']
    if kind in {'gradle_tasks', 'gradle_regression'}:
        from .contract_inputs import validate_baseline_gradle_tasks
        from .handlers import (_sandboxed_build_command, _locked_java_home, _remaining_timeout,
                               _forge_baseline_init, _client_launch_arguments)
        from .telemetry import probe_process
        tasks = validate_baseline_gradle_tasks(check['tasks'])
        root = Path(command.run_dir)
        contained_file(workspace, 'gradlew')
        snapshot, evidence = _check_snapshot(command, workspace)
        for relative in check.get('reports', []):
            path = snapshot / relative
            # Snapshot copying already rejects links. Do not allow a stale
            # committed/generated report to stand in for this execution.
            if path.exists():
                contained_file(snapshot, relative).unlink()
        baseline = command.payload.get('goal_scope') == 'contract'
        cache_name = 'baseline-contract-gradle-cache' if baseline else 'target-contract-gradle-cache'
        gradle = ['bash', '/workspace/gradlew', '--no-daemon', '--rerun-tasks', '--no-build-cache']
        if kind == 'gradle_regression':
            gradle.append('--console=plain')
        if baseline:
            gradle.extend(['--init-script', _forge_baseline_init(root, cache_name=cache_name)])
        from .harness_wiring import characterization_init_scripts
        from .evidence import file_digest
        wiring_directory = root / 'artifacts' / 'harness-wiring'
        wiring_refs = []
        for script in characterization_init_scripts(
                snapshot, workflow_version=command.options.get('workflow_version', 0),
                supplement_directory=wiring_directory):
            sandbox_path = ('/modport-wiring/' + script.name if script.parent == wiring_directory
                            else '/workspace/' + script.relative_to(snapshot).as_posix())
            gradle.extend(['--init-script', sandbox_path])
            wiring_refs.append({'path': script.relative_to(root).as_posix(), 'sha256': file_digest(script)})
        evidence['harness_wiring'] = wiring_refs
        gradle.extend(tasks)
        declarations, provenance = _goal_test_evidence(command, root, snapshot, baseline=baseline)
        for item in declarations.values():
            relative = relative_path(item['path'])
            if not relative.startswith('.modport/evidence/'):
                raise ValueError('goal runtime evidence must stay within .modport/evidence')
            (snapshot / relative).unlink(missing_ok=True)
        nonce = secrets.token_hex(32)
        if kind == 'gradle_regression':
            listener, execution_report = install_gradle_test_listener(snapshot, nonce)
            gradle.extend(['--init-script', '/workspace/' + listener])
        environment = {'MODPORT_EXECUTION_ID': command.command_id,
                       'MODPORT_EVIDENCE_NONCE': nonce,
                       'MODPORT_EXECUTOR_FINGERPRINTS': json.dumps(
                           {key: item['executor_fingerprint'] for key, item in provenance.items()},
                           sort_keys=True, separators=(',', ':'))}
        if (any(item.get('evidence_kind') == 'runtime' and item.get('executor') == 'client_smoke'
                for item in declarations.values())
                or any(task.split(':')[-1] == 'runClient' for task in tasks)):
            timeout = _remaining_timeout(command, 600)
            gradle = _client_launch_arguments(root, command, gradle, timeout=max(0.01, timeout - 5))
        args = _sandboxed_build_command(root, snapshot, gradle, cache_name=cache_name,
                                        java_home=None if baseline else _locked_java_home(root),
                                        environment=environment, operation=command)
        evidence['execution_nonce'] = nonce
        evidence['executor_provenance'] = provenance
        result = probe_process(args, cwd=snapshot, env={}, timeout=_remaining_timeout(command, 600),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for channel in ('stdout', 'stderr'):
            data = getattr(result, channel).encode()
            log = snapshot.parent / (channel + '.log')
            log.write_bytes(data)
            evidence[channel] = {'path': log.relative_to(root).as_posix(), 'sha256': sha256(data).hexdigest()}
        failure = f'Gradle check failed with exit code {result.returncode}' if result.returncode else ''
        if kind == 'gradle_regression' and not failure:
            try:
                observed = re.findall(r'^> Task[ \t]+(:\S+?)(?:[ \t]+([A-Z][A-Z-]*))?[ \t]*$',
                                      result.stdout, re.MULTILINE)
                for requested in tasks:
                    matched = [(task, outcome) for task, outcome in observed
                               if task == requested]
                    # Quiet Gradle logging may omit successful task lines.
                    # Positive execution evidence comes from the listener;
                    # explicit console skip outcomes remain contradictions.
                    for task, outcome in matched:
                        if outcome:
                            raise ValueError(f'regression task {task} did not execute: {outcome}')
                execution = validate_gradle_test_execution(snapshot, check, execution_report, nonce)
                evidence['regression_execution'] = {
                    **execution, 'path': (snapshot / execution_report).relative_to(root).as_posix()}
                evidence['reports'] = [_regression_report(snapshot, relative) for relative in check['reports']]
            except (OSError, ValueError, TypeError) as exc:
                failure = str(exc)
        return {'type': kind, 'tasks': tasks, 'returncode': result.returncode, **evidence,
                'passed': not failure,
                **({'detail': failure} if failure else {}),
                'stdout_sha256': sha256(result.stdout.encode()).hexdigest(),
                'stderr_sha256': sha256(result.stderr.encode()).hexdigest()}
    path = contained_file(workspace, check['path'])
    data = path.read_bytes()
    if kind in {'json_valid', 'contract_schema'}:
        body = json.loads(data)
        if kind == 'contract_schema':
            from .characterization import CharacterizationContract
            from .contract_inputs import validate_baseline_gradle_tasks
            CharacterizationContract.from_mapping(body)
            validate_baseline_gradle_tasks(body.get('baseline_gradle_tasks'))
    elif kind == 'python_syntax':
        compile(data, str(path), 'exec', dont_inherit=True)
    return {'type': kind, 'path': check['path'], 'sha256': sha256(data).hexdigest()}


def _goal_test_evidence(command, root, snapshot, *, baseline):
    """Use the same declared harness inputs as the independent runtime gates."""
    from .handlers import (_acceptance_rubric_for, _test_evidence_declarations,
                           _runtime_executor_provenance)
    contract = {}
    locked = root / 'artifacts/functional-contract.lock.json'
    if business_gates_disabled(command) and isinstance(
            command.artifact_refs.get('functional_contract_lock'), Mapping):
        from .evidence import verified_path
        locked = verified_path(root, command.artifact_refs['functional_contract_lock'])
    candidate = snapshot / '.modport/functional-contract.json'
    if not baseline and locked.exists():
        body = json.loads(contained_file(root, locked.relative_to(root).as_posix()).read_text())
        contract = {**body['contract'], **body.get('acceptance_rubric', {}),
                    **{key: body[key] for key in ('baseline_evidence_files', 'test_evidence')}}
    elif candidate.exists():
        contract = json.loads(contained_file(snapshot, '.modport/functional-contract.json').read_text())
    if not isinstance(contract, Mapping):
        raise ValueError('goal characterization contract must be an object')
    if not contract.get('test_evidence'):
        return {}, {}
    rubric = _acceptance_rubric_for(command, root)
    declarations = _test_evidence_declarations(contract, rubric)
    return declarations, _runtime_executor_provenance(snapshot, declarations, rubric)


def validate_goal_candidate(command, workspace: Path, goal: Mapping) -> dict:
    """Return fresh evidence for every declared check and report mapping.

    A successful result certifies this frozen check set only. Independent review,
    integration, and migration acceptance gates must still evaluate its adequacy.
    """
    from .handlers import _remaining_timeout
    _remaining_timeout(command, 600)
    failures, evidence = [], {'scope': 'task-check acceptance', 'checks': {}}
    advisory = business_gates_disabled(command)
    double_check = command.options.get('workflow_version', 0) >= 12
    try:
        task = command.payload.get('development_task')
        context = command.payload.get('planning_context')
        if not isinstance(task, Mapping) or not isinstance(context, Mapping):
            raise ValueError('frozen development task and planning context are required')
        goal = validate_goal(goal, task, context, require_double_check=double_check,
                             gates_disabled=advisory)
    except (TypeError, ValueError, KeyError) as exc:
        return {'accepted': False, 'failures': [str(exc)], 'evidence': evidence}
    used_identifiers = set()
    for index, check in enumerate(goal['checks']):
        _remaining_timeout(command, 600)
        identifier = check.get('id') if isinstance(check, Mapping) else None
        if not isinstance(identifier, str) or not identifier or identifier in used_identifiers:
            identifier = f'advisory-check-{index + 1}'
            while identifier in used_identifiers:
                identifier += '-duplicate'
        used_identifiers.add(identifier)
        try:
            if not isinstance(check, Mapping):
                raise TypeError('check observation must be an object')
            evidence['checks'][identifier] = {'passed': True, **_check(command, Path(workspace), check)}
            if not evidence['checks'][identifier]['passed']:
                failures.append(f"check {identifier}: {evidence['checks'][identifier]['detail']}")
        except TimeoutError:
            raise
        except (OSError, ValueError, TypeError, KeyError, SyntaxError, RuntimeError, subprocess.SubprocessError) as exc:
            failures.append(f"check {identifier}: {exc}")
            evidence['checks'][identifier] = {'passed': False, 'detail': str(exc)}
    _remaining_timeout(command, 600)
    try:
        path = contained_file(Path(workspace), goal['acceptance_report'])
        data = path.read_bytes()
        evidence['acceptance_report'] = {'path': goal['acceptance_report'],
            'sha256': sha256(data).hexdigest(), 'raw_report': data.decode('utf-8', errors='replace')}
        if not advisory:
            try:
                report = json.loads(data)
            except json.JSONDecodeError:
                if command.options.get('workflow_version', 0) >= 11:
                    raise
                report = None
            if report is None:
                raise_report_validation = False
            else:
                raise_report_validation = True
                validate_report_shape(report, acceptance_report_contract(goal, double_check=double_check))
            if not raise_report_validation:
                report = None
            else:
                entries = report['acceptance']
                expected_criteria = set(goal['acceptance'])
                check_by_id = {check['id']: check for check in goal['checks']}
                seen = set()
                for entry in entries:
                    criterion = entry.get('criterion')
                    refs = entry.get('evidence')
                    if (criterion not in expected_criteria or criterion in seen
                            or entry.get('state') != 'passed'
                            or not isinstance(refs, list) or not refs
                            or len(refs) != len(set(refs))):
                        raise ValueError('acceptance report must pass every frozen criterion exactly once')
                    seen.add(criterion)
                    for identifier in refs:
                        check = check_by_id.get(identifier)
                        if (check is None or criterion not in check['acceptance']
                                or not evidence['checks'].get(identifier, {}).get('passed')):
                            raise ValueError('acceptance report cites unknown, failed, or unmapped check evidence')
                if seen != expected_criteria:
                    raise ValueError('acceptance report must pass every frozen criterion exactly once')
                if double_check:
                    review = report.get('self_check')
                    if not isinstance(review, Mapping) or set(review) != {
                            'state', 'reviewed_paths', 'checks', 'summary'}:
                        raise ValueError('self_check must contain exactly state, reviewed_paths, checks, summary')
                    reviewed = review.get('reviewed_paths')
                    if (review.get('state') != 'passed' or not isinstance(reviewed, list)
                            or not reviewed or len(reviewed) != len(set(reviewed))
                            or any(not isinstance(item, str) for item in reviewed)
                            or not isinstance(review.get('summary'), str)
                            or not review['summary'].strip()
                            or set(review.get('checks', [])) != set(check_by_id)
                            or len(review.get('checks', [])) != len(check_by_id)):
                        raise ValueError('self_check must record a complete passing review and every frozen check')
                    for reviewed_path in reviewed:
                        owned_path(reviewed_path, goal['owned_paths'])
                    changed = command.payload.get('candidate_changed_paths')
                    if not isinstance(changed, list):
                        changed = [check['path'] for check in goal['checks'] if 'path' in check]
                    if any(path not in reviewed for path in changed):
                        raise ValueError('self_check omits changed candidate paths')
                evidence['acceptance_report']['report'] = report
    except (OSError, ValueError, TypeError, KeyError) as exc:
        failures.append(f'acceptance report: {exc}')
    _remaining_timeout(command, 600)
    result = {'accepted': not failures, 'failures': failures, 'evidence': evidence}
    if failures and not advisory:
        result['expected_format'] = acceptance_report_contract(goal, double_check=double_check)
    return result
