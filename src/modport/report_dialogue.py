"""Plan/execute conversations for report-producing assignments.

The policy is frozen in the workflow definition. It changes message delivery,
not business routing, rework authority, or assignment budgets.
"""
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
import json
import shutil

from .evidence import atomic_json, file_digest


def dialogue_enabled(command):
    policy = command.options.get('agent_dialogue_policy')
    return (isinstance(policy, Mapping) and type(policy.get('version')) is int
            and policy['version'] == 1 and policy.get('turns') == ['plan', 'execute'])


_OBJECTIVES = {
    'background': 'Describe the mod purpose and relevant background with sources and uncertainties.',
    'preparation': 'Identify the exact source loader and Java versions from fixed source evidence.',
    'research_cleanup': 'Inspect the original source and authenticated material references, then produce a concise navigation index for migration research without changing source files.',
    'project_init': 'Describe the source layout, build inputs, side separation and migration workspace.',
    'mod_analysis': 'Assess scanner candidates and knowledge gaps against actual mod usage.',
    'behavior_extract': 'Derive observable behavior and assertion requirements by reading original mod code and resources, preserving confirmed IDs without source execution.',
    'behavior_review': 'Independently assess source-derived behavior requirements and source references; record diagnostic findings without executing source code or requiring approval.',
    'artifact_test_design': 'Design and implement executable target tests from source-derived behavior requirements against the delivered artifact, using suitable official test channels and batched runtime sessions.',
    'contract_draft': 'Characterize original observable behavior and design executable characterization tests.',
    'contract_revise': 'Repair the characterization work using the supplied failure evidence.',
    'contract_review': 'Independently review the functional contract and baseline evidence.',
    'code_review': 'Independently review the integrated migration against its plan, frozen behavior contract and evidence.',
    'code_cleanup': 'Simplify the integrated migration code while preserving frozen behavior, contract and test identities and assertion bytes, interfaces, and resources; report changes without running project code, staging files, or committing.',
    'test_design': 'Design and author independent executable behavior tests for the current candidate.',
    'test_review': 'Independently review the test assertions and their relationship to the behavior contract.',
    'gap_review': 'Independently assess closure of the current knowledge and verification gaps.',
    'gap_research': 'Research the assigned knowledge gaps using authoritative versioned sources.',
    'research_review': 'Independently assess the research evidence and proposed gap resolutions.',
    'admin_review': 'Assess research dispositions and follow-up work from the supplied evidence.',
    'gap_plan': 'Prepare a research proposal for the supplied unresolved project gaps.',
    'gap_plan_review': 'Independently assess the research proposal and identify useful research work.',
    'goal_prepare': 'Prepare concrete context and relevant evidence for the assigned coder task.',
    'gate_handoff': 'Explain the supplied failed attempt and concrete upstream repair work.',
    'supervisor': 'Assess the supplied execution observations and appropriate supervision response.',
    'platform_diff': 'Research the exact platform version differences and produce the reusable migration skill.',
    'java_diff': 'Research the exact Java version differences and produce the reusable migration skill.',
    'platform_skill_review': 'Independently audit the platform migration skill against versioned official evidence.',
    'java_skill_review': 'Independently audit the Java migration skill against versioned official evidence.',
}


def planning_task(command):
    stage = command.stage_id
    from .prompts import CODER_RECOVERY_GUIDANCE, CODER_RECOVERY_OBJECTIVE
    from .behavior_requirements import source_reading_policy
    source_reading = source_reading_policy(command)
    if source_reading and stage in {'contract_draft', 'contract_revise', 'contract_review'}:
        objective = _OBJECTIVES['behavior_review' if stage == 'contract_review' else 'behavior_extract']
    elif stage == 'contract_review' and command.options.get('workflow_version', 0) >= 31:
        objective = (
            'Plan how to assess the baseline test matrix from completed original-mod execution '
            'evidence, then select a traceable migration suite while retaining coverage gaps and '
            'source-defect provenance.'
        )
    else:
        objective = (
            CODER_RECOVERY_OBJECTIVE
            if stage == 'coder_revival_plan' and command.options.get('workflow_version', 0) >= 26
            else _OBJECTIVES.get(stage)
        )
    if objective is None:
        if stage.endswith(('_inventory', '_diagnose')):
            objective = 'Analyze the available source and failure evidence to prepare the assigned issue inventory or diagnosis.'
        elif stage.endswith(('_plan', '_repair_plan')):
            objective = 'Develop the assigned migration or repair strategy from the supplied plan and evidence.'
        elif stage.endswith(('_tasks', '_repair_tasks')):
            objective = 'Organize the supplied plan into concrete executable tasks and dependencies.'
        elif stage in {'parallel_review', 'contract_repair_review', 'target_repair_review'}:
            objective = 'Assess task assignments and dependencies for dispatch from the supplied plan.'
        else:
            task = command.payload.get('development_task', {})
            objective = (task.get('objective') if isinstance(task, Mapping) else None)
            objective = objective or 'Complete the assigned work using the supplied task and failure evidence.'
    text = (
        'Turn 1 of 2: prepare only a task plan in Markdown.\n'
        'Task: ' + str(objective) + '\n'
        'Read the current operation input and the relevant source, plans and evidence references '
        'to decide what to inspect and how to carry out this task. Relevant planning input indexes '
        'may also be present in this execution artifact directory. '
        'Describe the intended steps, evidence to inspect, and known uncertainties. '
        'This turn ends after the plan: do not perform the review or implementation, modify task '
        'outputs, request upstream rework, or produce the final result. '
        'Return only the Markdown plan in your final reply; the host saves it as the plan file. '
        'The next user message in this same conversation will ask you to execute the plan and '
        'supply the final output requirements.'
    )
    if stage == 'coder_revival_plan':
        text += ('\nRecovery capabilities for the execution turn; plan how to use them without '
                 'performing repairs in this planning turn:\n' + CODER_RECOVERY_GUIDANCE)
    elif stage == 'supervisor' and 'watchdog_incident' in command.payload:
        text += ('\nPlan a root-cause investigation of the host watchdog incident using bounded '
                 'original inputs, raw errors, tool outcomes, source and SDK observations. '
                 'Identify a concrete repair, known dependency or external prerequisite. '
                 'Only budget exhaustion or conclusively unrecoverable causes justify stopping. '
                 'Do not edit goals, active workspaces or frozen inputs, or run project code. '
                 'The execution turn may correct confirmed small source errors in host-provided '
                 'isolated copies or explicitly request supplied upstream rework; the host '
                 'controls verification and resumption under the unchanged Run budget.')
    elif stage == 'supervisor' and 'progress_supervision' in command.payload:
        text += ('\nPlan a read-only investigation of the host progress observations and original '
                 'input/log/tool/workspace references for the exact target execution. Explain '
                 'how to distinguish useful work from a blocked dependency before choosing '
                 'continue or terminate; do not edit goals, product or harness files.')
    elif stage == 'supervisor' and command.options.get('workflow_version', 0) < 26:
        text += ('\nThis assignment is self-contained. Plan using only this supplied evidence packet: '
                 + json.dumps(command.payload.get('supervision_packet', {}), ensure_ascii=False))
    elif stage == 'supervisor':
        text += ('\nInvestigate raw failures, dispatch and downstream consumption, source code and '
                 'environment. Plan any necessary edits to the supplied goal documents; '
                 'the execution turn may publish those edits for subsequent matching dispatches.')
    return text


def prepare_dialogue(command, root, task, required_paths=()):
    """Archive the second-turn shape; return task texts, not built prompts."""
    from .report_schemas import report_contract
    from .rework_tools import is_forward_only_planning
    root = Path(root)
    directory = root / 'artifacts' / 'executions' / command.command_id / 'dialogue'
    if directory.resolve() != directory.absolute() or not directory.resolve().is_relative_to(root.resolve()):
        raise ValueError('unsafe dialogue artifact directory')
    directory.mkdir(parents=True, exist_ok=True)
    contract = report_contract(command, required_paths)
    schema = contract.get('schema')
    schema_path = directory / 'output-schema.json' if schema is not None else None
    if schema_path is not None:
        atomic_json(schema_path, schema)
    plan_path = directory / ('审阅计划.md' if 'review' in command.stage_id else '任务计划.md')
    rework_guidance = '' if is_forward_only_planning(command) else (
        'Tool availability is declared by this execution turn\'s host tool instructions. If real '
        'upstream targets are listed and your inspection shows that repair is needed, make the actual '
        'list_rework_targets and request_rework calls, read the returned result and updated artifacts, '
        'then continue the assignment. Describing rework only in report prose does not invoke it. If '
        'no targets are listed, preserve the failure and state that this session cannot request repair.\n'
    )
    execution_task = (task + '\n\nTurn 2 of 2: execute the plan from the preceding conversation '
        'turn using the actual source and evidence. Carry out the task now and report its result. '
        'If new evidence changes the plan, explain that change in the result.\n'
        + rework_guidance
        + contract.get('instructions', 'Return the completed report in Markdown.')
        + '\nThe output requirements in this turn replace earlier output-format wording. '
        'Output shape is a reporting protocol, not approval or an execution gate. '
        'Report actual failures and uncertainties without inventing evidence.')
    from .behavior_requirements import source_reading_policy
    if (not source_reading_policy(command) and command.stage_id == 'contract_review'
            and command.options.get('workflow_version', 0) >= 31):
        execution_task += (
            '\nBefore classifying any test as necessary, redundant or repairable, read the completed '
            'baseline verifier evidence for the original mod. Assess each .modport/test-matrix.json '
            'case in a separate .modport/test-assessment.json document with schema_version=1 and '
            'decisions containing test_id, decision, reason, and optional replacement_test_ids/evidence. '
            'Cases without observed execution remain unknown or infrastructure, never source_defect. '
            'Keep this assessment separate from the existing contract-review report and treat both as '
            'diagnostic outputs rather than downstream approval gates. Request upstream work only by '
            'making the explicit request_rework tool call.'
        )
    if schema_path is not None:
        execution_task += ('\nThe host supplies the fixed final-response JSON Schema at '
                           + str(schema_path) + '. Read it for the exact fields and types. '
                           'Return the JSON document itself, without Markdown fences.')
    if contract.get('output_path') is not None:
        execution_task += ('\nThe host saves your final response to ' + contract['output_path']
                           + '; you do not need to write that report separately. '
                           'Create other task files as instructed.')
    return {'planning_task': planning_task(command), 'execution_task': execution_task,
            'plan_path': plan_path, 'schema_path': schema_path,
            'contract': contract, 'directory': directory}


def phase_command(command, phase):
    return replace(command, options={**command.options, 'dialogue_phase': phase})


def _output_path(worktree, relative):
    path = Path(relative)
    if path.is_absolute() or not path.parts or any(part in {'.', '..', '.git'} for part in path.parts):
        raise ValueError('unsafe dialogue output path')
    target = worktree / path
    if target.resolve() != target.absolute() or not target.resolve().is_relative_to(worktree.resolve()):
        raise ValueError('unsafe dialogue output path')
    return target


def materialize_report(dialogue, worktree, text):
    """Save the actual reply where the existing stage consumer expects it.

    Malformed JSON remains raw evidence for the existing diagnostic handler.
    This function never repairs, invents, or approves the report.
    """
    contract = dialogue['contract']
    raw = dialogue['directory'] / 'final-report.txt'
    if raw.resolve() != raw.absolute():
        raise ValueError('unsafe dialogue report archive')
    raw.write_text(text, encoding='utf-8')
    paths = contract.get('output_paths')
    if paths:
        try:
            document = json.loads(text)
            outputs = document['outputs']
            if not isinstance(outputs, dict):
                raise ValueError('outputs must be an object')
        except (ValueError, TypeError, KeyError) as exc:
            return ['multi-document report could not be decoded: ' + str(exc)]
        diagnostics = []
        for relative in paths:
            if relative not in outputs:
                diagnostics.append('report omitted ' + relative)
                continue
            value = outputs[relative]
            if relative.endswith('.md') and not isinstance(value, str):
                diagnostics.append('Markdown output was not text: ' + relative)
                continue
            contents = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + '\n'
            _write_report(dialogue, Path(worktree), relative, contents)
        return diagnostics
    relative = contract.get('output_path')
    if relative is None:
        if contract.get('transform') == 'characterization_in_place':
            from .author_contracts import normalize_characterization_contract
            relative = '.modport/functional-contract.json'
            target = _output_path(Path(worktree), relative)
            try:
                authored = json.loads(target.read_text(encoding='utf-8'))
                document = normalize_characterization_contract(authored)
                if isinstance(authored.get('test_evidence'), list):
                    _write_report(dialogue, Path(worktree), relative,
                                  json.dumps(document, ensure_ascii=False, indent=2) + '\n')
            except (OSError, ValueError, TypeError, KeyError) as exc:
                return ['characterization file could not be decoded: ' + str(exc)]
        return []
    if contract.get('transform') == 'characterization':
        try:
            from .author_contracts import normalize_characterization_contract
            document = normalize_characterization_contract(json.loads(text))
            text = json.dumps(document, ensure_ascii=False, indent=2) + '\n'
        except (ValueError, TypeError, KeyError) as exc:
            # Preserve the actual malformed reply at the expected output path.
            # The stage's ordinary diagnostic parser will observe the failure.
            _write_report(dialogue, Path(worktree), relative, text)
            return ['characterization report could not be decoded: ' + str(exc)]
    _write_report(dialogue, Path(worktree), relative, text)
    return []


def _write_report(dialogue, worktree, relative, text):
    worktree = Path(worktree)
    target = _output_path(worktree, relative)
    if target.is_file():
        archived = dialogue['directory'] / 'agent-written' / relative
        if archived.resolve() != archived.absolute():
            raise ValueError('unsafe dialogue report backup')
        archived.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(target, archived)
    if target.is_dir():
        # Retain malformed agent output for diagnostics without replacing a
        # directory or turning report shape into an additional failure gate.
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding='utf-8')


def dialogue_artifacts(root, dialogue, metadata=None):
    """Seal the plan and both submitted messages, including partial failures."""
    root = Path(root)
    directory = dialogue['directory']
    if metadata is not None:
        atomic_json(directory / 'session.json', metadata)
    refs = {}
    for key, path, media in (
        ('agent_plan', dialogue['plan_path'], 'text/markdown'),
        ('agent_output_schema', dialogue.get('schema_path'), 'application/json'),
        ('agent_dialogue', directory / 'session.json', 'application/json'),
        ('agent_session_context_preflight', directory / 'session-context-preflight.json', 'application/json'),
        ('agent_plan_prompt', directory / 'plan-prompt.txt', 'text/plain'),
        ('agent_execute_prompt', directory / 'execute-prompt.txt', 'text/plain'),
        ('agent_final_report', directory / 'final-report.txt', 'text/plain'),
    ):
        if path is not None and path.is_file() and not path.is_symlink():
            refs[key] = {'path': path.relative_to(root).as_posix(),
                         'sha256': file_digest(path), 'media_type': media}
    session_path = directory / 'session.json'
    if metadata is None and session_path.is_file() and not session_path.is_symlink():
        metadata = json.loads(session_path.read_text(encoding='utf-8'))
    if isinstance(metadata, Mapping):
        for key in ('planning_log', 'execution_log'):
            name = metadata.get(key)
            if not isinstance(name, str):
                continue
            path = Path(name)
            path = path if path.is_absolute() else root / path
            if (path.is_file() and path.resolve() == path.absolute()
                    and path.is_relative_to(root)):
                refs['agent_' + key] = {'path': path.relative_to(root).as_posix(),
                    'sha256': file_digest(path), 'media_type': 'application/x-ndjson'}
    return refs


def stored_dialogue_artifacts(root, command):
    directory = Path(root) / 'artifacts' / 'executions' / command.command_id / 'dialogue'
    if not directory.is_dir() or directory.resolve() != directory.absolute():
        return {}
    return dialogue_artifacts(root, {
        'directory': directory,
        'plan_path': directory / ('审阅计划.md' if 'review' in command.stage_id else '任务计划.md'),
        'schema_path': directory / 'output-schema.json',
    })


def supervisor_document(document):
    """Translate strict nullable slots to the existing optional-field protocol."""
    if not isinstance(document, dict):
        return document
    result = dict(document)
    if 'intervention' in result:
        intervention = result['intervention']
        if intervention is None:
            result.pop('intervention')
        elif isinstance(intervention, dict):
            selected = {key: value for key, value in intervention.items() if value is not None}
            if selected:
                result['intervention'] = selected
            else:
                result.pop('intervention')
    return result
