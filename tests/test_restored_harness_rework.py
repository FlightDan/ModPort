"""Explicit reviewer repair for authenticated restored harnesses."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from modport.contracts import OperationInput, OperationResult, json_copy
from modport.evidence import atomic_json, file_digest
from modport.rework_orchestration import ReviewReworkOrchestration
from modport.rework_tools import rework_targets


LEGACY_OPTIONS = {"workflow_version": 21, "business_gates_disabled": True}


def settled(command, result, *, state="succeeded"):
    row = {
        "state": state,
        "command": {"execution_id": command.command_id,
                    "payload": command.to_dict()},
    }
    if result is not None:
        row["result"] = {"value": result.to_dict()}
    return row


class RecordingHost(ReviewReworkOrchestration):
    def __init__(self):
        self.scheduled = []
        self.charges = []

    @staticmethod
    def clock():
        return 0

    @staticmethod
    def _effective_deadline(_header, _app):
        return None

    @staticmethod
    def _memory_capacity(*_args, **_kwargs):
        return True

    @staticmethod
    def _refs(header, app):
        refs = dict(header.get("initial_refs", {}))
        for result in app.get("effective", {}).values():
            refs.update(result.get("outputs", {}).get("artifact_refs", {}))
        return refs

    def _schedule(self, _snapshot, _header, app, stage, **kwargs):
        self.scheduled.append((stage, json_copy(kwargs)))
        if stage == "contract_draft":
            app["agent_assignments"] += 1
        return [{"kind": "add_task", "task_id": kwargs["task_id"]}]

    def _charge_rework(self, _header, app, family):
        self.charges.append(family)
        app["rounds"][family] = app["rounds"].get(family, 0) + 1
        return True


class RestoredHarnessReworkTests(unittest.TestCase):
    OPTIONS = LEGACY_OPTIONS

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "baseline").mkdir()
        self.source_ref = {"path": "artifacts/source.json", "sha256": "1" * 64}
        self.inherited_ref = {"path": "artifacts/inherited-harness.json",
                              "sha256": "2" * 64}
        self.contract_ref = {"path": "artifacts/restored-contract.json",
                             "sha256": "3" * 64}
        self.restore = OperationInput(
            "run", "contract_restore", "contract_restore", "restore-1",
            str(self.root), options=self.OPTIONS,
            artifact_refs={"source_evidence": self.source_ref,
                           "inherited_harness": self.inherited_ref},
        )
        self.restore_result = OperationResult(
            "completed", "run", "contract_restore", "contract_restore", "restore-1",
            outputs={"artifact_refs": {
                "baseline_harness_snapshot": self.contract_ref,
                "functional_contract_candidate": self.contract_ref,
            }},
        )
        self.snapshot = {"tasks": {"contract_restore": {
            "attempts": [settled(self.restore, self.restore_result)]}}}
        self.reviewer = OperationInput(
            "run", "contract_review", "contract_review", "review-1",
            str(self.root), options=self.OPTIONS,
            upstream_results={"contract_restore": self.restore_result.to_dict()},
            artifact_refs={"source_evidence": self.source_ref},
        )

    def test_only_completed_authenticated_restore_is_exposed(self):
        targets = rework_targets(self.snapshot, self.reviewer)
        self.assertEqual(1, len(targets))
        self.assertEqual("contract_restore", targets[0]["target_agent"])
        self.assertEqual("restore-1", targets[0]["execution_id"])
        self.assertEqual("contract_restore", targets[0]["stage"])
        self.assertEqual("contract", targets[0]["goal_scope"])
        self.assertTrue(targets[0]["restored_harness"])

        historical = replace(self.reviewer,
                             options={**self.OPTIONS, "workflow_version": 20})
        self.assertEqual([], rework_targets(self.snapshot, historical))

        failed = replace(self.restore_result, status="failed")
        failed_snapshot = {"tasks": {"contract_restore": {
            "attempts": [settled(self.restore, failed)]}}}
        failed_reviewer = replace(
            self.reviewer,
            upstream_results={"contract_restore": failed.to_dict()},
        )
        self.assertEqual([], rework_targets(failed_snapshot, failed_reviewer))

        forged = replace(
            self.reviewer,
            upstream_results={"contract_restore": {
                **self.restore_result.to_dict(), "command_id": "not-restore-1"}},
        )
        self.assertEqual([], rework_targets(self.snapshot, forged))

        unauthenticated = {"tasks": {"contract_restore": {
            "attempts": [settled(self.restore, None)]}}}
        self.assertEqual([], rework_targets(unauthenticated, self.reviewer))

    def test_explicit_request_dispatches_author_then_verify_and_freeze_once(self):
        targets = rework_targets(self.snapshot, self.reviewer)
        reviewer = replace(
            self.reviewer,
            payload={"review_rework_targets": targets},
        )
        self.snapshot["tasks"]["contract_review"] = {
            "attempts": [settled(reviewer, None, state="running")]}
        session = self.root / "artifacts/rework-tools/review-1/session.json"
        atomic_json(session, {
            "run_id": "run", "reviewer_execution_id": "review-1",
            "workspace": "baseline", "deadline_epoch": 100,
        })

        app = {
            "agent_assignments": 0,
            "rounds": {},
            "cancel_sent": [],
            "processed": [],
            "history": [],
            "effective": {"contract_restore": self.restore_result.to_dict()},
            "active_group": None,
        }
        header = {
            "run_dir": str(self.root),
            "definition": {"workflow_version": self.OPTIONS["workflow_version"]},
            "initial_refs": {"source_evidence": self.source_ref,
                             "inherited_harness": self.inherited_ref},
            "request": {"budget": {"max_agent_assignments": 10,
                                    "max_rework_rounds": 3}},
        }
        host = RecordingHost()

        # Eligibility alone never starts rework.
        self.assertEqual([], host._review_rework_decision(
            self.snapshot, header, app))
        self.assertEqual([], host.scheduled)
        self.assertEqual([], host.charges)

        atomic_json(session.parent / "requests/repair.json", {
            "request_id": "repair", "run_id": "run",
            "reviewer_execution_id": "review-1",
            "target_agent": "contract_restore",
            "instructions": "Repair the restored harness assertion without replacing its contract.",
            "response_deadline_epoch": 90,
        })
        first = host._review_rework_decision(self.snapshot, header, app)
        self.assertEqual(["contract_draft"], [stage for stage, _ in host.scheduled])
        self.assertEqual("add_task", first[0]["kind"])
        draft_args = host.scheduled[0][1]
        self.assertEqual("contract", draft_args["payload"]["goal_scope"])
        self.assertIsInstance(draft_args["payload"]["reviewer_rework"], dict)
        self.assertEqual("restore-1",
                         draft_args["payload"]["reviewer_rework"]["source_execution_id"])
        self.assertNotIn("model", draft_args["extra_options"])
        self.assertNotIn("workspace", draft_args["extra_options"])
        for alias in ("source_evidence", "inherited_harness",
                      "baseline_harness_snapshot", "functional_contract_candidate"):
            self.assertIn(alias, draft_args["artifact_overrides"])

        record = app["review_rework"]["requests"]["review-1/repair"]
        self.assertTrue(record["restored_harness_repair"])
        self.assertEqual("restore-1", record["source_execution_id"])
        self.assertEqual("contract_draft", record["target_stage"])
        self.assertEqual(["review_rework:contract_restore"], host.charges)
        self.assertEqual(1, app["agent_assignments"])

        # Repeated host ticks neither replay nor recharge the accepted request.
        self.assertEqual([], host._review_rework_decision(
            self.snapshot, header, app))
        self.assertEqual(["review_rework:contract_restore"], host.charges)
        self.assertEqual(1, app["agent_assignments"])

        draft = OperationInput(
            "run", "agent-rework.repair", "contract_draft", "draft-1",
            str(self.root), options=self.OPTIONS,
        )
        draft_result = OperationResult(
            "completed", "run", draft.task_id, draft.stage_id, draft.command_id,
            outputs={"artifact_refs": {"functional_contract_candidate": self.contract_ref}},
        )
        self.snapshot["tasks"][draft.task_id] = {
            "attempts": [settled(draft, draft_result)]}
        second = host._review_rework_decision(self.snapshot, header, app)
        self.assertEqual("contract_verify", host.scheduled[-1][0])
        self.assertEqual("agent-rework.repair.verify", second[0]["task_id"])
        self.assertEqual(self.restore_result.to_dict(),
                         app["effective"]["contract_restore"])
        self.assertEqual("draft-1", app["effective"]["contract_draft"]["command_id"])
        self.assertEqual("contract_draft", record["updates"][0]["target_agent"])

        verify = OperationInput(
            "run", "agent-rework.repair.verify", "contract_verify", "verify-1",
            str(self.root), options=self.OPTIONS,
        )
        verify_result = OperationResult(
            "completed", "run", verify.task_id, verify.stage_id, verify.command_id,
        )
        self.snapshot["tasks"][verify.task_id] = {
            "attempts": [settled(verify, verify_result)]}
        third = host._review_rework_decision(self.snapshot, header, app)
        if self.OPTIONS["workflow_version"] >= 26:
            self.assertEqual([], third)
            self.assertEqual("completed", record["state"])
            self.assertEqual(
                ["contract_draft", "contract_verify"],
                [stage for stage, _ in host.scheduled],
            )
            self.assertNotIn("contract_freeze", app["effective"])
        else:
            self.assertEqual("contract_freeze", host.scheduled[-1][0])
            self.assertEqual("agent-rework.repair.verify.freeze", third[0]["task_id"])
            self.assertEqual(
                ["contract_draft", "contract_verify", "contract_freeze"],
                [stage for stage, _ in host.scheduled],
            )
        self.assertEqual(["review_rework:contract_restore"], host.charges)
        self.assertEqual(1, app["agent_assignments"])


class CurrentRestoredHarnessReworkTests(RestoredHarnessReworkTests):
    OPTIONS = {**LEGACY_OPTIONS, "workflow_version": 25}


class V26RestoredHarnessReworkTests(RestoredHarnessReworkTests):
    OPTIONS = {**LEGACY_OPTIONS, "workflow_version": 26}

    def test_budget_finish_does_not_leave_phantom_review_task(self):
        class BudgetHost(RecordingHost):
            def _schedule(self, snapshot, header, app, stage, **kwargs):
                if stage == 'contract_review':
                    return [{'kind': 'finish', 'state': 'failed'}]
                return super()._schedule(snapshot, header, app, stage, **kwargs)

        verify = OperationInput('run', 'agent-rework.late.verify', 'contract_verify',
            'verify-late', str(self.root), options=self.OPTIONS)
        verified = OperationResult('completed', 'run', verify.task_id,
            verify.stage_id, verify.command_id)
        self.snapshot['tasks'][verify.task_id] = {'attempts': [settled(verify, verified)]}
        record = {'state': 'running', 'task_id': verify.task_id,
            'reviewer_execution_id': 'planner-1', 'reviewer_stage': 'migration_plan',
            'request_id': 'late', 'target_agent': 'contract_restore',
            'target_stage': 'contract_draft', 'followup_stage': 'contract_verify',
            'repair_diagnostics': True, 'queue_deadline_epoch': 100,
            'updates': [], 'text': ''}
        app = {'review_rework': {'requests': {'planner-1/late': record},
                                'latest_targets': {}, 'sequence': 1},
               'processed': [], 'history': [], 'cancel_sent': [],
               'effective': {'contract_freeze': {'status': 'completed',
                                                  'command_id': 'old-freeze'}},
               'active_group': None}
        header = {'run_dir': str(self.root), 'definition': {'workflow_version': 26},
                  'request': {'budget': {'max_agent_assignments': 1,
                                         'max_rework_rounds': 1}}}
        operations = BudgetHost()._review_rework_decision(self.snapshot, header, app)
        self.assertEqual([{'kind': 'finish', 'state': 'failed'}], operations)
        self.assertEqual('failed', record['state'])
        self.assertEqual(verify.task_id, record['task_id'])
        self.assertIn('budget', record['error'])

    def test_contract_lock_handoff_rejects_wrong_digest(self):
        path = self.root / 'artifacts' / 'lock.json'
        path.parent.mkdir()
        atomic_json(path, {'acceptance_status': 'unverified'})
        freeze = OperationInput('run', 'agent-rework.late.freeze', 'contract_freeze',
            'freeze-late', str(self.root), options=self.OPTIONS)
        frozen = OperationResult('completed', 'run', freeze.task_id,
            freeze.stage_id, freeze.command_id, outputs={'artifact_refs': {
                'functional_contract_lock': {'path': 'artifacts/lock.json',
                                             'sha256': '0' * 64,
                                             'media_type': 'application/json'}}})
        self.snapshot['tasks'][freeze.task_id] = {'attempts': [settled(freeze, frozen)]}
        record = {'state': 'running', 'task_id': freeze.task_id,
            'reviewer_execution_id': 'planner-1', 'reviewer_stage': 'migration_plan',
            'request_id': 'late', 'target_agent': 'contract_restore',
            'target_stage': 'contract_draft', 'followup_stage': 'contract_freeze',
            'repair_diagnostics': True, 'queue_deadline_epoch': 100,
            'updates': [], 'text': ''}
        app = {'review_rework': {'requests': {'planner-1/late': record},
                                'latest_targets': {}, 'sequence': 1},
               'processed': [], 'history': [], 'cancel_sent': [],
               'effective': {}, 'active_group': None}
        header = {'run_dir': str(self.root), 'definition': {'workflow_version': 26},
                  'request': {'budget': {'max_agent_assignments': 1,
                                         'max_rework_rounds': 1}}}
        self.assertEqual([], RecordingHost()._review_rework_decision(
            self.snapshot, header, app))
        self.assertEqual('failed', record['state'])
        self.assertIn('digest does not match', record['error'])
        self.assertNotIn('Authenticated contract lock', record['text'])

    def test_post_freeze_planner_rework_requires_fresh_review_before_freeze(self):
        planner = OperationInput("run", "migration_plan", "migration_plan", "plan-1",
            str(self.root), options=self.OPTIONS)
        verify = OperationInput("run", "agent-rework.late.verify", "contract_verify",
            "verify-late", str(self.root), options=self.OPTIONS)
        verified = OperationResult("completed", "run", verify.task_id,
            verify.stage_id, verify.command_id)
        self.snapshot["tasks"].update({
            planner.task_id: {"attempts": [settled(planner, None, state="running")]},
            verify.task_id: {"attempts": [settled(verify, verified)]},
        })
        record = {"state": "running", "task_id": verify.task_id,
            "reviewer_execution_id": planner.command_id, "reviewer_stage": planner.stage_id,
            "request_id": "late", "target_agent": "contract_restore",
            "target_stage": "contract_draft", "followup_stage": "contract_verify",
            "restored_harness_repair": True, "repair_diagnostics": True,
            "queue_deadline_epoch": 100, "updates": [], "text": ""}
        app = {"review_rework": {"requests": {"plan-1/late": record},
                                 "latest_targets": {}, "sequence": 1},
               "processed": [], "history": [], "cancel_sent": [],
               "effective": {"contract_restore": self.restore_result.to_dict(),
                             "contract_freeze": {"status": "completed",
                                                 "command_id": "old-freeze"}},
               "active_group": None}
        header = {"run_dir": str(self.root),
                  "definition": {"workflow_version": 26},
                  "request": {"budget": {"max_agent_assignments": 10,
                                          "max_rework_rounds": 3}}}
        host = RecordingHost()
        operations = host._review_rework_decision(self.snapshot, header, app)
        self.assertEqual("contract_review", host.scheduled[-1][0])
        self.assertEqual("agent-rework.late.verify.review", operations[0]["task_id"])
        self.assertEqual(100, host.scheduled[-1][1]["extra_options"]["deadline_epoch"])
        self.assertEqual("old-freeze", app["effective"]["contract_freeze"]["command_id"])

        review = OperationInput("run", operations[0]["task_id"], "contract_review",
            "review-late", str(self.root), options=self.OPTIONS)
        reviewed = OperationResult("completed", "run", review.task_id,
            review.stage_id, review.command_id, outputs={"verdict": "approved"})
        self.snapshot["tasks"][review.task_id] = {
            "attempts": [settled(review, reviewed)]}
        followup = host._review_rework_decision(self.snapshot, header, app)
        self.assertEqual("contract_freeze", host.scheduled[-1][0])
        self.assertEqual("agent-rework.late.verify.review.freeze", followup[0]["task_id"])
        self.assertEqual(100, host.scheduled[-1][1]["extra_options"]["deadline_epoch"])
        self.assertEqual("review-late", app["effective"]["contract_review"]["command_id"])
        self.assertEqual("old-freeze", app["effective"]["contract_freeze"]["command_id"])

        freeze = OperationInput("run", followup[0]["task_id"], "contract_freeze",
            "freeze-late", str(self.root), options=self.OPTIONS)
        lock_path = self.root / "artifacts" / "new-contract-lock.json"
        atomic_json(lock_path, {"acceptance_status": "unverified"})
        lock_ref = {"path": "artifacts/new-contract-lock.json",
                    "sha256": file_digest(lock_path), "media_type": "application/json"}
        frozen = OperationResult("completed", "run", freeze.task_id,
            freeze.stage_id, freeze.command_id,
            outputs={"artifact_refs": {"functional_contract_lock": lock_ref}})
        self.snapshot["tasks"][freeze.task_id] = {
            "attempts": [settled(freeze, frozen)]}
        self.assertEqual([], host._review_rework_decision(self.snapshot, header, app))
        self.assertEqual("completed", record["state"])
        self.assertEqual("freeze-late", app["effective"]["contract_freeze"]["command_id"])
        self.assertEqual(["contract_verify", "contract_review", "contract_freeze"],
                         [update["stage"] for update in record["updates"]])
        self.assertIn('"freeze_execution_id": "freeze-late"', record["text"])
        self.assertIn(lock_ref["sha256"], record["text"])


if __name__ == "__main__":
    unittest.main()
