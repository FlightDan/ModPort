"""Behavioral regression coverage for the host-managed dependency seed store."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from modport.dependency_cache import (
    fetch_artifact, publish_artifact, snapshot_repository, verify_repository, verify_store,
)


class DependencyCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = self.root / "store"
        self.source = self.root / "source.jar"
        self.source.write_bytes(b"pinned artifact")
        self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.coordinate = "org.example:library:1.2.3"
        self.url = "https://example.org/library.jar"

    def publish(self, **kwargs):
        return publish_artifact(self.store, self.coordinate, self.source, self.digest,
                                self.url, no_transitive_dependencies=True, **kwargs)

    def test_publication_snapshot_and_independent_bytes(self):
        record = self.publish()
        snapshot = self.root / "snapshot"
        manifest = snapshot_repository(self.store, snapshot)
        self.assertEqual(verify_repository(snapshot), manifest)
        original = self.store / record["jar"]["path"]
        copied = snapshot / record["jar"]["path"]
        self.assertNotEqual(original.stat().st_ino, copied.stat().st_ino)
        copied.write_bytes(b"changed")
        self.assertEqual(original.read_bytes(), b"pinned artifact")
        with self.assertRaises(ValueError):
            verify_repository(snapshot)

    def test_conflict_does_not_mutate(self):
        record = self.publish()
        self.source.write_bytes(b"different")
        self.digest = hashlib.sha256(b"different").hexdigest()
        with self.assertRaisesRegex(ValueError, "conflict"):
            self.publish()
        self.assertEqual((self.store / record["jar"]["path"]).read_bytes(), b"pinned artifact")

    def test_bad_hash_and_implicit_pom_rejected(self):
        with self.assertRaises(ValueError):
            publish_artifact(self.store, self.coordinate, self.source, self.digest, self.url)
        self.digest = "0" * 64
        with self.assertRaises(ValueError):
            self.publish()
        self.assertFalse(self.store.exists())

    def test_original_pom_preserved(self):
        pom = self.root / "original.pom"
        pom.write_bytes(b"<project><dependencies><dependency>original</dependency></dependencies></project>")
        record = publish_artifact(self.store, self.coordinate, self.source, self.digest, self.url,
                                  pom_source=pom, pom_sha256=hashlib.sha256(pom.read_bytes()).hexdigest(),
                                  pom_url="https://example.org/library.pom")
        snapshot_repository(self.store, self.root / "snapshot")
        self.assertEqual((self.store / record["pom"]["path"]).read_bytes(), pom.read_bytes())

    def test_absent_store_empty_snapshot_without_creation(self):
        snapshot = self.root / "snapshot"
        self.assertEqual(snapshot_repository(self.store, snapshot)["artifacts"], {})
        self.assertFalse(self.store.exists())
        verify_repository(snapshot)

    def test_reuse_checks_snapshot_and_current_store(self):
        self.publish()
        snapshot = self.root / "snapshot"
        first = snapshot_repository(self.store, snapshot)
        self.assertEqual(snapshot_repository(self.store, snapshot), first)
        (snapshot / "extra").write_text("extra")
        with self.assertRaises(ValueError):
            snapshot_repository(self.store, snapshot)

    def test_symlinks_and_coordinate_traversal_rejected(self):
        link = self.root / "link"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            snapshot_repository(link / "store", self.root / "snapshot")
        self.coordinate = "org.example:../escape:1"
        with self.assertRaises(ValueError):
            self.publish()
        self.coordinate = "org.example:library:1.2.3"
        self.source.unlink()
        self.source.symlink_to("/etc/hosts")
        with self.assertRaises(ValueError):
            self.publish()

    def test_manifest_artifact_symlink_rejected(self):
        record = self.publish()
        snapshot = self.root / "snapshot"
        snapshot_repository(self.store, snapshot)
        copied = snapshot / record["jar"]["path"]
        copied.unlink()
        copied.symlink_to(self.source)
        with self.assertRaises(ValueError):
            verify_repository(snapshot)

    def test_cache_hit_does_not_use_network(self):
        self.publish()
        with patch("modport.dependency_cache.urllib.request.build_opener") as opener:
            fetch_artifact(self.store, self.coordinate, self.url, self.digest, no_transitive_dependencies=True)
            opener.assert_not_called()

    def test_unsafe_urls_rejected_before_network(self):
        for url in ("http://example.org/a", "https://user:secret@example.org/a", "https://example.org/a#fragment"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                fetch_artifact(self.store, self.coordinate, url, self.digest, no_transitive_dependencies=True)

    def test_long_retry_after_aborts_without_early_retry(self):
        error = urllib.error.HTTPError(self.url, 429, "slow down", {"Retry-After": "120"}, None)
        with patch("modport.dependency_cache.urllib.request.build_opener") as factory, patch("modport.dependency_cache.time.sleep") as sleep:
            factory.return_value.open.side_effect = error
            with self.assertRaisesRegex(ValueError, "bounded wait"):
                fetch_artifact(self.store, self.coordinate, self.url, self.digest, no_transitive_dependencies=True)
            self.assertEqual(factory.return_value.open.call_count, 1)
            sleep.assert_not_called()

    def test_transient_failure_has_at_most_three_retries(self):
        error = urllib.error.HTTPError(self.url, 503, "busy", {}, None)
        with patch("modport.dependency_cache.urllib.request.build_opener") as factory, patch("modport.dependency_cache.time.sleep") as sleep:
            factory.return_value.open.side_effect = error
            with self.assertRaises(urllib.error.HTTPError):
                fetch_artifact(self.store, self.coordinate, self.url, self.digest, no_transitive_dependencies=True)
            self.assertEqual(factory.return_value.open.call_count, 4)
            self.assertEqual(sleep.call_count, 3)

    def test_original_pom_conflict_is_immutable(self):
        original = self.publish()
        pom = self.root / "other.pom"
        pom.write_bytes(b"<project/>")
        with self.assertRaisesRegex(ValueError, "conflict"):
            publish_artifact(self.store, self.coordinate, self.source, self.digest, self.url,
                             pom_source=pom, pom_sha256=hashlib.sha256(pom.read_bytes()).hexdigest(),
                             pom_url="https://example.org/a.pom")
        self.assertEqual(self.publish(), original)

    def test_fetch_success_then_offline_hit(self):
        from unittest.mock import MagicMock
        response = MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = self.url
        response.read.return_value = self.source.read_bytes()
        with patch("modport.dependency_cache.urllib.request.build_opener") as factory:
            factory.return_value.open.return_value = response
            record = fetch_artifact(self.store, self.coordinate, self.url, self.digest,
                                    no_transitive_dependencies=True)
            factory.return_value.open.assert_called_once()
        with patch("modport.dependency_cache.urllib.request.build_opener", side_effect=AssertionError("network on hit")):
            self.assertEqual(fetch_artifact(self.store, self.coordinate, self.url, self.digest,
                                           no_transitive_dependencies=True)["artifact"], record["artifact"])

    def test_retry_after_is_respected(self):
        from unittest.mock import MagicMock
        response = MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = self.url
        response.read.return_value = self.source.read_bytes()
        error = urllib.error.HTTPError(self.url, 429, "busy", {"Retry-After": "7"}, None)
        with patch("modport.dependency_cache.urllib.request.build_opener") as factory, patch("modport.dependency_cache.time.sleep") as sleep:
            factory.return_value.open.side_effect = [error, response]
            fetch_artifact(self.store, self.coordinate, self.url, self.digest,
                           no_transitive_dependencies=True)
            sleep.assert_called_once_with(7.0)

    def test_verify_store_allows_locks_but_rejects_untracked_content(self):
        self.publish()
        self.assertIn(self.coordinate, verify_store(self.store)["artifacts"])
        (self.store / "repository" / "extra.jar").write_bytes(b"untracked")
        with self.assertRaises(ValueError):
            verify_store(self.store)

    def test_lock_wait_is_bounded(self):
        with patch("modport.dependency_cache.fcntl.flock", side_effect=BlockingIOError), patch("modport.dependency_cache.LOCK_TIMEOUT_SECONDS", 0):
            with self.assertRaises(TimeoutError):
                self.publish()
