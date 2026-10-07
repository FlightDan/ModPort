from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from modport.cli import main
from modport.model_policy import load_model_config


class ModelConfigCliTests(unittest.TestCase):
    def run_args(self, root, *extra):
        return ['run', '--mod-id', 'example', '--source-repository', 'https://example.invalid/mod.git',
                '--source-revision', 'fixed-source', '--source-minecraft', '1.20.1',
                '--target-minecraft', '26.1.2', '--output-root', str(root), *extra]

    def continuation_args(self, root, *extra):
        return ['continue', '--run-dir', str(root), '--run-id', 'original',
                '--next-run-id', 'next', '--reason', 'approved model settings', *extra]

    def test_set_and_show_use_current_directory_file_and_keep_other_roles(self):
        with TemporaryDirectory() as root:
            previous = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(StringIO()):
                    self.assertEqual(0, main(['models', 'set', '--role', 'planner',
                        '--model', 'gpt-6.1-sol', '--reasoning-effort', 'high']))
                stored = json.loads(Path('modport-models.json').read_text())
                self.assertEqual({'model': 'gpt-6.1-sol', 'reasoning_effort': 'high'}, stored['roles']['planner'])
                self.assertEqual('gpt-6.1-sol', stored['roles']['coder']['model'])
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(0, main(['models', 'show']))
                self.assertEqual(stored, json.loads(output.getvalue()))
            finally:
                os.chdir(previous)

    def test_explicit_file_updates_default(self):
        with TemporaryDirectory() as root:
            path = Path(root) / 'models.json'
            with redirect_stdout(StringIO()):
                self.assertEqual(0, main(['models', 'set', '--config', str(path), '--role', 'default',
                    '--model', 'gpt-6.1-sol', '--reasoning-effort', 'high']))
            self.assertEqual({'model': 'gpt-6.1-sol', 'reasoning_effort': 'high'}, load_model_config(path)['default'])

    def test_subagent_override_is_saved_without_changing_coder(self):
        with TemporaryDirectory() as root:
            path = Path(root) / 'models.json'
            with redirect_stdout(StringIO()):
                self.assertEqual(0, main(['models', 'set', '--config', str(path), '--role', 'subagent',
                    '--model', 'gpt-6-luna', '--reasoning-effort', 'max']))
            config = load_model_config(path)
            self.assertEqual('gpt-6-luna', config['roles']['subagent']['model'])
            self.assertEqual('gpt-6.1-sol', config['roles']['coder']['model'])

    def test_run_passes_explicit_configuration_to_run_and_handoff_submit(self):
        with TemporaryDirectory() as root:
            path = Path(root) / 'models.json'
            config = load_model_config()
            config['roles']['planner'] = {'model': 'gpt-6.1-sol', 'reasoning_effort': 'high'}
            path.write_text(json.dumps(config))
            run = SimpleNamespace(run_id='next', run_dir=Path(root), status='succeeded', snapshot={})
            for extra, method in (([], 'run'), (['--handoff', '/handoff'], 'submit')):
                with self.subTest(method=method), patch('modport.cli.MigrationOperations') as factory, redirect_stdout(StringIO()):
                    factory.return_value.execute.return_value = run
                    factory.return_value.run.return_value = run
                    self.assertEqual(0, main(self.run_args(root, '--model-config', str(path), *extra)))
                self.assertEqual(load_model_config(path), getattr(factory.return_value, method).call_args.kwargs['model_policy'])

    def test_continue_model_change_does_not_require_workflow_upgrade(self):
        with TemporaryDirectory() as root:
            path = Path(root) / 'models.json'
            config = load_model_config()
            config['roles']['planner'] = {'model': 'gpt-6-astra', 'reasoning_effort': 'high'}
            path.write_text(json.dumps(config))
            run = SimpleNamespace(run_id='next', run_dir=Path(root), status='succeeded', snapshot={})
            with patch('modport.cli.MigrationOperations') as factory, \
                    patch('modport.continuation.continue_from_planner', return_value=run) as continuation, \
                    redirect_stdout(StringIO()):
                factory.return_value.execute.return_value = run
                self.assertEqual(0, main(self.continuation_args(root, '--model-config', str(path),
                    '--additional-agent-assignments', '20')))
            self.assertEqual(load_model_config(path), continuation.call_args.kwargs['model_policy'])
            self.assertFalse(continuation.call_args.kwargs['upgrade_workflow'])
            self.assertEqual(20, continuation.call_args.kwargs['additional_agent_assignments'])
            self.assertNotIn('additional_seconds', continuation.call_args.kwargs)

    def test_continue_without_config_keeps_frozen_models_even_when_upgrading(self):
        with TemporaryDirectory() as root:
            run = SimpleNamespace(run_id='next', run_dir=Path(root), status='succeeded', snapshot={})
            with patch('modport.cli.MigrationOperations') as factory, \
                    patch('modport.continuation.continue_from_planner', return_value=run) as continuation, \
                    redirect_stdout(StringIO()):
                factory.return_value.execute.return_value = run
                self.assertEqual(0, main(self.continuation_args(root, '--upgrade-workflow')))
            self.assertNotIn('model_policy', continuation.call_args.kwargs)

    def test_invalid_configuration_reaches_no_run_writer(self):
        with TemporaryDirectory() as root:
            path = Path(root) / 'models.json'
            path.write_text('{invalid-json')
            for args in (self.run_args(root, '--model-config', str(path)),
                         self.continuation_args(root, '--model-config', str(path))):
                with self.subTest(command=args[0]), patch('modport.cli.MigrationOperations') as factory, \
                        patch('modport.continuation.continue_from_planner') as continuation, \
                        redirect_stderr(StringIO()):
                    self.assertNotEqual(0, main(args))
                factory.return_value.submit.assert_not_called()
                factory.return_value.run.assert_not_called()
                factory.return_value.execute.assert_not_called()
                continuation.assert_not_called()
