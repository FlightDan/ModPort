"""Whether a frozen workflow uses business checks as execution gates.

Version 17 records observations without requiring approval, report shape,
ownership classifications, or passing tests before executing subsequent work.
Older inputs retain the policy under which they were created.
"""

from collections.abc import Mapping


def business_gates_disabled(value):
    """Accept an operation, frozen header/definition, or operation options."""
    if hasattr(value, "options"):
        value = value.options
    if not isinstance(value, Mapping):
        return False
    definition = value.get("definition", value)
    version = definition.get("workflow_version", 0)
    return type(version) is int and version >= 17


def compile_package_scope(value):
    """Return whether this frozen Run limits acceptance to compile/package evidence."""
    if hasattr(value, "options"):
        value = value.options
    if not isinstance(value, Mapping):
        return False
    definition = value.get("definition", value)
    version = definition.get("workflow_version", value.get("workflow_version", 0))
    policy = definition.get("validation_policy", value.get("validation_policy", {}))
    return (type(version) is int and version >= 27 and isinstance(policy, Mapping)
            and policy.get("scope") == "compile_package")
