"""Private settings persist through the real desktop/OpenCode environment path."""
import copy
import json
import os
import shutil
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modport.desktop_driver import restore_host_environment, save_host_environment
from modport.desktop_model_settings import (ModelSettingsStore, PROVIDER_CONFIG_ENV,
                                            provider_key_environment)
from modport.opencode_provider import managed_provider_config
from modport.opencode_runtime import OpenCodeConfig, OpenCodeServer, _managed_environment
from modport.prompt_compressor import load_model_profile


class DesktopModelSettingsTests(unittest.TestCase):
    def settings(self, *, key='fixture-private-key'):
        provider = {'id': 'my-proxy', 'name': 'My proxy', 'api_type': 'openai-compatible',
                    'base_url': 'http://127.0.0.1:12345/v1/', 'api_key': key,
                    'models': [{'id': 'my-model', 'context_window': 100_000,
                                'max_output_tokens': 8000, 'reasoning_efforts': ['high', 'max']}]}
        return {'providers': [provider], 'model_config': {
            'default': {'model': 'my-proxy/my-model', 'reasoning_effort': 'high'}}}

    @unittest.skipUnless(shutil.which('node'), 'Node.js required for renderer producer')
    def test_guided_renderer_payload_saves_two_connections_and_preserves_keys(self):
        node = shutil.which('node')
        module = Path(__file__).resolve().parents[1] / 'src/modport/desktop_static/model_setup.js'
        script = """
            const {create, apply} = require(process.argv[1]);
            const fs = require('node:fs');
            const settings = JSON.parse(fs.readFileSync(0, 'utf8'));
            const draft = create(settings.providers, settings.model_config);
            draft.separateConnection = true;
            draft.sameModel = false;
            draft.secondary.base_url = 'https://second.example.invalid/v1';
            draft.secondary.api_key = 'second-private-key';
            draft.routine.model = {id: 'routine', context_window: 32000,
                max_output_tokens: 4000, reasoning_efforts: []};
            draft.routine.reasoning_effort = 'none';
            process.stdout.write(JSON.stringify(apply(draft, settings.providers, settings.model_config)));
        """
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            store = ModelSettingsStore(directory)
            public = store.save(self.settings())
            result = subprocess.run([node, '-e', script, str(module)], input=json.dumps(public),
                                    text=True, capture_output=True, check=True, timeout=15)
            saved = store.save(json.loads(result.stdout))
            self.assertEqual(saved['model_config']['roles']['planner']['model'], 'my-proxy/my-model')
            routine = saved['model_config']['roles']['coder']
            self.assertEqual(routine['model'], 'custom1/routine')
            self.assertEqual(routine['reasoning_effort'], 'none')
            self.assertEqual(saved['model_config']['default'], routine)
            self.assertNotIn('private-key', json.dumps(saved))
            overlay = store.runtime_environment()
            self.assertEqual(overlay[provider_key_environment('my-proxy')], 'fixture-private-key')
            self.assertEqual(overlay[provider_key_environment('custom1')], 'second-private-key')
            with patch.dict(os.environ, overlay):
                providers = managed_provider_config()
            self.assertEqual(providers['custom1']['models']['routine']['limit']['context'], 32000)
            self.assertEqual(providers['custom1']['models']['routine']['variants'], {'none': {}})

    @unittest.skipUnless(shutil.which('node'), 'Node.js required for renderer producer')
    def test_fallback_renderer_payload_reaches_frozen_run_and_private_launch(self):
        from modport.desktop_service import DesktopApplication
        from modport.operations import MigrationOperations
        from modport.model_policy import resolve_model_selection
        from modport.workflow import WORKFLOW_VERSION

        module = Path(__file__).resolve().parents[1] / 'src/modport/desktop_static/fallback_setup.js'
        script = """
            const {create, apply} = require(process.argv[1]);
            const settings = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
            const draft = create(settings.providers, settings.model_config);
            draft.mode = 'shared';
            draft.provider.base_url = 'https://backup.example.invalid/v1';
            draft.provider.api_key = 'private-backup-key';
            draft.choice = {model: {id: 'backup', context_window: 32000,
                max_output_tokens: 4000, reasoning_efforts: ['none']}, reasoning_effort: 'none'};
            process.stdout.write(JSON.stringify(apply(draft, settings.providers, settings.model_config)));
        """
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            application = DesktopApplication(directory, operations=MigrationOperations(isolation_mode='thread'))
            class Supervisor:
                def launch(self, identifier, *, environment=None):
                    save_host_environment(application.state, identifier, environment)
                    return {'kind': 'test-no-process'}
            application.supervisor = Supervisor()
            body = self.settings()
            primary = body['model_config']['default']
            body['model_config']['roles'] = {'planner': copy.deepcopy(primary)}
            body['model_config']['stages'] = {'coder': copy.deepcopy(primary)}
            public = application.request('POST', '/api/model-settings', body)
            produced = subprocess.run([shutil.which('node'), '-e', script, str(module)],
                input=json.dumps(public), text=True, capture_output=True, check=True, timeout=15)
            saved = application.request('POST', '/api/model-settings', json.loads(produced.stdout))
            expected = {'model': 'custom1/backup', 'reasoning_effort': 'none'}
            self.assertNotIn('private-backup-key', json.dumps(saved))
            with patch.object(application, 'environment', return_value={'ready': True, 'checks': []}):
                result = application.create_run({'project_name': 'Fallback handoff',
                    'source_repository': 'https://github.com/example/mod', 'source_revision': 'main',
                    'source_minecraft': '1.20.1', 'target_minecraft': '1.21.1',
                    'max_seconds': 600, 'max_tokens': 10000, 'model_config': saved['model_config']})
            header_path = application.state.run_dir(result['id']) / 'run.json'
            header = json.loads(header_path.read_text())
            self.assertEqual(header['definition']['workflow_version'], WORKFLOW_VERSION)
            for stage in ('coder', 'migration_plan', 'supervisor'):
                self.assertEqual(resolve_model_selection(header['model_policy'], stage)['fallback'], expected)
            # Editing shared settings later cannot rewrite the Run or launch credentials.
            application.request('POST', '/api/model-settings', self.settings())
            self.assertEqual(json.loads(header_path.read_text())['model_policy'], header['model_policy'])
            restore_host_environment(application.state, result['id'])
            self.assertEqual(os.environ[provider_key_environment('custom1')], 'private-backup-key')
            catalog = managed_provider_config()
            self.assertEqual(catalog['custom1']['models']['backup']['limit']['context'], 32000)
            self.assertNotIn('private-backup-key', json.dumps(header))

    def test_save_reload_preserve_key_without_exposing_it_or_mutating_environment(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            store = ModelSettingsStore(directory)
            original = self.settings()
            result = store.save(original)
            self.assertNotIn('fixture-private-key', json.dumps(result))
            self.assertTrue(result['providers'][0]['api_key_configured'])
            self.assertEqual(original['providers'][0]['base_url'], 'http://127.0.0.1:12345/v1/')
            update = self.settings(key='')
            store.save(update)
            store = ModelSettingsStore(directory)
            result = store.read()
            self.assertTrue(result['providers'][0]['api_key_configured'])
            del update['providers'][0]['api_key']
            store.save(update)
            overlay = store.runtime_environment()
            self.assertEqual(overlay[provider_key_environment('my-proxy')], 'fixture-private-key')
            self.assertNotIn(PROVIDER_CONFIG_ENV, os.environ)
            self.assertNotIn('fixture-private-key', overlay[PROVIDER_CONFIG_ENV])
            if os.name != 'nt':
                self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(store.directory.stat().st_mode & 0o777, 0o700)

    def test_snapshot_restore_provider_discovery_and_context_profile_use_saved_settings(self):
        class State:
            def __init__(self, root): self.root = Path(root)
            def get(self, identity): self.asserted = identity
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,
                {'PATH': '/fixture/bin', 'UNRELATED_SECRET': 'must-not-pass'}, clear=True):
            root = Path(directory)
            store = ModelSettingsStore(root)
            store.save(self.settings())
            state = State(root)
            overlay = store.runtime_environment()
            save_host_environment(state, 'fixture-instance', overlay)
            self.assertNotIn(PROVIDER_CONFIG_ENV, os.environ)
            # Later settings changes cannot change the saved launch snapshot.
            update = self.settings(key='replacement-secret')
            update['providers'][0]['models'][0]['context_window'] = 200_000
            store.save(update)
            restore_host_environment(state, 'fixture-instance')
            config = managed_provider_config()
            self.assertEqual(config['my-proxy']['options']['apiKey'],
                             '{env:' + provider_key_environment('my-proxy') + '}')
            self.assertEqual(config['my-proxy']['models']['my-model']['limit']['context'], 100_000)
            child = _managed_environment(cwd=root, config=OpenCodeConfig(provider=config),
                                          env=None, xdg_root=root / 'managed')
            self.assertEqual(child[provider_key_environment('my-proxy')], 'fixture-private-key')
            self.assertNotIn('UNRELATED_SECRET', child)
            self.assertEqual(child['PATH'], '/fixture/bin')
            self.assertNotIn('fixture-private-key', child['OPENCODE_CONFIG_CONTENT'])
            catalog = {'all': [{'id': 'my-proxy', 'models': config['my-proxy']['models']}],
                       'connected': ['my-proxy']}
            server = object.__new__(OpenCodeServer)
            with patch.object(server, 'providers', return_value=catalog):
                reference = server.require_model(cwd=root, model='my-proxy/my-model', variant='max')
            self.assertEqual(reference['providerID'], 'my-proxy')
            profile = load_model_profile('my-proxy/my-model', catalog=catalog)
            self.assertEqual(profile.context_window, 100_000)
            self.assertEqual(profile.variants, ('high', 'max'))

    def test_missing_key_preserves_inherited_openai_credential_and_nonreasoning_variant(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,
                {'OPENAI_API_KEY': 'inherited-fixture-secret'}, clear=True):
            body = self.settings(key='')
            body['providers'][0]['id'] = 'openai'
            body['providers'][0]['api_type'] = 'openai'
            body['providers'][0]['models'][0]['reasoning_efforts'] = []
            body['model_config']['default'] = {'model': 'openai/my-model', 'reasoning_effort': 'none'}
            store = ModelSettingsStore(directory)
            public = store.save(body)
            self.assertTrue(public['providers'][0]['api_key_configured'])
            overlay = store.runtime_environment()
            self.assertNotIn('OPENAI_API_KEY', overlay)
            with patch.dict(os.environ, overlay):
                config = managed_provider_config()
            self.assertEqual(config['openai']['npm'], '@ai-sdk/openai')
            self.assertEqual(config['openai']['options']['apiKey'], '{env:OPENAI_API_KEY}')
            self.assertEqual(config['openai']['models']['my-model']['variants'], {'none': {}})
            self.assertNotIn('inherited-fixture-secret', json.dumps(public))

    def test_empty_custom_key_does_not_invent_authentication(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            store = ModelSettingsStore(directory)
            self.assertFalse(store.save(self.settings(key=''))['providers'][0]['api_key_configured'])
            with patch.dict(os.environ, store.runtime_environment()):
                provider = managed_provider_config()['my-proxy']
            self.assertNotIn('apiKey', provider['options'])

    def test_explicit_none_effort_overrides_reasoning_default(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            body = self.settings()
            body['providers'][0]['api_type'] = 'openai'
            body['providers'][0]['models'][0]['reasoning_efforts'] = ['none', 'high']
            body['model_config']['default']['reasoning_effort'] = 'none'
            store = ModelSettingsStore(directory)
            store.save(body)
            with patch.dict(os.environ, store.runtime_environment()):
                model = managed_provider_config()['my-proxy']['models']['my-model']
            self.assertTrue(model['reasoning'])
            self.assertEqual(model['variants']['none'], {'reasoningEffort': 'none'})
            self.assertEqual(model['variants']['high'], {'reasoningEffort': 'high'})

    def test_custom_responses_without_key_blocks_unrelated_openai_key_fallback(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,
                {'OPENAI_API_KEY': 'unrelated-fixture-openai-key'}, clear=True):
            body = self.settings(key='')
            body['providers'][0]['api_type'] = 'openai'
            store = ModelSettingsStore(directory)
            public = store.save(body)
            self.assertFalse(public['providers'][0]['api_key_configured'])
            with patch.dict(os.environ, store.runtime_environment()):
                provider = managed_provider_config()['my-proxy']
            self.assertEqual(provider['options']['apiKey'], '')
            self.assertNotIn('unrelated-fixture-openai-key', json.dumps(provider))

    def test_rejects_injection_duplicates_unknown_model_and_invalid_limits_before_writing(self):
        mutations = [
            lambda p: p.update(base_url='https://user:secret@provider.invalid/v1'),
            lambda p: p.update(base_url='https://provider.invalid/v1?secret=value'),
            lambda p: p.update(base_url='https://provider.invalid/{file:/etc/private}'),
            lambda p: p.update(name='{env:PRIVATE_VALUE}'),
            lambda p: p.update(id='../provider'),
            lambda p: p.update(api_key='{file:/etc/private}'),
            lambda p: p['models'][0].update(context_window=True),
            lambda p: p['models'][0].update(max_output_tokens=100001),
            lambda p: p['models'][0].update(reasoning_efforts=['high', 'high']),
            lambda p: p['models'][0].update(reasoning_efforts=['{file:/etc/private}']),
            lambda p: p['models'].append(copy.deepcopy(p['models'][0])),
        ]
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            store = ModelSettingsStore(directory)
            for mutate in mutations:
                body = self.settings()
                mutate(body['providers'][0])
                with self.subTest(mutation=mutate), self.assertRaises(ValueError): store.save(body)
                self.assertFalse(store.path.exists())
            body = self.settings()
            body['model_config']['default']['model'] = 'missing/my-model'
            with self.assertRaises(ValueError): store.save(body)
            body = self.settings()
            body['providers'].append(copy.deepcopy(body['providers'][0]))
            with self.assertRaises(ValueError): store.save(body)

    def test_settings_file_symlink_cannot_be_read_or_overwritten(self):
        if os.name == 'nt': self.skipTest('POSIX symlink setup; Windows uses native reparse checks')
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            store = ModelSettingsStore(directory)
            outside = Path(directory) / 'outside.json'
            outside.write_text('original')
            store.path.symlink_to(outside)
            with self.assertRaises((OSError, ValueError)): store.read()
            with self.assertRaises((OSError, ValueError)): store.save(self.settings())
            self.assertEqual(outside.read_text(), 'original')


if __name__ == '__main__':
    unittest.main()
