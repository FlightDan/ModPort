"""Host-requested Gradle Test execution observations shared by regression gates."""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import re

from .goal_planning import relative_path


def _contained_file(workspace, relative):
    relative_path(relative)
    root = Path(workspace).absolute()
    path = root / relative
    if root.resolve() != root or path.resolve() != path.absolute() or not path.is_file():
        raise ValueError('Gradle execution evidence is missing or traverses a symlink')
    return path


def install_gradle_test_listener(snapshot, nonce, *, script_root='build/.modport-regression',
                                 output_root='build/.modport-regression'):
    """Install host-requested Test observations outside coder-authored inputs."""
    if not isinstance(nonce, str) or not re.fullmatch('[0-9a-f]{32,128}', nonce):
        raise ValueError('Gradle regression nonce must be a fresh hexadecimal token')
    script = relative_path(script_root) + '/' + nonce + '.init.gradle'
    report = relative_path(output_root) + '/' + nonce + '.json'
    path = snapshot / script
    if path.resolve() != path.absolute() or (snapshot / report).resolve() != (snapshot / report).absolute():
        raise ValueError('Gradle regression host paths must not traverse symlinks')
    if path.exists() or (snapshot / report).exists():
        raise ValueError('Gradle regression listener requires fresh script and evidence paths')
    path.parent.mkdir(parents=True, exist_ok=True)
    source = '''import groovy.json.JsonOutput
import org.gradle.api.tasks.testing.Test
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.nio.file.AtomicMoveNotSupportedException
def observed = [:]
def observationLock = new Object()
def output = new File(rootDirPlaceholder, reportPlaceholder)
def publish = {
    output.parentFile.mkdirs()
    def pending = new File(output.parentFile, output.name + ".tmp")
    pending.text = JsonOutput.toJson([nonce: noncePlaceholder, tasks: observed])
    try {
        Files.move(pending.toPath(), output.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
    } catch (AtomicMoveNotSupportedException ignored) {
        Files.move(pending.toPath(), output.toPath(), StandardCopyOption.REPLACE_EXISTING)
    }
}
gradle.projectsEvaluated {
    gradle.rootProject.allprojects { project ->
        project.tasks.withType(Test).all { task ->
            task.afterSuite { suite, result ->
                if (suite.parent == null) {
                    synchronized (observationLock) {
                        observed[task.path] = [tests: result.testCount, failures: result.failedTestCount,
                            skipped: result.skippedTestCount]
                    }
                }
            }
        }
    }
}
gradle.taskGraph.afterTask { task, state ->
    if (task instanceof Test) {
      synchronized (observationLock) {
        def record = observed[task.path] ?: [tests: 0, failures: 0, skipped: 0]
        record.executed = state.executed && !state.skipped && !state.upToDate && !state.noSource && state.failure == null
        record.outcome = state.skipMessage ?: (state.failure == null ? "executed" : "failed")
        record.report_directory = task.reports.junitXml.outputLocation.get().asFile.absolutePath
        observed[task.path] = record
        publish()
      }
    }
}
'''
    source = (source.replace('rootDirPlaceholder', json.dumps('/workspace'))
              .replace('reportPlaceholder', json.dumps(report).replace('$', '\\$'))
              .replace('noncePlaceholder', json.dumps(nonce)))
    path.write_text(source)
    return script, report


def validate_gradle_test_execution(snapshot, check, relative, nonce):
    path = _contained_file(snapshot, relative)
    if path.stat().st_size > 1024 * 1024:
        raise ValueError('Gradle regression execution record exceeds 1 MiB')
    data = path.read_bytes()
    body = json.loads(data)
    if (not isinstance(body, dict) or body.get('nonce') != nonce
            or not isinstance(body.get('tasks'), dict)):
        raise ValueError('Gradle regression execution record has invalid nonce or tasks')
    reports = check['reports']
    mapped, executions = set(), {}
    for task in check['tasks']:
        record = body['tasks'].get(task)
        if (not isinstance(record, dict) or record.get('executed') is not True
                or type(record.get('tests')) is not int or record['tests'] <= 0
                or type(record.get('failures')) is not int or record['failures'] != 0
                or type(record.get('skipped')) is not int or record['skipped'] != 0):
            raise ValueError(f'regression task {task} needs actual passing Gradle Test execution without skips')
        directory = record.get('report_directory')
        if not isinstance(directory, str) or not directory.startswith('/workspace/'):
            raise ValueError('Gradle Test report directory must be inside the candidate workspace')
        directory = relative_path(directory[len('/workspace/'):])
        matching = [report for report in reports if report.startswith(directory + '/')]
        if not matching:
            raise ValueError(f'regression task {task} has no declared report in its JUnit output directory')
        if mapped.intersection(matching):
            raise ValueError('regression tasks must have distinct JUnit report directories')
        mapped.update(matching)
        executions[task] = {**record, 'reports': matching}
    if mapped != set(reports):
        raise ValueError('regression report does not belong to a requested Gradle Test task')
    return {'path': relative, 'sha256': sha256(data).hexdigest(), 'nonce': nonce, 'tasks': executions}
