from pathlib import Path
import json
import unittest
from modport.cli import parser, _request, _task_summary


class CliTests(unittest.TestCase):
    @staticmethod
    def task(state='succeeded', value=None, stage='mod_analysis'):
        return {'attempts': [{'state': state,
                             'command': {'payload': {'stage_id': stage}},
                             'result': {'value': value}}]}

    def test_status_keeps_sdk_success_and_business_failure_distinct(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from modport.cli import main
        snapshot = {'status_summary': {
            'source': 'dispatcher-sdk public summary and work-availability APIs',
            'revision': 2, 'task_count': 1,
            'requested_task': {'task_id': 'analysis.retry-2',
                               'execution_state': 'succeeded',
                               'business_status': 'failed',
                               'error_code': 'relevant_skill_gap',
                               'detail': 'Missing relevant migration evidence'},
        }}
        run = SimpleNamespace(run_id='r', run_dir=Path('/run'), status='succeeded', snapshot=snapshot)
        with patch('modport.cli.MigrationOperations') as factory, patch('builtins.print') as output:
            factory.return_value.status.return_value = run
            self.assertEqual(main(['status', '--run-dir', '/run', '--run-id', 'r',
                                   '--task-id', 'analysis.retry-2']), 0)
            factory.return_value.status.assert_called_once_with(
                '/run', 'r', task_id='analysis.retry-2')
        result = json.loads(output.call_args.args[0])
        self.assertEqual(result['status'], 'succeeded')
        self.assertNotIn('snapshot', result)
        self.assertEqual(result['requested_task'], snapshot['status_summary']['requested_task'])
        self.assertEqual(result['execution']['revision'], 2)

    def test_summary_missing_business_result_is_unknown(self):
        for task in (self.task(), self.task(value='not an outcome'), {'attempts': []},
                     {'attempts': [{'state': 'succeeded', 'result': None}]}):
            with self.subTest(task=task):
                summary = _task_summary({'tasks': {'analysis': task}})
                row = summary['tasks'][0]
                self.assertEqual(row['business_status'], 'unknown')
                self.assertIsNone(row['error_code'])
                self.assertIsNone(row['review_verdict'])

    def test_summary_preserves_completed_review_and_rejected_verdict(self):
        task = self.task(value={'status': 'completed', 'outputs': {'verdict': 'rejected'}},
                         stage='research_review')
        row = _task_summary({'tasks': {'review': task}})['tasks'][0]
        self.assertEqual((row['execution_state'], row['business_status'], row['review_verdict']),
                         ('succeeded', 'completed', 'rejected'))

    def test_summary_uses_latest_attempt_and_bounds_rows_but_counts_all_tasks(self):
        tasks = {f'task-{i}': self.task(value={'status': 'completed', 'detail': 'x' * 2000})
                 for i in range(100)}
        task = self.task(value={'status': 'completed'})
        task['attempts'].extend(self.task('running')['attempts'])
        tasks['latest'] = task
        tasks['failed'] = self.task(value={'status': 'failed'})
        summary = _task_summary({'tasks': tasks})
        self.assertEqual(len(summary['tasks']), 100)
        self.assertEqual(summary['omitted_tasks'], 2)
        self.assertEqual(len(summary['tasks'][0]['detail']), 1000)
        self.assertEqual(summary['execution_state_counts'], {'succeeded': 101, 'running': 1})
        self.assertEqual(summary['business_status_counts'], {'completed': 100, 'unknown': 1, 'failed': 1})

    def args(self, *extra):
        return parser().parse_args(['compile', '--mod-id', 'example', '--source-repository', 'https://example.invalid/mod.git',
            '--source-revision', 'a' * 40, '--source-minecraft', '1.20.1', '--target-minecraft', '26.1.2', *extra])

    def test_defaults_and_explicit_unlimited_caps(self):
        budget = _request(self.args()).budget
        self.assertEqual((budget.max_seconds, budget.max_agent_assignments, budget.max_rework_rounds,
                          budget.execution_max_attempts), (43200, 40, 10, 3))
        unlimited = _request(self.args('--max-seconds', 'none', '--max-agent-assignments', 'none')).budget
        self.assertIsNone(unlimited.max_seconds)
        self.assertIsNone(unlimited.max_agent_assignments)

    def test_compile_package_scope_is_frozen_from_cli(self):
        request = _request(self.args('--validation-scope', 'compile_package'))
        self.assertEqual('compile_package', request.validation_scope)
        self.assertEqual('compile_package', request.to_dict()['validation_scope'])

    def test_packaged_rules_match_reviewable_documents(self):
        root = Path(__file__).resolve().parents[1]
        for name in ('AGENT_RULES.md', 'EVIDENCE_PROTOCOL.md'):
            self.assertEqual((root / 'docs' / name).read_bytes(), (root / 'src/modport/rules' / name).read_bytes())

    def test_retry_defaults_do_not_override_parent_budget(self):
        args = parser().parse_args(['retry', '--parent-run-dir', '/parent', '--parent-run-id', 'p', '--run-dir', '/child'])
        self.assertFalse(hasattr(args, 'max_seconds'))
        self.assertFalse(hasattr(args, 'max_agent_assignments'))
        self.assertFalse(args.inherit_harness)
        self.assertIsNone(args.budget_reason)

    def test_retry_passes_explicit_overrides_and_harness_intent(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from modport.cli import main
        run = SimpleNamespace(run_id='c', run_dir=Path('/child'), status='succeeded', snapshot={})
        with patch('modport.cli.MigrationOperations') as factory, patch('builtins.print'):
            operations = factory.return_value
            operations.retry.return_value = run
            result = main(['retry', '--parent-run-dir', '/parent', '--parent-run-id', 'p', '--run-dir', '/child',
                           '--max-seconds', 'none', '--max-agent-assignments', '80', '--max-rework-rounds', '20',
                           '--execution-max-attempts', '4', '--budget-reason', 'diagnostic repair', '--inherit-harness', '--dependency-cache', '/cache'])
            self.assertEqual(result, 0)
            operations.load_retry_parent.assert_called_once_with("/parent", "p")
            operations.status.assert_not_called()
            operations.retry.assert_called_once_with(operations.load_retry_parent.return_value, run_dir='/child', run_id=None,
                budget_overrides={'max_seconds': None, 'max_agent_assignments': 80, 'max_rework_rounds': 20,
                                  'execution_max_attempts': 4}, budget_reason='diagnostic repair', inherit_harness=True, dependency_cache='/cache')

    def test_continue_passes_assignment_increment_and_inherits_deadline_by_default(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from modport.cli import main

        run = SimpleNamespace(run_id='next', run_dir=Path('/run'), status='succeeded', snapshot={})
        with patch('modport.continuation.continue_from_planner', return_value=run) as continuation, \
                patch('modport.cli.MigrationOperations') as factory, patch('builtins.print'):
            factory.return_value.execute.return_value = run
            self.assertEqual(0, main(['continue', '--run-dir', '/run', '--run-id', 'current',
                '--next-run-id', 'successor', '--reason', 'apply approved model and budget policy',
                '--upgrade-workflow', '--additional-agent-assignments', '20']))
        self.assertEqual(20, continuation.call_args.kwargs['additional_agent_assignments'])
        self.assertTrue(continuation.call_args.kwargs['upgrade_workflow'])
        self.assertNotIn('additional_seconds', continuation.call_args.kwargs)

    def test_continue_exposes_target_freeze_restart_with_fifty_assignments(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from modport.cli import main

        run = SimpleNamespace(run_id='next', run_dir=Path('/run'), status='succeeded', snapshot={})
        with patch('modport.continuation.continue_from_planner', return_value=run) as continuation, \
                patch('modport.cli.MigrationOperations') as factory, patch('builtins.print'):
            factory.return_value.execute.return_value = run
            self.assertEqual(0, main(['continue', '--run-dir', '/run', '--run-id', 'current',
                '--next-run-id', 'successor', '--reason', 'repair target protocol',
                '--upgrade-workflow', '--start-stage', 'target_contract_freeze',
                '--additional-agent-assignments', '50']))
        self.assertEqual('target_contract_freeze', continuation.call_args.kwargs['start_stage'])
        self.assertEqual(50, continuation.call_args.kwargs['additional_agent_assignments'])
        self.assertNotIn('additional_seconds', continuation.call_args.kwargs)
