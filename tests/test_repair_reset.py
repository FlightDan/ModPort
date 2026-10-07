"""Regression tests for contract repair-tail projection invalidation."""
import copy
import unittest

from modport.repair_reset import invalidate_contract_tail


class ContractRepairResetTests(unittest.TestCase):
    @staticmethod
    def result(stage, marker):
        return {"stage_id": stage, "status": "completed", "marker": marker}

    def application(self):
        return {
            "effective": {
                "contract_diagnose": self.result("contract_diagnose", "diagnosis"),
                "contract_repair_plan": self.result("contract_repair_plan", "plan"),
                "contract_repair_review": self.result("contract_repair_review", "review"),
                "review.scope-001": self.result("contract_repair_review", "scoped review"),
                "contract_repair_integrate": self.result(
                    "contract_repair_integrate", "integration"),
                "integration.scope-001": self.result(
                    "contract_repair_integrate", "scoped integration"),
                "migration_inventory": self.result("migration_inventory", "downstream"),
                "inventory.scope-001": self.result("migration_inventory", "scoped downstream"),
                "gap_research": self.result("gap_research", "unrelated"),
                "target_repair_review": self.result("target_repair_review", "target repair"),
            },
            "early_failures": {
                "contract_repair_review": "review failure",
                "contract_repair_review.scope-002": "scoped review failure",
                "contract_repair_integrate.scope-002": "scoped integration failure",
                "migration_inventory.scope-002": "downstream failure",
                "gap_research": "unrelated failure",
            },
            "format_retries": {
                "contract_repair_review": 1,
                "contract_repair_review.scope-002": 2,
                "contract_repair_integrate.scope-002": 1,
                "migration_inventory.scope-002": 2,
                "gap_research": 1,
            },
            "history": [{"stage": "contract_repair_review", "execution_id": "old:review:1"}],
            "repair_history": [{"failure_execution_id": "old:verify:1"}],
            "diagnostic_history": {"old:verify:1": {"detail": "retain evidence"}},
            "rounds": {"contract_revise": 3, "review_rework:migration_tasks": 2},
            "agent_assignments": 17,
            "research_budget": {"platform": {"limit": 2, "dispatched": 2}},
        }

    def test_invalidates_exact_and_scoped_review_integration_and_downstream_projections(self):
        app = self.application()
        retained = copy.deepcopy({key: app[key] for key in (
            "history", "repair_history", "diagnostic_history", "rounds",
            "agent_assignments", "research_budget")})

        removed = invalidate_contract_tail(app)

        self.assertEqual(removed, sorted([
            "contract_repair_integrate", "contract_repair_plan", "contract_repair_review",
            "integration.scope-001", "inventory.scope-001", "migration_inventory",
            "review.scope-001",
        ]))
        self.assertEqual(set(app["effective"]), {
            "contract_diagnose", "gap_research", "target_repair_review"})
        self.assertEqual(app["early_failures"], {"gap_research": "unrelated failure"})
        self.assertEqual(app["format_retries"], {"gap_research": 1})
        for key, value in retained.items():
            self.assertEqual(app[key], value, key)

    def test_diagnosis_is_retained_by_default_and_optionally_invalidated(self):
        default = self.application()
        invalidate_contract_tail(default)
        self.assertIn("contract_diagnose", default["effective"])

        reset = self.application()
        reset["early_failures"]["contract_diagnose"] = "diagnostic failure"
        reset["format_retries"]["contract_diagnose.scope-001"] = 2
        removed = invalidate_contract_tail(reset, include_diagnosis=True)
        self.assertIn("contract_diagnose", removed)
        self.assertNotIn("contract_diagnose", reset["effective"])
        self.assertNotIn("contract_diagnose", reset["early_failures"])
        self.assertNotIn("contract_diagnose.scope-001", reset["format_retries"])


if __name__ == "__main__":
    unittest.main()
