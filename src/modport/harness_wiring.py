"""Wire the conventional characterization source directory into Gradle."""
from pathlib import Path


CHARACTERIZATION_INIT = '''// ModPort conventional characterization harness, version 1.
// This script is used only for explicit characterization invocations.
allprojects { p ->
    p.pluginManager.withPlugin('java') {
        p.sourceSets.main.java.srcDir(p.file('.modport/harness'))
    }
}
gradle.projectsEvaluated {
    allprojects { p ->
        p.tasks.withType(JavaExec).configureEach { t ->
            if (t.name == 'runClient' || t.name == 'runServer') {
                t.systemProperty('modport.characterization', 'true')
                t.systemProperty('modport.projectRoot', p.projectDir.absolutePath)
                t.workingDir(p.file('.modport/run-characterization'))
                t.doFirst { t.workingDir.mkdirs() }
            }
        }
    }
}
'''

CONVENTIONAL_SOURCES_INIT = '''// ModPort additive characterization sources, version 1.
// Preserve authored launch configuration; register conventional Java sources only.
gradle.projectsEvaluated {
    allprojects { p ->
        if (p.plugins.hasPlugin('java')) {
            def local = p.file('.modport/harness')
            def shared = p.rootProject.file('.modport/harness')
            def launches = p.tasks.names.contains('runClient') || p.tasks.names.contains('runServer')
            def delegates = p == p.rootProject && !launches && p.subprojects.any { child ->
                child.plugins.hasPlugin('java') &&
                    (child.tasks.names.contains('runClient') || child.tasks.names.contains('runServer'))
            }
            if (local.isDirectory() && !delegates) {
                p.sourceSets.main.java.srcDir(local)
            } else if (shared.isDirectory() && launches) {
                p.sourceSets.main.java.srcDir(shared)
            }
        }
    }
}
'''


def characterization_init_scripts(worktree: Path, *, workflow_version: int,
                                  supplement_directory: Path | None = None) -> tuple[Path, ...]:
    """For v21+, supplement authored scripts without rewriting their contents.

    Merely having an init script does not prove that conventional sources are
    registered. Older frozen workflows retain their original launch semantics.
    """
    script = ensure_characterization_init(worktree)
    if script is None:
        return ()
    if workflow_version < 21:
        return (script,)
    harness = script.parent / 'harness'
    if harness.is_symlink():
        raise ValueError('characterization source directory must not be a symlink')
    if not harness.is_dir():
        return (script,)
    sources = False
    for path in harness.rglob('*'):
        if path.is_symlink():
            raise ValueError('characterization sources must not contain symlinks')
        sources = sources or (path.is_file() and path.suffix == '.java')
    if not sources or script.read_text(encoding='utf-8') == CHARACTERIZATION_INIT:
        return (script,)
    directory = script.parent if supplement_directory is None else Path(supplement_directory).absolute()
    if directory.is_symlink() or directory.resolve() != directory.absolute():
        raise ValueError('characterization supplement directory must not contain a symlink')
    directory.mkdir(parents=True, exist_ok=True)
    supplement = directory / 'characterization-sources.init.gradle'
    if supplement.is_symlink() or supplement.resolve() != supplement.absolute():
        raise ValueError('characterization supplement must not contain a symlink')
    from .evidence import workspace_lock
    lock = directory / '.characterization-wiring.lock'
    if lock.is_symlink() or (lock / '.workspace.lock').is_symlink():
        raise ValueError('characterization wiring lock must not be a symlink')
    with workspace_lock(lock, blocking=True):
        if supplement.exists():
            if not supplement.is_file() or supplement.read_text(encoding='utf-8') != CONVENTIONAL_SOURCES_INIT:
                raise ValueError('characterization supplement conflicts with an existing file')
        else:
            with supplement.open('x', encoding='utf-8') as handle:
                handle.write(CONVENTIONAL_SOURCES_INIT)
    return (script, supplement)


def ensure_characterization_init(worktree: Path) -> Path | None:
    """Preserve authored wiring; supply the standard JavaExec convention if absent.

    Nonstandard source directories and launchers remain author-owned. The
    generated script lives with the harness so freeze/handoff preserve it.
    """
    root = Path(worktree).resolve()
    directory = root / '.modport'
    script = directory / 'characterization.init.gradle'
    for path in (directory, script):
        if path.is_symlink() or path.resolve() != path.absolute():
            raise ValueError('characterization wiring path must not contain a symlink')
    if script.exists():
        if not script.is_file():
            raise ValueError('characterization init script must be a regular file')
        return script
    harness = directory / 'harness'
    if harness.is_symlink():
        raise ValueError('characterization source directory must not be a symlink')
    if not harness.is_dir():
        return None
    sources = False
    for path in harness.rglob('*'):
        if path.is_symlink():
            raise ValueError('characterization sources must not contain symlinks')
        sources = sources or (path.is_file() and path.suffix == '.java')
    if not sources:
        return None
    with script.open('x', encoding='utf-8') as handle:
        handle.write(CHARACTERIZATION_INIT)
    return script
