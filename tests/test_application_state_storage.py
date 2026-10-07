import concurrent.futures
import copy
import json
from pathlib import Path
import sys
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch

from modport.application_state_storage import (
    ApplicationStateStorageError,
    hydrate_run_snapshot,
    is_packed_application_state,
    pack_application_state,
    unpack_application_state,
)
from modport.audit_storage import INLINE_LIMIT, load_data, store_data
from modport.contracts import json_copy


ROOT_MARKER = "__modport_application_state__"
FIELD_INDEX = "__modport_application_state_fields__"


class ApplicationStateStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    @staticmethod
    def _field_refs(packed):
        return packed[FIELD_INDEX]["refs"]

    def test_inline_legacy_state_round_trips_without_storage_marker(self):
        state = {
            "active_stage": "source",
            "effective": {"source": {"status": "completed"}},
            "history": ["完整记录"],
        }
        original = copy.deepcopy(state)
        packed = pack_application_state(self.root, state)
        self.assertEqual(state, packed)
        self.assertEqual(json_copy(state), unpack_application_state(self.root, packed))
        self.assertEqual(original, state)
        self.assertFalse((self.root / "audit-blobs").exists())

    def test_large_fields_are_independent_and_exactly_restored(self):
        state = {
            "effective": {"body": "甲" * INLINE_LIMIT},
            "repair_feedback": [{"detail": "b" * INLINE_LIMIT}],
            "active_stage": "review",
            "counter": 7,
        }
        packed = pack_application_state(self.root, state)
        self.assertNotIn(ROOT_MARKER, packed)
        self.assertEqual({"effective", "repair_feedback"},
                         set(self._field_refs(packed)))
        self.assertNotIn("effective", packed)
        self.assertNotIn("repair_feedback", packed)
        self.assertEqual(json_copy(state), unpack_application_state(self.root, packed))
        self.assertEqual(2, len(list((self.root / "audit-blobs").glob("*.json.gz"))))

    def test_changed_field_adds_one_blob_and_reuses_unchanged_field(self):
        first = {
            "effective": {"body": "a" * INLINE_LIMIT},
            "repair_feedback": [{"detail": "b" * INLINE_LIMIT}],
            "sequence": 1,
        }
        second = copy.deepcopy(first)
        second["effective"]["body"] = "c" * INLINE_LIMIT
        second["sequence"] = 2
        packed_first = pack_application_state(self.root, first)
        packed_second = pack_application_state(self.root, second)
        refs_first = self._field_refs(packed_first)
        refs_second = self._field_refs(packed_second)
        self.assertNotEqual(refs_first["effective"], refs_second["effective"])
        self.assertEqual(refs_first["repair_feedback"], refs_second["repair_feedback"])
        self.assertEqual(3, len(list((self.root / "audit-blobs").glob("*.json.gz"))))
        self.assertEqual(second, unpack_application_state(self.root, packed_second))

    def test_compact_root_is_externalized_when_many_small_fields_cross_limit(self):
        state = {f"field_{index:03d}": "x" * 1000 for index in range(80)}
        packed = pack_application_state(self.root, state)
        self.assertEqual({ROOT_MARKER}, set(packed))
        root_marker = packed[ROOT_MARKER]
        root_document = load_data(
            self.root,
            json.dumps(root_marker["blob"], sort_keys=True, separators=(",", ":")),
        )
        self.assertEqual("modport.application-state-root", root_document["schema"])
        self.assertNotIn(FIELD_INDEX, root_document["state"])
        self.assertEqual(state, unpack_application_state(self.root, packed))

    def test_same_state_is_deterministic_and_reuses_content(self):
        state = {"effective": {"body": "same" * INLINE_LIMIT}, "sequence": 4}
        first = pack_application_state(self.root, state)
        first_files = sorted(path.name for path in (self.root / "audit-blobs").glob("*.json.gz"))
        second = pack_application_state(self.root, copy.deepcopy(state))
        self.assertEqual(first, second)
        self.assertEqual(first_files,
                         sorted(path.name for path in (self.root / "audit-blobs").glob("*.json.gz")))

    def test_concurrent_identical_states_publish_one_verified_blob(self):
        state = {"repair_cycle_results": {"body": "z" * INLINE_LIMIT}}
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            packed = list(pool.map(
                lambda _: pack_application_state(self.root, state), range(24)
            ))
        self.assertEqual(1, len({json.dumps(value, sort_keys=True) for value in packed}))
        self.assertEqual(1, len(list((self.root / "audit-blobs").glob("*.json.gz"))))
        self.assertEqual(state, unpack_application_state(self.root, packed[0]))
        self.assertEqual([], list((self.root / "audit-blobs").glob("*.tmp")))

    def test_corrupt_missing_and_symlink_field_blobs_fail_closed(self):
        for mode in ("corrupt", "missing", "symlink"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state = {"effective": {"body": mode * INLINE_LIMIT}}
                packed = pack_application_state(root, state)
                blob_marker = self._field_refs(packed)["effective"]["__modport_audit_blob__"]
                blob = root / blob_marker["path"]
                original = blob.read_bytes()
                if mode == "corrupt":
                    blob.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
                elif mode == "missing":
                    blob.unlink()
                else:
                    outside = root / "outside.json.gz"
                    outside.write_bytes(original)
                    blob.unlink()
                    blob.symlink_to(outside)
                with self.assertRaises(ApplicationStateStorageError):
                    unpack_application_state(root, packed)

    def test_strict_root_and_field_markers_reject_malformed_values(self):
        malformed_root = {
            ROOT_MARKER: {
                "schema": "modport.application-state-root",
                "version": 1,
                "blob": {},
                "extra": True,
            }
        }
        with self.assertRaisesRegex(ApplicationStateStorageError, "root marker"):
            unpack_application_state(self.root, malformed_root)
        malformed_fields = {
            FIELD_INDEX: {
                "schema": "modport.application-state-fields",
                "version": 1,
                "refs": [],
            }
        }
        with self.assertRaisesRegex(ApplicationStateStorageError, "field index"):
            unpack_application_state(self.root, malformed_fields)
        with self.assertRaises(ApplicationStateStorageError):
            unpack_application_state(self.root, {"__modport_audit_blob__": {}})

    def test_declared_field_identity_and_digest_are_authenticated(self):
        state = {"effective": {"body": "q" * INLINE_LIMIT}}
        packed = pack_application_state(self.root, state)
        wrong_field = copy.deepcopy(packed)
        refs = self._field_refs(wrong_field)
        refs["renamed"] = refs.pop("effective")
        with self.assertRaisesRegex(ApplicationStateStorageError, "field document"):
            unpack_application_state(self.root, wrong_field)

        wrong_digest = copy.deepcopy(packed)
        marker = self._field_refs(wrong_digest)["effective"]["__modport_audit_blob__"]
        marker["sha256"] = "0" * 64
        marker["path"] = "audit-blobs/" + "0" * 64 + ".json.gz"
        with self.assertRaises(ApplicationStateStorageError):
            unpack_application_state(self.root, wrong_digest)

    def test_raw_state_limit_is_enforced_before_writing(self):
        with (patch("modport.application_state_storage.MAX_RAW_BYTES", 1024),
              patch("modport.application_state_storage.json_copy",
                    side_effect=AssertionError("copy must follow the bound"))):
            with self.assertRaisesRegex(ApplicationStateStorageError, "raw size limit"):
                pack_application_state(self.root, {"body": "x" * 2048})
        self.assertFalse((self.root / "audit-blobs").exists())

    def test_oversized_inline_state_is_rejected_before_copying(self):
        with (patch("modport.application_state_storage.MAX_RAW_BYTES", 1024),
              patch("modport.application_state_storage.json_copy",
                    side_effect=AssertionError("copy must follow the bound"))):
            with self.assertRaisesRegex(ApplicationStateStorageError, "raw size limit"):
                unpack_application_state(self.root, {"body": "x" * 2048})

    def test_size_bound_does_not_use_whole_scalar_json_encoding(self):
        with (patch("modport.application_state_storage.MAX_RAW_BYTES", 1024),
              patch.object(json.JSONEncoder, "iterencode",
                           side_effect=AssertionError("whole scalar encoder used")),
              patch("modport.application_state_storage.json_copy",
                    side_effect=AssertionError("copy must follow the bound"))):
            with self.assertRaisesRegex(ApplicationStateStorageError, "raw size limit"):
                pack_application_state(self.root, {"body": "\"\\文" * 2048})

    def test_reserved_envelopes_are_rejected_and_legacy_lookalike_is_inline(self):
        for name in ("__modport_audit_blob__", ROOT_MARKER, FIELD_INDEX):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ApplicationStateStorageError, "reserved storage field"):
                    pack_application_state(self.root, {name: {"legacy": True}})

        legacy = {ROOT_MARKER: {"legacy": True}, "active_stage": "source"}
        self.assertFalse(is_packed_application_state(legacy))
        self.assertEqual(legacy, unpack_application_state(self.root, legacy))

    def test_huge_integer_is_bounded_before_decimal_conversion(self):
        previous = sys.get_int_max_str_digits()
        try:
            sys.set_int_max_str_digits(0)
            huge = 1 << 1_000_000
            tracemalloc.start()
            try:
                with patch("modport.application_state_storage.MAX_RAW_BYTES", 1024):
                    with self.assertRaisesRegex(ApplicationStateStorageError, "raw size limit"):
                        pack_application_state(self.root, {"value": huge})
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
        finally:
            sys.set_int_max_str_digits(previous)
        self.assertLess(peak, 128 * 1024)

        class HostileInteger(int):
            def __str__(self):
                raise AssertionError("integer subclass conversion executed")

        with self.assertRaisesRegex(ApplicationStateStorageError, "canonical JSON"):
            pack_application_state(self.root, {"value": HostileInteger(7)})

    def test_json_subclasses_cannot_run_custom_allocation_hooks(self):
        class HostileString(str):
            def __getitem__(self, key):
                raise AssertionError("string subclass slice executed")

        class HostileList(list):
            def __iter__(self):
                raise AssertionError("list subclass iteration executed")

        class HostileDict(dict):
            def items(self):
                raise AssertionError("dict subclass items executed")

        for value in (HostileString("value"), HostileList([1]), HostileDict({"a": 1})):
            with self.subTest(kind=type(value).__name__):
                with self.assertRaisesRegex(ApplicationStateStorageError, "canonical JSON"):
                    pack_application_state(self.root, {"value": value})

    def test_field_index_rejects_empty_and_reserved_names(self):
        empty = {
            FIELD_INDEX: {
                "schema": "modport.application-state-fields",
                "version": 1,
                "refs": {},
            }
        }
        with self.assertRaisesRegex(ApplicationStateStorageError, "field index"):
            unpack_application_state(self.root, empty)

        serialized = store_data(self.root, {
            "schema": "modport.application-state-field",
            "version": 1,
            "field": ROOT_MARKER,
            "value": {"body": "x" * INLINE_LIMIT},
        })
        reserved = {
            FIELD_INDEX: {
                "schema": "modport.application-state-fields",
                "version": 1,
                "refs": {ROOT_MARKER: json.loads(serialized)},
            }
        }
        with self.assertRaisesRegex(ApplicationStateStorageError, "invalid or duplicate"):
            unpack_application_state(self.root, reserved)

    def test_hydrate_snapshot_preserves_other_snapshot_objects(self):
        state = {"effective": {"body": "h" * INLINE_LIMIT}}
        packed = pack_application_state(self.root, state)
        tasks = {"source": {"attempts": []}}
        snapshot = {"run_id": "run", "application_state": packed, "tasks": tasks}
        hydrated = hydrate_run_snapshot(self.root, snapshot)
        self.assertEqual(state, hydrated["application_state"])
        self.assertIs(tasks, hydrated["tasks"])
        self.assertEqual(packed, snapshot["application_state"])
        self.assertIsNone(hydrate_run_snapshot(
            self.root, {"application_state": None}
        )["application_state"])


if __name__ == "__main__":
    unittest.main()
