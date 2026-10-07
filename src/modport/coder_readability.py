"""Shared, protected readability guidance for current target authoring."""

from pathlib import Path
from typing import Any, Mapping


AUTHORING_STAGES = frozenset({
    'coder', 'implementation', 'target_revise', 'development_prepare',
    'goal_prepare', 'test_design', 'artifact_test_design',
    'code_cleanup', 'final_cleanup',
})
SKILL_PATH = 'artifacts/rules/debug-skills/code-simplifier/SKILL.md'
CORE_REQUIREMENTS = (
    'Readability and project standards: preserve observable behavior, public interfaces, '
    'resources, registrations, persistence, networking, frozen requirement IDs, test IDs '
    'and assertions. Follow the project conventions and exact locked Java/Minecraft/NeoForge '
    'APIs. Prefer clear names, explicit control flow and focused responsibilities; reduce '
    'unnecessary nesting, duplication and abstractions in the assigned changes. Avoid '
    'nested ternaries, dense one-liners and clever shortcuts. Keep useful abstractions and '
    'comments that explain intent. Check dynamic registration, reflection and resource '
    'references before removing code. Apply these principles while implementing or repairing, '
    'within the assigned scope and host execution permissions; do not weaken tests, introduce '
    'approval gates or add hash/checksum/fingerprint verification.'
)


def requirements(command: Any, root: Path) -> str:
    """Use the Run's bundled copy rather than the agent machine's installed skill."""
    authoring = (command.stage_id in AUTHORING_STAGES
                 or command.stage_id == 'agent_rework'
                 and isinstance(command.payload.get('development_task'), Mapping))
    if command.options.get('workflow_version', 0) < 37 or not authoring:
        return ''
    return ('\n' + CORE_REQUIREMENTS + '\nRead the Run-local skill with '
            'modport_sandbox_read_run_artifact: ' + str(root / SKILL_PATH) + '. '
            'The host supplies this fixed copy; keep it unchanged.\n')


def append_readability(prompt: str, command: Any, root: Path) -> str:
    """Reinforce each native turn without duplicating an already protected core."""
    guidance = requirements(command, root)
    if not guidance or guidance in prompt:
        return prompt
    return prompt + guidance
