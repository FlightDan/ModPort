"""Host-owned execution inputs for selected migration behavior tests.

This module turns a host-selected subset of a frozen functional contract into
Gradle ``Test`` filters.  It does not run project code or decide which behavior
IDs belong in the selection.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from xml.etree import ElementTree
import json
import re

from .runtime_result_identity import validate_runtime_result_identity


_TEST_ID = re.compile(r"[A-Za-z0-9_.:-]+\Z")
_GRADLE_TASK = re.compile(r":?[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)*\Z")
_FORBIDDEN_TASK_NAMES = {"help", "tasks", "properties", "runClient", "runServer"}
_MAX_JUNIT_XML_BYTES = 16 * 1024 * 1024
GAMETEST_REPORT_FILENAME = "TEST-modport-gametest.xml"


def _native_report_configuration() -> list[str]:
    """Bind official ModDevGradle reports to the host's exact task directory."""
    return [
        "    def modportBoundNativeRuns = [:]",
        "    modportNativeTaskPaths.each { selectedPath ->",
        "        def selectedTask = graph.allTasks.find { it.path == selectedPath }",
        "        if (selectedTask == null) return",
        "        def dependencies = [] as Set",
        "        def visitDependencies",
        "        visitDependencies = { task ->",
        "            if (dependencies.add(task)) {",
        "                task.taskDependencies.getDependencies(task).each { visitDependencies(it) }",
        "            }",
        "        }",
        "        visitDependencies(selectedTask)",
        "        def nativeRuns = []",
        "        dependencies.each { task ->",
        "            def neoForge = task.project.extensions.findByName('neoForge')",
        "            if (neoForge != null) {",
        "                neoForge.runs.each { run ->",
        "                    def runTaskName = 'run' + run.name.substring(0, 1).toUpperCase() + run.name.substring(1)",
        "                    if (task.name == runTaskName && run.type.getOrElse('') == 'gameTestServer') {",
        "                        nativeRuns.add([task: task, run: run])",
        "                    }",
        "                }",
        "            }",
        "        }",
        "        if (nativeRuns.size() != 1) {",
        "            throw new GradleException('Selected native GameTest task must resolve to one official gameTestServer run: ' + selectedPath)",
        "        }",
        "        def binding = nativeRuns[0]",
        "        def previousBinding = modportBoundNativeRuns.put(binding.task.path, selectedPath)",
        "        if (previousBinding != null && previousBinding != selectedPath) {",
        "            throw new GradleException('Selected native GameTest tasks share a runner: ' + previousBinding + ', ' + selectedPath)",
        "        }",
        "        def report = binding.task.project.rootProject.file('build/test-results/' + selectedTask.name + '/" + GAMETEST_REPORT_FILENAME + "')",
        "        def originalArguments = binding.run.programArguments.getOrElse([])",
        "        def reportArguments = []",
        "        for (int index = 0; index < originalArguments.size(); index++) {",
        "            def argument = originalArguments[index]",
        "            if (argument == '--report') {",
        "                if (++index >= originalArguments.size()) throw new GradleException('GameTest --report has no value')",
        "            } else if (!argument.startsWith('--report=')) {",
        "                reportArguments.add(argument)",
        "            }",
        "        }",
        "        reportArguments.addAll(['--report', report.absolutePath])",
        "        binding.run.programArguments.set(reportArguments)",
        "        binding.task.logger.lifecycle('MODPORT_GAMETEST_REPORT task=' + selectedPath + ' runner=' + binding.task.path + ' path=' + report.absolutePath)",
        "        binding.task.doFirst { report.parentFile.mkdirs() }",
        "    }",
    ]


@dataclass(frozen=True)
class SelectedTestExecution:
    """Validated Gradle filters and environment for one host test selection."""

    test_ids: tuple[str, ...]
    gradle_tasks: tuple[str, ...]
    gradle_init_script: str
    environment: Mapping[str, str]


def _canonical_gradle_task(value: object, *, test_id: str) -> str:
    if not isinstance(value, str) or not _GRADLE_TASK.fullmatch(value):
        raise ValueError(f"selected test {test_id!r} has an invalid JUnit Gradle task")
    canonical = ":" + value.lstrip(":")
    if canonical.rsplit(":", 1)[-1] in _FORBIDDEN_TASK_NAMES:
        raise ValueError(f"selected test {test_id!r} uses a non-test Gradle task")
    return canonical


def _selected_identities(
    contract: Mapping[str, object], test_ids: Sequence[str], *, workflow_version: int = 0,
) -> tuple[dict[str, tuple[str, str, str]], dict[str, list[str]], str]:
    if not isinstance(contract, Mapping):
        raise ValueError("selected test contract must be an object")
    if not isinstance(test_ids, Sequence) or isinstance(test_ids, (str, bytes)):
        raise ValueError("selected test IDs must be an array")
    if not test_ids:
        raise ValueError("selected test execution requires at least one test ID")

    declarations = contract.get("test_evidence")
    if not isinstance(declarations, Mapping):
        raise ValueError("selected test contract has no test_evidence mapping")

    ordered_ids: list[str] = []
    seen_ids: set[str] = set()
    selectors_by_task: dict[str, list[str]] = {}
    identities: dict[str, tuple[str, str, str]] = {}
    seen_identities: set[tuple[str, str, str]] = set()
    native_tasks: set[str] = set()
    for test_id in test_ids:
        if (not isinstance(test_id, str) or not _TEST_ID.fullmatch(test_id)
                or test_id in seen_ids):
            raise ValueError("selected test IDs must be unique safe identifiers")
        seen_ids.add(test_id)
        declaration = declarations.get(test_id)
        if not isinstance(declaration, Mapping):
            raise ValueError(f"selected test ID {test_id!r} is not declared by the contract")
        native = workflow_version >= 34 and declaration.get('executor') == 'gametest'
        if declaration.get("evidence_kind") != "runtime" or (declaration.get("executor") != "junit" and not native):
            raise ValueError(f"selected test {test_id!r} does not use a runtime JUnit executor")
        try:
            identity_key = validate_runtime_result_identity(
                declaration.get('result_identity'), native=native,
                gradle_tasks=contract.get('baseline_gradle_tasks'),
            )
        except ValueError as exc:
            raise ValueError(f'selected test {test_id!r}: {exc}') from exc
        task, classname, method = identity_key
        if identity_key in seen_identities:
            raise ValueError("selected test IDs reuse a JUnit result identity")
        seen_identities.add(identity_key)
        ordered_ids.append(test_id)
        identities[test_id] = identity_key
        selectors_by_task.setdefault(task, []).append(classname + "." + method)
        if native:
            if workflow_version >= 35 and task.count(':') != 1:
                raise ValueError("native GameTest report binding requires a root-project Gradle task")
            native_tasks.add(task)

    # Preserve the caller's selected-ID order for the custom harness, but emit
    # task groups in a stable order for reproducible init scripts.
    script_lines = [
        "import org.gradle.api.tasks.testing.Test",
        "",
        "def modportSelectedTestPatterns = [",
    ]
    for task in sorted(selectors_by_task):
        patterns = selectors_by_task[task]
        # The identifier schema excludes quotes, escapes, and wildcards, so
        # these Groovy single-quoted literals remain literal and exact.
        pattern_literals = ", ".join("'" + pattern + "'" for pattern in patterns)
        script_lines.append(f"    '{task}': [{pattern_literals}],")
    script_lines.extend([
        "]",
        "def modportNativeTaskPaths = " + json.dumps(sorted(native_tasks)) + " as Set",
        "def modportSelectedTaskPaths = modportSelectedTestPatterns.keySet() as Set",
        "",
        "gradle.taskGraph.whenReady { graph ->",
        *(_native_report_configuration() if workflow_version >= 35 and native_tasks else []),
        "    def observedSelectedTaskPaths = [] as Set",
        "    graph.allTasks.each { graphTask ->",
        "        if (modportSelectedTaskPaths.contains(graphTask.path)) {",
        "            observedSelectedTaskPaths.add(graphTask.path)",
        *(["            graphTask.outputs.upToDateWhen { false }"] if workflow_version >= 34 else []),
        "        }",
        "        if (graphTask instanceof Test) {",
        "            def selectedPatterns = modportSelectedTestPatterns[graphTask.path]",
        "            if (selectedPatterns == null || modportNativeTaskPaths.contains(graphTask.path)) {",
        "                // A build dependency may add other Test tasks; skip them entirely.",
        "                graphTask.enabled = false",
        "            } else {",
        "                // Replace project filters so only host-selected exact cases can run.",
        "                graphTask.enabled = true",
        "                // Forward nested runtime witnesses to the host's captured process log.",
        "                graphTask.testLogging.showStandardStreams = true",
        "                graphTask.filter.setIncludePatterns(*selectedPatterns)",
        "                graphTask.filter.setFailOnNoMatchingTests(true)",
        "            }",
        "        }",
        "    }",
        "    def missingSelectedTaskPaths = modportSelectedTaskPaths - observedSelectedTaskPaths",
        "    if (!missingSelectedTaskPaths.isEmpty()) {",
        "        throw new GradleException('Selected Gradle tasks are absent from this graph: ' + missingSelectedTaskPaths)",
        "    }",
        "}",
        "",
    ])
    return identities, selectors_by_task, "\n".join(script_lines)


def build_selected_test_execution(
    contract: Mapping[str, object], selected_test_ids: Sequence[str], *, workflow_version: int = 0,
) -> SelectedTestExecution:
    """Build an init script and environment for precisely selected JUnit IDs.

    The contract's v29 ``result_identity`` fields are the source of truth. Any
    malformed ID, unsupported executor, unsafe identity, or missing mapping is
    rejected before the caller launches Gradle.
    """

    _, selectors_by_task, init_script = _selected_identities(
        contract, selected_test_ids, workflow_version=workflow_version,
    )
    test_ids = tuple(selected_test_ids)
    return SelectedTestExecution(
        test_ids=test_ids,
        gradle_tasks=tuple(sorted(selectors_by_task)),
        gradle_init_script=init_script,
        environment={
            "MODPORT_SELECTED_TEST_IDS": json.dumps(
                list(test_ids), ensure_ascii=True, separators=(",", ":"),
            ),
        },
    )


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _report_payloads(value: object, *, gradle_task: str) -> tuple[bytes, ...]:
    if isinstance(value, (str, bytes)):
        candidates: Iterable[object] = (value,)
    elif isinstance(value, Iterable):
        candidates = value
    else:
        raise ValueError(f"fresh JUnit reports for {gradle_task!r} must be XML payloads")
    payloads: list[bytes] = []
    for candidate in candidates:
        if isinstance(candidate, str):
            payload = candidate.encode("utf-8")
        elif isinstance(candidate, bytes):
            payload = candidate
        else:
            raise ValueError(f"fresh JUnit reports for {gradle_task!r} must be XML payloads")
        if len(payload) > _MAX_JUNIT_XML_BYTES:
            raise ValueError("JUnit result XML exceeds the size limit")
        if b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
            raise ValueError("JUnit result XML contains a forbidden XML declaration")
        payloads.append(payload)
    return tuple(payloads)


def find_executed_excluded_junit_cases(
    contract: Mapping[str, object],
    excluded_test_ids: Sequence[str],
    reports_by_gradle_task: Mapping[str, object],
) -> list[dict[str, str]]:
    """Return excluded contract cases observed in fresh, task-bound JUnit XML.

    ``reports_by_gradle_task`` must map the exact Gradle task path that produced
    each fresh XML document to one payload or an iterable of payloads. The
    detector compares the complete contract identity (task, classname, name);
    an unrelated testcase with only a matching name is ignored. Freshness and
    task-to-report attribution remain the caller's responsibility.
    """

    if not isinstance(excluded_test_ids, Sequence) or isinstance(excluded_test_ids, (str, bytes)):
        raise ValueError("excluded test IDs must be an array")
    if not excluded_test_ids:
        return []
    if not isinstance(reports_by_gradle_task, Mapping):
        raise ValueError("JUnit reports must be mapped by exact Gradle task")
    identities, _, _ = _selected_identities(contract, excluded_test_ids)
    expected: dict[str, dict[tuple[str, str], list[str]]] = {}
    excluded_order: list[str] = []
    for test_id, (task, classname, method) in identities.items():
        key = (classname, method)
        expected.setdefault(task, {}).setdefault(key, []).append(test_id)
        excluded_order.append(test_id)

    canonical_reports: dict[str, object] = {}
    for raw_task, value in reports_by_gradle_task.items():
        task = _canonical_gradle_task(raw_task, test_id="<report>")
        if task in canonical_reports:
            raise ValueError(f"JUnit reports repeat Gradle task {task!r}")
        canonical_reports[task] = value

    found: list[dict[str, str]] = []
    for task in sorted(set(expected).intersection(canonical_reports)):
        task_expected = expected[task]
        for payload in _report_payloads(canonical_reports[task], gradle_task=task):
            try:
                document = ElementTree.fromstring(payload)
            except ElementTree.ParseError as exc:
                raise ValueError(f"JUnit result XML is malformed: {exc}") from exc
            if _local_tag(document.tag) not in {"testsuite", "testsuites"}:
                raise ValueError("JUnit result XML must have a testsuite or testsuites root")
            for case in document.iter():
                if _local_tag(case.tag) != "testcase":
                    continue
                classname, method = case.get("classname"), case.get("name")
                if classname is None or method is None:
                    continue
                test_ids = task_expected.get((classname, method), ())
                if not test_ids:
                    continue
                child_tags = {_local_tag(child.tag) for child in case}
                outcome = ("error" if "error" in child_tags else
                           "failed" if "failure" in child_tags else
                           "skipped" if "skipped" in child_tags else "passed")
                for test_id in test_ids:
                    found.append({
                        "test_id": test_id,
                        "gradle_task": task,
                        "classname": classname,
                        "name": method,
                        "outcome": outcome,
                    })
    rank = {test_id: index for index, test_id in enumerate(excluded_order)}
    found.sort(key=lambda row: (rank[row["test_id"]], row["gradle_task"], row["outcome"]))
    return found
