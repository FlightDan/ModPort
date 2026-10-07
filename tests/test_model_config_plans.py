"""Current planning consumers use the Run's independent frozen model config."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modport.contracts import OperationInput
from modport.development import _plan, validate_plan
from modport.execution_plan import normalize_execution_plan
from modport.planning import _validate_execution_review
from modport.ungated_planning import available_execution_plan
from modport.workflow import WORKFLOW_VERSION


class ModelConfigPlanTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            'default': {'model': 'gpt-6-luna', 'reasoning_effort': 'max'},
            'roles': {'coder': {'model': 'gpt-6.1-sol', 'reasoning_effort': 'high'}},
            'stages': {},
        }
        self.report = {
            'base_commit': 'a' * 40,
            'tasks': [{'id': 'repair', 'objective': 'Repair the implementation',
                       'owned_paths': ['src/example'], 'dependencies': [],
                       'acceptance': ['Preserve the behavior'], 'complexity': 'simple'}],
        }

    def command(self, root='/tmp/run', *, payload=None, artifact_refs=None):
        return OperationInput('run', 'task', 'parallel_review', 'run:task:1', str(root),
            options={'workflow_version': WORKFLOW_VERSION, 'model_policy': self.config},
            payload=payload or {}, artifact_refs=artifact_refs or {})

    def assert_coder(self, plan, model='gpt-6.1-sol', effort='high'):
        self.assertEqual((model, effort),
                         (plan['tasks'][0]['model'], plan['tasks'][0]['reasoning_effort']))

    def test_model_config_changes_defaults_without_a_workflow_change(self):
        original = normalize_execution_plan(self.report, workflow_version=WORKFLOW_VERSION,
                                            model_policy=self.config)
        revised = copy.deepcopy(self.config)
        revised['roles']['coder'] = {'model': 'gpt-6-astra', 'reasoning_effort': 'xhigh'}
        changed = normalize_execution_plan(self.report, workflow_version=WORKFLOW_VERSION,
                                           model_policy=revised)
        self.assert_coder(original)
        self.assert_coder(changed, 'gpt-6-astra', 'xhigh')
        self.assertEqual(self.report['tasks'][0]['id'], changed['tasks'][0]['id'])

    def test_current_validate_plan_forwards_config_in_ungated_path(self):
        self.assert_coder(validate_plan(self.report, workflow_version=WORKFLOW_VERSION,
                                       model_policy=self.config))

    def test_explicit_plan_metadata_is_preserved(self):
        self.report['tasks'][0].update(model='gpt-6-luna', reasoning_effort='max')
        plan = normalize_execution_plan(self.report, workflow_version=WORKFLOW_VERSION,
                                        model_policy=self.config)
        self.assert_coder(plan, 'gpt-6-luna', 'max')
        self.assert_coder(validate_plan(plan, workflow_version=WORKFLOW_VERSION,
                                       model_policy=self.config), 'gpt-6-luna', 'max')

    def test_planning_review_uses_command_frozen_config(self):
        self.assert_coder(_validate_execution_review(self.command(), self.report))

    def test_available_authenticated_plan_uses_command_frozen_config(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            document = root / 'plan.json'
            document.write_text(json.dumps(self.report))
            command = self.command(root, artifact_refs={'development_plan': {'path': 'plan.json'}})
            with patch('modport.ungated_planning._strict_path', return_value=document), \
                 patch('modport.ungated_planning._base', return_value=self.report['base_commit']):
                self.assert_coder(available_execution_plan(command))

    def test_scheduled_development_plan_uses_command_frozen_config(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            document = root / 'plan.json'
            archived = copy.deepcopy(self.report)
            archived['tasks'][0].update(model='archived-model', reasoning_effort='low')
            document.write_text(json.dumps(archived))
            command = self.command(root, payload={
                'development_base': self.report['base_commit'],
                'execution_development_plan': self.report,
            }, artifact_refs={'development_plan': {'path': 'plan.json'}})
            with patch('modport.development._verified', return_value=document):
                self.assert_coder(_plan(command))


if __name__ == '__main__':
    unittest.main()
