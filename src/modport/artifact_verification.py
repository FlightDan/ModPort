"""Verify a delivered binary with fresh tests, without migrating its source."""
from .workspace import project_path, project_relative, is_project_workspace
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
from zipfile import BadZipFile, ZipFile

from .evidence import atomic_json, file_digest, verified_path
from .contracts import OperationResult
from .telemetry import redact
from .target_contract import live_target_contract, target_only_workflow


def validate_submission(manifest):
    if not isinstance(manifest, dict):
        raise ValueError('artifact verification requires an authenticated handoff')
    receipts = [row for row in manifest['artifacts']
                if row['source_path'].endswith('/target-package-receipt.json')]
    if len(receipts) != 1:
        raise ValueError('select exactly one target-package-receipt.json for artifact verification')


def install_artifact_input(root, manifest, refs):
    validate_submission(manifest)
    row = next(row for row in manifest['artifacts']
               if row['source_path'].endswith('/target-package-receipt.json'))
    receipt_ref = refs['handoff:' + row['source_path']]
    receipt = json.loads(verified_path(root, receipt_ref).read_text())
    source = manifest['source']
    if (receipt.get('status') != 'passed' or receipt.get('target_clean') is not True
            or receipt.get('run_id') != source['run_id']
            or receipt.get('target_commit') != source['target_commit']):
        raise ValueError('target package receipt does not bind a clean delivered candidate')
    jars = [item for item in receipt.get('artifacts', []) if item.get('path', '').endswith('.jar')]
    if len(jars) != 1:
        raise ValueError('artifact verification requires one delivered mod JAR')
    jar = jars[0]
    jar_ref = refs.get('handoff:' + jar['path'])
    if (not isinstance(jar_ref, dict) or jar_ref.get('sha256') != jar.get('sha256')):
        raise ValueError('select the exact JAR authenticated by the target package receipt')
    jar_path = verified_path(root, jar_ref)
    if jar_path.stat().st_size != jar.get('size'):
        raise ValueError('delivered JAR size differs from its package receipt')
    descriptor = {'schema_version': 1, 'source_run_id': source['run_id'],
                  'target_commit': source['target_commit'], 'jar_ref': jar_ref,
                  'package_receipt_ref': receipt_ref}
    target = Path(root) / 'artifacts' / 'artifact-input.json'
    atomic_json(target, descriptor)
    return {'artifact_input': {'path': project_relative(root, target).as_posix(),
            'sha256': file_digest(target), 'media_type': 'application/json'}}


def artifact_input(command):
    root = Path(command.run_dir)
    descriptor = json.loads(verified_path(root, command.artifact_refs['artifact_input']).read_text())
    jar = verified_path(root, descriptor['jar_ref'])
    if file_digest(jar) != descriptor['jar_ref']['sha256']:
        raise ValueError('delivered JAR changed after submission')
    return descriptor, jar


def extract_binary(jar, destination):
    """Materialize immutable archive entries for development launch classpaths."""
    destination.mkdir(parents=True, exist_ok=False)
    with ZipFile(jar) as archive:
        seen = set()
        size = 0
        for item in archive.infolist():
            path = PurePosixPath(item.filename)
            if (path.is_absolute() or '..' in path.parts or '\\' in item.filename
                    or item.filename in seen
                    or stat.S_ISLNK(item.external_attr >> 16)):
                raise ValueError('unsafe or duplicate delivered JAR entry')
            seen.add(item.filename)
            size += item.file_size
            if size > 256 * 1024 * 1024:
                raise ValueError('expanded delivered JAR exceeds its size limit')
            target = destination / path
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as source, target.open('xb') as output:
                    shutil.copyfileobj(source, output)


ARTIFACT_INIT = '''// Host-owned binary verification: compile harness sources only.
gradle.projectsEvaluated {
    def p = rootProject
    if (!p.plugins.hasPlugin('java')) {
        throw new GradleException('Artifact verification requires a root Java source set')
    }
    def binary = p.files('/modport-artifact/classes')
    def harnessOutput = p.layout.buildDirectory.dir('modport-artifact-harness')
    p.sourceSets.main.java.setSrcDirs([p.file('.modport/harness')])
    def junitMainExclusions = __JUNIT_MAIN_EXCLUSIONS__
    if (!junitMainExclusions.isEmpty()) {
        p.sourceSets.main.java.exclude(*junitMainExclusions)
    }
    // Compile precisely the frozen test-source files, not product tests.
    p.sourceSets.test.java.setSrcDirs([])
    p.tasks.named('compileTestJava').configure { t ->
        t.setSource(p.files(__DECLARED_TEST_SOURCES__))
    }
    p.sourceSets.main.output.classesDirs.setFrom(binary, harnessOutput)
    p.sourceSets.main.output.resourcesDir = p.file('/modport-artifact/classes')
    p.tasks.named('compileJava').configure { t ->
        t.destinationDirectory.set(harnessOutput)
        t.classpath += binary
        t.source(p.files(__GAMETEST_SOURCES__))
    }
    p.tasks.named('processResources').configure { t -> t.enabled = false }
    p.tasks.withType(Jar).configureEach { t -> t.enabled = false }
    p.tasks.withType(JavaExec).configureEach { t ->
        t.doFirst {
            println('MODPORT_ARTIFACT_CLASSPATH=' + p.sourceSets.main.output.classesDirs.files)
        }
    }
}
'''


_RUNTIME_OUTPUT_DIRECTORIES = frozenset({'evidence', 'run-client', 'run-server'})


def _runtime_output_path(relative):
    parts = PurePosixPath(relative).parts
    return len(parts) >= 2 and parts[0] == '.modport' and parts[1] in _RUNTIME_OUTPUT_DIRECTORIES


def _protected_tree(worktree, *, include_protocol=False, mutable_paths=(), mutable_directories=()):
    excluded = {'.git', '.modport', '.gradle', 'build', 'run', 'runs', 'logs'}
    result = {}
    if include_protocol:
        harness = worktree / '.modport' / 'harness'
        init = worktree / '.modport' / 'characterization.init.gradle'
        if harness.is_symlink() or init.is_symlink():
            raise ValueError('symlink in artifact verification harness')
        if harness.exists() and any(path.is_symlink() for path in harness.rglob('*')):
            raise ValueError('symlink in artifact verification harness')
    for directory, names, files in os.walk(worktree, followlinks=False):
        from .local_workspace_sandbox import is_sensitive_name
        names[:] = [name for name in names if not is_sensitive_name(name)]
        files = [name for name in files if not is_sensitive_name(name)]
        parent = Path(directory)
        if parent == worktree:
            ignored = excluded - {'.modport'} if include_protocol else excluded
            names[:] = [name for name in names if name not in ignored]
            files = [name for name in files if name not in ignored]
        if parent == worktree / '.modport' and include_protocol:
            names[:] = [name for name in names if name != 'harness'
                        and name not in mutable_directories and name not in _RUNTIME_OUTPUT_DIRECTORIES]
            files = [name for name in files if name != 'characterization.init.gradle']
        for name in names + files:
            path = parent / name
            if path.is_symlink():
                raise ValueError('symlink in artifact verification candidate')
            if path.is_file():
                if include_protocol and path.relative_to(worktree).as_posix() in mutable_paths:
                    continue
                result[path.relative_to(worktree).as_posix()] = file_digest(path)
    return result


def declared_test_sources(command):
    """Return the frozen Java test paths the artifact author may adapt."""
    lock = _target_declarations(command)
    paths = set()
    for declaration in lock.get('test_evidence', {}).values():
        for value in declaration.get('test_source_files', []):
            path = PurePosixPath(value)
            if (path.is_absolute() or '..' in path.parts or '\\' in value
                    or path.as_posix() != value or len(path.parts) < 3
                    or path.parts[0] != '.modport' or path.suffix != '.java'):
                raise ValueError('artifact test source must be a contained .modport Java file')
            paths.add(value)
    return tuple(sorted(paths))


def _target_declarations(command):
    if target_only_workflow(command) and command.stage_id == 'artifact_test_design':
        path = project_path(Path(command.run_dir), 'worktree/.modport/functional-contract.json')
        return live_target_contract(command) if path.exists() else {}
    if 'functional_contract_lock' in command.artifact_refs:
        return json.loads(verified_path(Path(command.run_dir),
            command.artifact_refs['functional_contract_lock']).read_text())
    if target_only_workflow(command):
        path = project_path(Path(command.run_dir), 'worktree/.modport/functional-contract.json')
        return live_target_contract(command) if path.exists() else {}
    raise ValueError('frozen functional contract is missing')


def prepare_artifact_runtime(command):
    """Use the same binary-only classpath during author compilation and execution."""
    root = Path(command.run_dir)
    descriptor, jar = artifact_input(command)
    directory = root / 'artifacts' / 'artifact-runtime' / command.command_id
    if not (directory / 'classes').exists():
        extract_binary(jar, directory / 'classes')
    wiring = root / 'artifacts' / 'harness-wiring'
    wiring.mkdir(parents=True, exist_ok=True)
    script = wiring / (command.command_id + '-artifact.init.gradle')
    harness_directory = 'modport-artifact-harness/' + sha256(command.command_id.encode()).hexdigest()[:20]
    test_sources = junit_test_sources(command)
    main_exclusions = [value.removeprefix('.modport/harness/') for value in test_sources
                       if value.startswith('.modport/harness/')]
    contents = ARTIFACT_INIT.replace('modport-artifact-harness', harness_directory)
    if target_only_workflow(command):
        contents = contents.replace("        t.classpath += binary", "        t.classpath += binary\n        t.source(p.file('/modport-support/modport/harness/SharedGameSession.java'))")
    if target_only_workflow(command) and command.stage_id == 'artifact_test_design':
        # Resolve current declarations on each diagnostic compile, so files
        # authored after this assignment started reach compileTestJava.
        contents = contents.replace("    def junitMainExclusions = __JUNIT_MAIN_EXCLUSIONS__", """    def targetContractFile = p.file('.modport/functional-contract.json')
    def targetContract = targetContractFile.isFile() ? new groovy.json.JsonSlurper().parse(targetContractFile) : [:]
    targetContract = targetContract.contract ?: targetContract
    def junitDeclarations = (targetContract.test_evidence ?: [:]).values().findAll { it.executor == 'junit' }
    def gameTestSources = (targetContract.test_evidence ?: [:]).values().findAll { it.executor == 'gametest' }.collectMany { it.test_source_files ?: [] }.unique()
    def junitSources = junitDeclarations.collectMany { it.test_source_files ?: [] }.unique().findAll { source ->
        !source.startsWith('.modport/harness/') || junitDeclarations.any { declaration ->
            source.tokenize('/').last() == declaration.result_identity?.classname?.tokenize('.')?.last()?.tokenize('$')?.first() + '.java'
        }
    }
    (junitSources + gameTestSources).each { source ->
        if (!source.startsWith('.modport/') || source.contains('..') || source.contains('\\\\') || !source.endsWith('.java')) {
            throw new GradleException('Unsafe target test source path: ' + source)
        }
    }
    def junitMainExclusions = junitSources.findAll { it.startsWith('.modport/harness/') }.collect { it.substring('.modport/harness/'.length()) }""")
        contents = contents.replace('__DECLARED_TEST_SOURCES__', 'junitSources')
        contents = contents.replace('__GAMETEST_SOURCES__', 'gameTestSources')
    game_test_sources = [value for declaration in _target_declarations(command).get('test_evidence', {}).values()
                         if declaration.get('executor') == 'gametest'
                         for value in declaration.get('test_source_files', [])]
    script.write_text(contents.replace('__DECLARED_TEST_SOURCES__', json.dumps(test_sources))
                      .replace('__GAMETEST_SOURCES__', json.dumps(game_test_sources))
                      .replace('__JUNIT_MAIN_EXCLUSIONS__', json.dumps(main_exclusions)))
    prepared = replace(command, options={**command.options,
        'artifact_runtime_directory': str(directory), 'artifact_init_script': str(script)})
    return descriptor, directory, harness_directory, prepared


def junit_test_sources(command):
    """Keep declared JUnit classes in test output even inside the harness tree."""
    lock = _target_declarations(command)
    names = {declaration.get('result_identity', {}).get('classname', '')
             .rsplit('.', 1)[-1].split('$', 1)[0] + '.java'
             for declaration in lock.get('test_evidence', {}).values()
             if declaration.get('executor', 'junit') == 'junit'}
    allowed = {value for declaration in lock.get('test_evidence', {}).values()
               if declaration.get('executor', 'junit') == 'junit'
               for value in declaration.get('test_source_files', [])}
    return tuple(value for value in declared_test_sources(command)
                 if value in allowed and (not value.startswith('.modport/harness/')
                 or PurePosixPath(value).name in names))


DESIGN_PROMPT = '''Adapt the frozen selected source characterization tests to the target runtime.
This is verification of an already delivered JAR, not a migration assignment.
Change ONLY .modport/harness/**, .modport/characterization.init.gradle and the
exact declared Java test-source paths supplied below. Preserve
selected test IDs, assertion IDs, exact JUnit task/class/method identities and report/evidence paths from the host's
functional_contract_lock. Read the original baseline harness and fresh source
results. Repair test fixtures and port harness API calls to the exact target.
Use the current functional_contract_lock's runtime_operations names when binding
each adapted live action to evidence. Older handoff operation names are reference
material; preserve the actual action and all frozen assertions when aligning names.
Do not edit product code, Gradle project files, the frozen contract, or the JAR.
Do not weaken assertions or classify missing execution as a source defect.
The host compiles ONLY .modport/harness Java against the delivered binary's classes
and supplies that binary's resources/classes to launch; product source is excluded.
The read-only binary classes are mounted at /modport-artifact/classes during host
execution. Do not define classes with the same names as any delivered JAR class.
Keep Gradle launch, test dependencies and the frozen JUnit engine/task configuration
in characterization.init.gradle. The host registers the exact frozen Java test
files outside .modport/harness as test sources; compileTestJava must compile those
files rather than report NO-SOURCE.
If a JUnit case launches another Gradle process, pass --init-script from the
host-supplied MODPORT_ARTIFACT_INIT_SCRIPT environment variable to that process;
this keeps its classpath on the delivered binary and excludes product sources.
Read the
exact target's cached sources/class signatures; original Forge API names are not
target API evidence. Use modport_artifact_compile_artifact_harness to compile the
runtime harness and JUnit test sources against the delivered binary in the
credential-free sandbox, inspect raw errors and repair the API calls. The tool
is diagnostic, not approval or acceptance; it does not run gameplay tests or
change the delivered product. Do not launch gameplay yourself: the next deterministic host stage runs the selected game/runtime
suite, including its declared client launch, in the credential-free sandbox.
Report unsupported assertions as concrete gaps. Only explicit request_rework may
request upstream work; keep any such request confined to tests and fixtures.
'''


TARGET_DESIGN_PROMPT = '''Design executable target tests for the frozen behavior_requirements.
The user confirms that the source mod functions normally. Read the original code
and frozen requirements; source reading is the verification basis and is not a
source runtime test. Do not build, run, adapt or repair a source harness. There
is no source-test-ID port obligation. Preserve every behavior_id, assertion_id
and expected observation from behavior_requirements; choose target test IDs.
Write .modport/functional-contract.json with schema_version 1, behaviors using
id, side, preconditions, action, assertions, test_mapping and assertion_contracts
(assertion_id, text exactly equal to the requirement's expected, test_ids).
Declare baseline_gradle_tasks (these are target tasks), baseline_evidence_files
and test_evidence. Every target test needs a distinct .modport/evidence/*.json,
evidence_kind runtime, executor junit or gametest, concrete runtime_operations,
test_source_files under .modport/harness/ or .modport/tests/, and an exact
result_identity {kind:junit_xml, gradle_task, classname, name}.
The runtime .modport/functional-contract.json keeps this top-level authored
schema after freeze: behaviors and test_evidence stay at the root. The host
archives its provenance wrapper separately; do not depend on that wrapper.
Prefer official target GameTest for server behaviors. Register cases against the
locked target loader and Minecraft APIs; reuse the official run configuration.
The host binds each selected GameTest task to its own XML report under
build/test-results/<selected-task>/; do not invent a separate report directory.
Map the actual class/name identities emitted by the official reporter.
Keep launch, report collection and batch management reusable across mods and
target versions. Put version-specific game APIs and mod-specific assertions in
adapters and cases, without embedding a particular mod or target version in
the shared runner. For client cases, use the host
modport.harness.SharedGameSession from the host support (automatically compiled
into the harness output; do not duplicate its source) and batch compatible cases
in one isolated client session. Implement the version-specific adapter; use
resetBefore and resetAfter to restore world/player/config/resource state around
each case. Publish separate live evidence and XML results for each case. The
runner caches individual results for JUnit wrappers, launches once per group,
and rejects absent cases and failed resets. Do not launch nested Gradle clients
per case or use --rerun-tasks per case by default. Explicitly explain exceptional
cases requiring separate sessions. Keep JUnit wrappers out of main output.
Change ONLY .modport/harness/**, .modport/tests/**,
.modport/characterization.init.gradle and .modport/functional-contract.json.
This is verification of an already delivered JAR; never edit product source,
Gradle project files, or the binary. The host mounts delivered classes read-only
at /modport-artifact/classes and compiles only the verification harness.
Use modport_artifact_compile_artifact_harness for live declaration compilation;
it reports raw compiler failures before target contract freeze. Do not launch
gameplay yourself; the host executes the declared target suite afterwards.
Each assertion requires genuine runtime execution and operation witnesses.
Missing, skipped, failed or unimplemented required assertions cannot pass.
Do not add hashes, checksums, source_fingerprint checks or candidate/rubric
identity gates. Copy provenance supplied by the host without inventing fields.
'''


class ArtifactTestDesignHandler:
    def __call__(self, command):
        from . import handlers
        root = Path(command.run_dir)
        if target_only_workflow(command):
            return self._design_target(command, handlers, root)
        mutable_paths = declared_test_sources(command)
        # The migrated checkout can still contain a handoff-era contract.
        # Bind its runtime reader to this Run's newly frozen source contract
        # before the author snapshot; never ask the author to change the lock.
        frozen = verified_path(root, command.artifact_refs['functional_contract_lock'])
        runtime_contract = project_path(root, 'worktree/.modport/functional-contract.json')
        if (runtime_contract.is_symlink()
                or runtime_contract.parent.resolve() != runtime_contract.parent.absolute()):
            raise ValueError('artifact runtime contract path must not contain a symlink')
        runtime_contract.parent.mkdir(parents=True, exist_ok=True)
        if runtime_contract.is_file():
            archived = root / 'artifacts/executions' / command.command_id / 'target-contract-before-adaptation.json'
            archived.parent.mkdir(parents=True, exist_ok=True)
            if not archived.exists():
                shutil.copy2(runtime_contract, archived)
        # Replace the directory entry, not an existing inode that a prior
        # workspace author could have hard-linked to a product file.
        atomic_json(runtime_contract, json.loads(frozen.read_text()))
        for relative in mutable_paths:
            source = root / 'baseline' / relative
            target = project_path(root, 'worktree') / relative
            if target.is_symlink():
                raise ValueError('artifact test source must not be a symlink')
            if not target.exists() and source.is_file() and not source.is_symlink():
                if target.parent.resolve().is_relative_to((project_path(root, 'worktree')).resolve()):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
        before = _protected_tree(project_path(root, 'worktree'), include_protocol=True, mutable_paths=mutable_paths)
        descriptor, _, _, prepared = prepare_artifact_runtime(command)
        snapshot = root / 'artifacts' / 'executions' / command.command_id / 'artifact-product-snapshot.json'
        atomic_json(snapshot, before)
        snapshot_ref = {'path': project_relative(root, snapshot).as_posix(),
                        'sha256': file_digest(snapshot), 'media_type': 'application/json'}
        result = handlers.CodexStageHandler(DESIGN_PROMPT + '\nDelivered input: '
                                            + json.dumps(descriptor)
                                            + '\nDeclared mutable test sources: '
                                            + json.dumps(mutable_paths))(prepared)
        result = replace(result, outputs={**result.outputs, 'artifact_refs': {
            **result.outputs.get('artifact_refs', {}), 'artifact_product_snapshot': snapshot_ref}})
        if before != _protected_tree(project_path(root, 'worktree'), include_protocol=True, mutable_paths=mutable_paths):
            return handlers._result(command, 'failed', outputs=result.outputs,
                error_code='artifact_candidate_changed', detail='Test author changed delivered product files')
        return result

    def _design_target(self, command, handlers, root):
        mutable = ('.modport/functional-contract.json',)
        before = _protected_tree(project_path(root, 'worktree'), include_protocol=True,
                                 mutable_paths=mutable, mutable_directories=('tests',))
        descriptor, _, _, prepared = prepare_artifact_runtime(command)
        snapshot = root / 'artifacts/executions' / command.command_id / 'artifact-product-snapshot.json'
        atomic_json(snapshot, before)
        result = handlers.CodexStageHandler(TARGET_DESIGN_PROMPT + '\nDelivered input: '
                                            + json.dumps(descriptor))(prepared)
        result = replace(result, outputs={**result.outputs, 'artifact_refs': {
            **result.outputs.get('artifact_refs', {}), 'artifact_product_snapshot': {
                'path': project_relative(root, snapshot).as_posix(), 'media_type': 'application/json'}}})
        if before != _protected_tree(project_path(root, 'worktree'), include_protocol=True,
                                     mutable_paths=mutable, mutable_directories=('tests',)):
            return handlers._result(command, 'failed', outputs=result.outputs,
                error_code='artifact_candidate_changed', detail='Test author changed delivered product files')
        return result


class ArtifactTestExecuteHandler:
    def __call__(self, command):
        from . import handlers
        try:
            prepared = self._prepare(command)
        except (OSError, ValueError, KeyError, TypeError, BadZipFile, RuntimeError) as exc:
            diagnostic = {'category': 'artifact_test_setup', 'exception': type(exc).__name__,
                          'detail': redact(str(exc)), 'process_executed': False,
                          'acceptance_status': 'unverified'}
            path = Path(command.run_dir) / 'artifacts' / 'executions' / command.command_id / 'artifact-setup-failure.json'
            atomic_json(path, diagnostic)
            return handlers._result(command, 'failed', outputs={**diagnostic, 'artifact_refs': {
                'artifact_setup_failure': {'path': path.relative_to(command.run_dir).as_posix(),
                    'sha256': file_digest(path), 'media_type': 'application/json'}}},
                error_code='artifact_test_setup_failed', detail=diagnostic['detail'])
        if isinstance(prepared, OperationResult):
            return prepared
        return self._execute(command, *prepared)

    def _prepare(self, command):
        from . import handlers
        root = Path(command.run_dir)
        before = _protected_tree(project_path(root, 'worktree'))
        expected = json.loads(verified_path(root,
            command.artifact_refs['artifact_product_snapshot']).read_text())
        # Runtime receipts and isolated game directories are regenerated by
        # verification. Old author snapshots may include them; they are never
        # delivered product inputs and must not block a fresh target execution.
        expected = {path: value for path, value in expected.items() if not _runtime_output_path(path)}
        mutable_paths = (('.modport/functional-contract.json',) if target_only_workflow(command)
                         else declared_test_sources(command))
        mutable_directories = ('tests',) if target_only_workflow(command) else ()
        if _protected_tree(project_path(root, 'worktree'), include_protocol=True, mutable_paths=mutable_paths,
                           mutable_directories=mutable_directories) != expected:
            return handlers._result(command, 'failed', error_code='artifact_candidate_changed',
                detail='Delivered product files changed during test design; binary execution was not started',
                outputs={'process_executed': False, 'acceptance_status': 'unverified'})
        descriptor, directory, harness_directory, execution = prepare_artifact_runtime(command)
        return root, before, descriptor, directory, harness_directory, execution

    def _execute(self, command, root, before, descriptor, directory, harness_directory, execution):
        from . import handlers
        result = handlers.BaselineContractVerificationHandler(baseline=False,
                    require_client_evidence=True)(execution)
        collisions = []
        classes = project_path(root, 'worktree') / 'build' / harness_directory
        if classes.exists():
            for path in classes.rglob('*.class'):
                if (directory / 'classes' / path.relative_to(classes)).is_file():
                    collisions.append(project_relative(root, path).as_posix())
        outputs = {**result.outputs, 'artifact_input': descriptor,
                   'artifact_class_collisions': collisions,
                   'product_sources_unchanged': before == _protected_tree(project_path(root, 'worktree'))}
        if collisions or not outputs['product_sources_unchanged']:
            return handlers._result(command, 'failed', outputs=outputs,
                error_code='artifact_binary_shadowed', detail='Harness changed or shadowed delivered classes')
        return replace(result, outputs=outputs)


class ArtifactTestReportHandler:
    def __call__(self, command):
        from . import handlers
        root = Path(command.run_dir)
        result = command.upstream_results.get('artifact_test_execute', {})
        descriptor, _ = artifact_input(command)
        report = {'schema_version': 1, 'run_id': command.run_id,
                  'artifact_input': descriptor, 'acceptance_status': 'unverified',
                  'source_verification': command.upstream_results.get('contract_verify', {}),
                  'target_verification': result}
        from .artifact_verification_policy import (
            required_behavior_policy, required_behavior_assessments)
        required_status = None
        if required_behavior_policy(command):
            assessments = required_behavior_assessments(command, root, command.upstream_results)
            required_status = ('passed' if all(row['status'] == 'passed'
                               for row in assessments.values()) else 'failed')
            report.update(required_behavior_status=required_status,
                          required_behavior_assessments=assessments)
        if target_only_workflow(command):
            report['source_verification'] = {'verification_basis': 'source_reading',
                'source_assumption': 'user_confirmed_functional', 'runtime_tested': False,
                'behavior_requirements': command.artifact_refs.get('behavior_requirements')}
        target = root / 'artifacts' / 'artifact-verification-report.json'
        atomic_json(target, report)
        return handlers._result(command, 'failed' if required_status == 'failed' else 'completed',
            error_code='required_behavior_unverified' if required_status == 'failed' else None,
            outputs={'acceptance_status': 'unverified',
            **({'required_behavior_status': required_status,
                'required_behavior_assessments': assessments} if required_status is not None else {}),
            'target_verification_status': result.get('status', 'unknown'),
            'artifact_refs': {'artifact_verification_report': {'path': project_relative(root, target).as_posix(),
                'sha256': file_digest(target), 'media_type': 'application/json'}}},
            detail=('Source-reading requirements and delivered binary test observations archived'
                    if target_only_workflow(command) else
                    'Fresh source and delivered binary test observations archived'))
