"""Host Gradle Test observations cannot be replayed or mapped to unrelated XML."""
import json
from pathlib import Path
import tempfile
import unittest

from modport.regression_evidence import install_gradle_test_listener, validate_gradle_test_execution


class GradleExecutionEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.nonce = 'a' * 64
        self.script, self.report = install_gradle_test_listener(self.root, self.nonce)
        self.check = {'tasks': [':test'], 'reports': ['build/test-results/test/TEST-Behavior.xml']}
        self.record = {'nonce': self.nonce, 'tasks': {':test': {'executed': True, 'tests': 1,
                       'failures': 0, 'skipped': 0, 'report_directory': '/workspace/build/test-results/test'}}}

    def validate(self):
        return validate_gradle_test_execution(self.root, self.check, self.report, self.nonce)

    def test_execution_record_must_be_present_fresh_and_nonce_bound(self):
        with self.assertRaises(ValueError):
            self.validate()
        path = self.root / self.report
        path.write_text(json.dumps(self.record))
        self.assertEqual(self.validate()['tasks'][':test']['reports'], self.check['reports'])
        self.record['nonce'] = 'b' * 64
        path.write_text(json.dumps(self.record))
        with self.assertRaisesRegex(ValueError, 'nonce'):
            self.validate()
        with self.assertRaisesRegex(ValueError, 'fresh'):
            install_gradle_test_listener(self.root, self.nonce)

    def test_execution_record_symlink_is_rejected(self):
        original = self.root / 'other.json'
        original.write_text(json.dumps(self.record))
        (self.root / self.report).symlink_to(original)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.validate()

    def test_each_task_needs_distinct_report_output(self):
        self.record['tasks'][':otherTest'] = self.record['tasks'][':test']
        self.check['tasks'].append(':otherTest')
        (self.root / self.report).write_text(json.dumps(self.record))
        with self.assertRaisesRegex(ValueError, 'distinct'):
            self.validate()
