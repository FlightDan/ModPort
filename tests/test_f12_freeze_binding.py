"""A fresh inherited verifier must bind the same host candidate at freeze."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from modport.contracts import OperationInput
from modport.evidence import file_digest, verified_path
from modport.handlers import BaselineContractVerificationHandler, FreezeContractHandler


class InheritedFreezeBindingTests(unittest.TestCase):
    def test_freeze_observes_selected_identity_and_detects_post_verifier_change(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            baseline = root / 'baseline'
            baseline.mkdir()
            subprocess.run(['git', 'init', '-q', str(baseline)], check=True)
            source_file = baseline / 'src/Example.java'
            source_file.parent.mkdir()
            source_file.write_text('class Example {}\n')
            subprocess.run(['git', '-C', str(baseline), 'add', 'src/Example.java'], check=True)
            subprocess.run(['git', '-C', str(baseline), '-c', 'user.name=Fixture',
                            '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'initial'],
                           check=True)
            head = subprocess.check_output(['git', '-C', str(baseline), 'rev-parse', 'HEAD'],
                                           text=True).strip()
            selected = {'contract_id': 'contract-f12', 'source_fingerprint': head,
                'behaviors': [{'id': 'behavior-f12', 'source_evidence': 'src/Example.java:1',
                    'preconditions': ['fixture exists'], 'action': ['invoke fixture'],
                    'assertions': ['fixture responds'], 'side': 'both',
                    'test_mapping': ['ExampleTest.testBehavior']}],
                'baseline_gradle_tasks': []}
            selected_path = root / 'artifacts/parent-evidence/functional-contract.json'
            selected_path.parent.mkdir(parents=True)
            selected_path.write_text(json.dumps(selected))
            candidate_path = baseline / '.modport/functional-contract.json'
            candidate_path.parent.mkdir(parents=True)
            candidate_path.write_text(json.dumps({**selected, 'description': 'reviewed source link'}))
            source_record = root / 'artifacts/source.json'
            source_record.write_text(json.dumps({'source_commit': head}))
            selected_ref = {'path': selected_path.relative_to(root).as_posix(),
                            'sha256': file_digest(selected_path)}
            manifest = root / 'artifacts/inherited-harness.json'
            manifest.write_text(json.dumps({'source_commit': head,
                'files': [{'path': '.modport/functional-contract.json', 'ref': selected_ref}]}))
            refs = {'inherited_harness': {'path': manifest.relative_to(root).as_posix(),
                                         'sha256': file_digest(manifest)},
                    'inherited_harness:.modport/functional-contract.json': selected_ref}
            verify = OperationInput('f12-binding', 'contract_verify', 'contract_verify',
                'f12-binding:contract_verify:1', str(root),
                options={'workflow_version': 25}, artifact_refs=refs)
            verified = BaselineContractVerificationHandler()(verify)
            self.assertEqual('failed', verified.status)
            self.assertEqual('preserved',
                             verified.outputs['inherited_contract_identity']['status'])
            self.assertIsNotNone(verified.outputs['verification_candidate_id'])

            freeze = OperationInput('f12-binding', 'contract_freeze', 'contract_freeze',
                'f12-binding:contract_freeze:1', str(root),
                options={'workflow_version': 25}, artifact_refs=refs,
                upstream_results={'contract_verify': verified.to_dict()})
            result = FreezeContractHandler()(freeze)
            self.assertEqual('completed', result.status, result.detail)
            self.assertEqual('unverified', result.outputs['acceptance_status'])
            binding = result.outputs['inherited_harness_binding']
            self.assertEqual('matched', binding['status'])
            self.assertEqual('failed', binding['verifier_status'])
            self.assertEqual(verified.outputs['verification_candidate_id'],
                             binding['freeze_candidate_id'])
            observation = json.loads(verified_path(root,
                result.outputs['artifact_refs']['functional_contract_lock']).read_text())
            self.assertEqual(binding, observation['inherited_harness_binding'])

            candidate_path.write_text(json.dumps({**selected,
                'description': 'changed after verification'}))
            changed = replace(freeze, command_id='f12-binding:contract_freeze:2')
            changed_result = FreezeContractHandler()(changed)
            self.assertEqual('completed', changed_result.status)
            self.assertEqual('unverified',
                             changed_result.outputs['inherited_harness_binding']['status'])
            self.assertNotEqual(binding['freeze_candidate_id'],
                changed_result.outputs['inherited_harness_binding']['freeze_candidate_id'])

            candidate_path.write_text(json.dumps({**selected,
                'description': 'reviewed source link'}))
            scope_options = {'workflow_version': 28,
                             'validation_policy': {'scope': 'compile_package'}}
            identity_command = replace(verify, command_id='v28:contract_verify:1',
                                       options=scope_options)
            identity_result = BaselineContractVerificationHandler()(identity_command)
            self.assertEqual('completed', identity_result.status)
            self.assertFalse(identity_result.outputs['process_executed'])
            self.assertEqual('preserved',
                             identity_result.outputs['inherited_contract_identity']['status'])
            scope_freeze = replace(freeze, command_id='v28:contract_freeze:1',
                                   options=scope_options,
                                   upstream_results={'contract_verify': identity_result.to_dict()})
            frozen_scope = FreezeContractHandler()(scope_freeze)
            self.assertEqual('completed', frozen_scope.status)
            self.assertEqual('matched',
                             frozen_scope.outputs['inherited_harness_binding']['status'])
            self.assertEqual('unverified', frozen_scope.outputs['acceptance_status'])

            candidate_path.write_text(json.dumps({**selected,
                'contract_id': 'different-contract'}))
            invalid_identity = BaselineContractVerificationHandler()(
                replace(identity_command, command_id='v28:contract_verify:2'))
            self.assertEqual('blocked', invalid_identity.status)
            self.assertEqual('inherited_harness_identity_invalid', invalid_identity.error_code)
            self.assertFalse(invalid_identity.outputs['process_executed'])


if __name__ == '__main__':
    unittest.main()
