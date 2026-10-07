"""Validation shared by contract discovery and deterministic execution gates."""

import re
from typing import Any


_TASK = re.compile(r":?[A-Za-z][A-Za-z0-9_-]*(?::[A-Za-z][A-Za-z0-9_-]*)*")
_REPORTING_TASKS = frozenset({
    "help", "tasks", "properties", "dependencies", "projects", "components", "model",
    "outgoingVariants", "dependencyInsight", "buildEnvironment", "javaToolchains",
    "resolvableConfigurations", "dependentComponents",
})


def validate_baseline_gradle_tasks(value: Any) -> list[str]:
    """Return task names only; Gradle options and init scripts belong to the host."""
    if not isinstance(value, list) or not value:
        raise ValueError(f"baseline_gradle_tasks={value!r}: requires a non-empty list of task names")
    for index, task in enumerate(value):
        diagnostic = f"baseline_gradle_tasks[{index}]={task!r}"
        if not isinstance(task, str) or not _TASK.fullmatch(task):
            raise ValueError(diagnostic + ": expected a Gradle task name (for example test or :mod:test); "
                             "flags, paths and command fragments are not allowed. The host automatically "
                             "discovers .modport/characterization.init.gradle; do not include --init-script")
        if task.rsplit(":", 1)[-1] in _REPORTING_TASKS:
            raise ValueError(diagnostic + ": reporting tasks do not execute characterization tests")
    return list(value)
