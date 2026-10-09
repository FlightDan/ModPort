"""Shared XML result identities for target freezing and runtime selection."""

from collections.abc import Iterable, Mapping
import re


RUNTIME_TEST_SOURCE_SUFFIXES = frozenset({'.java', '.kt', '.groovy', '.gradle', '.kts'})
_GRADLE_TASK = re.compile(r':?[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)*')
_CLASSNAME = re.compile(r'[A-Za-z_$][A-Za-z0-9_$.]*')
_METHOD_NAME = re.compile(r'[A-Za-z_$][A-Za-z0-9_$]*')
# Official native reports use resource locations for structures and test IDs.
# These are XML attribute values, never filesystem paths. The restricted
# alphabet also keeps generated Groovy literals free of quoting or wildcards.
_NATIVE_RESULT = re.compile(r'[A-Za-z0-9_.:/-]+')
_FORBIDDEN_TASK_NAMES = {'help', 'tasks', 'properties', 'runClient', 'runServer'}


def validate_runtime_result_identity(
    identity: object, *, native: bool, gradle_tasks: Iterable[str] | None = None,
) -> tuple[str, str, str]:
    """Return canonical task, class and name without changing the declaration."""
    if (not isinstance(identity, Mapping)
            or set(identity) != {'kind', 'gradle_task', 'classname', 'name'}
            or identity.get('kind') != 'junit_xml'):
        raise ValueError('an exact JUnit XML result_identity is required')
    task = identity.get('gradle_task')
    classname, name = identity.get('classname'), identity.get('name')
    if (not isinstance(task, str) or not _GRADLE_TASK.fullmatch(task)
            or task.rsplit(':', 1)[-1] in _FORBIDDEN_TASK_NAMES):
        raise ValueError('an unsafe JUnit XML result_identity gradle_task was supplied')
    if (not isinstance(classname, str)
            or not (_NATIVE_RESULT if native else _CLASSNAME).fullmatch(classname)):
        raise ValueError('an unsafe JUnit XML result_identity classname was supplied')
    if (not isinstance(name, str)
            or not (_NATIVE_RESULT if native else _METHOD_NAME).fullmatch(name)):
        raise ValueError('an unsafe JUnit XML result_identity name was supplied')
    task = ':' + task.lstrip(':')
    if native and task.count(':') != 1:
        raise ValueError('native GameTest report binding requires a root-project Gradle task')
    if gradle_tasks is not None and task not in {':' + value.lstrip(':') for value in gradle_tasks}:
        raise ValueError('result task is not declared for verification')
    return task, classname, name
