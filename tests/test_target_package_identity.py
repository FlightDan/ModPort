"""The compile/package receipt names the clean commit that produced the JAR."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from modport.contracts import OperationInput
from modport.handlers import _target_package_receipt
from modport.workflow import WORKFLOW_VERSION


class PackageIdentityTests(unittest.TestCase):
    def test_current_receipt_requires_a_clean_committed_target(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            worktree = root / 'worktree'
            worktree.mkdir()
            subprocess.run(['git', 'init', '-q', str(worktree)], check=True)
            (worktree / '.gitignore').write_text('build/\n', encoding='utf-8')
            source = worktree / 'Example.java'
            source.write_text('class Example {}\n', encoding='utf-8')
            subprocess.run(['git', '-C', str(worktree), 'add', '.gitignore', 'Example.java'], check=True)
            subprocess.run(['git', '-C', str(worktree), '-c', 'user.name=Fixture',
                            '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'target'],
                           check=True)
            head = subprocess.check_output(['git', '-C', str(worktree), 'rev-parse', 'HEAD'],
                                           text=True).strip()
            libs = worktree / 'build/libs'
            libs.mkdir(parents=True)
            (libs / 'example.jar').write_bytes(b'packaged mod')
            command = OperationInput('run', 'target_build', 'target_build', 'target-build:1',
                str(root), options={'workflow_version': WORKFLOW_VERSION,
                                    'validation_policy': {'scope': 'compile_package'}})
            receipt, ref = _target_package_receipt(command)
            self.assertEqual('passed', receipt['status'])
            self.assertEqual(head, receipt['target_commit'])
            self.assertTrue(receipt['target_clean'])
            self.assertEqual(receipt, json.loads((root / ref['path']).read_text()))

            source.write_text('class Example { int value; }\n', encoding='utf-8')
            changed, _ = _target_package_receipt(command)
            self.assertEqual('failed', changed['status'])
            self.assertFalse(changed['target_clean'])

    def test_cleanup_rework_build_excludes_only_the_preserved_untracked_review_report(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            worktree = root / 'worktree'
            worktree.mkdir()
            subprocess.run(['git', 'init', '-q', str(worktree)], check=True)
            (worktree / '.gitignore').write_text('build/\n', encoding='utf-8')
            (worktree / 'Example.java').write_text('class Example {}\n', encoding='utf-8')
            subprocess.run(['git', '-C', str(worktree), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(worktree), '-c', 'user.name=Fixture',
                            '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'target'],
                           check=True)
            libs = worktree / 'build/libs'
            libs.mkdir(parents=True)
            (libs / 'example.jar').write_bytes(b'packaged mod')
            report = worktree / '.modport/code-review.json'
            report.parent.mkdir()
            report.write_text('{"verdict":"rejected"}\n', encoding='utf-8')
            command = OperationInput('run', 'target_build', 'target_build', 'target-build:2',
                str(root), options={'workflow_version': WORKFLOW_VERSION,
                                    'validation_policy': {'scope': 'compile_package'}},
                payload={'reviewer_rework': {'request_id': 'cleanup-001'},
                         'reviewer_report_paths': ['.modport/code-review.json']})
            receipt, _ = _target_package_receipt(command)
            self.assertEqual('passed', receipt['status'])
            self.assertTrue(receipt['target_clean'])
            self.assertEqual(['.modport/code-review.json'], receipt['preserved_reviewer_reports'])

            (worktree / 'other.txt').write_text('unrelated dirty file\n', encoding='utf-8')
            dirty, _ = _target_package_receipt(command)
            self.assertEqual('failed', dirty['status'])
            self.assertFalse(dirty['target_clean'])
            (worktree / 'other.txt').unlink()

            unapproved = OperationInput('run', 'target_build', 'target_build', 'target-build:3',
                str(root), options=command.options)
            no_exclusion, _ = _target_package_receipt(unapproved)
            self.assertEqual('failed', no_exclusion['status'])
            self.assertFalse(no_exclusion['target_clean'])

            (worktree / 'Example.java').write_text('class Example { int changed; }\n',
                                                   encoding='utf-8')
            tracked_change, _ = _target_package_receipt(command)
            self.assertEqual('failed', tracked_change['status'])
            self.assertFalse(tracked_change['target_clean'])


if __name__ == '__main__':
    unittest.main()
