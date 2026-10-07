from pathlib import Path
import tempfile
import unittest

from dispatcher_sdk.orchestrator import Orchestrator

from modport.contracts import OperationInput, OperationResult
from modport.kernel_runtime import open_runtime
from modport.payload_storage import (
    hydrate_transport_snapshot,
    pack_input,
)


MIB = 1024 * 1024
NODE_MARKER = "__modport_payload_node__"
AUDIT_MARKER = "__modport_audit_blob__"


class SharedResultHandler:
    __execution_kernel_revision__ = "payload-growth-fixture-v1"

    def __call__(self, operation):
        shared = operation.upstream_results["seed"]["outputs"]["shared"]
        return OperationResult(
            "completed",
            operation.run_id,
            operation.task_id,
            operation.stage_id,
            operation.command_id,
            outputs={"shared": shared},
            detail="shared payload growth fixture",
        )


def sdk_bytes(root):
    return sum(
        (root / name).stat().st_size
        for name in (
            "kernel.sqlite3", "kernel.sqlite3-wal", "kernel.sqlite3-journal",
            "orchestrator.sqlite3", "orchestrator.sqlite3-wal",
            "orchestrator.sqlite3-journal",
        )
        if (root / name).is_file()
    )


def node_digests(value):
    found = []

    def visit(item):
        if isinstance(item, dict):
            if set(item) == {NODE_MARKER}:
                found.append(
                    item[NODE_MARKER]["blob"][AUDIT_MARKER]["sha256"]
                )
                return
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return found


class PayloadGrowthTests(unittest.TestCase):
    def test_twelve_shared_megabyte_commands_keep_both_sdk_databases_bounded(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        shared = "s" * MIB
        raw_state = None

        with open_runtime(
                root, handlers={"modport.source": SharedResultHandler()},
                isolation_mode="thread") as runtime:
            sdk = Orchestrator(
                root / "orchestrator.sqlite3", runtime.kernel, runtime=runtime
            )
            try:
                state = sdk.create_run(
                    "growth", command_id="create", input={}, definition={}
                )
                initial_bytes = sdk_bytes(root)
                for index in range(12):
                    task_id = f"source.{index:02d}"
                    execution_id = f"growth:source:{index:02d}"
                    operation = OperationInput(
                        "growth", task_id, "source", execution_id, str(root),
                        upstream_results={
                            "seed": {"outputs": {"shared": shared}}
                        },
                    )
                    command = runtime.command(
                        "modport.source",
                        execution_id=execution_id,
                        idempotency_key=execution_id,
                        correlation_id="growth",
                        timeout_seconds=10,
                        payload=pack_input(root, operation.to_dict()),
                    ).to_dict()
                    state = sdk.apply_operations(
                        "growth",
                        command_id=f"schedule:{index:02d}",
                        expected_revision=state["revision"],
                        operations=[
                            {"kind": "add_task", "task_id": task_id,
                             "command": command, "dependencies": []},
                            {"kind": "dispatch", "task_id": task_id},
                        ],
                    )
                    sdk.flush()
                    execution = runtime.run_once()
                    self.assertEqual("succeeded", execution.state)
                    sdk.sync()
                    state = sdk.get_run("growth")

                raw_state = state
                final_bytes = sdk_bytes(root)
            finally:
                sdk.close()

        growth = final_bytes - initial_bytes
        self.assertGreater(growth, 0)
        self.assertLess(
            growth, 8 * MIB,
            f"two SDK databases grew by {growth} bytes for shared payloads",
        )

        digests = []
        for task in raw_state["tasks"].values():
            digests.extend(node_digests(task["attempts"][0]))
        self.assertGreater(len(digests), 12)
        self.assertEqual(
            1, len(set(digests)),
            "the identical 1 MiB subtree must use one content-addressed node",
        )

        hydrated = hydrate_transport_snapshot(root, raw_state)
        for task in hydrated["tasks"].values():
            attempt = task["attempts"][0]
            self.assertEqual(
                shared,
                attempt["command"]["payload"]
                ["upstream_results"]["seed"]["outputs"]["shared"],
            )
            self.assertEqual(
                shared, attempt["result"]["value"]["outputs"]["shared"]
            )
            self.assertEqual(
                shared,
                attempt["kernel_snapshot"]["command"]["payload"]
                ["upstream_results"]["seed"]["outputs"]["shared"],
            )
            self.assertEqual(
                shared,
                attempt["kernel_snapshot"]["result"]["value"]
                ["outputs"]["shared"],
            )


if __name__ == "__main__":
    unittest.main()
