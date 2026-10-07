import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modport.audit_storage import INLINE_LIMIT
from modport.contracts import OperationInput, OperationResult
from modport.payload_storage import (
    PayloadStorageError,
    hydrate_transport_snapshot,
    is_packed_input,
    is_packed_result,
    pack_input,
    pack_result,
    unpack_input,
    unpack_result,
    verify_operations,
)


WIRE_MARKER = "__modport_operation_payload__"
NODE_MARKER = "__modport_payload_node__"
AUDIT_MARKER = "__modport_audit_blob__"


class PayloadStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    @staticmethod
    def operation(*, command_id="command-1", payload=None, upstream=None):
        return OperationInput(
            run_id="run-1",
            task_id="source",
            stage_id="source",
            command_id=command_id,
            run_dir="/tmp/run-1",
            payload={} if payload is None else payload,
            options={"workflow_version": 18},
            upstream_results={} if upstream is None else upstream,
            artifact_refs={},
            prior_findings=(),
        ).to_dict()

    @staticmethod
    def result(*, command_id="command-1", outputs=None):
        return OperationResult(
            status="completed",
            run_id="run-1",
            task_id="source",
            stage_id="source",
            command_id=command_id,
            outputs={} if outputs is None else outputs,
            detail="done",
        ).to_dict()

    @staticmethod
    def node_references(value):
        found = []

        def visit(item):
            if isinstance(item, dict):
                if set(item) == {NODE_MARKER}:
                    found.append(item[NODE_MARKER]["blob"])
                    return
                for child in item.values():
                    visit(child)
            elif isinstance(item, list):
                for child in item:
                    visit(child)

        visit(value)
        return found

    def test_small_input_retains_legacy_shape_and_round_trips_without_blob(self):
        value = self.operation(payload={"request": "迁移"})
        original = copy.deepcopy(value)
        packed = pack_input(self.root, value)

        self.assertFalse(is_packed_input(packed))
        self.assertFalse(is_packed_result(packed))
        self.assertEqual(value, packed)
        self.assertEqual("command-1", packed["command_id"])
        self.assertEqual("/tmp/run-1", packed["run_dir"])
        self.assertEqual(value, unpack_input(self.root, packed))
        self.assertEqual(original, value)
        self.assertFalse((self.root / "audit-blobs").exists())
        self.assertNotIn(WIRE_MARKER, packed)

    def test_large_subobject_is_reused_across_commands_and_results(self):
        shared = {"body": "复" * INLINE_LIMIT, "verdict": "diagnostic"}
        first = pack_input(
            self.root,
            self.operation(command_id="command-1", upstream={"analysis": shared}),
        )
        second = pack_input(
            self.root,
            self.operation(command_id="command-2", upstream={"analysis": shared}),
        )
        outcome = pack_result(
            self.root,
            self.result(command_id="command-2", outputs={"analysis": shared}),
        )

        first_refs = self.node_references(first)
        second_refs = self.node_references(second)
        result_refs = self.node_references(outcome)
        self.assertEqual(1, len(first_refs))
        self.assertEqual(first_refs, second_refs)
        self.assertEqual(first_refs, result_refs)
        self.assertEqual(1, len(list((self.root / "audit-blobs").glob("*.json.gz"))))
        self.assertEqual(shared, unpack_input(self.root, first)["upstream_results"]["analysis"])
        self.assertEqual(shared, unpack_result(self.root, outcome)["outputs"]["analysis"])

    def test_distinct_large_children_are_stored_independently(self):
        value = self.operation(payload={
            "first": "a" * INLINE_LIMIT,
            "second": "b" * INLINE_LIMIT,
        })
        packed = pack_input(self.root, value)

        refs = self.node_references(packed)
        self.assertEqual(2, len(refs))
        self.assertNotEqual(
            refs[0][AUDIT_MARKER]["sha256"], refs[1][AUDIT_MARKER]["sha256"]
        )
        self.assertEqual(value, unpack_input(self.root, packed))

    def test_large_result_keeps_routing_identity_and_round_trips(self):
        value = self.result(outputs={"report": "z" * INLINE_LIMIT})
        original = copy.deepcopy(value)
        packed = pack_result(self.root, value)

        self.assertTrue(is_packed_result(packed))
        self.assertEqual("completed", packed["status"])
        self.assertEqual("command-1", packed["command_id"])
        self.assertEqual(value, unpack_result(self.root, packed))
        self.assertEqual(original, value)

    def test_historical_inline_values_are_copied_unchanged(self):
        legacy_input = self.operation(payload={NODE_MARKER: {"ordinary": True}})
        legacy_result = {"status": "completed", "outputs": {"old": True}}

        restored_input = unpack_input(self.root, legacy_input)
        restored_result = unpack_result(self.root, legacy_result)
        self.assertEqual(legacy_input, restored_input)
        self.assertEqual(legacy_result, restored_result)
        self.assertIsNot(legacy_input, restored_input)
        self.assertIsNot(legacy_result, restored_result)
        self.assertFalse(is_packed_input(legacy_input))
        self.assertFalse(is_packed_result(legacy_result))

    def test_new_values_cannot_collide_with_reserved_markers(self):
        value = self.operation(payload={
            NODE_MARKER: {"ordinary": True},
            "large": "x" * INLINE_LIMIT,
        })
        with self.assertRaisesRegex(PayloadStorageError, "reserved storage field"):
            pack_input(self.root, value)

    def test_wire_identity_is_bound_and_malformed_markers_fail(self):
        packed = pack_input(
            self.root, self.operation(payload={"large": "x" * INLINE_LIMIT})
        )
        changed = copy.deepcopy(packed)
        changed["command_id"] = "another-command"
        self.assertFalse(is_packed_input(changed))
        with self.assertRaisesRegex(PayloadStorageError, "wire marker"):
            unpack_input(self.root, changed)

        extra = copy.deepcopy(packed)
        extra[WIRE_MARKER]["extra"] = True
        with self.assertRaisesRegex(PayloadStorageError, "wire marker"):
            unpack_input(self.root, extra)

        bare = {WIRE_MARKER: packed[WIRE_MARKER]}
        with self.assertRaisesRegex(PayloadStorageError, "wire envelope"):
            unpack_input(self.root, bare)

    def test_missing_corrupt_and_changed_references_fail_closed(self):
        for mode in ("missing", "corrupt", "path", "size"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                packed = pack_input(
                    root, self.operation(payload={"large": mode * INLINE_LIMIT})
                )
                reference = self.node_references(packed)[0][AUDIT_MARKER]
                blob = root / reference["path"]
                if mode == "missing":
                    blob.unlink()
                elif mode == "corrupt":
                    raw = blob.read_bytes()
                    blob.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
                elif mode == "path":
                    reference["path"] = "audit-blobs/not-the-digest.json.gz"
                else:
                    reference["raw_size"] += 1
                with self.assertRaises(PayloadStorageError):
                    unpack_input(root, packed)

    def test_raw_limit_is_checked_before_contract_copy(self):
        value = self.operation(payload={"large": "x" * 2048})
        with (
            patch("modport.payload_storage.MAX_RAW_BYTES", 1024),
            patch("modport.payload_storage.json_copy",
                  side_effect=AssertionError("copy ran before size check")),
        ):
            with self.assertRaisesRegex(PayloadStorageError, "raw size limit"):
                pack_input(self.root, value)
        self.assertFalse((self.root / "audit-blobs").exists())

    def test_reference_count_and_nesting_are_bounded(self):
        packed = pack_input(self.root, self.operation(payload={
            "first": "a" * INLINE_LIMIT,
            "second": "b" * INLINE_LIMIT,
        }))
        with patch("modport.payload_storage.MAX_REFERENCE_COUNT", 1):
            with self.assertRaisesRegex(PayloadStorageError, "too many"):
                unpack_input(self.root, packed)

        nested = {"large": "x" * INLINE_LIMIT}
        for _ in range(140):
            nested = {"next": nested}
        with patch("modport.payload_storage.MAX_REFERENCE_DEPTH", 32):
            with self.assertRaisesRegex(PayloadStorageError, "nesting"):
                pack_input(self.root, self.operation(payload=nested))

    def test_verify_operations_checks_references_and_execution_identity(self):
        packed = pack_input(self.root, self.operation())
        operations = [{
            "kind": "add_task",
            "task_id": "source",
            "command": {"execution_id": "command-1", "payload": packed},
        }, {"kind": "dispatch", "task_id": "source"}]
        original = copy.deepcopy(operations)
        self.assertIsNone(verify_operations(self.root, operations))
        self.assertEqual(original, operations)

        wrong = copy.deepcopy(operations)
        wrong[0]["command"]["execution_id"] = "wrong"
        with self.assertRaisesRegex(PayloadStorageError, "execution identity"):
            verify_operations(self.root, wrong)

    def test_hydrate_transport_snapshot_restores_all_sdk_copies(self):
        operation = self.operation(payload={"large": "i" * INLINE_LIMIT})
        result = self.result(outputs={"large": "r" * INLINE_LIMIT})
        packed_input = pack_input(self.root, operation)
        packed_result = pack_result(self.root, result)
        snapshot = {
            "run_id": "run-1",
            "application_state": {"opaque": True},
            "tasks": {"source": {"attempts": [{
                "state": "succeeded",
                "command": {"execution_id": "command-1", "payload": packed_input},
                "result": {"result_id": "result-1", "value": packed_result},
                "kernel_snapshot": {
                    "command": {"execution_id": "command-1", "payload": packed_input},
                    "result": {"result_id": "result-1", "value": packed_result},
                },
            }]}},
        }
        original = copy.deepcopy(snapshot)

        hydrated = hydrate_transport_snapshot(self.root, snapshot)
        attempt = hydrated["tasks"]["source"]["attempts"][0]
        self.assertEqual(operation, attempt["command"]["payload"])
        self.assertEqual(result, attempt["result"]["value"])
        self.assertEqual(operation, attempt["kernel_snapshot"]["command"]["payload"])
        self.assertEqual(result, attempt["kernel_snapshot"]["result"]["value"])
        self.assertEqual({"opaque": True}, hydrated["application_state"])
        self.assertEqual(original, snapshot)
        self.assertIsNot(hydrated, snapshot)

    def test_hydrate_transport_snapshot_preserves_inline_task_objects(self):
        tasks = {"source": {"attempts": [{
            "command": {"payload": self.operation()},
            "result": {"value": self.result()},
            "kernel_snapshot": None,
        }]}}
        application_state = {"opaque": True}
        snapshot = {"tasks": tasks, "application_state": application_state}

        hydrated = hydrate_transport_snapshot(self.root, snapshot)
        self.assertIsNot(hydrated, snapshot)
        self.assertIs(tasks, hydrated["tasks"])
        self.assertIs(application_state, hydrated["application_state"])


if __name__ == "__main__":
    unittest.main()
