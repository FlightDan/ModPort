"""Persistent dialogue evidence must stay inside trusted output paths."""
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest

from modport.agent_dialogue import _metadata, _safe_output_path


class DialogueEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / 'worktree'
        self.workspace.mkdir()

    def test_symlinked_log_or_plan_cannot_overwrite_another_file(self):
        victim = self.root / 'victim'
        victim.write_text('keep', encoding='utf-8')
        for relative in ('logs/agent.log', 'artifacts/plan.md'):
            with self.subTest(relative=relative):
                target = self.root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(victim)
                with self.assertRaisesRegex(ValueError, 'unsafe'):
                    _safe_output_path(target, self.workspace, 'dialogue output')
                self.assertEqual(victim.read_text(encoding='utf-8'), 'keep')
                target.unlink()

    def test_symlinked_parent_is_rejected(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (self.root / 'logs').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'unsafe'):
            _safe_output_path(self.root / 'logs' / 'agent.log', self.workspace,
                              'dialogue output')

    def test_metadata_records_session_and_exact_prompt_hashes(self):
        metadata = _metadata(
            turns=1, thread_id='ses_persisted',
            planning_log=self.root / 'plan.log',
            execution_log=self.root / 'execute.log',
            plan_path=self.root / 'plan.md',
            planning_prompt='Plan this task', execution_prompt='Execute this task',
            schema_path=None).to_dict()
        self.assertEqual(metadata['transport'], 'opencode')
        self.assertEqual(metadata['thread_id'], 'ses_persisted')
        self.assertEqual(metadata['prompt_refs']['execute']['sha256'],
                         sha256(b'Execute this task').hexdigest())


if __name__ == '__main__':
    unittest.main()
