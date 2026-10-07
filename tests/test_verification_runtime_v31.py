"""Current producer declarations reach verification despite an optional cache miss."""
from copy import deepcopy
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from modport.author_contracts import characterization_evidence_schema
from modport.contracts import OperationInput
from modport.evidence import atomic_json, file_digest
from modport.handlers import BaselineContractVerificationHandler, _test_evidence_declarations
from modport.opencode_shell_mcp import _dynamic_contract_session
from modport.rubric import acceptance_rubric
from modport.test_selection_execution import build_selected_test_execution


def current_contract():
    contract = deepcopy(characterization_evidence_schema(workflow_version=31)['examples']['contract'])
    rubric = acceptance_rubric()
    contract.update(contract_id='current-runtime', source_fingerprint='source',
                    rubric_id=rubric['rubric_id'], rubric_version=rubric['rubric_version'])
    contract['behaviors'][0]['side'] = 'client'
    contract['test_evidence']['example.test']['result_identity']['gradle_task'] = ':test'
    return contract, rubric


class CurrentVerificationRuntimeTests(unittest.TestCase):
    def test_author_task_alias_reaches_deterministic_and_selected_tool_consumers(self):
        contract, rubric = current_contract()
        declarations = _test_evidence_declarations(contract, rubric,
            workflow_version=31, gradle_tasks=contract['baseline_gradle_tasks'])
        selection = build_selected_test_execution(contract, list(declarations))
        self.assertEqual(('example.test',), selection.test_ids)
        self.assertEqual((':test',), selection.gradle_tasks)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            atomic_json(root / '.modport/functional-contract.json', contract)
            session = _dynamic_contract_session({'dynamic_contract': True,
                'root': str(root), 'contract_path': '.modport/functional-contract.json'},
                ['example.test'], 'selected')
            self.assertEqual(['example.test'], list(session['test_cases']))

    def test_task_alias_does_not_allow_launch_tasks_or_duplicate_case_identities(self):
        contract, rubric = current_contract()
        contract['test_evidence']['example.test']['result_identity']['gradle_task'] = ':runClient'
        with self.assertRaisesRegex(ValueError, 'unsafe JUnit'):
            _test_evidence_declarations(contract, rubric, workflow_version=31,
                gradle_tasks=['runClient'])
        contract, rubric = current_contract()
        second = deepcopy(contract['test_evidence']['example.test'])
        second['path'] = '.modport/evidence/second.json'
        second['result_identity']['gradle_task'] = 'test'
        contract['test_evidence']['second'] = second
        contract['baseline_evidence_files'].append(second['path'])
        contract['behaviors'][0]['test_mapping'].append('second')
        with self.assertRaisesRegex(ValueError, 'unique across test IDs'):
            _test_evidence_declarations(contract, rubric, workflow_version=31,
                gradle_tasks=['test'])

    def test_unavailable_asset_seed_runs_client_harness_and_preserves_its_failure(self):
        self._assert_cache_skip_reaches_client_harness('client')

    def test_mixed_side_junit_mapping_also_launches_the_client(self):
        self._assert_cache_skip_reaches_client_harness('both')

    def _assert_cache_skip_reaches_client_harness(self, side):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            baseline = root / 'baseline'
            baseline.mkdir()
            source = baseline / 'src/main/java/Example.java'
            source.parent.mkdir(parents=True)
            source.write_text('\n' * 41 + 'class Example {}\n')
            subprocess.run(['git', 'init', '-q', str(baseline)], check=True)
            subprocess.run(['git', '-C', str(baseline), 'add', 'src'], check=True)
            subprocess.run(['git', '-C', str(baseline), '-c', 'user.name=Test',
                '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'source'], check=True)
            commit = subprocess.check_output(['git', '-C', str(baseline),
                'rev-parse', 'HEAD'], text=True).strip()
            contract, rubric = current_contract()
            contract['behaviors'][0]['side'] = side
            contract['source_fingerprint'] = commit
            atomic_json(baseline / '.modport/functional-contract.json', contract)
            harness = baseline / '.modport/harness/ExampleTest.java'
            harness.parent.mkdir(parents=True, exist_ok=True)
            harness.write_text('class ExampleTest { void returnsExpectedValue() {} }\n')
            atomic_json(root / 'artifacts/source.json', {'source_commit': commit})
            rubric_path = root / 'artifacts/acceptance-rubric.json'
            atomic_json(rubric_path, rubric)
            command = OperationInput('run', 'verify', 'contract_verify', 'run:verify:1',
                str(root), options={'workflow_version': 31, 'business_gates_disabled': True,
                'deadline_epoch': time.time() + 600}, artifact_refs={'acceptance_rubric':
                {'path': rubric_path.relative_to(root).as_posix(), 'sha256': file_digest(rubric_path)}})
            events = []

            class UnavailableCache:
                def seed(self, **kwargs):
                    events.append('seed')
                    raise ValueError('private Minecraft version manifest failed launcher SHA-1')

                def harvest(self, **kwargs):
                    raise AssertionError('a rejected optional seed must not be retried')

            def execute(args, *, cwd, log, timeout):
                label = args[0]
                events.append(label)
                output = 'BUILD SUCCESSFUL\n' if label == 'dry' else 'real harness compile failure\n'
                log.parent.mkdir(parents=True, exist_ok=True)
                log.write_text(output)
                return subprocess.CompletedProcess(args, 0 if label == 'dry' else 1, output)

            with patch('modport.handlers._forge_baseline_init', return_value='baseline.init.gradle'), \
                 patch('modport.handlers._sandboxed_build_command',
                       side_effect=lambda root, workspace, args, **kw:
                       ['dry'] if '--dry-run' in args else ['full']), \
                 patch('modport.handlers._contract_asset_cache_context',
                       return_value=(UnavailableCache(), {})), \
                 patch('modport.handlers._client_launch_arguments',
                       side_effect=lambda root, command, args, **kw: args) as client_launcher, \
                 patch('modport.handlers._exec', side_effect=execute):
                result = BaselineContractVerificationHandler(require_client_evidence=True)(command)
            self.assertEqual(['dry', 'seed', 'full'], events)
            self.assertEqual(1, client_launcher.call_count)
            self.assertEqual('baseline_contract_failed', result.error_code, result.detail)
            self.assertEqual('skipped', result.outputs['asset_cache']['state'])
            self.assertEqual('unverified', result.outputs['acceptance_status'])
            self.assertNotIn('no executable client startup mapping was observed',
                result.outputs.get('business_diagnostics', []))


if __name__ == '__main__':
    unittest.main()
