"""Create the native build sandbox command used by production handlers."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
from uuid import uuid4

from .platform_files import atomic_write, assert_no_reparse


def _materialize_runtime(root: Path, source: Path, kind: str) -> Path:
    """Give the sandbox a private user-owned copy of an installed runtime.

Granting AppContainer rights on Program Files requires administrator rights.
Runtime copies live outside project/cache write grants and carry no credentials.
The source path is navigation/provenance, not an added fingerprint gate.
"""
    from .platform_files import FileLock, make_private_directory, safe_open
    destination = make_private_directory(root / 'toolchains' / 'native-runtimes')
    with FileLock(destination / 'publication.lock'):
        registry = destination / 'runtimes.json'
        records = []
        if registry.exists():
            fd = safe_open(destination, registry.name)
            with os.fdopen(fd, 'r', encoding='utf-8') as stream:
                records = json.load(stream)
        for record in records:
            if record['source'] == str(source) and record['kind'] == kind:
                candidate = destination / record['directory']
                assert_no_reparse(candidate)
                return candidate
        name = kind + '-' + uuid4().hex
        candidate = destination / name
        def copy(directory, target):
            assert_no_reparse(directory)
            target.mkdir()
            for entry in directory.iterdir():
                if kind == 'python' and entry.name.lower() in {'site-packages', 'scripts', '__pycache__'}:
                    continue
                if kind == 'git' and entry.name.lower() in {'gitconfig', '.gitconfig', '.git-credentials'}:
                    continue
                assert_no_reparse(entry)
                if entry.is_dir():
                    copy(entry, target / entry.name)
                elif entry.is_file():
                    shutil.copyfile(entry, target / entry.name)
        try:
            copy(source, candidate)
            records.append({'source': str(source), 'kind': kind, 'directory': name})
            atomic_write(registry, (json.dumps(records, ensure_ascii=False) + '\n').encode())
        except BaseException:
            shutil.rmtree(candidate, ignore_errors=True)
            raise
        return candidate


def build_command(root, worktree, args, *, cache_name, java_home=None,
                  environment=None, readonly_workspace=False,
                  gradle_ro_cache=None, operation=None, timeout_seconds=None,
                  writable_workspace_paths=(), _workspace_staged=False, _project_git=None):
    from .windows_sandbox import SandboxMount, WindowsSandboxSpec
    if not re.fullmatch(r'[a-z][a-z0-9-]*', cache_name):
        raise ValueError('invalid isolated Gradle cache name')
    root, worktree = Path(root).absolute(), Path(worktree).absolute()
    assert_no_reparse(worktree)
    if not _workspace_staged:
        from .local_workspace_sandbox import external_workspace, windows_stage_command
        if external_workspace(root, worktree):
            return windows_stage_command(root, worktree, args, cache_name=cache_name,
                java_home=java_home, environment=environment,
                readonly_workspace=readonly_workspace, gradle_ro_cache=gradle_ro_cache,
                operation=operation, timeout_seconds=timeout_seconds,
                writable_workspace_paths=list(writable_workspace_paths))
    cache = root / 'toolchains' / cache_name
    cache.mkdir(parents=True, exist_ok=True)
    for name in ('init.gradle', 'init.gradle.kts', 'init.d'):
        if (cache / name).exists():
            raise RuntimeError('isolated Gradle cache contains an untrusted init script')
    if java_home is None:
        java = shutil.which('java')
        configured = os.environ.get('JAVA_HOME')
        java_home = Path(configured) if configured else Path(java).resolve().parents[1] if java else None
    if java_home is None or not (Path(java_home) / 'bin' / 'java.exe').is_file():
        raise RuntimeError('a native Windows JDK is required for isolated project execution')
    java_home = Path(java_home).absolute()
    original_java_home = java_home
    java_home = _materialize_runtime(root, java_home, 'jdk')
    def native_workload(values):
        if len(values) >= 2 and values[0] in ('bash', 'sh') and values[1] == '/workspace/gradlew':
            return [str(java_home / 'bin' / 'java.exe'), '-classpath',
                str(worktree / 'gradle' / 'wrapper' / 'gradle-wrapper.jar'),
                'org.gradle.wrapper.GradleWrapperMain', *values[2:]]
        return list(values)
    argv = native_workload(list(args))
    python_home = None
    if argv[0] in ('python3', 'python'):
        python_home = _materialize_runtime(root, Path(sys.base_prefix).absolute(), 'python')
        python = python_home / 'python.exe'
        if not python.is_file():
            raise RuntimeError('native Python runtime unavailable for target client support')
        argv[0] = str(python)
        if '--' in argv:
            split = argv.index('--') + 1
            argv[split:] = native_workload(argv[split:])
    elif len(argv) == 3 and argv[:2] in (['/bin/sh', '-lc'], ['bash', '-lc']):
        system = Path(os.environ.get('SystemRoot', r'C:\Windows'))
        argv = [str(system / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'),
                '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', argv[2]]
    elif argv and argv[0] in ('java', '/java-home/bin/java'):
        argv[0] = str(java_home / 'bin' / 'java.exe')
    elif argv and not Path(argv[0]).is_absolute() and not argv[0].startswith(('/gradle-cache/', '/java-home/')):
        raise ValueError('native sandbox commands require a declared executable')
    mounts = [SandboxMount(worktree, '/workspace', readonly_workspace,
                           credential_filtered=_workspace_staged),
              SandboxMount(cache, '/gradle-cache', False),
              SandboxMount(java_home, '/java-home', True)]
    if python_home is not None:
        mounts.append(SandboxMount(python_home, '/python-home', True))
    for index, relative in enumerate(writable_workspace_paths):
        relative = Path(relative)
        if relative.is_absolute() or '..' in relative.parts or not relative.parts:
            raise ValueError('sandbox writable output must be a contained relative directory')
        output = worktree / relative
        output.mkdir(parents=True, exist_ok=True)
        assert_no_reparse(output)
        mounts.append(SandboxMount(output, '/workspace-output-' + str(index), False))
    extra_environment = dict(environment or {})
    if _project_git is not None:
        git_directory = Path(_project_git).absolute()
        if git_directory.parent.parent != root / 'toolchains' / 'local-build-stages':
            raise ValueError('build Git view must be a Run-owned isolated snapshot')
        mounts.append(SandboxMount(git_directory, '/project-git', True))
        extra_environment.update(GIT_DIR=str(git_directory), GIT_WORK_TREE=str(worktree),
            GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
            GIT_CONFIG_NOSYSTEM='1', GIT_ATTR_NOSYSTEM='1', GIT_OPTIONAL_LOCKS='0')
        git = shutil.which('git')
        if git:
            git = Path(git).absolute()
            if git.parent.name.lower() not in {'cmd', 'bin'}:
                raise RuntimeError('native Git must come from a declared Git installation')
            runtime = _materialize_runtime(root, git.parent.parent, 'git')
            mounts.append(SandboxMount(runtime, '/git-home', True))
            extra_environment['MODPORT_GIT_HOME'] = str(runtime / git.parent.name)
    if gradle_ro_cache is not None:
        mounts.append(SandboxMount(Path(gradle_ro_cache).absolute(), '/gradle-ro-cache', True))
        extra_environment['GRADLE_RO_DEP_CACHE'] = str(Path(gradle_ro_cache).absolute())
    for folder, alias in (('harness-support', '/modport-support'), ('harness-wiring', '/modport-wiring')):
        directory = root / 'artifacts' / folder
        if directory.is_dir():
            mounts.append(SandboxMount(directory, alias, True))
    if operation is not None and operation.options.get('artifact_runtime_directory'):
        directory = Path(operation.options['artifact_runtime_directory']).absolute()
        if not directory.is_relative_to(root / 'artifacts' / 'artifact-runtime'):
            raise ValueError('unsafe delivered artifact runtime directory')
        mounts.append(SandboxMount(directory, '/modport-artifact', True))
        script = Path(operation.options['artifact_init_script']).absolute()
        if script.parent != root / 'artifacts' / 'harness-wiring':
            raise ValueError('unsafe artifact Gradle wiring')
        extra_environment['MODPORT_ARTIFACT_INIT_SCRIPT'] = str(script)
    if timeout_seconds is None:
        if operation is None:
            raise ValueError('native sandbox requires the active command deadline')
        from .execution_budget import remaining_timeout
        deadline = operation.options.get('deadline_epoch')
        if not isinstance(deadline, (float, int)):
            raise ValueError('native sandbox requires a finite overall deadline')
        timeout_seconds = remaining_timeout(operation, max(0, deadline - time.time()))
    private = root / 'toolchains' / 'windows-sandboxes' / uuid4().hex
    private.mkdir(parents=True)
    spec = WindowsSandboxSpec(argv, worktree, private, mounts,
        environment=extra_environment, java_home=java_home)
    value = {'argv': list(spec.argv), 'cwd': str(spec.cwd), 'private_root': str(private),
             'mounts': [{'source': str(m.source), 'target': m.target, 'readonly': m.readonly,
                         'credential_filtered': m.credential_filtered} for m in mounts],
             'environment': extra_environment, 'java_home': str(java_home),
             'network_policy': spec.network_policy}
    path = private / 'command.json'
    atomic_write(path, (json.dumps(value, ensure_ascii=False) + '\n').encode())
    source_root = Path(__file__).absolute().parents[1]
    launcher = (f'import sys; sys.path.insert(0, {str(source_root)!r}); '
                'from modport.windows_sandbox import main; raise SystemExit(main())')
    return [sys.executable, '-I', '-c', launcher, '--spec', str(path),
            '--timeout', str(timeout_seconds)]
