import concurrent.futures
import gzip
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from modport.audit_storage import (
    AuditStorageError,
    INLINE_LIMIT,
    MAX_COMPRESSED_BYTES,
    MAX_RAW_BYTES,
    inspect_data_reference,
    load_data,
    store_data,
    store_raw,
)


class AuditStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_small_data_stays_inline_unchanged(self):
        data = {"run_id": "run-1", "payload": {"message": "完整记录", "value": 3}}
        serialized = store_data(self.root, data)
        self.assertEqual(serialized, json.dumps(data, ensure_ascii=False, default=str))
        self.assertIsNone(inspect_data_reference(serialized))
        self.assertEqual(load_data(self.root, serialized), data)
        self.assertFalse((self.root / "audit-blobs").exists())

    def test_large_data_round_trips_and_keeps_only_safe_identity_inline(self):
        secret_body = "full-redacted-event-body:" + "x" * INLINE_LIMIT
        data = {
            "run_id": "run-1", "task_id": "task-2", "attempt": 4,
            "payload": {"body": secret_body, "nested": [1, 2, 3]},
        }
        serialized = store_data(self.root, data)
        reference = inspect_data_reference(serialized)
        self.assertIsNotNone(reference)
        self.assertEqual(reference["identity"], {
            "run_id": "run-1", "task_id": "task-2", "attempt": 4,
        })
        self.assertNotIn(secret_body, serialized)
        self.assertNotIn("payload", reference)
        blob = self.root / reference["path"]
        self.assertTrue(blob.is_file())
        self.assertEqual(blob.stat().st_size, reference["gzip_size"])
        self.assertEqual(load_data(self.root, serialized), data)

    def test_exact_inline_limit_is_externalized(self):
        empty = json.dumps({"payload": {"body": ""}}, ensure_ascii=False, default=str)
        body_size = INLINE_LIMIT - len(empty.encode("utf-8"))
        inline = {"payload": {"body": "x" * (body_size - 1)}}
        external = {"payload": {"body": "x" * body_size}}
        self.assertIsNone(inspect_data_reference(store_data(self.root, inline)))
        self.assertIsNotNone(inspect_data_reference(store_data(self.root, external)))

    def test_store_raw_preserves_exact_bytes(self):
        raw = (b'{  "run_id" : "historical", "payload" : {"body":"'
               + b"x" * 100_000 + b'"} }\n')
        serialized = store_raw(self.root, raw)
        reference = inspect_data_reference(serialized)
        self.assertEqual(reference["raw_size"], len(raw))
        self.assertEqual(reference["sha256"], hashlib.sha256(raw).hexdigest())
        with gzip.open(self.root / reference["path"], "rb") as stream:
            self.assertEqual(stream.read(), raw)
        self.assertEqual(load_data(self.root, serialized), json.loads(raw))

    def test_concurrent_duplicate_writes_share_one_verified_blob(self):
        data = {"run_id": "same", "payload": {"body": "z" * 200_000}}
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: store_data(self.root, data), range(24)))
        self.assertEqual(len(set(results)), 1)
        blobs = list((self.root / "audit-blobs").glob("*.json.gz"))
        self.assertEqual(len(blobs), 1)
        self.assertEqual(load_data(self.root, results[0]), data)
        self.assertEqual(list((self.root / "audit-blobs").glob("*.tmp")), [])

    def test_blob_is_fsynced_before_reference_is_returned(self):
        observed = []
        original_fsync = os.fsync

        def recording_fsync(descriptor):
            mode = os.fstat(descriptor).st_mode
            observed.append("file" if stat.S_ISREG(mode) else "directory")
            return original_fsync(descriptor)

        with patch("modport.audit_storage.os.fsync", side_effect=recording_fsync):
            serialized = store_data(self.root, {"payload": {"body": "x" * 100_000}})
        self.assertIsNotNone(inspect_data_reference(serialized))
        file_sync = observed.index("file")
        self.assertIn("directory", observed[file_sync + 1:])

    def test_corruption_missing_file_and_symlink_fail_closed(self):
        data = {"payload": {"body": "x" * 100_000}}
        serialized = store_data(self.root, data)
        reference = inspect_data_reference(serialized)
        blob = self.root / reference["path"]

        original = blob.read_bytes()
        blob.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        with self.assertRaises(AuditStorageError):
            load_data(self.root, serialized)

        blob.unlink()
        with self.assertRaises(AuditStorageError):
            load_data(self.root, serialized)

        target = self.root / "outside.gz"
        target.write_bytes(original)
        blob.symlink_to(target)
        with self.assertRaises(AuditStorageError):
            load_data(self.root, serialized)

    def test_outside_and_noncanonical_paths_are_rejected_before_read(self):
        serialized = store_data(self.root, {"payload": {"body": "x" * 100_000}})
        envelope = json.loads(serialized)
        marker = envelope["__modport_audit_blob__"]
        for unsafe in ("../outside.json.gz", "/tmp/outside.json.gz",
                       "audit-blobs/../outside.json.gz"):
            with self.subTest(path=unsafe):
                marker["path"] = unsafe
                with self.assertRaises(AuditStorageError):
                    load_data(self.root, json.dumps(envelope))

    def test_symlinked_blob_directory_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / "audit-blobs").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(AuditStorageError):
            store_data(self.root, {"payload": {"body": "x" * 100_000}})

    def test_malformed_or_oversized_reference_sizes_are_rejected(self):
        serialized = store_data(self.root, {"payload": {"body": "x" * 100_000}})
        original = json.loads(serialized)
        for field, value in (
            ("raw_size", 0), ("raw_size", -1), ("raw_size", True),
            ("raw_size", MAX_RAW_BYTES + 1),
            ("gzip_size", 0), ("gzip_size", True),
            ("gzip_size", MAX_COMPRESSED_BYTES + 1),
        ):
            with self.subTest(field=field, value=value):
                envelope = json.loads(json.dumps(original))
                envelope["__modport_audit_blob__"][field] = value
                with self.assertRaises(AuditStorageError):
                    load_data(self.root, json.dumps(envelope))

    def test_reserved_marker_inside_original_data_is_not_a_reference(self):
        data = {
            "run_id": "run",
            "payload": {"__modport_audit_blob__": {"path": "../../attack"}},
            "__modport_audit_blob__": {"also": "ordinary when root has other keys"},
        }
        serialized = store_data(self.root, data)
        self.assertIsNone(inspect_data_reference(serialized))
        self.assertEqual(load_data(self.root, serialized), data)

    def test_exact_marker_with_malformed_shape_fails_closed(self):
        malformed = json.dumps({"__modport_audit_blob__": {"path": "missing-fields"}})
        with self.assertRaises(AuditStorageError):
            inspect_data_reference(malformed)
        with self.assertRaises(AuditStorageError):
            load_data(self.root, malformed)

    def test_declared_sizes_and_digest_are_verified(self):
        serialized = store_data(self.root, {"payload": {"body": "x" * 100_000}})
        for field, replacement in (("raw_size", 10), ("gzip_size", 10),
                                   ("sha256", "0" * 64)):
            with self.subTest(field=field):
                envelope = json.loads(serialized)
                envelope["__modport_audit_blob__"][field] = replacement
                if field == "sha256":
                    envelope["__modport_audit_blob__"]["path"] = (
                        "audit-blobs/" + replacement + ".json.gz"
                    )
                with self.assertRaises(AuditStorageError):
                    load_data(self.root, json.dumps(envelope))

    def test_raw_requires_one_non_reference_json_object(self):
        for value in (b"[]", b"not-json"):
            with self.subTest(value=value), self.assertRaises(AuditStorageError):
                store_raw(self.root, value)
        reference = store_raw(self.root, b'{"payload":{"body":"value"}}')
        with self.assertRaises(AuditStorageError):
            store_raw(self.root, reference.encode())


if __name__ == "__main__":
    unittest.main()
