"""Explicit real production workflow definitions for historical routing tests."""

from modport.evidence import digest
from modport.contracts import json_copy
from modport.workflow import WorkflowDefinition
from modport.workflow_upgrade import _known_v16_definition, _known_v15_definition
from modport.workflow_upgrade import _known_v14_definition, _known_v13_definition, _known_v12_definition


class FrozenDefinition:
    def __init__(self, value):
        self.value = value

    def to_dict(self):
        return json_copy(self.value)

    def sha256(self):
        return digest(self.value)


def compile_v16(request):
    return FrozenDefinition(_known_v16_definition(WorkflowDefinition(request.to_dict(), version=18).to_dict()))


def compile_v15(request):
    return FrozenDefinition(_known_v15_definition(compile_v16(request).to_dict()))


def compile_v12(request):
    return FrozenDefinition(_known_v12_definition(_known_v13_definition(
        _known_v14_definition(compile_v15(request).to_dict()))))
