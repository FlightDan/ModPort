import copy
import unittest

from modport.planning_schema import (
    FAILURE_ANALYSIS_TEMPLATE, PlanningValidationError, validate_failure_analysis,
)


class DiagnosisSchemaTests(unittest.TestCase):
    def test_template_satisfies_shared_type_contract(self):
        validate_failure_analysis({'failure_analysis': copy.deepcopy(FAILURE_ANALYSIS_TEMPLATE)})

    def test_field_errors_distinguish_missing_empty_and_wrong_types(self):
        cases = [
            ('previous_attempts_analysis', {}, 'invalid_type', 'object'),
            ('previous_attempts_analysis', [], 'invalid_type', 'array'),
            ('previous_attempts_analysis', None, 'invalid_type', 'null'),
            ('previous_attempts_analysis', '  ', 'empty_value', 'string'),
            ('cause', False, 'invalid_type', 'boolean'),
            ('unknowns', '', 'invalid_type', 'string'),
            ('evidence_refs', [], 'empty_value', 'array'),
            ('evidence_refs', ['alias', 'alias'], 'duplicate_value', 'array'),
        ]
        for field, value, code, actual in cases:
            with self.subTest(field=field, value=value):
                analysis = copy.deepcopy(FAILURE_ANALYSIS_TEMPLATE)
                analysis[field] = value
                with self.assertRaises(PlanningValidationError) as raised:
                    validate_failure_analysis({'failure_analysis': analysis})
                self.assertEqual('/failure_analysis/' + field, raised.exception.diagnostic['path'])
                self.assertEqual(code, raised.exception.diagnostic['code'])
                self.assertEqual(actual, raised.exception.diagnostic['actual_type'])
        analysis = copy.deepcopy(FAILURE_ANALYSIS_TEMPLATE)
        del analysis['previous_attempts_analysis']
        with self.assertRaises(PlanningValidationError) as raised:
            validate_failure_analysis({'failure_analysis': analysis})
        self.assertEqual('missing_field', raised.exception.diagnostic['code'])
        self.assertEqual('string', raised.exception.diagnostic['expected_type'])

    def test_nested_element_errors_identify_array_index(self):
        analysis = copy.deepcopy(FAILURE_ANALYSIS_TEMPLATE)
        analysis['unknowns'] = [{}]
        with self.assertRaises(PlanningValidationError) as raised:
            validate_failure_analysis({'failure_analysis': analysis})
        self.assertEqual('/failure_analysis/unknowns/0', raised.exception.diagnostic['path'])
        self.assertEqual('string', raised.exception.diagnostic['expected_type'])


class PlanningWireShapeTests(unittest.TestCase):
    def test_all_bad_acceptance_fields_reported_together(self):
        from modport.planning_schema import CHECKS_SCHEMA, validate_shape
        checks = [{'id': f'T{i}', 'type': 'json_valid', 'path': f'owned/{i}.json',
                   'acceptance': 'Exact criterion'} for i in range(5)]
        with self.assertRaises(PlanningValidationError) as raised:
            validate_shape(checks, CHECKS_SCHEMA, '/tasks/0/validation_checks')
        errors = raised.exception.diagnostics
        self.assertEqual(5, len(errors))
        self.assertEqual([f'/tasks/0/validation_checks/{i}/acceptance' for i in range(5)],
                         [error['path'] for error in errors])
        self.assertTrue(all(error['expected_type'] == 'array' for error in errors))
        for check in checks:
            check['acceptance'] = [check['acceptance']]
        validate_shape(checks, CHECKS_SCHEMA)

    def test_check_type_selects_exact_fields(self):
        from modport.planning_schema import CHECKS_SCHEMA, validate_shape
        variants = [('json_valid', {'path': 'owned/file.json'}),
                    ('gradle_tasks', {'tasks': [':test']}),
                    ('gradle_regression', {'tasks': [':test'], 'reports': ['build/TEST-Suite.xml']})]
        for kind, fields in variants:
            check = {'id': 'check', 'type': kind, 'acceptance': ['criterion'], **fields}
            validate_shape([check], CHECKS_SCHEMA)
            for field in fields:
                bad = {key: value for key, value in check.items() if key != field}
                with self.subTest(kind=kind, missing=field), self.assertRaises(PlanningValidationError):
                    validate_shape([bad], CHECKS_SCHEMA)
            wrong = 'path' if kind.startswith('gradle') else 'tasks'
            with self.subTest(kind=kind, extra=wrong), self.assertRaises(PlanningValidationError):
                validate_shape([{**check, wrong: 'unexpected'}], CHECKS_SCHEMA)

    def test_deferred_tasks_allow_empty_checks_but_immediate_requires_complexity(self):
        from modport.planning_schema import planning_shape, validate_shape
        task = {'id': 'later', 'kind': 'deferred', 'objective': 'Later verification',
                'inputs': ['source'], 'outputs': ['result'], 'issue_ids': ['I1'],
                'strategy_ids': ['S1'], 'dependencies': [], 'acceptance': ['criterion'],
                'owned_paths': [], 'validation_checks': []}
        validate_shape({'tasks': [task]}, planning_shape(2))
        task.update(kind='coder', owned_paths=['owned'], validation_checks=[
            {'id': 'check', 'type': 'json_valid', 'path': 'owned/file.json', 'acceptance': ['criterion']}])
        with self.assertRaises(PlanningValidationError) as raised:
            validate_shape({'tasks': [task]}, planning_shape(2))
        self.assertEqual('/tasks/0/complexity', raised.exception.diagnostic['path'])

    def test_review_decision_requires_its_own_fields(self):
        from modport.planning_schema import planning_shape, validate_shape
        for decision in ('parallel', 'sequential', 'replan'):
            with self.subTest(decision=decision), self.assertRaises(PlanningValidationError):
                validate_shape({'parallel_decision': decision, 'reason': 'Evidence'}, planning_shape(3))
        validate_shape({'parallel_decision': 'prepare_first', 'reason': 'Required interface'}, planning_shape(3))
