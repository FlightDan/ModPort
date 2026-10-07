"""Desktop local-workspace authorization and reservation lifecycle tests."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import patch

from modport.desktop_driver import run_instance
from modport.desktop_service import DesktopApplication
from modport.desktop_state import read_json
from modport.evidence import atomic_json
from modport.workflow import WORKFLOW_VERSION


class InertOperations:
    """Record submitted Runs without starting migration work."""

    def __init__(self):
        self.submissions = []
        self.terminal_snapshot = None

    def submit(self, request, *, run_dir, run_id, model_policy=None):
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True)
        snapshot = {'state': 'running', 'revision': 0, 'generation': 0,
                    'tasks': {}, 'waits': {}, 'application_state': {}}
        atomic_json(run_dir / 'run.json', {
            'run_id': run_id,
            'run_dir': str(run_dir.resolve()),
            'started_at': time.time(),
            'request': request.to_dict(),
            'definition': {'workflow_version': WORKFLOW_VERSION},
        })
        self.submissions.append((run_id, request))
        return SimpleNamespace(run_dir=run_dir, snapshot=snapshot)

    def resume(self, run_dir, run_id):
        return SimpleNamespace(snapshot=self.terminal_snapshot)


class InertSupervisor:
    def __init__(self):
        self.launched = []

    def launch(self, instance_id, *, environment=None):
        self.launched.append(instance_id)
        return {'kind': 'test'}


class DesktopWorkspaceBackendTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    @staticmethod
    def body(token, *, mode='direct', confirmed=False):
        return {
            'project_name': 'Workspace test',
            'source_mode': 'local',
            'local_source_token': token,
            'local_workspace_mode': mode,
            'direct_workspace_confirmed': confirmed,
            'source_minecraft': '1.20.1',
            'target_minecraft': '1.21.1',
            'max_seconds': 600,
            'max_tokens': 10000,
            'model_config': {'default': {'model': 'test-model', 'reasoning_effort': 'low'}},
        }

    def application(self):
        operations = InertOperations()
        supervisor = InertSupervisor()
        application = DesktopApplication(self.root / 'app-data', operations=operations, supervisor=supervisor)
        return application, operations, supervisor

    def local_source_patches(self):
        def inspect(path, *, application_root):
            source = Path(path).resolve()
            return {'path': str(source), 'name': source.name, 'documents': {},
                    'warnings': [], 'git': {'can_branch': False}}

        def prepare(path, *, application_root, snapshot_id, mode, branch=None):
            source = Path(path).resolve()
            return {
                'source_repository': source.as_uri(),
                'source_revision': None,
                'source_snapshot': True,
                'workspace': {'mode': mode, 'path': str(source), 'original_path': str(source)},
                'file_count': 1,
                'excluded_count': 0,
                'warnings': [],
            }

        return (patch('modport.desktop_service.inspect_local_source', side_effect=inspect),
                patch('modport.desktop_service.prepare_local_workspace', side_effect=prepare))

    def test_direct_development_requires_explicit_acknowledgement(self):
        application, operations, supervisor = self.application()
        source = self.root / 'source'
        source.mkdir()
        application._local_sources['selected'] = {'path': str(source)}
        inspect_patch, prepare_patch = self.local_source_patches()
        with inspect_patch, prepare_patch, patch.object(application, 'environment', return_value={'ready': True, 'checks': []}):
            with self.assertRaisesRegex(ValueError, '确认直接修改'):
                application.create_run(self.body('selected'))
            self.assertEqual(operations.submissions, [])

            result = application.create_run(self.body('selected', confirmed=True))

        request = operations.submissions[0][1]
        self.assertEqual(request.local_workspace['mode'], 'direct')
        self.assertEqual(request.local_workspace['path'], str(source.resolve()))
        self.assertEqual(supervisor.launched, [result['id']])
        self.assertEqual(application.state.status(result['id'])['workspace']['mode'], 'direct')
        source.rmdir()
        self.assertEqual(application.state.status(result['id'])['workspace']['path'], str(source))

    def test_remote_mode_rejects_all_local_workspace_parameters(self):
        application, _, _ = self.application()
        base = {'project_name': 'Remote test', 'source_mode': 'remote'}
        for name, value in [
            ('local_source_token', 'token'),
            ('local_workspace_mode', 'direct'),
            ('local_branch_name', 'topic'),
            ('direct_workspace_confirmed', True),
        ]:
            with self.subTest(field=name), self.assertRaisesRegex(ValueError, '不接受本地目录参数'):
                application.create_run({**base, name: value})

    def test_overlapping_direct_workspaces_are_reserved_until_terminal_driver_settlement(self):
        application, operations, _ = self.application()
        outer = self.root / 'source'
        inner = outer / 'nested'
        inner.mkdir(parents=True)
        application._local_sources.update({
            'outer': {'path': str(outer)},
            'inner': {'path': str(inner)},
        })
        inspect_patch, prepare_patch = self.local_source_patches()
        with inspect_patch, prepare_patch, patch.object(application, 'environment', return_value={'ready': True, 'checks': []}):
            first = application.create_run(self.body('outer', confirmed=True))
            first_id = first['id']
            with self.assertRaisesRegex(ValueError, '开发区重叠'):
                application.create_run(self.body('inner', confirmed=True))
            self.assertEqual(len(operations.submissions), 1)

            first_run_dir = application.state.run_dir(first_id)
            header = read_json(first_run_dir / 'run.json')
            operations.terminal_snapshot = {
                'state': 'succeeded', 'revision': 1, 'generation': 0,
                'tasks': {}, 'waits': {}, 'application_state': {},
            }
            with patch('modport.desktop_driver.restore_host_environment'), \
                    patch('modport.desktop_driver.read_run_availability', return_value=SimpleNamespace(run_state='running', run_revision=0)), \
                    patch('modport.sdk_compat.inspect_runtime', return_value={}), \
                    patch('modport.sdk_compat.require_compatible_storage'), \
                    patch('modport.platform_runtime.process_birth', return_value='test-birth'):
                self.assertEqual(run_instance(application.state.root, first_id, operations=operations), 0)

            self.assertEqual(application.state.status(first_id)['workspace']['path'], str(outer.resolve()))
            second = application.create_run(self.body('inner', confirmed=True))

        self.assertNotEqual(first_id, second['id'])
        self.assertEqual(len(operations.submissions), 2)
        self.assertEqual(header['request']['local_workspace']['path'], str(outer.resolve()))
        self.assertEqual(application.state.status(second['id'])['workspace']['path'], str(inner.resolve()))


if __name__ == '__main__':
    unittest.main()
