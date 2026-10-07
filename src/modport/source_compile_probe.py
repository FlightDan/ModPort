"""Compile conventional Java sources in a separate authenticated MDK skeleton.

Custom project build logic is preserved, not translated or certified here.
"""

from .workspace import project_path, is_project_workspace
from hashlib import sha256
from dataclasses import replace
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from .build_preparation import locked_probe_files, UnsupportedBuildLayout


def probe_java_home(root, manifest):
    from .models import LockedManifest
    locked = manifest if isinstance(manifest, LockedManifest) else LockedManifest.from_mapping(manifest)
    cache = Path(root).absolute() / 'toolchains/gradle-cache'
    relative = Path(locked.java_toolchain.get('executable', ''))
    executable = cache / relative
    if (relative.is_absolute() or '..' in relative.parts or not relative.parts
            or cache.resolve() != cache or executable.resolve() != executable
            or not executable.is_file() or executable.stat().st_size > 16 * 1024 * 1024
            or sha256(executable.read_bytes()).hexdigest() != locked.java_toolchain.get('java_sha256')):
        raise ValueError('probe Java executable differs from authenticated toolchain')
    return executable.parent.parent


def prepare_source_probe(root, work, manifest):
    root, work = Path(root).resolve(), Path(work).absolute()
    if work.resolve() != work or not is_project_workspace(root, work):
        raise ValueError('source probe input must be a contained regular worktree')
    files, properties = locked_probe_files(manifest, root / 'toolchains/mdk')
    source = work / 'src/main/java'
    if source.resolve() != source or not source.is_dir():
        raise UnsupportedBuildLayout(['conventional src/main/java is unavailable'])
    copied, total = {}, 0
    for visited, path in enumerate(source.rglob('*')):
        if visited >= 20000:
            raise UnsupportedBuildLayout(['source probe directory traversal limit reached'])
        if path.is_symlink() or path.resolve() != path.absolute():
            raise ValueError('source probe rejects symlinked input')
        if path.is_dir():
            continue
        if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            raise UnsupportedBuildLayout(['source probe input exceeds file limit'])
        data = path.read_bytes()
        total += len(data)
        if total > 32 * 1024 * 1024 or len(copied) >= 10000:
            raise UnsupportedBuildLayout(['source probe input exceeds aggregate limit'])
        copied[path.relative_to(work).as_posix()] = data
    if not any(name.endswith('.java') for name in copied):
        raise UnsupportedBuildLayout(['no conventional Java sources to compile'])
    parent = root / 'workspaces' / 'source-probes'
    if parent.resolve() != parent.absolute():
        raise ValueError('source probe output parent contains a symlink')
    parent.mkdir(parents=True, exist_ok=True)
    probe = Path(tempfile.mkdtemp(prefix='candidate-', dir=parent))
    for relative, (data, mode) in files.items():
        path = probe / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as stream:
            stream.write(data)
        path.chmod(mode)
    for relative, data in copied.items():
        path = probe / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as stream:
            stream.write(data)
    record = {
        'scope': 'provisional_mdk_source_compile',
        'source_files': {name: sha256(data).hexdigest() for name, data in copied.items()},
        'configuration_files': {name: sha256(data).hexdigest() for name, (data, _) in files.items()},
        'required_properties': dict(properties),
        'original_build_preserved': True, 'project_build_verified': False,
        'acceptance_evidence': False,
        'limitations': ['Only src/main/java; custom source sets and generated sources not covered.',
                        'Custom dependencies, processors, resources and packaging are not translated.',
                        'Missing dependency errors may reflect this provisional classpath.'],
    }
    return probe, properties, record


def compile_source_probe(command, manifest):
    from . import handlers
    root = Path(command.run_dir)
    try:
        probe, properties, record = prepare_source_probe(root, project_path(root, 'worktree'), manifest)
    except UnsupportedBuildLayout as error:
        return handlers._result(command, 'failed', error_code='target_build_configuration_missing',
            detail=str(error), outputs={'process_executed': False, 'acceptance_status': 'unverified'})
    identity = (probe.stat().st_dev, probe.stat().st_ino)
    result, cleanup_error = None, None
    try:
        result = _run_source_probe(command, manifest, probe, properties, record)
    finally:
        # Preserve a mutated compiler workspace for forensic inspection. Normal
        # scratch sources and build products are recoverable from the frozen
        # candidate/MDK, so remove only this exact host-created directory.
        if result is None or result.error_code != 'candidate_identity_mismatch':
            try:
                parent = root.absolute() / 'workspaces/source-probes'
                if (probe.parent != parent or probe.resolve() != probe.absolute()
                        or not probe.name.startswith('candidate-')
                        or (probe.stat().st_dev, probe.stat().st_ino) != identity):
                    raise ValueError('probe workspace identity changed; cleanup refused')
                shutil.rmtree(probe)
            except (OSError, ValueError) as error:
                cleanup_error = str(error)
    retained = probe.exists() or probe.is_symlink()
    return replace(result, outputs={**result.outputs,
        'probe_workspace_removed': not retained,
        **({'retained_probe_workspace': probe.relative_to(root).as_posix()} if retained else {}),
        **({'probe_cleanup_diagnostic': cleanup_error} if cleanup_error else {})})


def _run_source_probe(command, manifest, probe, properties, record):
    from . import handlers
    from .development import _artifact
    root = Path(command.run_dir)
    refs = {'source_compile_probe': _artifact(command, 'source-compile-probe.json',
                                             json.dumps(record, sort_keys=True).encode())}
    java_home = probe_java_home(root, manifest)
    try:
        args = handlers._sandboxed_build_command(root, probe,
            ['bash', '/workspace/gradlew', '--no-daemon', '--max-workers=1',
             '-Dorg.gradle.jvmargs=-Xmx1536m', *('-P' + key + '=' + value for key, value in properties), 'compileJava'],
            java_home=java_home, operation=command)
    except RuntimeError as error:
        return handlers._result(command, 'failed', error_code='build_sandbox_unavailable',
            detail=str(error), outputs={'process_executed': False, 'artifact_refs': refs})
    log = root / 'logs' / ('source-compile-probe-' + command.command_id + '.log')
    timed_out = False
    try:
        result = handlers._exec(args, cwd=root, log=log,
                                timeout=handlers._remaining_timeout(command, 1200))
        returncode = result.returncode
        output = result.stdout
    except TimeoutError:
        return handlers._result(command, 'failed', error_code='budget_exhausted',
            detail='Source compile probe has no remaining execution budget.',
            outputs={'process_executed': False, 'artifact_refs': refs})
    except subprocess.TimeoutExpired as error:
        timed_out = True
        returncode = 124
        output = error.stdout or ''
        if isinstance(output, bytes):
            output = output.decode('utf-8', errors='replace')
    raw_log = output.encode('utf-8')
    oversized = len(raw_log) > 32 * 1024 * 1024
    refs['gradle_log:provisional_source_compile'] = _artifact(
        command, 'source-compile-probe.log', raw_log[:32 * 1024 * 1024])
    unchanged = True
    for relative, expected in {**record['source_files'], **record['configuration_files']}.items():
        path = probe / relative
        if (path.is_symlink() or path.resolve() != path.absolute() or not path.is_file()
                or path.stat().st_size > 2 * 1024 * 1024
                or sha256(path.read_bytes()).hexdigest() != expected):
            unchanged = False
            break
    if not unchanged:
        return handlers._result(command, 'failed', error_code='candidate_identity_mismatch',
            detail='Provisional compiler modified its declared source/configuration inputs.',
            outputs={'process_executed': True, 'probe_inputs_unchanged': False,
                     'acceptance_status': 'unverified', 'artifact_refs': refs})
    if oversized:
        return handlers._result(command, 'failed', error_code='compile_evidence_too_large',
            detail='Probe log exceeds the capture limit; bounded prefix retained, raw process log remains on disk.',
            outputs={'process_executed': True, 'probe_inputs_unchanged': True,
                     'log_truncated': True, 'raw_log': log.relative_to(root).as_posix(),
                     'compile_scope': record['scope'], 'project_build_verified': False,
                     'acceptance_status': 'unverified', 'artifact_refs': refs})
    compiler_error = any(
        match.group('source') in record['source_files']
        for match in re.finditer(
            r'(?m)^[ \t]*(?:/workspace/)?(?P<source>src/main/java/[^\r\n]+?\.java):[1-9]\d*: error:',
            output))
    diagnostic_only = (command.options.get('workflow_version', 0) >= 26
                       and not timed_out and compiler_error)
    return handlers._result(command,
        'completed' if returncode == 0 or diagnostic_only else 'failed',
        error_code=(None if returncode == 0 or diagnostic_only else
                    'source_compile_probe_timeout' if timed_out else
                    'provisional_source_compile_failed'),
        detail='Provisional locked-MDK source compilation; original project build remains unverified.',
        outputs={'process_executed': True, 'probe_returncode': returncode,
                 'probe_status': ('timed_out' if timed_out else
                                  'passed' if returncode == 0 else 'failed'),
                 'diagnostic_code': (None if returncode == 0 else
                                     'source_compile_probe_timeout' if timed_out else
                                     'provisional_source_compile_failed'),
                 'diagnostics': ([] if returncode == 0 else [
                     ('Provisional MDK source compile timed out' if timed_out else
                      'Provisional MDK source compile emitted Java errors' if compiler_error else
                      'Provisional MDK source compile did not complete successfully')
                     + '; inspect its captured log. The provisional classpath omits custom '
                       'project dependencies and does not verify the original project build.']),
                 'probe_inputs_unchanged': True,
                 'compile_scope': record['scope'], 'project_build_verified': False,
                 'acceptance_status': 'unverified', 'artifact_refs': refs})
