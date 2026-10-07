"""Focused tests for authenticated host candidate collection."""
from pathlib import Path
from types import SimpleNamespace
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from modport.contracts import OperationInput
from modport.host_candidate import collect_host_candidate


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        cwd=root, stderr=subprocess.DEVNULL).decode().strip()


class HostCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        _git(self.source, "init")
        (self.source / "base.txt").write_text("base\n")
        _git(self.source, "add", ".")
        _git(self.source, "commit", "-m", "initial")
        self.start = _git(self.source, "rev-parse", "HEAD")
        self.workspace = self.root / "workspace"
        _git(self.root, "clone", "--no-hardlinks", "--no-checkout",
             str(self.source), str(self.workspace))
        _git(self.workspace, "checkout", "--detach", self.start)
        self.command = OperationInput(
            "run", "task", "coder", "coder-task", str(self.root),
            options={"workflow_version": 17})

    def test_collection_batches_blob_hash_and_index_updates(self):
        (self.workspace / "base.txt").write_text("edited\n")
        for position in range(40):
            (self.workspace / f"new-{position:03d}.txt").write_text(
                f"new file {position}\n")

        from modport import host_candidate
        with patch.object(host_candidate, "_git", wraps=host_candidate._git) as observed:
            candidate = collect_host_candidate(
                self.command, self.workspace, self.start, "task")

        calls = [call.args for call in observed.call_args_list]
        hash_calls = [args for args in calls if "hash-object" in args]
        index_calls = [args for args in calls if "update-index" in args]
        self.assertEqual(1, len(hash_calls))
        self.assertEqual(1, len(index_calls))
        self.assertNotIn("--cacheinfo", index_calls[0])
        self.assertIn("--stdin-paths", hash_calls[0])
        self.assertIn("--index-info", index_calls[0])
        self.assertIn("\0", next(call for call in observed.call_args_list
                                  if "update-index" in call.args).kwargs["input_text"])
        self.assertEqual(41, candidate.record["collected_file_count"])

    def test_cached_candidate_requires_content_and_mode_match(self):
        first = collect_host_candidate(self.command, self.workspace, self.start, "task")

        from modport import host_candidate
        with patch.object(host_candidate, "_git", wraps=host_candidate._git) as observed:
            reused = collect_host_candidate(
                self.command, self.workspace, self.start, "task",
                cached_candidate=first)
        self.assertIs(first, reused)
        self.assertFalse(any("hash-object" in call.args for call in observed.call_args_list))

        (self.workspace / "base.txt").write_text("changed after cache\n")
        refreshed = collect_host_candidate(
            self.command, self.workspace, self.start, "task",
            cached_candidate=first)
        self.assertNotEqual(first.head, refreshed.head)

        (self.workspace / "base.txt").chmod(0o755)
        refreshed_again = collect_host_candidate(
            self.command, self.workspace, self.start, "task",
            cached_candidate=refreshed)
        self.assertNotEqual(refreshed.head, refreshed_again.head)

    def test_copy_rejects_source_changed_during_collection(self):
        from modport import host_candidate

        source = self.root / "source-file"
        destination = self.root / "copied-file"
        source.write_bytes(b"source contents\n")
        attributes = source.stat()
        initial = SimpleNamespace(
            st_mode=attributes.st_mode, st_dev=attributes.st_dev,
            st_ino=attributes.st_ino, st_size=attributes.st_size,
            st_mtime_ns=attributes.st_mtime_ns, st_nlink=attributes.st_nlink)
        changed = SimpleNamespace(
            st_mode=initial.st_mode, st_dev=initial.st_dev,
            st_ino=initial.st_ino, st_size=initial.st_size,
            st_mtime_ns=initial.st_mtime_ns + 1, st_nlink=initial.st_nlink)

        with patch.object(host_candidate.os, "fstat", side_effect=[initial, changed]):
            with self.assertRaisesRegex(ValueError, "changed during collection"):
                host_candidate._copy_regular(source, destination)


if __name__ == "__main__":
    unittest.main()
