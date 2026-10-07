"""Diagnostic edits must reach future task files without overwriting live work."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modport.contracts import OperationInput, OperationResult
from modport.diagnostic_repairs import apply, collect, enabled, instructions, prepare
from modport.platform_files import atomic_write


class DiagnosticRepairTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'workspaces' / 'development' / 'old'
        (self.source / 'src').mkdir(parents=True)
        (self.source / 'src' / 'A.java').write_text('class A { wrong(); }\n')
        (self.source / 'src' / 'B.java').write_text('class B { wrong(); }\n')
        self.plan = {'path': 'artifacts/plan.json', 'metadata': {'execution_id': 'planner-1'}}
        self.target = {'task_id': 'a', 'plan_ref': self.plan,
                       'source_workspace': 'workspaces/development/old',
                       'source_execution_id': 'coder-old'}
        self.command = OperationInput('run', 'supervisor-1', 'supervisor', 'supervisor:1', str(self.root),
                                      options={'workflow_version': 40},
                                      payload={'diagnostic_repair_targets': [self.target]})
        self.consumer = OperationInput('run', 'coder-a', 'coder', 'coder:2', str(self.root),
                                      options={'workflow_version': 40},
                                      payload={'development_task': {'id': 'a'}, 'goal_scope': 'migration'},
                                      artifact_refs={'development_plan': self.plan})
        self.future = self.root / 'workspaces' / 'development' / 'new'
        (self.future / 'src').mkdir(parents=True)
        for path in (self.source / 'src').iterdir():
            (self.future / 'src' / path.name).write_bytes(path.read_bytes())

    def result(self, command, status='completed'):
        return OperationResult(status, command.run_id, command.task_id,
                               command.stage_id, command.command_id, outputs={'original': 'retained'})

    def edited(self, names=('A.java',), status='completed'):
        prepared = prepare(self.command)
        for name in names:
            path = self.root / prepared.options['workspace'] / 'source' / 'task-0' / 'src' / name
            path.write_text(path.read_text().replace('wrong()', 'correct()'))
        result = collect(prepared, self.result(prepared, status))
        return prepared, result.outputs['diagnostic_repairs']

    def document(self, ref):
        return json.loads((self.root / ref['path']).read_text())

    def test_future_task_consumes_fix_once_and_original_author_is_untouched(self):
        prepared, refs = self.edited()
        self.assertEqual('a', refs[0]['metadata']['task_id'])
        self.assertEqual('supervisor:1', refs[0]['metadata']['producer_execution_id'])
        self.assertNotIn('sha256', refs[0])
        receipts = apply(self.consumer, self.future, refs)
        self.assertEqual('applied', receipts[0]['status'])
        self.assertEqual([{'path': 'src/A.java', 'status': 'applied'}], receipts[0]['files'])
        self.assertIn('correct()', (self.future / 'src' / 'A.java').read_text())
        self.assertIn('wrong()', (self.source / 'src' / 'A.java').read_text())
        self.assertEqual('already_applied', apply(self.consumer, self.future, refs)[0]['status'])
        self.assertIn('small code errors', instructions(prepared))

    def test_conflict_preserves_every_file_in_the_repair(self):
        _, refs = self.edited(('A.java', 'B.java'))
        (self.future / 'src' / 'B.java').write_text('class B { newer(); }\n')
        receipt = apply(self.consumer, self.future, refs)[0]
        self.assertEqual('conflict', receipt['status'])
        self.assertIn('wrong()', (self.future / 'src' / 'A.java').read_text())
        self.assertIn('newer()', (self.future / 'src' / 'B.java').read_text())
        self.assertEqual({'conflict'}, {item['status'] for item in receipt['files']})

    def test_recovery_keeps_edited_snapshot_and_original_before_evidence(self):
        prepared = prepare(self.command)
        edited = self.root / prepared.options['workspace'] / 'source' / 'task-0' / 'src' / 'A.java'
        edited.write_text('class A { corrected(); }\n')
        (self.source / 'src' / 'A.java').write_text('class A { live_author_changed(); }\n')
        resumed = prepare(self.command)
        self.assertEqual(prepared.payload['diagnostic_repair_manifest'], resumed.payload['diagnostic_repair_manifest'])
        self.assertEqual('class A { corrected(); }\n', edited.read_text())
        collected = collect(resumed, self.result(resumed))
        change = self.document(collected.outputs['diagnostic_repairs'][0])['files'][0]
        self.assertIn('wrong()', change['before'])
        self.assertIn('corrected()', change['after'])

    def test_deleted_consumer_file_conflicts_without_applying_other_files(self):
        _, refs = self.edited(('A.java', 'B.java'))
        (self.future / 'src' / 'B.java').unlink()
        receipt = apply(self.consumer, self.future, refs)[0]
        self.assertEqual('conflict', receipt['status'])
        self.assertIn('wrong()', (self.future / 'src' / 'A.java').read_text())
        self.assertFalse((self.future / 'src' / 'B.java').exists())

    def test_failed_agent_edits_are_retained_and_cannot_be_applied(self):
        prepared, refs = self.edited(status='failed')
        self.assertFalse(refs[0]['metadata']['applicable'])
        self.assertFalse(self.document(refs[0])['applicable'])
        self.assertEqual('invalid', apply(self.consumer, self.future, refs)[0]['status'])
        self.assertIn('wrong()', (self.future / 'src' / 'A.java').read_text())
        self.assertEqual('failed', collect(prepared, self.result(prepared, 'failed')).status)

    def test_task_plan_run_and_ref_metadata_must_match_host_binding(self):
        _, refs = self.edited()
        consumers = [replace(self.consumer, payload={'development_task': {'id': 'other'}}),
                     replace(self.consumer, artifact_refs={'development_plan': {'path': 'other-plan.json'}}),
                     replace(self.consumer, run_id='another-run')]
        for consumer in consumers:
            with self.subTest(consumer=consumer.command_id, run=consumer.run_id, payload=consumer.payload):
                self.assertEqual('invalid', apply(consumer, self.future, refs)[0]['status'])
        foreign = {**refs[0], 'metadata': {**refs[0]['metadata'], 'task_id': 'another'}}
        self.assertEqual('invalid', apply(self.consumer, self.future, [foreign])[0]['status'])
        self.assertIn('wrong()', (self.future / 'src' / 'A.java').read_text())

    def test_creation_and_deletion_are_explicit_nonapplicable_diagnostics(self):
        prepared = prepare(self.command)
        snapshot = self.root / prepared.options['workspace'] / 'source' / 'task-0' / 'src'
        (snapshot / 'A.java').unlink()
        (snapshot / 'B.java').write_text('class B { correct(); }\n')
        (snapshot / 'New.java').write_text('class New {}\n')
        collected = collect(prepared, self.result(prepared))
        self.assertEqual('completed', collected.status)
        self.assertEqual('retained', collected.outputs['original'])
        reasons = {item['path']: item['reason'] for item in collected.outputs['diagnostic_repair_diagnostics']}
        self.assertIn('unsupported deletion', reasons['src/A.java'])
        self.assertEqual('unsupported new file', reasons['src/New.java'])
        refs = collected.outputs['diagnostic_repairs']
        self.assertFalse(self.document(refs[0])['applicable'])
        self.assertEqual('invalid', apply(self.consumer, self.future, refs)[0]['status'])

    def test_snapshot_excludes_credentials_generated_outputs_and_frozen_protocol(self):
        for relative in ('.env', '.modport/functional-contract.json', 'build/generated.java',
                         '.aws/credentials', 'src/private.key'):
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('secret or frozen')
        (self.source / 'src' / 'Binary.class').write_bytes(b'\0\xff')
        (self.source / 'src' / 'Link.java').symlink_to(self.source / 'src' / 'A.java')
        prepared = prepare(self.command)
        manifest = self.document(prepared.payload['diagnostic_repair_manifest'])
        self.assertEqual({'src/A.java', 'src/B.java'}, {item['path'] for item in manifest['sources'][0]['files']})
        self.assertEqual({'src/Binary.class', 'src/Link.java'}, {item['path'] for item in manifest['sources'][0]['skipped']})

    def test_source_baselines_and_artifacts_are_never_edited(self):
        _, refs = self.edited()
        for relative in ('baseline', 'workspaces/artifact-target'):
            workspace = self.root / relative
            (workspace / 'src').mkdir(parents=True)
            (workspace / 'src' / 'A.java').write_text('class A { wrong(); }\n')
            self.assertEqual('invalid', apply(self.consumer, workspace, refs)[0]['status'])
        contract = replace(self.consumer, payload={**self.consumer.payload, 'goal_scope': 'contract'})
        self.assertEqual('invalid', apply(contract, self.future, refs)[0]['status'])

    def test_operational_failure_rolls_back_the_whole_repair(self):
        _, refs = self.edited(('A.java', 'B.java'))
        calls = []
        def write(path, data, mode=0o600):
            calls.append(str(path))
            if len(calls) == 2:
                raise OSError('simulated write failure')
            return atomic_write(path, data, mode)
        with patch('modport.diagnostic_repairs.atomic_write', side_effect=write):
            receipts = apply(self.consumer, self.future, refs)
        self.assertEqual('invalid', receipts[0]['status'])
        self.assertEqual(3, len(calls))
        for path in (self.future / 'src').iterdir():
            self.assertIn('wrong()', path.read_text())

    def test_disabled_workflow_has_no_snapshot_or_apply_side_effect(self):
        command = replace(self.command, options={'workflow_version': 39})
        self.assertFalse(enabled(command))
        self.assertIs(command, prepare(command))
        self.assertEqual('', instructions(command))
        self.assertEqual([], apply(command, self.future, [{'path': '../outside'}]))
        self.assertFalse((self.root / 'artifacts').exists())

    def test_host_reference_cannot_escape_or_traverse_a_link(self):
        _, refs = self.edited()
        outside = self.root / 'outside.json'
        outside.write_text(json.dumps(self.document(refs[0])))
        redirected = self.root / 'artifacts' / 'diagnostic-repairs' / 'redirect.json'
        redirected.symlink_to(outside)
        for path in ('../outside.json', 'artifacts/diagnostic-repairs/redirect.json'):
            self.assertEqual('invalid', apply(self.consumer, self.future, [{**refs[0], 'path': path}])[0]['status'])


if __name__ == '__main__':
    unittest.main()
