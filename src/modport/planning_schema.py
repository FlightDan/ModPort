"""Planning and goal field contracts shared by prompts and output validation."""

import json


FAILURE_ANALYSIS_SCHEMA = {
    'type': 'object',
    'required': ['cause', 'evidence_refs', 'unknowns', 'previous_attempts_analysis'],
    'properties': {
        'cause': {'type': 'string', 'minLength': 1, 'pattern': r'\S'},
        'evidence_refs': {'type': 'array', 'minItems': 1, 'uniqueItems': True,
                          'items': {'type': 'string', 'minLength': 1, 'pattern': r'\S'},
                          'description': 'Only authenticated input aliases are accepted.'},
        'unknowns': {'type': 'array', 'uniqueItems': True,
                     'items': {'type': 'string', 'minLength': 1, 'pattern': r'\S'}},
        'previous_attempts_analysis': {
            'type': 'string', 'minLength': 1, 'pattern': r'\S',
            'description': 'Write the analysis as prose in one JSON string. '
                           'Explain relevant prior attempts or state that none were found.',
        },
    },
}

FAILURE_ANALYSIS_TEMPLATE = {
    'cause': '<explain the original failure using evidence>',
    'evidence_refs': ['<an authenticated input alias actually used>'],
    'unknowns': [],
    'previous_attempts_analysis': '<prose describing relevant prior attempts, or their absence>',
}


def json_type(value):
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'boolean'
    if isinstance(value, str):
        return 'string'
    if isinstance(value, dict):
        return 'object'
    if isinstance(value, list):
        return 'array'
    if isinstance(value, (int, float)):
        return 'number'
    return type(value).__name__


class PlanningValidationError(ValueError):
    """A field error the next model call can correct without guessing its type."""

    def __init__(self, path, code, expected_type, actual_type, constraint):
        self.diagnostic = {
            'path': path, 'code': code,
            'expected_type': expected_type, 'actual_type': actual_type,
            'constraint': constraint,
        }
        super().__init__(f'{path}: {code}; expected {constraint}, got {actual_type}')


def _validate(value, schema, path):
    expected_type = schema['type']
    actual_type = json_type(value)
    if actual_type != expected_type:
        raise PlanningValidationError(path, 'invalid_type', expected_type, actual_type,
                                      expected_type)
    if expected_type == 'string' and not value.strip():
        raise PlanningValidationError(path, 'empty_value', 'string', 'string',
                                      'nonempty string containing non-whitespace text')
    if expected_type == 'object':
        for field in schema['required']:
            child = schema['properties'][field]
            field_path = path + '/' + field
            if field not in value:
                raise PlanningValidationError(field_path, 'missing_field', child['type'],
                                              'missing', child['type'])
            _validate(value[field], child, field_path)
    if expected_type == 'array':
        if len(value) < schema.get('minItems', 0):
            raise PlanningValidationError(path, 'empty_value', 'array', 'array',
                                          'nonempty array')
        for index, item in enumerate(value):
            _validate(item, schema['items'], path + '/' + str(index))
        if schema.get('uniqueItems') and len(set(value)) != len(value):
            raise PlanningValidationError(path, 'duplicate_value', 'array', 'array',
                                          'array of unique strings')


def validate_failure_analysis(document):
    if 'failure_analysis' not in document:
        raise PlanningValidationError('/failure_analysis', 'missing_field', 'object',
                                      'missing', 'object')
    _validate(document['failure_analysis'], FAILURE_ANALYSIS_SCHEMA, '/failure_analysis')


def diagnosis_schema_prompt():
    return ('\nRequired failure_analysis JSON Schema: '
            + json.dumps(FAILURE_ANALYSIS_SCHEMA, ensure_ascii=False, sort_keys=True)
            + '\nDiagnosis example fragment (replace placeholders with grounded content): '
            + json.dumps({'failure_analysis': FAILURE_ANALYSIS_TEMPLATE}, ensure_ascii=False)
            + '\nMerge this fragment with the exact envelope and issues array. '
              'previous_attempts_analysis must be one nonempty JSON string, not an object or array. '
              'Use prose to retain the reasoning and cite authenticated evidence; do not invent prior attempts.')


# These are wire shapes, not substitutes for evidence, ownership, DAG and oracle
# validation. The same objects are printed in protected prompts and checked before
# semantic validation so independent type errors arrive in one correction round.
TEXT = {'type': 'string', 'minLength': 1}

def strings(*, nonempty=True):
    return {'type': 'array', 'items': TEXT, 'minItems': 1 if nonempty else 0,
            'uniqueItems': True}


def object_shape(properties, required=None, **extra):
    return {'type': 'object', 'properties': properties,
            'required': list(properties) if required is None else required, **extra}


def array_shape(items, *, nonempty=True):
    return {'type': 'array', 'items': items, 'minItems': 1 if nonempty else 0}


CHECK_SCHEMA = object_shape({
    'id': TEXT, 'type': {'type': 'string', 'enum': ['file_exists', 'json_valid',
        'python_syntax', 'contract_schema', 'gradle_tasks', 'gradle_regression']},
    'acceptance': strings(), 'path': TEXT, 'tasks': strings(), 'reports': strings(),
}, required=['id', 'type', 'acceptance'], additionalProperties=False)
CHECK_SCHEMA['allOf'] = [
    {'if': object_shape({'type': {'type': 'string', 'enum': kinds}}),
     'then': object_shape({key: CHECK_SCHEMA['properties'][key] for key in fields},
                          additionalProperties=False)}
    for kinds, fields in [
        (['file_exists', 'json_valid', 'python_syntax', 'contract_schema'], ['id', 'type', 'acceptance', 'path']),
        (['gradle_tasks'], ['id', 'type', 'acceptance', 'tasks']),
        (['gradle_regression'], ['id', 'type', 'acceptance', 'tasks', 'reports']),
    ]
]
CHECKS_SCHEMA = array_shape(CHECK_SCHEMA)
TASK_PROPERTIES = {
    'id': TEXT, 'objective': TEXT, 'dependencies': strings(nonempty=False),
    'owned_paths': strings(), 'acceptance': strings(),
    'complexity': {'type': 'string', 'enum': ['simple', 'complex']},
    'validation_checks': CHECKS_SCHEMA,
    'validation_kind': {'type': 'string', 'enum': ['structural', 'regression']},
    'structural_reason': TEXT, 'blocked_by_gaps': strings(nonempty=False),
}
GOAL_SCHEMA = object_shape({
    'task_id': TEXT, 'objective': TEXT, 'owned_paths': strings(),
    'dependencies': strings(nonempty=False), 'acceptance': strings(),
    'context_refs': object_shape({}, required=[]), 'stop_conditions': strings(),
    'acceptance_report': TEXT, 'checks': CHECKS_SCHEMA,
})


def planning_shape(index, *, repair=False, scoped=True):
    if index == 0:
        fields = {'id': TEXT, 'summary': TEXT,
                  'classification': {'type': 'string', 'enum': ['fact', 'hypothesis', 'unknown']},
                  'evidence_refs': strings(), 'obligation_ids': strings(nonempty=False)}
        if scoped:
            fields.update(resolution_scope={'type': 'string', 'enum': ['current', 'prerequisite', 'downstream']},
                          resolution_stage=TEXT, scope_reason=TEXT)
        root = {'issues': array_shape(object_shape(fields))}
        if repair:
            root['failure_analysis'] = FAILURE_ANALYSIS_SCHEMA
        return object_shape(root)
    if index == 1:
        fields = {'id': TEXT, 'issue_ids': strings(), 'approach': TEXT,
                  'regression_method': TEXT, 'disposition_reason': TEXT,
                  'interfaces': strings(nonempty=False), 'prerequisites': strings(nonempty=False)}
        if repair:
            fields.update(risks=strings(nonempty=False), constraints=strings(nonempty=False))
        return object_shape({'strategies': array_shape(object_shape(fields))})
    if index == 2:
        fields = {**TASK_PROPERTIES, 'kind': {'type': 'string', 'enum': ['prepare', 'coder', 'deferred']},
                  'inputs': strings(), 'outputs': strings(), 'issue_ids': strings(),
                  'strategy_ids': strings(), 'stop_conditions': strings()}
        required = ['id', 'kind', 'objective', 'inputs', 'outputs', 'issue_ids',
                    'strategy_ids', 'dependencies', 'acceptance']
        if repair:
            required.append('stop_conditions')
        fields['validation_checks'] = array_shape(CHECK_SCHEMA, nonempty=False)
        fields['owned_paths'] = strings(nonempty=False)
        task_shape = object_shape(fields, required)
        immediate = {'owned_paths': strings(), 'complexity': TASK_PROPERTIES['complexity']}
        if scoped:
            immediate['validation_checks'] = CHECKS_SCHEMA
        task_shape['allOf'] = [{
            'if': object_shape({'kind': {'type': 'string', 'enum': ['deferred']}}),
            'else': object_shape(immediate)}]
        root = {'tasks': array_shape(task_shape)}
        if repair:
            root['consistency_review'] = object_shape({
                'consistent': {'type': 'boolean'}, 'explanation': TEXT,
                'contradictions': strings(nonempty=False)})
        return object_shape(root)
    group = object_shape({**TASK_PROPERTIES, 'source_task_ids': strings(),
        'source_objectives': object_shape({}, required=[], additionalProperties=TEXT),
        'source_acceptance': object_shape({}, required=[], additionalProperties=strings()),
        'merge_rationale': TEXT}, required=['id', 'objective', 'dependencies', 'owned_paths',
            'acceptance', 'complexity', 'source_task_ids', 'source_objectives', 'source_acceptance'])
    review_shape = object_shape({
        'parallel_decision': {'type': 'string', 'enum': ['parallel', 'sequential', 'prepare_first', 'replan']},
        'reason': TEXT, 'replan_stage': TEXT,
        'coupling_checks': array_shape(object_shape({'groups': strings(), 'evidence': TEXT,
            'resolution': {'type': 'string', 'enum': ['independent', 'dependency']}}), nonempty=False),
        'development_plan': object_shape({'schema_version': {'type': 'integer', 'enum': [1]}, 'base_commit': TEXT,
            'shared_paths': strings(nonempty=False), 'tasks': array_shape(group)}),
        'deferred_obligations': array_shape(object_shape({'id': TEXT, 'source_task_id': TEXT,
            'source_issue_ids': strings(), 'objective': TEXT, 'closure_criteria': strings(),
            'resolution_stage': TEXT, 'scope_reason': TEXT}), nonempty=False),
        'preparation_assessment': object_shape({'conforms_to_plan': {'type': 'boolean'}, 'evidence': TEXT}),
    }, required=['parallel_decision', 'reason'])
    review_shape['allOf'] = [
        {'if': object_shape({'parallel_decision': {'type': 'string', 'enum': decisions}}),
         'then': object_shape({key: review_shape['properties'][key] for key in required})}
        for decisions, required in [(['replan'], ['replan_stage']),
                                   (['parallel', 'sequential'], ['development_plan', 'coupling_checks'])]]
    if scoped:
        group['required'].append('validation_checks')
    return review_shape


def shape_errors(value, schema, path=''):
    errors = []
    def error(code, expected, actual, constraint, at=path):
        errors.append(PlanningValidationError(at or '/', code, expected, actual, constraint).diagnostic)
    expected = schema['type']
    actual = json_type(value)
    if not (actual == expected or (expected == 'integer' and type(value) is int)):
        error('invalid_type', expected, actual, expected)
        return errors
    for branch in schema.get('allOf', []):
        matched = not shape_errors(value, branch['if'], path)
        selected = branch.get('then' if matched else 'else')
        if selected:
            errors.extend(shape_errors(value, selected, path))
    if expected == 'string' and schema.get('minLength', 0) and not value.strip():
        error('empty_value', expected, actual, 'nonempty string containing non-whitespace text')
    if 'enum' in schema and value not in schema['enum']:
        error('invalid_value', expected, actual, 'one of ' + json.dumps(schema['enum']))
    if expected == 'object':
        props = schema.get('properties', {})
        for key in schema.get('required', []):
            if key not in value:
                error('missing_field', props[key]['type'], 'missing', props[key]['type'], path + '/' + key)
        for key, child in value.items():
            escaped = key.replace('~', '~0').replace('/', '~1')
            child_schema = props.get(key, schema.get('additionalProperties'))
            if child_schema is False:
                error('unknown_field', 'absent', json_type(child), 'no additional fields', path + '/' + escaped)
            elif isinstance(child_schema, dict):
                errors.extend(shape_errors(child, child_schema, path + '/' + escaped))
    if expected == 'array':
        if len(value) < schema.get('minItems', 0):
            error('empty_value', expected, actual, 'nonempty array')
        for i, child in enumerate(value):
            errors.extend(shape_errors(child, schema['items'], path + '/' + str(i)))
        if schema.get('uniqueItems'):
            encoded = [json.dumps(item, sort_keys=True) for item in value]
            if len(set(encoded)) != len(encoded):
                error('duplicate_value', expected, actual, 'array of unique values')
    return list({json.dumps(error, sort_keys=True): error for error in errors}.values())


def validate_shape(value, schema, path=''):
    errors = shape_errors(value, schema, path)
    if errors:
        first = errors[0]
        exc = PlanningValidationError(first['path'], first['code'], first['expected_type'],
                                      first['actual_type'], first['constraint'])
        exc.diagnostics = errors
        raise exc


def check_schema_prompt():
    return ('\nShared goal/check wire contract: ' + json.dumps(CHECKS_SCHEMA, sort_keys=True)
            + '\nEvery check.acceptance is a NONEMPTY ARRAY of exact task acceptance strings, '
              'even for one criterion. Example: {"id":"T01-check","type":"json_valid",'
              '"acceptance":["Exact criterion copied from task.acceptance"],"path":"owned/file.json"}. '
              'File check fields are exactly id,type,acceptance,path. gradle_tasks fields are exactly '
              'id,type,acceptance,tasks. gradle_regression fields are exactly '
              'id,type,acceptance,tasks,reports. No extra fields. The task checks collectively cover '
              'every acceptance criterion. Goal checks must copy frozen validation_checks byte for byte '
              'in JSON value, including order; never replace an array with prose.')


def planning_schema_prompt(index, *, repair=False, scoped=True):
    return ('\nRequired planning wire shape (merge with exact envelope): '
            + json.dumps(planning_shape(index, repair=repair, scoped=scoped), sort_keys=True)
            + (check_schema_prompt() if index >= 2 else ''))
