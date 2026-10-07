from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dispatcher_sdk.execution_kernel import RetryPolicy

from modport.audit_storage import INLINE_LIMIT
from modport.contracts import OperationInput, OperationResult
from modport.evidence import read_json
from modport.kernel_runtime import open_runtime, reconcile_receipt
from modport.payload_storage import (
    is_packed_input,
    is_packed_result,
    pack_input,
    unpack_result,
)
from modport.storage_budget import StorageBudgetError


class Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value


class LargeResultHandler:
    __execution_kernel_revision__ = "large-result-fixture-v1"

    def __init__(self):
        self.calls = 0

    def __call__(self, operation):
        self.calls += 1
        return OperationResult(
            "completed",
            operation.run_id,
            operation.task_id,
            operation.stage_id,
            operation.command_id,
            outputs={"large": "结" * INLINE_LIMIT},
            detail="large fixture result",
        )


class KernelPayloadStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.clock = Clock()

    def operation(self):
        return OperationInput(
            "payload-run",
            "source",
            "source",
            "payload-command",
            str(self.root),
            payload={"large": "入" * INLINE_LIMIT},
        )

    def test_real_kernel_persists_wire_effect_and_execution_and_replays_effect(self):
        handler = LargeResultHandler()
        phases = []

        def admitted(_root, *, phase, **_kwargs):
            phases.append(phase)
            return {"status": "ok"}

        with patch("modport.kernel_runtime.check_storage_budget", side_effect=admitted):
            with open_runtime(
                    self.root, handlers={"modport.source": handler},
                    isolation_mode="thread", now=self.clock) as runtime:
                operation = self.operation()
                stored_input = pack_input(self.root, operation.to_dict())
                self.assertTrue(is_packed_input(stored_input))
                command = runtime.command(
                    "modport.source",
                    execution_id=operation.command_id,
                    idempotency_key=operation.command_id,
                    correlation_id=operation.run_id,
                    timeout_seconds=10,
                    retry_policy=RetryPolicy(max_attempts=2),
                    payload=stored_input,
                )
                runtime.submit(command)

                # Leave a committed Effect but no terminal execution result.
                with patch.object(
                        runtime.kernel, "complete",
                        side_effect=RuntimeError("fixture commit gap")):
                    with self.assertRaisesRegex(RuntimeError, "fixture commit gap"):
                        runtime.run_once()

                effect = runtime.kernel.get_effect(
                    "modport:" + operation.command_id
                )
                self.assertEqual("committed", effect.state)
                self.assertTrue(is_packed_result(effect.response))
                self.assertEqual(
                    "completed", effect.response["status"]
                )
                self.assertEqual(
                    "结" * INLINE_LIMIT,
                    unpack_result(self.root, effect.response)["outputs"]["large"],
                )

                receipt = read_json(
                    self.root / "artifacts/executions/payload-command/receipt.json"
                )
                frozen_input = read_json(
                    self.root / "artifacts/executions/payload-command/input.json"
                )
                self.assertFalse(is_packed_result(receipt["response"]))
                self.assertEqual(operation.to_dict(), frozen_input)

                current = runtime.kernel.get(operation.command_id)
                self.clock.value = current.lease.expires_at + 1
                runtime.reap()
                current = runtime.kernel.get(operation.command_id)
                self.clock.value = current.next_attempt_at
                result = runtime.run_once()

                self.assertEqual("succeeded", result.state)
                self.assertTrue(is_packed_result(result.result.value))
                self.assertEqual("completed", result.result.value["status"])
                self.assertEqual(
                    receipt["response"],
                    unpack_result(self.root, result.result.value),
                )
                self.assertEqual(1, handler.calls)

        self.assertEqual("kernel-open", phases[0])
        first_before = phases.index("kernel-handler:before")
        first_effect = phases.index("kernel-handler:source:effect-result")
        first_execution = phases.index("kernel-handler:source:execution-result")
        self.assertLess(first_before, first_effect)
        self.assertLess(first_effect, first_execution)
        self.assertEqual(
            2, phases.count("kernel-handler:source:execution-result")
        )

    def test_result_budget_pause_keeps_receipt_for_public_effect_recovery(self):
        handler = LargeResultHandler()

        def admitted(_root, *, phase, **_kwargs):
            if phase == "kernel-handler:source:effect-result":
                raise StorageBudgetError("fixture result budget exhausted")
            return {"status": "ok"}

        with patch("modport.kernel_runtime.check_storage_budget", side_effect=admitted):
            with open_runtime(
                    self.root, handlers={"modport.source": handler},
                    isolation_mode="thread", now=self.clock) as runtime:
                operation = self.operation()
                command = runtime.command(
                    "modport.source",
                    execution_id=operation.command_id,
                    idempotency_key=operation.command_id,
                    correlation_id=operation.run_id,
                    timeout_seconds=10,
                    payload=pack_input(self.root, operation.to_dict()),
                )
                runtime.submit(command)
                parked = runtime.run_once()
                self.assertEqual("recovery_required", parked.state)
                effect = runtime.kernel.get_effect(
                    "modport:" + operation.command_id
                )
                self.assertEqual("indeterminate", effect.state)

        receipt = read_json(
            self.root / "artifacts/executions/payload-command/receipt.json"
        )
        self.assertFalse(is_packed_result(receipt["response"]))
        self.assertEqual(1, handler.calls)
        with patch(
                "modport.kernel_runtime.check_storage_budget",
                return_value={"status": "ok"}):
            recovered = reconcile_receipt(
                self.root, command.to_dict(), effect
            )
        self.assertTrue(is_packed_result(recovered))
        self.assertEqual(receipt["response"], unpack_result(self.root, recovered))

    def test_open_runtime_checks_budget_before_storage_compatibility(self):
        calls = []

        def budget(*_args, **_kwargs):
            calls.append("budget")

        def compatible(*_args, **_kwargs):
            calls.append("compatible")

        sentinel = object()
        with (
            patch("modport.kernel_runtime.check_storage_budget", side_effect=budget),
            patch("modport.kernel_runtime.require_compatible_storage",
                  side_effect=compatible),
            patch("modport.kernel_runtime.sdk_handlers", return_value={}),
            patch("modport.kernel_runtime.Kernel.open_sqlite",
                  return_value=sentinel),
        ):
            observed = open_runtime(self.root, isolation_mode="thread")
        self.assertIs(sentinel, observed)
        self.assertEqual(["budget", "compatible"], calls)


if __name__ == "__main__":
    unittest.main()
