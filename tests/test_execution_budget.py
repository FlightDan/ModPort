"""A nested tool must settle its SDK Effect before the outer timer fires."""

from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dispatcher_sdk.execution_kernel import (
    BudgetClockUnknownError, BudgetEnvelope, ClockCheckpoint, DeadlineConstraint,
)

from modport.contracts import OperationInput, OperationResult
from modport.evidence import read_json
from modport.execution_budget import (
    SDK_LEASE_SECONDS,
    current_deadline_budget, deadline_budget, execution_budget, remaining_timeout,
)
from modport.handlers import _remaining_timeout
from modport.kernel_runtime import open_runtime
from modport.workflow import WORKFLOW_VERSION


class BudgetContext:
    def __init__(self, command, deadline_at, *, reserve_seconds=0):
        self.command = command
        self.lease = SimpleNamespace(expires_at=10_000_000)
        self.envelope = BudgetEnvelope((DeadlineConstraint(
            "sdk-execution", "execution", deadline_at, reserve_seconds),), self.sample())

    @staticmethod
    def sample():
        return ClockCheckpoint(time.time(), time.monotonic(), "modport-budget-test", "boot")

    @property
    def budget(self):
        self.envelope = self.envelope.recheckpoint(sample=self.sample())
        return self.envelope.view(sample=self.envelope.checkpoint)


class ExecutionBudgetTests(unittest.TestCase):
    @staticmethod
    def command(*, deadline=None):
        return OperationInput(
            "run", "source", "source", "run:source:1", "/tmp/modport-budget-fixture",
            options={} if deadline is None else {"deadline_epoch": deadline},
        )

    @staticmethod
    def sdk_context(*, seconds, deadline_at, payload=None, reserve_seconds=0):
        return BudgetContext(
            SimpleNamespace(
                execution_id="run:source:1", timeout_seconds=seconds,
                payload={} if payload is None else payload,
            ),
            deadline_at, reserve_seconds=reserve_seconds,
        )

    def test_nested_timeout_reserves_sixty_seconds_and_restores_context(self):
        operation = self.command()
        with patch("modport.execution_budget.time.monotonic", return_value=1000.0), \
                patch("modport.execution_budget.time.time", return_value=100.0):
            with execution_budget(self.sdk_context(seconds=7200, deadline_at=7300)):
                self.assertEqual(7140, remaining_timeout(operation, 7200))
                self.assertEqual(35, remaining_timeout(operation, 35))
            self.assertEqual(7200, remaining_timeout(operation, 7200))

    def test_nested_budget_combines_run_deadline_and_sdk_work_bound(self):
        operation = self.command(deadline=102)
        with patch("modport.execution_budget.time.monotonic", return_value=1000.0), \
                patch("modport.execution_budget.time.time", return_value=100.0):
            with execution_budget(self.sdk_context(seconds=7200, deadline_at=165)):
                with self.assertRaisesRegex(TimeoutError, "settlement"):
                    remaining_timeout(operation, 7200)
        operation = self.command()
        with patch("modport.execution_budget.time.monotonic", return_value=1000.0), \
                patch("modport.execution_budget.time.time", return_value=100.0):
            with execution_budget(self.sdk_context(seconds=120, deadline_at=220)):
                self.assertEqual(96, remaining_timeout(operation, 7200))

    def test_expired_budget_rejects_new_work_without_changing_operation(self):
        operation = self.command()
        frozen = operation.to_dict()
        with patch("modport.execution_budget.time.time", return_value=150.0), \
                patch("modport.execution_budget.time.monotonic", return_value=1000.0):
            with execution_budget(self.sdk_context(seconds=60, deadline_at=160)):
                with self.assertRaisesRegex(TimeoutError, "settlement"):
                    remaining_timeout(operation, 7200)
        self.assertEqual(frozen, operation.to_dict())

    def test_cross_operation_timeout_cannot_inherit_another_execution(self):
        other = OperationInput("run", "other", "source", "run:other:1", "/tmp/modport-budget-fixture")
        with patch("modport.execution_budget.time.monotonic", return_value=1000.0), \
                patch("modport.execution_budget.time.time", return_value=100.0):
            with execution_budget(self.sdk_context(seconds=120, deadline_at=195)):
                with self.assertRaisesRegex(ValueError, "identity"):
                    remaining_timeout(other, 30)

    def test_absolute_deadline_api_applies_reserve_once(self):
        budget = deadline_budget(
            run_deadline=180.0, stage_deadline=200.0, settlement_reserve=30.0)
        self.assertEqual(180, budget.effective_deadline)
        self.assertEqual(150, budget.work_deadline)
        self.assertEqual(40, budget.timeout(100, now=110))
        # Reusing the absolute budget does not turn the reserve into 60 seconds.
        self.assertEqual(40, budget.timeout(100, now=110))
        self.assertEqual(70, budget.remaining_settlement(now=110))

    def test_active_budget_exposes_effective_and_work_deadlines(self):
        operation = self.command(deadline=180)
        with patch("modport.execution_budget.time.time", return_value=100.0), \
                patch("modport.execution_budget.time.monotonic", return_value=1000.0):
            context = self.sdk_context(
                seconds=120, deadline_at=215,
                payload={"options": {"deadline_epoch": 180}},
            )
            with execution_budget(context):
                budget = current_deadline_budget(operation)
                self.assertEqual(180, budget.effective_deadline)
                self.assertEqual(156, budget.work_deadline)
                self.assertEqual(56, remaining_timeout(operation, 100))

    def test_nested_caller_cannot_remove_or_extend_original_run_deadline(self):
        without_deadline = self.command()
        later = self.command(deadline=300)
        with patch("modport.execution_budget.time.time", return_value=100.0), \
                patch("modport.execution_budget.time.monotonic", return_value=1000.0):
            context = self.sdk_context(
                seconds=200, deadline_at=365,
                payload={"options": {"deadline_epoch": 180}},
            )
            with execution_budget(context):
                self.assertEqual(40, remaining_timeout(without_deadline, 100))
                self.assertEqual(40, remaining_timeout(later, 100))

    def test_public_execution_cutoff_ignores_renewed_lease(self):
        operation = self.command()
        # The SDK retained the cutoff from actual handler entry at 100.
        # A later budget check and renewed lease cannot grant another timeout.
        with patch("modport.execution_budget.time.time", return_value=140.0), \
                patch("modport.execution_budget.time.monotonic", return_value=1000.0):
            with execution_budget(self.sdk_context(seconds=1000, deadline_at=1100)):
                budget = current_deadline_budget(operation)
                self.assertEqual(1100, budget.effective_deadline)
                self.assertEqual(1040, budget.work_deadline)
                self.assertEqual(900, remaining_timeout(operation, 10_000))

    def test_sdk_reserve_is_inherited_once(self):
        with patch("modport.execution_budget.time.time", return_value=100.0), \
                patch("modport.execution_budget.time.monotonic", return_value=1000.0):
            context = self.sdk_context(seconds=1000, deadline_at=1100, reserve_seconds=30)
            with execution_budget(context):
                self.assertEqual(1070, current_deadline_budget(self.command()).stage_deadline)
                self.assertEqual(910, remaining_timeout(self.command(), 10_000))

    def test_sdk_clock_floor_survives_wall_clock_rollback(self):
        with patch("modport.execution_budget.time.time", return_value=100.0), \
                patch("modport.execution_budget.time.monotonic", return_value=1000.0):
            context = self.sdk_context(seconds=1000, deadline_at=1100)
            with execution_budget(context):
                with patch("modport.execution_budget.time.time", return_value=50.0), \
                        patch("modport.execution_budget.time.monotonic", return_value=1040.0):
                    self.assertEqual(900, remaining_timeout(self.command(), 10_000))

    def test_unknown_sdk_clock_refuses_entry(self):
        context = SimpleNamespace(command=SimpleNamespace(timeout_seconds=100),
                                  budget=SimpleNamespace(clock_status="unknown", observed_at=None,
                                                         unknown_reason="entry_unconfirmed"))
        with self.assertRaisesRegex(BudgetClockUnknownError, "entry_unconfirmed"):
            with execution_budget(context):
                self.fail("unknown clock admitted workload")

    def test_runtime_lease_is_independent_of_budget_source(self):
        with tempfile.TemporaryDirectory() as directory:
            with open_runtime(Path(directory), handlers={"modport.source": lambda value: value},
                              isolation_mode="thread") as runtime:
                self.assertEqual(30, SDK_LEASE_SECONDS)
                self.assertEqual(SDK_LEASE_SECONDS, runtime.lease_seconds)


class BudgetedSubprocessHandler:
    __execution_kernel_revision__ = "budgeted-subprocess-handler-v1"

    def __call__(self, operation):
        try:
            subprocess.run(
                [sys.executable, "-c", "import time; time.sleep(3)"],
                check=True,
                timeout=_remaining_timeout(operation, 2.5),
            )
        except subprocess.TimeoutExpired:
            return OperationResult(
                "failed", operation.run_id, operation.task_id,
                operation.stage_id, operation.command_id,
                error_code="nested_subprocess_timeout",
            )
        raise AssertionError("test process unexpectedly completed")


class SDKReceiptBudgetTests(unittest.TestCase):
    def test_real_sdk_subprocess_times_out_and_settles_with_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with open_runtime(
                    root, handlers={"modport.source": BudgetedSubprocessHandler()},
                    isolation_mode="thread") as runtime:
                operation = OperationInput(
                    "run", "source", "source", "run:source:1", str(root),
                    options={"workflow_version": WORKFLOW_VERSION},
                )
                command = runtime.command(
                    "modport.source", execution_id=operation.command_id,
                    idempotency_key=operation.command_id, correlation_id="run",
                    timeout_seconds=2.5, payload=operation.to_dict(),
                )
                runtime.submit(command)
                result = runtime.run_once()
                self.assertEqual("succeeded", result.state)
                self.assertEqual("failed", result.result.value["status"])
                self.assertEqual(
                    "nested_subprocess_timeout", result.result.value["error_code"]
                )
                receipt = read_json(
                    root / "artifacts/executions/run:source:1/receipt.json"
                )
                self.assertEqual(operation.command_id, receipt["execution_id"])
                self.assertEqual(result.result.value, receipt["response"])


if __name__ == "__main__":
    unittest.main()
