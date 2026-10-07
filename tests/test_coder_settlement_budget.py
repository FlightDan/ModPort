"""Coder model work and host settlement consume distinct absolute windows."""

from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from modport.development import _artifact, build_development_registry
from modport.execution_budget import (
    current_settlement_budget, execution_budget, remaining_timeout,
    publication_phase, receipt_phase, reserve_settlement, settlement_phase,
)
from modport.handlers import _result
import test_development


class Clock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


class CoderSettlementBudgetTests(unittest.TestCase):
    def setUp(self):
        fixture = test_development.IsolatedDevelopmentTests()
        fixture.workflow_version = 17
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.root = fixture.root
        command = fixture.command("coder", "a")
        self.command = replace(
            command, options={**command.options, "workflow_version": 17})
        self.handler = build_development_registry()["coder"]
        self.clock = Clock()

    def context(self):
        return SimpleNamespace(
            command=SimpleNamespace(
                execution_id=self.command.command_id,
                timeout_seconds=1000,
                payload=self.command.to_dict(),
            ),
            lease=SimpleNamespace(expires_at=1135.0),
        )

    def run_budgeted(self, fake, *, observe_collection=False):
        observations = []
        patches = [
            patch("modport.execution_budget.time.time", self.clock),
            patch("modport.development.time.time", self.clock),
            patch("modport.handlers.CodexStageHandler.__call__", fake),
        ]
        if observe_collection:
            from modport.host_candidate import collect_host_candidate

            def observed(command, *args, **kwargs):
                observations.append({
                    "at": self.clock.now,
                    "timeout": remaining_timeout(command, 10_000),
                    "deadline": command.options.get("deadline_epoch"),
                })
                return collect_host_candidate(command, *args, **kwargs)

            patches.append(patch(
                "modport.host_candidate.collect_host_candidate",
                side_effect=observed))
        entered = []
        try:
            for item in patches:
                entered.append(item)
                item.start()
            with execution_budget(self.context()):
                result = self.handler(self.command)
        finally:
            for item in reversed(entered):
                item.stop()
        return result, observations

    def test_validation_near_model_deadline_uses_reserved_capture_window(self):
        seen = {}

        def fake(handler, model_command):
            window = current_settlement_budget(model_command)
            seen["window"] = window
            seen["model_timeout"] = remaining_timeout(model_command, 10_000)
            self.assertEqual(window.model_deadline,
                             model_command.options["model_deadline_epoch"])
            self.assertNotIn("deadline_epoch", model_command.options)
            self.assertEqual(window.capture_deadline,
                             model_command.options["host_settlement_deadline_epoch"])
            workspace = self.root / model_command.options["workspace"]
            (workspace / "a.txt").write_text("near deadline completion\n")
            self.clock.now = window.model_deadline - 1
            seen["verdict"] = handler.goal_validator()
            return _result(model_command, "completed", outputs={
                "native_goal": {"producer_stopped": True}})

        result, observations = self.run_budgeted(
            fake, observe_collection=True)
        window = seen["window"]
        self.assertEqual(760, seen["model_timeout"])
        self.assertEqual(180, window.capture_reserve)
        self.assertEqual(30, window.publication_reserve)
        self.assertEqual(30, window.receipt_reserve)
        self.assertEqual(860, window.model_deadline)
        self.assertEqual(1040, window.capture_deadline)
        self.assertEqual(1070, window.publication_deadline)
        self.assertEqual(1100, window.effective_deadline)
        self.assertTrue(observations)
        self.assertTrue(all(row["deadline"] is None for row in observations))
        self.assertTrue(all(0 < row["timeout"] <= 181 for row in observations))
        self.assertEqual("completed", result.status, result.detail)
        self.assertIn("coder_patch", result.outputs["artifact_refs"])

    def test_model_timeout_still_captures_partial_files(self):
        seen = {}

        def timed_out(_handler, model_command):
            window = current_settlement_budget(model_command)
            seen["window"] = window
            workspace = self.root / model_command.options["workspace"]
            (workspace / "a.txt").write_text("partial at model timeout\n")
            self.clock.now = window.model_deadline
            return _result(
                model_command, "blocked", error_code="agent_timeout",
                detail="model timed out", outputs={
                    "native_goal": {"producer_stopped": True}})

        result, observations = self.run_budgeted(
            timed_out, observe_collection=True)
        self.assertEqual("blocked", result.status)
        self.assertEqual("agent_timeout", result.error_code)
        self.assertTrue(observations)
        self.assertTrue(all(row["timeout"] <= 180 for row in observations))
        patch_ref = result.outputs["artifact_refs"]["coder_patch"]
        self.assertIn(
            "partial at model timeout",
            (self.root / patch_ref["path"]).read_text(),
        )

    def test_patch_export_uses_publication_window_after_capture_deadline(self):
        seen = {}

        def stopped(_handler, model_command):
            seen['window'] = current_settlement_budget(model_command)
            workspace = self.root / model_command.options['workspace']
            (workspace / 'a.txt').write_text('receipt export candidate\n')
            return _result(model_command, 'blocked', error_code='agent_timeout',
                           outputs={'native_goal': {'producer_stopped': True}})

        from modport.host_candidate import export_host_candidate

        def delayed_export(*args, **kwargs):
            self.clock.now = seen['window'].capture_deadline + 1
            seen['publication_timeout'] = remaining_timeout(args[0], 10_000)
            return export_host_candidate(*args, **kwargs)

        with patch('modport.host_candidate.export_host_candidate',
                   side_effect=delayed_export):
            result, _ = self.run_budgeted(stopped)
        self.assertEqual('blocked', result.status)
        self.assertEqual(29, seen['publication_timeout'])
        self.assertIn('coder_patch', result.outputs['artifact_refs'])

    def test_patch_export_cannot_consume_sdk_receipt_tail(self):
        seen = {}
        from modport.host_candidate import export_host_candidate

        def stopped(_handler, model_command):
            seen['window'] = current_settlement_budget(model_command)
            workspace = self.root / model_command.options['workspace']
            (workspace / 'a.txt').write_text('expired receipt candidate\n')
            return _result(model_command, 'blocked', error_code='agent_timeout',
                           outputs={'native_goal': {'producer_stopped': True}})

        def expired_export(*args, **kwargs):
            self.clock.now = seen['window'].publication_deadline
            return export_host_candidate(*args, **kwargs)

        with patch('modport.host_candidate.export_host_candidate',
                   side_effect=expired_export):
            result, _ = self.run_budgeted(stopped)
        self.assertEqual('blocked', result.status)
        self.assertEqual('coder_settlement_timeout', result.error_code)
        self.assertNotIn('coder_patch', result.outputs.get('artifact_refs', {}))

    def test_nested_settlement_reservation_is_not_added_twice(self):
        with patch("modport.execution_budget.time.time", self.clock):
            with execution_budget(self.context()):
                first = reserve_settlement(self.command, 120)
                second = reserve_settlement(self.command, 120)
                smaller = reserve_settlement(self.command, 30)
                self.assertEqual(first, second)
                self.assertEqual(first, smaller)
                self.assertEqual(120, first.capture_reserve)
                self.assertEqual(30, first.publication_reserve)
                self.assertEqual(30, first.receipt_reserve)
                with settlement_phase(self.command):
                    outer = remaining_timeout(self.command, 10_000)
                    with settlement_phase(self.command):
                        inner = remaining_timeout(self.command, 10_000)
                with publication_phase(self.command):
                    publication = remaining_timeout(self.command, 10_000)
                with receipt_phase(self.command):
                    compatibility_publication = remaining_timeout(self.command, 10_000)
                self.assertEqual(940, outer)
                self.assertEqual(outer, inner)
                self.assertEqual(970, publication)
                self.assertEqual(publication, compatibility_publication)
                self.assertEqual(30, first.receipt_deadline - first.publication_deadline)

    def test_real_run_goal_validation_can_finish_after_model_deadline(self):
        from modport.prompt_compressor import PromptCompressor
        from modport.rubric import acceptance_rubric
        from test_goal_runtime import FakeOpenCode

        refs = dict(self.command.artifact_refs)
        for name in ('agent_rules', 'evidence_protocol'):
            refs[name] = _artifact(self.command, name + '.md', b'Fixture rules')
        refs['acceptance_rubric'] = _artifact(
            self.command, 'acceptance-rubric.json',
            json.dumps(acceptance_rubric()).encode())
        self.command = replace(self.command, artifact_refs=refs)
        outer = self

        class CompletingOpenCode(FakeOpenCode):
            instances = []
            sessions_by_root = {}
            behaviors = []

            def send_message(peer, *args, **kwargs):
                workspace = outer.root / outer.command.options['workspace']
                (workspace / 'a.txt').write_text('real run_goal candidate\n')
                result = super().send_message(*args, **kwargs)
                window = current_settlement_budget(outer.command)
                outer.clock.now = window.model_deadline + 1
                return result

        compressor = PromptCompressor(catalog={'models': [{
            'slug': 'gpt-5.6-luna', 'context_window': 1_000_000}]})
        with patch('modport.execution_budget.time.time', self.clock), \
                patch('modport.development.time.time', self.clock), \
                patch('modport.goal_runtime.time.time', self.clock), \
                patch('modport.goal_runtime.time.monotonic', self.clock), \
                patch('modport.input_preparation.time.time', self.clock), \
                patch('modport.input_preparation.time.monotonic', self.clock), \
                patch('modport.goal_runtime.OpenCodeServer', CompletingOpenCode), \
                patch('modport.handlers.PromptCompressor.from_environment',
                      return_value=compressor):
            with execution_budget(self.context()):
                result = self.handler(self.command)

        self.assertEqual('completed', result.status, result.detail)
        runtime = result.outputs['native_goal']
        self.assertEqual('completed_with_diagnostics', runtime['status'])
        self.assertGreater(runtime['validation_records'][0]['at'],
                           runtime['deadline_epoch'])
        self.assertLess(runtime['validation_records'][0]['at'],
                        runtime['host_deadline_epoch'])
        self.assertIn('coder_patch', result.outputs['artifact_refs'])


if __name__ == "__main__":
    unittest.main()
