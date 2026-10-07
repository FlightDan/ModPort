"""Progress supervision selected by the frozen Run policy."""

from collections.abc import Mapping


def progress_supervised(value):
    """Accept an operation, frozen header/definition, or operation options."""
    if hasattr(value, 'options'):
        value = value.options
    if not isinstance(value, Mapping):
        return False
    definition = value.get('definition', value)
    if not isinstance(definition, Mapping):
        return False
    policy = definition.get('progress_supervision_policy', {})
    return isinstance(policy, Mapping) and policy.get('enabled') is True
