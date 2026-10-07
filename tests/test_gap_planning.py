"""Bounded planner attempts and dependency-local reconciliation."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modport import handlers
from modport.contracts import OperationInput
from modport.development import _artifact
from modport.gap_planning import (dependency_closure, reconcile_task_updates,
                                 GapPlanHandler, GapPlanReviewHandler)


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tasks = [dict(id=k, dependencies=d, inputs=['input'], interfaces=['ABI'], acceptance=['behavior'])
                      for k, d in [('a', []), ('b', ['a']), ('c', [])]]

    def test_local_change_invalidates_consumers_only(self):
        tasks, invalid, preserved = reconcile_task_updates(self.tasks, [{'id': 'a', 'inputs': ['new']}], {'a': {}, 'b': {}, 'c': {}}, ['a'])
        self.assertEqual(invalid, {'a', 'b'})
        self.assertEqual(preserved, {'c'})
        self.assertEqual(self.tasks[0]['inputs'], ['input'])
        self.assertEqual(tasks[0]['inputs'], ['new'])

    def test_only_gap_blocker_changes_preserve_completed_results(self):
        _, invalid, preserved = reconcile_task_updates(self.tasks, [{'id': 'a', 'blocked_by_gaps': []}], ['a', 'b', 'c'], ['a'])
        self.assertFalse(invalid)
        self.assertEqual(preserved, {'a', 'b', 'c'})

    def test_reject_unrelated_duplicate_and_cyclic_updates(self):
        for updates in [[{'id': 'c', 'inputs': []}], [{'id': 'a'}, {'id': 'a'}], [{'id': 'a', 'dependencies': ['b']}]]:
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                reconcile_task_updates(self.tasks, updates, {}, ['a'])
        self.assertEqual(dependency_closure(self.tasks, ['a']), {'a', 'b'})


class GapPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'worktree/.modport').mkdir(parents=True)
        self.command = OperationInput('run', 'gap_plan', 'gap_plan', 'planner', str(self.root), payload={
            'unresolved_knowledge_gaps': [{'gap_id': 'stable'}],
            'development_tasks': [{'id': 'a', 'dependencies': [], 'acceptance': ['preserve']}],
            'attempted_gap_alternatives': []})
        ref = _artifact(self.command, 'evidence.json', b'{}')
        self.command = replace(self.command, artifact_refs={'evidence': ref})
        self.row = {'gap_id': 'stable', 'action': 'compatibility_layer', 'alternative_id': 'shim', 'rationale': 'preserve callback semantics', 'evidence_artifact_ids': ['evidence'], 'affected_tasks': ['a'], 'verification_requirements': [{'id': 'verify-shim', 'closure_criteria': ['old and new callback behavior agree'], 'resolution_stage': 'test_execute'}]}

    def execute(self, command, review=False, mutate=None):
        def fake(agent, cmd):
            if review:
                self.assertIn('approved_gap_resolutions (array of objects)', agent.prompt)
                self.assertIn('Raw candidate report (input evidence):', agent.prompt)
            doc = {'schema_version': 1, 'run_id': 'run', 'stage': cmd.stage_id, 'producer_execution_id': cmd.command_id}
            if review:
                doc.update(reviewer_id='gap-plan-review-agent', verdict='approved', findings=[], approved_gap_resolutions=[deepcopy(self.row)], approved_task_updates=[])
            else:
                doc.update(resolutions=[deepcopy(self.row)], task_updates=[])
            if mutate:
                mutate(doc)
            (self.root / 'worktree' / agent.required_paths[0]).write_text(json.dumps(doc))
            return handlers._result(cmd, 'completed')
        with patch('modport.handlers.CodexStageHandler.__call__', fake):
            return (GapPlanReviewHandler() if review else GapPlanHandler())(command)

    def test_plan_and_independent_approval_keep_verification_pending(self):
        plan = self.execute(self.command)
        self.assertEqual(plan.status, 'completed', plan.detail)
        command = replace(self.command, stage_id='gap_plan_review', command_id='reviewer', artifact_refs={**self.command.artifact_refs, **plan.outputs['artifact_refs']})
        review = self.execute(command, True)
        self.assertEqual(review.status, 'completed', review.detail)
        self.assertEqual(review.outputs['approved_gap_resolutions'][0]['verification_requirements'], self.row['verification_requirements'])
        self.assertNotIn('resolved', review.outputs)
        invalid = self.execute(replace(command, command_id='planner'), True)
        self.assertEqual(invalid.status, 'failed')

    def test_repeat_without_new_evidence_fails(self):
        command = replace(self.command, payload={**self.command.payload, 'attempted_gap_alternatives': [self.row]})
        plan = self.execute(command)
        self.assertEqual(plan.status, 'completed', plan.detail)
        review_command = replace(command, stage_id='gap_plan_review', command_id='reviewer',
                                 artifact_refs={**command.artifact_refs, **plan.outputs['artifact_refs']})
        result = self.execute(review_command, True)
        self.assertEqual(result.status, 'failed')
        self.assertIn('without new evidence', result.detail)

    def test_unknown_evidence_missing_obligation_and_behavior_change_fail_at_review(self):
        plan = self.execute(self.command)
        command = replace(self.command, stage_id='gap_plan_review', command_id='reviewer',
                          artifact_refs={**self.command.artifact_refs, **plan.outputs['artifact_refs']})
        for mutate in [lambda d: d['approved_gap_resolutions'][0].update(evidence_artifact_ids=['missing']),
                       lambda d: d['approved_gap_resolutions'][0].update(verification_requirements=[]),
                       lambda d: d.update(approved_task_updates=[{'id': 'a', 'acceptance': ['weaker']}])]:
            self.assertEqual(self.execute(command, True, mutate).status, 'failed')

    def test_raw_proposal_is_preserved_and_forwarded_without_schema_or_echo(self):
        raw = '## Alternative\r\nUse a shim; investigate the callback semantics first.\n{unfinished JSON'
        def proposal(agent, cmd):
            self.assertIn('No JSON schema', agent.prompt)
            (self.root / 'worktree' / agent.required_paths[0]).write_text(raw)
            return handlers._result(cmd, 'completed')
        with patch('modport.handlers.CodexStageHandler.__call__', proposal):
            plan = GapPlanHandler()(self.command)
        self.assertEqual(plan.status, 'completed', plan.detail)
        ref = plan.outputs['artifact_refs']['gap_plan']
        self.assertEqual((self.root / ref['path']).read_bytes(), raw.encode())
        command = replace(self.command, stage_id='gap_plan_review', command_id='reviewer',
                          artifact_refs={**self.command.artifact_refs, **plan.outputs['artifact_refs']})
        def review(agent, cmd):
            self.assertTrue(agent.prompt.endswith(raw))
            # The independent reviewer supplies machine decisions, no copied identity.
            decision = {'verdict': 'approved', 'findings': 'The shim needs the stated regression check.',
                        'approved_gap_resolutions': [self.row], 'approved_task_updates': []}
            (self.root / 'worktree' / agent.required_paths[0]).write_text(json.dumps(decision))
            return handlers._result(cmd, 'completed')
        with patch('modport.handlers.CodexStageHandler.__call__', review):
            result = GapPlanReviewHandler()(command)
        self.assertEqual(result.status, 'completed', result.detail)
        self.assertEqual(result.outputs['approved_gap_resolutions'], [self.row])
        self.assertEqual('The shim needs the stated regression check.', result.outputs['findings'][0]['report'])

    def test_malformed_legacy_candidate_reaches_reviewer(self):
        plan = self.execute(self.command, mutate=lambda d: d.update(schema_version=900,
            run_id='wrong', resolutions='just a proposal', producer_execution_id='reviewer'))
        self.assertEqual(plan.status, 'completed', plan.detail)
        command = replace(self.command, stage_id='gap_plan_review', command_id='reviewer',
                          artifact_refs={**self.command.artifact_refs, **plan.outputs['artifact_refs']})
        result = self.execute(command, True, lambda d: d.update(schema_version='ignored',
            reviewer_id='ignored', findings='Readable review notes'))
        self.assertEqual(result.status, 'completed', result.detail)

    def test_review_rejects_invalid_routing_and_rejected_mutations(self):
        plan = self.execute(self.command)
        command = replace(self.command, stage_id='gap_plan_review', command_id='reviewer',
                          artifact_refs={**self.command.artifact_refs, **plan.outputs['artifact_refs']})
        for mutate in [lambda d: d.update(verdict='maybe'),
                       lambda d: d.update(verdict='rejected'),
                       lambda d: d.update(approved_task_updates='not an array')]:
            with self.subTest(mutate=mutate):
                self.assertEqual(self.execute(command, True, mutate).status, 'failed')

    def test_rejected_review_emits_no_approved_changes(self):
        plan = self.execute(self.command)
        command = replace(self.command, stage_id='gap_plan_review', command_id='reviewer', artifact_refs={**self.command.artifact_refs, **plan.outputs['artifact_refs']})
        result = self.execute(command, True, lambda d: d.update(verdict='rejected', findings=[{'reason': 'unproven'}], approved_gap_resolutions=[], approved_task_updates=[]))
        self.assertEqual(result.status, 'completed', result.detail)
        self.assertFalse(result.outputs['approved_gap_resolutions'])
