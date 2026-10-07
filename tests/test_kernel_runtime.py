from pathlib import Path
import tempfile
import time
import unittest

from modport.contracts import OperationInput
from modport.evidence import WorkspaceLockTimeout, workspace_lock
from modport.kernel_runtime import operation_lock
from dataclasses import replace


class OperationLockTests(unittest.TestCase):
    def test_workspace_lock_wait_is_bounded_and_reports_occupancy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            observations = []
            with workspace_lock(root):
                started = time.monotonic()
                with self.assertRaisesRegex(WorkspaceLockTimeout, "workspace_wait_timeout"):
                    with workspace_lock(root, timeout_seconds=0.08, poll_seconds=0.01,
                                        on_wait=observations.append):
                        self.fail("occupied lock must not admit the worker")
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertTrue(observations)
            self.assertEqual("workspace_lock_busy", observations[0]["reason"])

    def test_v19_candidate_stages_share_one_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            locks = [operation_lock(root, replace(self.operation(root, stage=stage),
                options={'workflow_version': 19})) for stage in (
                    'mod_scan', 'codemod', 'early_compile', 'migration_inventory', 'target_build')]
            self.assertEqual({root / '.locks/scopes/worktree'}, set(locks))

    def operation(self, root, workspace=None, stage="source"):
        return OperationInput(
            run_id="run",
            task_id="task",
            stage_id=stage,
            command_id="run:task:1",
            run_dir=str(root),
            options={} if workspace is None else {"workspace": workspace},
        )

    def test_source_setup_keeps_the_run_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(operation_lock(root, self.operation(root)), root)

    def test_known_nonworkspace_stage_uses_its_output_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            operation = self.operation(root)
            operation = OperationInput(**{**operation.to_dict(), "stage_id": "project_init"})
            self.assertEqual(operation_lock(root, operation), root / ".locks/scopes/.modport/project-init")

    def test_baseline_stages_share_the_harness_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            locks = {stage: operation_lock(root, self.operation(root, stage=stage))
                     for stage in ("baseline_build", "contract_draft", "contract_verify",
                                   "contract_review", "contract_freeze", "contract_revise",
                                   "contract_diagnose", "contract_repair_plan", "contract_repair_tasks",
                                   "contract_restore", "contract_repair_review", "contract_repair_integrate")}
            self.assertEqual(set(locks.values()), {root / ".locks/scopes/baseline-harness"})

    def test_goal_planners_have_independent_locks_and_target_publish_uses_worktree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self.operation(root, stage='goal_prepare')
            second = OperationInput(**{**first.to_dict(), 'task_id': 'another'})
            self.assertNotEqual(operation_lock(root, first), operation_lock(root, second))
            self.assertEqual(operation_lock(root, self.operation(root, stage='target_repair_integrate')),
                             operation_lock(root, self.operation(root, stage='target_build')))

    def test_independent_preparation_scopes_do_not_share_run_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stages = ("project_init", "baseline_build", "skill_lookup", "mod_analysis", "gap_research")
            locks = {stage: operation_lock(root, self.operation(root, stage=stage)) for stage in stages}
            self.assertEqual(locks["project_init"], root / ".locks/scopes/.modport/project-init")
            self.assertEqual(locks["skill_lookup"], root / ".locks/scopes/artifacts/skill-lookup")
            self.assertEqual(locks["mod_analysis"], root / ".locks/scopes/.modport/mod-analysis")
            self.assertEqual(locks["gap_research"], root / ".locks/scopes/.modport/gap-research")
            self.assertNotEqual(locks["project_init"], locks["baseline_build"])
            self.assertEqual(len(set(locks.values())), 5)

    def test_isolated_workspaces_have_independent_scope_locks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = operation_lock(root, self.operation(root, "workspaces/development/g1/a"))
            second = operation_lock(root, self.operation(root, "workspaces/development/g1/b"))
            retry = operation_lock(root, self.operation(root, "workspaces/development/g1/a"))
            self.assertEqual(first, root / ".locks/scopes/workspaces/development/g1/a")
            self.assertNotEqual(first, second)
            self.assertEqual(first, retry)

    def test_isolated_workspace_must_be_contained_under_workspaces(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for workspace in ("../outside", "/tmp/outside", "baseline", "workspaces/../outside"):
                with self.subTest(workspace=workspace), self.assertRaises(ValueError):
                    operation_lock(root, self.operation(root, workspace))

    def test_waiting_review_releases_author_scope_but_rework_serializes_with_build(self):
        from dataclasses import replace
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            review = replace(self.operation(root, stage='code_review'),
                             payload={'review_rework_targets': [{'target_agent': 'coder-a'}]})
            rework = replace(self.operation(root, stage='agent_rework'),
                             payload={'reviewer_workspace': 'worktree'})
            build = self.operation(root, stage='target_build')
            self.assertNotEqual(operation_lock(root, review), operation_lock(root, rework))
            self.assertEqual(operation_lock(root, build), operation_lock(root, rework))
