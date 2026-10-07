"""OpenCode preflight must fail before an SDK assignment is charged."""

from pathlib import Path
import json
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from modport.handlers import preflight_opencode_host
from modport.opencode_runtime import OpenCodeCleanupError


class HostPreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_startup_cleanup_failure_retains_safe_diagnostic(self):
        with patch('modport.opencode_agent.run_agent', side_effect=OpenCodeCleanupError({
                'target_pid': 900005, 'cleanup_confirmed': False,
                'detail': 'private provider message'})):
            with self.assertRaisesRegex(RuntimeError, 'cleanup is unconfirmed'):
                preflight_opencode_host(self.root)
        record = (self.root / 'artifacts/host-preflight/opencode-cleanup.json').read_text()
        self.assertEqual(900005, json.loads(record)['target_pid'])
        self.assertNotIn('private provider message', record)

    def test_success_records_no_tool_model_probe(self):
        completed = subprocess.CompletedProcess(
            ['opencode', 'serve'], 0,
            '{"type":"item.completed","item":{"type":"agent_message",'
            '"text":"{\\"modport_preflight\\":\\"ok\\"}"}}', '')

        def execute(**kwargs):
            self.assertEqual(kwargs['model'], 'gpt-6-luna')
            self.assertEqual(kwargs['variant'], 'max')
            self.assertTrue(kwargs['no_tools'])
            self.assertEqual(self.root, kwargs['token_budget_root'])
            kwargs['log'].parent.mkdir(parents=True, exist_ok=True)
            kwargs['log'].write_text('probe\n', encoding='utf-8')
            return completed

        with patch('modport.opencode_agent.run_agent', side_effect=execute):
            result = preflight_opencode_host(self.root)
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['tools'], 'disabled')
        self.assertEqual(json.loads((self.root / 'artifacts/host-preflight/result.json').read_text())['exit_code'], 0)

    def test_missing_provider_is_a_preflight_failure(self):
        with patch('modport.opencode_agent.run_agent',
                   side_effect=RuntimeError("OpenCode provider 'openai' is not connected")):
            with self.assertRaisesRegex(RuntimeError, 'provider.*not connected'):
                preflight_opencode_host(self.root)
        self.assertFalse((self.root / 'artifacts/host-preflight/result.json').exists())

    def test_expired_original_deadline_never_sends_a_paid_probe(self):
        (self.root / 'run.json').write_text(json.dumps({'deadline_epoch': time.time() - 1}), encoding='utf-8')
        with patch('modport.opencode_agent.run_agent') as execute:
            with self.assertRaisesRegex(TimeoutError, 'original Run deadline'):
                preflight_opencode_host(self.root)
            execute.assert_not_called()


if __name__ == '__main__':
    unittest.main()
