from pathlib import Path
import signal
import tempfile
import unittest

from modport.process_diagnostics import (
    CgroupMemoryEvents, diagnose_process_exit, memory_event_delta,
    process_cgroup, read_memory_events, read_process_memory_events,
)


class ProcessDiagnosticsTests(unittest.TestCase):
    @staticmethod
    def events(scope, *, oom=0, oom_kill=0, group=0):
        return CgroupMemoryEvents(
            scope, {"oom": oom, "oom_kill": oom_kill,
                    "oom_group_kill": group}, 1.0)

    def test_sigkill_and_exit_137_are_not_oom_without_scoped_counter_change(self):
        before = self.events("/worker", oom=2, oom_kill=1)
        after = self.events("/worker", oom=2, oom_kill=1)
        for returncode in (-signal.SIGKILL, 137):
            with self.subTest(returncode=returncode):
                result = diagnose_process_exit(returncode, before=before, after=after)
                self.assertEqual("signal_exit", result.classification)
                self.assertEqual(signal.SIGKILL, result.signal_number)

    def test_only_exclusive_same_scope_sigkill_confirms_oom(self):
        before = self.events("/worker", oom=3, oom_kill=4)
        after = self.events("/worker", oom=4, oom_kill=5)
        result = diagnose_process_exit(
            137, before=before, after=after, exclusive_scope=True)
        self.assertEqual("confirmed_oom", result.classification)
        self.assertEqual("confirmed", result.attribution)
        self.assertEqual(1, result.memory_event_delta["oom_kill"])

        other = self.events("/other", oom=4, oom_kill=5)
        result = diagnose_process_exit(137, before=before, after=other)
        self.assertEqual("signal_exit", result.classification)
        self.assertEqual("scope_mismatch", result.evidence_status)

    def test_shared_cgroup_oom_is_observed_but_not_attributed(self):
        before = self.events("/shared", oom=3, oom_kill=4)
        after = self.events("/shared", oom=4, oom_kill=5)
        for returncode in (1, 137, -signal.SIGKILL, 0):
            with self.subTest(returncode=returncode):
                result = diagnose_process_exit(
                    returncode, before=before, after=after)
                self.assertEqual("cgroup_oom_observed", result.classification)
                self.assertEqual("unknown", result.attribution)

    def test_exclusive_scope_still_requires_sigkill(self):
        before = self.events("/worker", oom=3, oom_kill=4)
        after = self.events("/worker", oom=4, oom_kill=5)
        result = diagnose_process_exit(
            1, before=before, after=after, exclusive_scope=True)
        self.assertEqual("cgroup_oom_observed", result.classification)
        self.assertEqual("unknown", result.attribution)

    def test_oom_observation_without_kill_is_not_a_confirmed_oom(self):
        before = self.events("/worker", oom=3, oom_kill=4)
        after = self.events("/worker", oom=4, oom_kill=4)
        result = diagnose_process_exit(137, before=before, after=after)
        self.assertEqual("signal_exit", result.classification)

        successful = diagnose_process_exit(0, before=before, after=self.events(
            "/worker", oom=4, oom_kill=5), exclusive_scope=True)
        self.assertEqual("cgroup_oom_observed", successful.classification)
        self.assertEqual("unknown", successful.attribution)

    def test_admission_timeout_process_timeout_deadline_and_unknown_are_distinct(self):
        self.assertEqual(
            "memory_admission_timeout",
            diagnose_process_exit(None, admission="timeout").classification,
        )
        self.assertEqual(
            "process_timeout",
            diagnose_process_exit(-signal.SIGKILL, timed_out=True).classification,
        )
        self.assertEqual(
            "deadline_exceeded",
            diagnose_process_exit(None, deadline_exceeded=True).classification,
        )
        self.assertEqual("unknown", diagnose_process_exit(None).classification)

    def test_memory_events_reader_is_bounded_to_supplied_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "memory.events").write_text(
                "low 0\nhigh 2\nmax 3\noom 4\noom_kill 5\n",
                encoding="ascii",
            )
            (root / 'memory.max').write_text('max\n')
            (root / 'memory.peak').write_text('12345\n')
            snapshot = read_memory_events(root, clock=lambda: 9.0)
        self.assertIsNone(snapshot.error)
        self.assertEqual(str(root), snapshot.scope)
        self.assertEqual(5, snapshot.counters["oom_kill"])
        self.assertEqual(0, snapshot.counters["oom_group_kill"])
        self.assertEqual('max', snapshot.memory['memory.max'])
        self.assertEqual(12345, snapshot.memory['memory.peak'])
        self.assertIsNone(snapshot.memory['memory.current'])
        self.assertIn('memory.current', snapshot.memory_errors)

    def test_counter_reset_is_not_evidence(self):
        before = self.events("/worker", oom_kill=5)
        after = self.events("/worker", oom_kill=1)
        delta, status = memory_event_delta(before, after)
        self.assertEqual({}, delta)
        self.assertEqual("counter_reset", status)

    def test_process_probe_resolves_the_target_pid_cgroup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proc = root / "proc"
            process = proc / "42"
            cgroup = root / "sys/fs/cgroup/workers/42"
            process.mkdir(parents=True)
            cgroup.mkdir(parents=True)
            (process / "cgroup").write_text("0::/workers/42\n", encoding="utf-8")
            (process / "mountinfo").write_text(
                f"29 23 0:26 / {root / 'sys/fs/cgroup'} rw - cgroup2 cgroup rw\n",
                encoding="utf-8",
            )
            (cgroup / "memory.events").write_text(
                "oom 1\noom_kill 2\noom_group_kill 0\n", encoding="ascii")
            resolved, error = process_cgroup(42, proc_root=proc)
            self.assertIsNone(error)
            self.assertEqual(cgroup, resolved)
            snapshot = read_process_memory_events(42, proc_root=proc, clock=lambda: 8.0)
        self.assertIsNone(snapshot.error)
        self.assertEqual(str(cgroup), snapshot.scope)
        self.assertEqual(2, snapshot.counters["oom_kill"])


if __name__ == "__main__":
    unittest.main()
