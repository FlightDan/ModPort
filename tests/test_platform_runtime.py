import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from modport.platform_files import FileLock, UnsafePathError, atomic_write, safe_open, flock, LOCK_EX, LOCK_NB
from modport.platform_runtime import capture_process, process_birth, trusted_mcp_argv


class PlatformRuntimeTests(unittest.TestCase):
    def test_trusted_capture_can_inherit_caller_lifetime_without_new_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            result = capture_process([sys.executable, "-I", "-c", "print('completed')"],
                                     cwd=directory, timeout=None)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), b"completed")

    def test_real_simultaneous_stdout_stderr_are_bounded_and_fully_observed(self):
        seen = {"stdout": 0, "stderr": 0}

        def observe(name, chunk):
            seen[name] += len(chunk)

        with tempfile.TemporaryDirectory() as directory:
            result = capture_process([
                sys.executable, "-I", "-c",
                "import os; "
                "[(os.write(1,b'o'*8192),os.write(2,b'e'*8192)) for _ in range(64)]",
            ], cwd=directory, timeout=10, max_output_bytes=2048, on_chunk=observe)
        self.assertEqual(result.returncode, 0)
        self.assertFalse(result.timed_out)
        self.assertFalse(result.drain_incomplete)
        self.assertEqual(seen, {"stdout": 524288, "stderr": 524288})
        self.assertEqual(result.stdout, b"o" * 2048)
        self.assertEqual(result.stderr, b"e" * 2048)
        self.assertTrue(result.stdout_truncated)

    def test_file_backed_large_stdin_does_not_deadlock_early_output(self):
        with tempfile.TemporaryDirectory() as directory:
            result = capture_process([
                sys.executable, "-I", "-c",
                "import os,sys; os.write(1,b'x'*131072); "
                "data=sys.stdin.buffer.read(); print(len(data))",
            ], cwd=directory, timeout=10, input_bytes=b"p" * 2_000_000,
                max_output_bytes=32)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.endswith(b"2000000\n"))

    def test_timeout_stops_child_writing_after_parent_returns(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "activity"
            child = ("import pathlib,time; p=pathlib.Path(" + repr(str(marker)) + "); "
                     "\nwhile True:\n p.write_text(str(time.time())); time.sleep(.02)")
            parent = ("import subprocess,sys,time; "
                      "subprocess.Popen([sys.executable,'-I','-c'," + repr(child) + "]); "
                      "time.sleep(60)")
            result = capture_process([sys.executable, "-I", "-c", parent],
                                     cwd=directory, timeout=0.8)
            self.assertTrue(result.timed_out)
            self.assertTrue(marker.is_file(), "descendant never started")
            previous = marker.read_text()
            time.sleep(0.1)
            self.assertEqual(marker.read_text(), previous)

    def test_birth_identity_is_live_process_specific(self):
        identity = process_birth(os.getpid())
        if identity is None:
            self.skipTest("process birth identity unavailable in this PID namespace")
        self.assertEqual(identity, process_birth(os.getpid()))
        self.assertIsNone(process_birth(-1))

    def test_mcp_launcher_uses_isolated_python_and_explicit_source(self):
        command = trusted_mcp_argv("modport.rework_mcp", Path(__file__).parents[1] / "src",
                                   Path(tempfile.gettempdir()) / "session.json")
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[1:3], ["-I", "-c"])
        self.assertIn(str(Path(__file__).parents[1] / "src"), command[3])
        with self.assertRaises(ValueError):
            trusted_mcp_argv("project.modport", "/tmp", "/tmp/session.json")


class PlatformFilesTests(unittest.TestCase):
    def test_close_and_descriptor_reuse_reacquires_actual_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = safe_open(root, "lock", os.O_CREAT | os.O_RDWR)
            flock(first, LOCK_EX | LOCK_NB)
            os.close(first)
            second = safe_open(root, "lock", os.O_RDWR)
            try:
                flock(second, LOCK_EX | LOCK_NB)
                program = (
                    "from modport.platform_files import FileLock; import sys\n"
                    "try:\n with FileLock(sys.argv[1],blocking=False): pass\n"
                    "except BlockingIOError:\n sys.exit(9)\n")
                result = subprocess.run([sys.executable, "-c", program, str(root / "lock")],
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 9, result.stderr)
            finally:
                os.close(second)

    def test_explicit_readonly_hardlink_policy_never_permits_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original"
            original.write_bytes(b"immutable host data")
            os.link(original, root / "alias")
            descriptor = safe_open(root, "alias", allow_readonly_hardlinks=True)
            try:
                self.assertEqual(os.read(descriptor, 99), b"immutable host data")
            finally:
                os.close(descriptor)
            with self.assertRaises(UnsafePathError):
                safe_open(root, "alias", os.O_WRONLY | os.O_TRUNC,
                          allow_readonly_hardlinks=True)
            self.assertEqual(original.read_bytes(), b"immutable host data")

    def test_real_other_process_cannot_acquire_held_file_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "lock"
            with FileLock(lock):
                program = (
                    "from modport.platform_files import FileLock; import sys\n"
                    "try:\n with FileLock(sys.argv[1],blocking=False): pass\n"
                    "except BlockingIOError:\n print('held'); sys.exit(9)\n")
                result = subprocess.run([sys.executable, "-c", program, str(lock)],
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 9, result.stderr)
                self.assertEqual(result.stdout.strip(), "held")
            with FileLock(lock, blocking=False):
                pass

    def test_anchored_file_access_rejects_traversal_and_hardlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").write_bytes(b"original")
            with self.assertRaises(UnsafePathError):
                safe_open(root, "../data")
            os.link(root / "data", root / "alias")
            with self.assertRaises(UnsafePathError):
                safe_open(root, "data", os.O_WRONLY | os.O_TRUNC)
            self.assertEqual((root / "data").read_bytes(), b"original")

    @unittest.skipIf(os.name == "nt", "Windows junction coverage is in native sandbox tests")
    def test_leaf_and_ancestor_links_cannot_redirect_read_or_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "outside").mkdir()
            (root / "outside" / "secret").write_text("private")
            (root / "link").symlink_to(root / "outside", target_is_directory=True)
            (root / "leaf").symlink_to(root / "outside" / "secret")
            for relative in ("link/secret", "leaf"):
                with self.assertRaises((OSError, UnsafePathError)):
                    safe_open(root, relative, os.O_WRONLY | os.O_TRUNC)
            self.assertEqual((root / "outside" / "secret").read_text(), "private")

    def test_atomic_write_replaces_regular_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "state.json"
            atomic_write(target, b'{"state":"first"}')
            atomic_write(target, b'{"state":"second"}')
            self.assertEqual(json.loads(target.read_text()), {"state": "second"})
            self.assertEqual([path.name for path in target.parent.iterdir()], ["state.json"])


if __name__ == "__main__":
    unittest.main()
