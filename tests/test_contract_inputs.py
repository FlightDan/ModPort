import unittest

from modport.contract_inputs import validate_baseline_gradle_tasks


class ContractInputTests(unittest.TestCase):
    def test_executable_task_names_and_project_qualified_names(self):
        tasks = ['test', 'characterizationTest', ':client:runClient', 'mod:runtime-test_2']
        self.assertEqual(tasks, validate_baseline_gradle_tasks(tasks))
        self.assertIsNot(tasks, validate_baseline_gradle_tasks(tasks))

    def test_flags_paths_and_command_fragments_identify_the_exact_item(self):
        for invalid in ('--init-script', '-I', '.modport/characterization.init.gradle',
                        'test --stacktrace', '../test', '/tmp/test', 'test;help',
                        '', ':', 'mod::test', 'mod:', None, 2):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError) as raised:
                    validate_baseline_gradle_tasks(['test', invalid])
                self.assertIn(f'baseline_gradle_tasks[1]={invalid!r}', str(raised.exception))
                self.assertIn('host automatically discovers', str(raised.exception))

    def test_reporting_tasks_are_not_runtime_evidence(self):
        for name in ('help', 'tasks', ':mod:dependencies', 'dependencyInsight', 'javaToolchains'):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, 'reporting tasks'):
                    validate_baseline_gradle_tasks([name])

    def test_empty_or_non_list_input_is_rejected(self):
        for value in (None, [], 'test', ('test',)):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, 'non-empty list'):
                    validate_baseline_gradle_tasks(value)
