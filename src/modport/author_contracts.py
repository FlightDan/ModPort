"""Shared author-facing output contracts used by prompts and host gates."""
from __future__ import annotations

import json
from typing import Any, Mapping

from .test_matrix import matrix_protocol


def behavior_requirements_protocol() -> dict[str, Any]:
    """Shared source-reading output protocol; target test design follows later."""
    text = {'type': 'string', 'minLength': 1, 'pattern': r'\S'}
    identifier = {'type': 'string', 'pattern': r'^[A-Za-z0-9_.:-]+$'}
    anchor = {'type': 'object', 'required': ['path', 'symbol'],
              'properties': {'path': text, 'symbol': text,
                             'line': {'type': 'integer', 'minimum': 1},
                             'lines': {'type': 'array', 'items': {'type': 'integer', 'minimum': 1}}}}
    assertion = {'type': 'object', 'required': ['assertion_id', 'expected'],
                 'properties': {'assertion_id': identifier, 'expected': text,
                                'trigger': text, 'conditions': {}}}
    behavior = {'type': 'object',
                'required': ['behavior_id', 'description', 'source_anchors', 'assertions'],
                'properties': {'behavior_id': identifier, 'description': text,
                               'source_anchors': {'type': 'array', 'minItems': 1, 'items': anchor},
                               'assertions': {'type': 'array', 'minItems': 1, 'items': assertion}}}
    return {'schema': {'type': 'object', 'required': ['schema_version', 'behaviors'],
                      'properties': {'schema_version': {'const': 1},
                                     'behaviors': {'type': 'array', 'minItems': 1, 'items': behavior},
                                     'uncertainties': {}}},
            'guidance': [
                'Read original mod code and resources to derive observable behavior and expected outcomes.',
                'Behavior IDs and assertion IDs are globally unique; retain IDs in confirmed carried requirements.',
                'Source anchors name contained relative paths and concrete symbols. Line information is optional.',
                'The user confirmed the original mod functions. Source reading is the verification basis; acceptance remains unverified.',
                'Write only .modport/behavior-requirements.json. Do not execute the original project or generate, run or repair source harnesses.',
                'No source test IDs, test fixtures, Gradle tasks, test declarations or runtime result records are required.',
                'Host provenance comes from supplied source evidence. Do not calculate hashes, checksums or fingerprint comparisons.',
                'Independent review findings are diagnostic and do not require approval before downstream execution.',
            ]}


def normalize_characterization_contract(document: Mapping[str, Any]) -> dict[str, Any]:
    """Decode the final-response evidence table without changing its values.

    Authors can invoke the verifier before final-response publication. Both
    that reader and the publisher must understand the same finite wire array
    and the canonical test-ID mapping. Semantic validation stays downstream.
    """
    if not isinstance(document, Mapping):
        raise ValueError("functional contract must be an object")
    contract = dict(document)
    rows = contract.get("test_evidence")
    if rows is None or isinstance(rows, Mapping):
        return contract
    if not isinstance(rows, list):
        raise ValueError("test_evidence must be a mapping or a wire array")
    mapped = {}
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {"test_id", "declaration"}:
            raise ValueError("test_evidence wire rows require test_id and declaration")
        identifier = row["test_id"]
        if not isinstance(identifier, str) or not identifier.strip() or identifier in mapped:
            raise ValueError("test_evidence has an invalid or duplicate test_id")
        mapped[identifier] = row["declaration"]
    contract["test_evidence"] = mapped
    return contract


def acceptance_report_contract(goal: Mapping[str, Any] | None = None, *, double_check: bool = False) -> dict[str, Any]:
    goal = goal or {}
    checks = goal.get('checks', [{'id': 'check-id', 'acceptance': ['exact criterion']}])
    criteria = goal.get('acceptance', ['exact criterion'])
    entry = {'type': 'object', 'additionalProperties': False,
             'required': ['criterion', 'state', 'evidence'],
             'properties': {'criterion': {'type': 'string', 'enum': list(criteria)},
                            'state': {'const': 'passed'},
                            'evidence': {'type': 'array', 'minItems': 1,
                                         'items': {'type': 'string', 'enum': [c['id'] for c in checks]}}}}
    schema = {'type': 'object', 'additionalProperties': False, 'required': ['acceptance'],
              'properties': {'acceptance': {'type': 'array', 'minItems': 1, 'items': entry}}}
    example = {'acceptance': [{'criterion': criterion, 'state': 'passed',
                              'evidence': [c['id'] for c in checks if criterion in c['acceptance']]}
                             for criterion in criteria]}
    if double_check:
        schema['required'].append('self_check')
        schema['properties']['self_check'] = {
            'type': 'object', 'additionalProperties': False,
            'required': ['state', 'reviewed_paths', 'checks', 'summary'],
            'properties': {'state': {'const': 'passed'},
                           'reviewed_paths': {'type': 'array', 'minItems': 1, 'uniqueItems': True,
                                              'items': {'type': 'string'}},
                           'checks': {'type': 'array', 'uniqueItems': True, 'items': {'type': 'string'}},
                           'summary': {'type': 'string', 'minLength': 1}}}
        example['self_check'] = {'state': 'passed', 'reviewed_paths': ['every changed owned file'],
                                 'checks': [c['id'] for c in checks], 'summary': 'Concrete review findings'}
    return {'schema': schema, 'example': example, 'semantics': [
        'Write exactly one entry for every frozen criterion, with its exact text and no duplicates.',
        'Evidence lists host check IDs mapped to that criterion; all referenced checks must pass independently.',
        'Keep this report untracked and unstaged at the frozen acceptance_report path. A report alone proves nothing.',
        'When self_check is required, reviewed_paths are unique owned relative paths covering every changed file; checks lists every frozen check ID exactly once and summary is nonblank.',
    ]}


def acceptance_report_prompt(goal: Mapping[str, Any] | None = None, *, double_check: bool = False) -> str:
    return '\nAcceptance report contract (replace example review paths/findings with actual evidence): ' + json.dumps(
        acceptance_report_contract(goal, double_check=double_check), ensure_ascii=False)


def validate_report_shape(report: Any, contract: Mapping[str, Any]) -> None:
    """Check exact object keys from the same schema published to the author.

    Goal-specific semantic checks remain in goal_validation.
    """
    schema = contract['schema']
    if not isinstance(report, dict) or set(report) != set(schema['required']) or not isinstance(report['acceptance'], list):
        raise ValueError('acceptance report must contain exactly ' + ', '.join(schema['required']))
    entry_keys = set(schema['properties']['acceptance']['items']['required'])
    for index, entry in enumerate(report['acceptance']):
        if not isinstance(entry, dict) or set(entry) != entry_keys:
            raise ValueError(f'acceptance[{index}] must contain exactly criterion (exact frozen string), state ("passed"), evidence (nonempty host check ID array)')


def characterization_evidence_schema(*, workflow_version: int = 0) -> dict[str, Any]:
    schema = {'required_fields': ['path', 'evidence_kind', 'executor', 'runtime_operations'],
     'runtime_required_declaration_fields': ['test_source_files'],
     'runtime_test_source_suffixes': ['.java', '.kt', '.groovy'],
     'runtime_witness_log_marker': 'MODPORT_RUNTIME_WITNESS <execution_nonce> <test_id>',
     'evidence_kinds': ['runtime', 'static_client'],
     'runtime_executors': ['gametest', 'junit', 'integration', 'client_smoke'],
     'static_executor': 'static_analysis',
     'runtime_required_record_fields': ['test_id',
                                        'evidence_kind',
                                        'executor',
                                        'source_fingerprint',
                                        'execution_nonce',
                                        'execution_inputs',
                                        'runtime_operations',
                                        'runtime_witnesses',
                                        'observations',
                                        'status'],
     'static_client_required_record_fields': ['test_id',
                                              'evidence_kind',
                                              'executor',
                                              'source_fingerprint',
                                              'execution_nonce',
                                              'execution_inputs',
                                              'runtime_operations',
                                              'observations',
                                              'static_reason',
                                              'acceptance_gates',
                                              'status']}
    schema['record_shapes'] = {
        'status': {'const': 'passed'},
        'execution_inputs': {'oneOf': [{'type': 'array', 'minItems': 1}, {'type': 'object', 'minProperties': 1}]},
        'observations': {'oneOf': [{'type': 'array', 'minItems': 1}, {'type': 'object', 'minProperties': 1}]},
        'runtime_operations': {'type': 'array', 'minItems': 1, 'items': {'type': 'string', 'minLength': 1}},
    }
    nonempty_text = {'type': 'string', 'minLength': 1, 'pattern': r'\S'}
    text_sequence = {'oneOf': [nonempty_text, {'type': 'array', 'minItems': 1,
                                             'uniqueItems': True, 'items': nonempty_text}]}
    schema['behavior_schema'] = {
        'type': 'object',
        'required': ['id', 'source_evidence', 'preconditions', 'action', 'assertions', 'side', 'test_mapping'],
        'properties': {'id': nonempty_text, 'source_evidence': nonempty_text,
                       'preconditions': text_sequence, 'action': text_sequence, 'assertions': text_sequence,
                       'side': {'enum': ['client', 'server', 'both', 'shared']},
                       'test_mapping': {'type': 'array', 'minItems': 1, 'uniqueItems': True,
                                        'items': nonempty_text}},
    }
    if workflow_version >= 29:
        schema['assertion_contract_schema'] = {
            'type': 'object', 'additionalProperties': False,
            'required': ['assertion_id', 'text', 'source_anchor', 'test_ids'],
            'properties': {
                'assertion_id': nonempty_text,
                'text': nonempty_text,
                'source_anchor': {
                    'type': 'object', 'additionalProperties': False,
                    'required': ['path', 'start_line', 'end_line'],
                    'properties': {
                        'path': {'type': 'string', 'pattern': r'^(?!/)(?!.*(?:^|/)\.\.?/)(?!.*\\).+'},
                        'start_line': {'type': 'integer', 'minimum': 1},
                        'end_line': {'type': 'integer', 'minimum': 1},
                    },
                },
                'test_ids': {'type': 'array', 'minItems': 1, 'uniqueItems': True,
                             'items': nonempty_text},
            },
        }
        schema['behavior_schema']['required'].append('assertion_contracts')
        schema['behavior_schema']['properties']['assertion_contracts'] = {
            'type': 'array', 'minItems': 1, 'items': schema['assertion_contract_schema']}
    schema['contract_guidance'] = [
        'Wire characterization sources into Gradle with .modport/characterization.init.gradle. A bare runClient task only opens the game unless the harness is compiled, registered, enabled and exits after tests. For the conventional .modport/harness Java directory the host supplies missing JavaExec wiring, modport.characterization=true and modport.projectRoot; custom launchers or layouts require an authored init script. Existing scripts are preserved. Verify startup and bounded completion; files alone are not execution evidence.',
        'Write schema_version=1, nonblank generator_id (characterization-agent for initial authorship), and a nonempty behaviors array. Behavior IDs must be nonblank strings and unique.',
        'Every behavior includes all behavior_schema required fields. source_evidence is a nonblank string identifying concrete source evidence; preconditions/action/assertions are nonempty arrays of unique nonblank strings (a single nonblank string is also accepted). test_mapping must be a nonempty array, not a string.',
        'When assertion_contracts is supplied, assertions must be the exact assertion texts projected from assertion_contracts in the same order. Keep assertion IDs, texts and source anchors intact when repairing their ordering.',
        'Use the active source_commit as source_fingerprint and copy rubric_id/rubric_version from the supplied rubric. The host fills omitted source/rubric identity fields; do not calculate digests or substitute the current template version for a frozen rubric version. Generator and independent reviewer must be distinct.',
        'The user prohibits additional hash/checksum/fingerprint verification. The runtime harness must not compare source_fingerprint, candidate or rubric metadata or fail gameplay/evidence writing when the authored contract omits host-owned identity fields. Copy required record provenance from the supplied host context; remove these extra gates during repair while preserving gameplay assertions.',
        'If entries is also supplied, it must be canonically identical to behaviors. Prefer only behaviors and the field spellings shown here; aliases must not contradict their canonical values.',
        'baseline_evidence_files is a nonempty unique string array and test_evidence binds every globally unique mapped test ID exactly once. Supply executable baseline_gradle_tasks as declared in the evidence protocol.',
    ]
    witness = {'type': 'object',
               'required': ['operation', 'event_index', 'invocation', 'execution_nonce', 'observations'],
               'properties': {'operation': nonempty_text, 'event_index': {'type': 'integer', 'minimum': 0},
                              'invocation': nonempty_text, 'execution_nonce': nonempty_text,
                              'observations': schema['record_shapes']['observations']}}
    common = {**schema['record_shapes'], 'test_id': {'type': 'string', 'pattern': '^[A-Za-z0-9_.:-]+$'},
              'source_fingerprint': nonempty_text, 'execution_nonce': nonempty_text}
    schema['record_schemas'] = {
        'runtime': {'type': 'object', 'required': schema['runtime_required_record_fields'],
                    'properties': {**common, 'evidence_kind': {'const': 'runtime'},
                                   'executor': {'enum': schema['runtime_executors']},
                                   'runtime_witnesses': {'type': 'array', 'minItems': 1, 'items': witness}}},
        'static_client': {'type': 'object', 'required': schema['static_client_required_record_fields'],
                          'properties': {**common, 'evidence_kind': {'const': 'static_client'},
                                         'executor': {'const': schema['static_executor']},
                                         'static_reason': nonempty_text,
                                         'acceptance_gates': {'type': 'array', 'contains': {'const': 'client_smoke'}}}},
    }
    schema['declaration_schemas'] = {
        kind: {'type': 'object', 'required': schema['required_fields'] + extra,
               'properties': {'path': {'type': 'string', 'pattern': '^\\.modport/evidence/.+'},
                              'evidence_kind': {'const': kind},
                              'executor': schema['record_schemas'][kind]['properties']['executor'],
                              'runtime_operations': schema['record_shapes']['runtime_operations'],
                              'test_source_files': {'type': 'array', 'minItems': 1, 'uniqueItems': True,
                                                    'items': nonempty_text},
                              'static_reason': nonempty_text,
                              'acceptance_gates': {'type': 'array', 'contains': {'const': 'client_smoke'}}}}
        for kind, extra in [('runtime', ['test_source_files']), ('static_client', ['static_reason', 'acceptance_gates'])]
    }
    if workflow_version >= 29:
        result_identity_schema = {
            'type': 'object', 'additionalProperties': False,
            'required': ['kind', 'gradle_task', 'classname', 'name'],
            'properties': {
                'kind': {'const': 'junit_xml'},
                'gradle_task': {'type': 'string',
                                'pattern': r'^:?[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)*$'},
                'classname': {'type': 'string', 'minLength': 1, 'pattern': r'^\S+$'},
                'name': {'type': 'string', 'minLength': 1, 'pattern': r'^\S+$'},
            },
        }
        schema['result_identity_schema'] = result_identity_schema
        schema['declaration_schemas']['runtime']['properties']['executor'] = {'const': 'junit'}
        schema['declaration_schemas']['runtime']['required'].append('result_identity')
        schema['declaration_schemas']['runtime']['properties']['result_identity'] = result_identity_schema
        schema['declaration_schemas']['static_client']['not'] = {'required': ['result_identity']}
    schema['semantics'] = [
        'Every behavior has nonempty test_mapping. Test IDs are globally unique across all behaviors: two behaviors MUST NOT share a test ID. Use distinct test IDs and records even when implementation helpers are shared.',
        'test_evidence keys exactly equal all mapped IDs; identifiers match [A-Za-z0-9_.:-]+. Each ID owns one distinct baseline_evidence_files path under .modport/evidence/, without empty, dot, parent, or backslash components.',
        'Runtime declarations require nonempty unique test_source_files pointing to real nonsymlink harness sources under .modport with .java/.kt/.groovy suffixes.',
        'Record test_id equals the declaration key. evidence_kind, executor and runtime_operations exactly equal the declaration. source_fingerprint equals the current baseline source commit; execution_nonce equals the current verifier nonce.',
        'Runtime records need one ordered runtime_witness per declared operation. Each witness has operation equal to that operation, event_index equal to its zero-based index (integer, not boolean), nonblank invocation, current execution_nonce and nonempty array/object observations.',
        'Emit MODPORT_RUNTIME_WITNESS <execution_nonce> <test_id> in the captured execution log after actual execution. Never fabricate a passing record or copy example observations without executing the behavior.',
        'Static_client is restricted to client-only visual behavior that cannot be automated: executor static_analysis, nonblank static_reason identical in declaration and record, and acceptance_gates containing client_smoke in both.',
    ]
    if workflow_version >= 29:
        schema['semantics'] += [
            'Every assertion has one globally unique assertion_id, an exact text, a concrete original-source line range, and one or more exact test_ids. The union of assertion test_ids must equal that behavior test_mapping.',
            'Source anchors are relative to the original Forge checkout. The host resolves each path and line range against the pinned source commit, hashes both the whole file and exact bytes in the range, and rejects missing files, traversal, symlinks, wrong commits or out-of-range lines.',
            'For v29 and later, every test_id must have runtime JUnit XML result_identity with an exact Gradle test task, classname and method name. Each result identity is unique and the host must observe that exact testcase in fresh Gradle XML before the assertion can pass. Static client declarations do not establish an actual test-result identity.',
            'Use the host verify_characterization tool for a single selected testcase. It wires the declared init scripts, adds a host nonce, derives the candidate identity at invocation, enforces the host timeout, and writes a typed receipt. A raw runClient call is never a passing case receipt.',
            'If a harness cannot select the requested case, report selection_unsupported and leave it unverified. A full suite diagnostic is permitted only through the host-wired tool and does not stand in for affected-case result identities.',
            'Carry a previous passing case only from an authenticated host receipt whose exact assertion source anchor, test declaration, result identity, test source and harness wiring identities still match. Preserve the old receipt and create a new verification record for every changed case.',
        ]
    declaration = {'path': '.modport/evidence/example.test.json', 'evidence_kind': 'runtime',
                   'executor': 'junit', 'runtime_operations': ['invoke behavior'],
                   'test_source_files': ['.modport/harness/ExampleTest.java']}
    record = {'test_id': 'example.test', 'evidence_kind': 'runtime', 'executor': 'junit',
              'source_fingerprint': '<current source commit>', 'execution_nonce': '<current verifier nonce>',
              'execution_inputs': {'argument': 'actual input'}, 'runtime_operations': ['invoke behavior'],
              'runtime_witnesses': [{'operation': 'invoke behavior', 'event_index': 0,
                  'invocation': 'ExampleTest invokes the real behavior', 'execution_nonce': '<current verifier nonce>',
                  'observations': {'result': 'actual asserted result'}}],
              'observations': {'result': 'actual asserted result'}, 'status': 'passed'}
    static_declaration = {'path': '.modport/evidence/client.visual.json', 'evidence_kind': 'static_client',
                          'executor': 'static_analysis', 'runtime_operations': ['inspect visual layout'],
                          'static_reason': 'Explain why this client-only visual behavior cannot be automated',
                          'acceptance_gates': ['client_smoke']}
    static_record = {**{k: v for k, v in record.items() if k != 'runtime_witnesses'},
                     **{k: v for k, v in static_declaration.items() if k != 'path'}, 'test_id': 'client.visual'}
    behavior = {'id': 'example', 'source_evidence': 'src/main/java/Example.java:42',
                'preconditions': ['A concrete initialized state'], 'action': ['Invoke behavior with actual input'],
                'assertions': ['The observable result equals the expected value'], 'side': 'both',
                'test_mapping': ['example.test']}
    if workflow_version >= 29:
        behavior['assertion_contracts'] = [{
            'assertion_id': 'example.result',
            'text': 'The observable result equals the expected value',
            'source_anchor': {'path': 'src/main/java/Example.java', 'start_line': 42, 'end_line': 42},
            'test_ids': ['example.test'],
        }]
        declaration['result_identity'] = {
            'kind': 'junit_xml', 'gradle_task': 'test',
            'classname': 'example.ExampleTest', 'name': 'returnsExpectedValue',
        }
    schema['examples'] = {'contract': {'schema_version': 1, 'generator_id': 'characterization-agent',
                                      'behaviors': [behavior], 'baseline_gradle_tasks': ['test'],
                                      'test_evidence': {'example.test': declaration},
                                      'baseline_evidence_files': [declaration['path']]},
                          'declaration': declaration, 'runtime_record': record,
                          'static_client_declaration': static_declaration, 'static_client_record': static_record,
                          'mapping': {'behaviors': [{'id': 'example', 'test_mapping': ['example.test']}],
                                      'test_evidence': {'example.test': declaration},
                                      'baseline_evidence_files': [declaration['path']]}}
    if workflow_version >= 34:
        schema['verification_basis'] = 'target_execution_from_source_requirements'
        schema['runtime_required_record_fields'].remove('source_fingerprint')
        schema['record_schemas']['runtime']['required'] = list(schema['runtime_required_record_fields'])
        schema['declaration_schemas']['runtime']['properties']['executor'] = {'enum': ['junit', 'gametest']}
        schema['record_schemas']['runtime']['properties']['executor'] = {'enum': ['junit', 'gametest']}
        schema['assertion_contract_schema']['required'] = ['assertion_id', 'text', 'test_ids']
        schema['assertion_contract_schema']['properties'].pop('source_anchor', None)
        schema['assertion_contract_schema']['properties']['source_anchors'] = {
            'type': 'array', 'items': behavior_requirements_protocol()['schema']['properties'][
                'behaviors']['items']['properties']['source_anchors']['items']}
        schema['contract_guidance'] = [
            'Read behavior_requirements.requirements. Preserve every behavior_id as behavior id and every assertion_id and expected outcome in target assertion_contracts.',
            'Create the executable functional contract and harness in the target workspace. There is no required source harness or source runtime evidence.',
            'The names baseline_gradle_tasks and baseline_evidence_files are retained wire fields for the target executor; they describe target tasks and target evidence paths in this workflow.',
            'Use exact target APIs and official available test channels. Batch compatible cases in shared target runtime sessions while retaining distinct case/assertion results.',
            'Every behavior includes the behavior_schema fields. Preserve requirements source references; do not calculate source, candidate or rubric hashes.',
            'Host context supplies record provenance and execution nonce. Copy it without source_fingerprint, candidate or rubric comparison gates.',
            'Wire target sources and launch configuration in .modport/characterization.init.gradle. Actual target execution must produce fresh runtime witnesses and required assertions; a game menu or compilation does not pass behavior tests.',
            'Once frozen, preserve target test IDs, assertion IDs, expected outcomes, declarations and result identities during repair.',
        ]
        schema['semantics'] = [
            'Map every required source behavior and assertion to executable target tests. Test IDs are unique across behaviors and test_evidence keys equal all test_mapping IDs.',
            'Use distinct contained .modport/evidence/ paths; baseline_evidence_files lists those target output paths.',
            'Runtime declarations identify actual target harness sources and result identities. Required assertions need fresh host-observed results from real target actions.',
            'Copy source provenance from host context and the current execution nonce. Do not calculate or compare source, candidate or rubric fingerprints.',
            'Records match declared test_id, evidence_kind, executor and runtime_operations. Emit ordered runtime witnesses and the process log marker after the actual action executes.',
            'Skipped, missing and unimplemented required cases remain unresolved. Report infrastructure and implementation failures without fabricating passing evidence.',
            'Use official test channels suitable for the locked target and batch compatible cases under one runtime while preserving distinct case and assertion records.',
        ]
    return schema


def characterization_evidence_prompt(template_ref: Mapping[str, Any], *, workflow_version: int = 0) -> str:
    return ('\nBefore authoring characterization evidence, read the complete JSON contract at '
            + json.dumps(dict(template_ref), ensure_ascii=False)
            + '. It contains declaration/record schemas, runtime/static examples and mapping rules, '
            'including full behavior fields and requirements absent from older rubrics. Retain the supplied source/rubric identity and distinct generator/reviewer IDs. '
            'Passing records require status="passed", nonempty array/object execution_inputs and observations, '
            'exact declaration/source/nonce bindings and ordered runtime witnesses. '
            'Test IDs are globally unique across behaviors and each owns a distinct declared evidence file. '
            + ('Each assertion also requires an exact original-source anchor and test-result identity. Use the '
               'host verify_characterization tool for selected cases; a raw shell or runClient result cannot '
               'produce acceptance evidence. '
               if workflow_version >= 29 else '')
            + 'Examples are formats, never execution proof.')


def matrix_protocol_prompt(template_ref: Mapping[str, Any], *, role: str) -> str:
    """Point v31 authors at the shared case and assessment wire protocol."""
    protocol = matrix_protocol()
    if not isinstance(protocol, Mapping) or not {
            'matrix_schema', 'assessment_schema', 'guidance'} <= set(protocol):
        raise ValueError('test matrix protocol is missing its shared schemas or guidance')
    if role == 'producer':
        assignment = (
            'Write .modport/test-matrix.json from the original source discovery. '
            'Keep its test IDs aligned with behavior mappings and exact assertion IDs in '
            '.modport/functional-contract.json. Record useful risk, ordering and property '
            'strategies in actions, conditions and exploration_notes.'
        )
    elif role == 'planner':
        assignment = (
            'After reading actual host baseline execution evidence, write the case decisions '
            'to .modport/test-assessment.json. Keep the existing review report at '
            '.modport/contract-review.json as a separate report.'
        )
    else:
        raise ValueError('test matrix prompt role must be producer or planner')
    return ('\nWorkflow v31 shared test-matrix protocol: read the complete JSON contract at '
            + json.dumps(dict(template_ref), ensure_ascii=False)
            + '. It supplies matrix_schema, assessment_schema and shared discovery/execution '
            'guidance; follow those definitions without inventing another schema. ' + assignment)


def target_build_requirements(manifest: Any) -> dict[str, Any]:
    return {'build_file': 'build.gradle', 'properties_file': 'gradle.properties',
            'properties': {'minecraft_version': manifest.minecraft_version or manifest.request.target_minecraft,
                           'neo_version': manifest.neoforge_version},
            'property_aliases': {'minecraft_version': ['minecraft_version', 'mc_version'],
                                 'neo_version': ['neo_version', 'neoforge_version']},
            'java_version': str(manifest.java_version)}


def target_build_prompt(manifest: Any) -> str:
    requirements = target_build_requirements(manifest)
    return ('\nTarget build contract: ' + json.dumps(requirements, ensure_ascii=False)
            + '. Supply these two files at the project root. In gradle.properties use explicit key=value lines for each locked property or a listed alias; '
            'in build.gradle declare the Java toolchain with JavaLanguageVersion.of('
            + requirements['java_version'] + '). Whitespace around assignments and Java call punctuation is allowed. '
            'Property indirection for this Java expression or a Kotlin-only build.gradle.kts layout does not satisfy this gate.')
