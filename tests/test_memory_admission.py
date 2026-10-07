import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modport.memory_admission import MemoryPolicy, MemorySnapshot, MIB, host_memory_snapshot


class HostMemoryProbeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.proc = self.root / "proc"
        (self.proc / "self").mkdir(parents=True)

    def meminfo(self, total_mib=16_384, available_mib=12_288):
        (self.proc / "meminfo").write_text(
            f"MemTotal: {total_mib * 1024} kB\nMemAvailable: {available_mib * 1024} kB\n"
            "SwapTotal: 999999999 kB\nSwapFree: 999999999 kB\n")

    def cgroup(self, membership="/parent/leaf", mount_root="/"):
        mount = self.root / "cgroup"
        mount.mkdir()
        (self.proc / "self" / "cgroup").write_text(f"0::{membership}\n")
        (self.proc / "self" / "mountinfo").write_text(
            f"36 25 0:32 {mount_root} {mount} rw - cgroup2 cgroup rw\n")
        return mount

    @staticmethod
    def values(path, *, current, maximum="max", high="max"):
        path.mkdir(parents=True, exist_ok=True)
        (path / "memory.current").write_text(str(current))
        (path / "memory.max").write_text(str(maximum))
        (path / "memory.high").write_text(str(high))

    def test_every_model_and_bootstrap_build_stage_has_a_reservation(self):
        from modport.workflow import AGENT_STAGES, SUPERVISOR_STAGE
        policy = MemoryPolicy()
        for stage in AGENT_STAGES | {SUPERVISOR_STAGE, "environment", "development_prepare"}:
            with self.subTest(stage=stage):
                self.assertIsNotNone(policy.for_stage(stage))
        self.assertEqual(policy, policy.for_stage("environment"))
        self.assertEqual(policy, policy.for_stage("development_prepare"))
        self.assertEqual(policy, policy.for_stage("early_compile"))
        self.assertIsNone(policy.for_stage("delivery"))

    def test_early_compile_worker_cannot_bypass_heavy_memory_admission(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from modport.contracts import OperationInput
        from modport.kernel_runtime import SDKHandler
        command = OperationInput('memory', 'early_compile', 'early_compile',
                                 'memory:early_compile:1', str(self.proc.parent))
        handler = Mock()
        effects = Mock()
        context = SimpleNamespace(command=SimpleNamespace(
            execution_id=command.command_id, correlation_id=command.run_id,
            handler_id='modport.early_compile'), effects=effects)
        policy = MemoryPolicy()
        adapter = SDKHandler(handler, 'memory-regression', memory_policy=policy)
        with patch('modport.kernel_runtime.check_storage_budget'), patch(
                'modport.kernel_runtime.memory_permit',
                side_effect=RuntimeError('heavy memory denied')) as admission:
            with self.assertRaisesRegex(RuntimeError, 'heavy memory denied'):
                adapter._execute(command.to_dict(), context)
        self.assertEqual(policy, admission.call_args.kwargs['policy'])
        handler.assert_not_called()
        effects.execute_once.assert_not_called()

    def test_worker_reservation_matches_the_requested_business_stage(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from modport.contracts import OperationInput
        from modport.kernel_runtime import SDKHandler

        policy = MemoryPolicy()
        for stage in ("contract_draft", "contract_verify"):
            with self.subTest(stage=stage):
                command = OperationInput(
                    "memory", stage, stage, "memory:" + stage + ":1",
                    str(self.proc.parent),
                )
                handler = Mock()
                effects = Mock()
                context = SimpleNamespace(command=SimpleNamespace(
                    execution_id=command.command_id, correlation_id=command.run_id,
                    handler_id="modport." + stage), effects=effects)
                adapter = SDKHandler(handler, "memory-stage-regression", memory_policy=policy)
                with patch("modport.kernel_runtime.check_storage_budget"), patch(
                        "modport.kernel_runtime.memory_permit",
                        side_effect=RuntimeError("stage reservation checked")) as admission:
                    with self.assertRaisesRegex(RuntimeError, "stage reservation checked"):
                        adapter._execute(command.to_dict(), context)
                self.assertEqual(policy.for_stage(stage), admission.call_args.kwargs["policy"])
                handler.assert_not_called()
                effects.execute_once.assert_not_called()

    def test_proc_meminfo_uses_available_ram_and_never_swap(self):
        self.meminfo(total_mib=4096, available_mib=1536)
        observed = host_memory_snapshot(proc_root=self.proc)
        self.assertEqual(observed.ceiling_bytes, 4096 * MIB)
        self.assertEqual(observed.available_bytes, 1536 * MIB)
        self.assertEqual(observed.source, "proc_meminfo")

    def test_nested_cgroup_ancestors_constrain_ceiling_and_headroom(self):
        self.meminfo()
        mount = self.cgroup()
        self.values(mount, current=0)
        self.values(mount / "parent", current=2 * 1024 * MIB, maximum=8 * 1024 * MIB)
        self.values(mount / "parent" / "leaf", current=1 * 1024 * MIB,
                    maximum=6 * 1024 * MIB, high=5 * 1024 * MIB)
        observed = host_memory_snapshot(proc_root=self.proc)
        self.assertEqual(observed.ceiling_bytes, 5 * 1024 * MIB)
        self.assertEqual(observed.available_bytes, 4 * 1024 * MIB)
        self.assertEqual(observed.source, "proc_meminfo+cgroup2")

    def test_mount_root_is_applied_to_membership(self):
        self.meminfo()
        mount = self.cgroup(membership="/parent/leaf", mount_root="/parent")
        self.values(mount, current=512 * MIB, maximum=4 * 1024 * MIB)
        self.values(mount / "leaf", current=256 * MIB, maximum=3 * 1024 * MIB)
        observed = host_memory_snapshot(proc_root=self.proc)
        self.assertEqual(observed.ceiling_bytes, 3 * 1024 * MIB)
        self.assertEqual(observed.available_bytes, 2816 * MIB)

    def test_unlimited_cgroup_root_leaves_proc_values_in_force(self):
        self.meminfo(total_mib=4096, available_mib=2048)
        mount = self.cgroup(membership="/")
        self.values(mount, current=3 * 1024 * MIB)
        observed = host_memory_snapshot(proc_root=self.proc)
        self.assertEqual(observed.ceiling_bytes, 4096 * MIB)
        self.assertEqual(observed.available_bytes, 2048 * MIB)
        self.assertEqual(observed.source, "proc_meminfo+cgroup2")

    def test_malformed_cgroup_limit_fails_closed(self):
        self.meminfo()
        mount = self.cgroup(membership="/")
        self.values(mount, current=0, maximum="invalid")
        observed = host_memory_snapshot(proc_root=self.proc)
        self.assertIsNone(observed.ceiling_bytes)
        self.assertIsNone(observed.available_bytes)
        self.assertEqual(observed.source, "cgroup2_metrics_malformed")

    def test_permission_error_reading_cgroup_limit_fails_closed(self):
        self.meminfo()
        mount = self.cgroup(membership="/")
        self.values(mount, current=0, maximum=4096 * MIB)
        denied = mount / "memory.max"
        original = Path.read_text

        def read_text(path, *args, **kwargs):
            if path == denied:
                raise PermissionError("denied")
            return original(path, *args, **kwargs)

        with patch.object(Path, "read_text", read_text):
            observed = host_memory_snapshot(proc_root=self.proc)
        self.assertIsNone(observed.ceiling_bytes)
        self.assertIsNone(observed.available_bytes)
        self.assertEqual(observed.source, "cgroup2_metrics_malformed")

    def test_known_v2_membership_without_resolvable_mount_fails_closed(self):
        self.meminfo()
        (self.proc / "self" / "cgroup").write_text("0::/limited\n")
        (self.proc / "self" / "mountinfo").write_text("")
        observed = host_memory_snapshot(proc_root=self.proc)
        self.assertIsNone(observed.ceiling_bytes)
        self.assertEqual(observed.source, "cgroup2_mount_unresolved")

    def test_detected_v1_memory_controller_fails_closed_explicitly(self):
        self.meminfo()
        (self.proc / "self" / "cgroup").write_text("5:cpu,memory:/limited\n")
        observed = host_memory_snapshot(proc_root=self.proc)
        self.assertIsNone(observed.ceiling_bytes)
        self.assertEqual(observed.source, "cgroup1_memory_unsupported")

    def test_finite_cgroup_still_works_when_proc_meminfo_is_malformed(self):
        (self.proc / "meminfo").write_text("MemTotal: unknown kB\n")
        mount = self.cgroup(membership="/")
        self.values(mount, current=1024 * MIB, maximum=4096 * MIB)
        observed = host_memory_snapshot(proc_root=self.proc)
        self.assertEqual(observed.ceiling_bytes, 4096 * MIB)
        self.assertEqual(observed.available_bytes, 3072 * MIB)
        self.assertEqual(observed.source, "cgroup2")


class MemoryPolicyTests(unittest.TestCase):
    def test_defaults_reserve_two_gib_per_heavy_worker_and_half_gib_for_host(self):
        policy = MemoryPolicy()
        self.assertEqual(policy.heavy_slot_bytes, 2048 * MIB)
        self.assertEqual(policy.host_guard_bytes, 512 * MIB)

    def test_environment_policy_uses_decimal_mib_without_persisted_inputs(self):
        policy = MemoryPolicy.from_env({
            "MODPORT_CODER_MEMORY_MIB": "3072",
            "MODPORT_MEMORY_RESERVE_MIB": "0",
        })
        self.assertEqual(policy.heavy_slot_bytes, 3072 * MIB)
        self.assertEqual(policy.host_guard_bytes, 0)

    def test_environment_policy_rejects_unsafe_or_zero_slot_values(self):
        for environment in (
            {"MODPORT_CODER_MEMORY_MIB": "0"},
            {"MODPORT_CODER_MEMORY_MIB": "-1"},
            {"MODPORT_CODER_MEMORY_MIB": "1.5"},
            {"MODPORT_MEMORY_RESERVE_MIB": " 1"},
        ):
            with self.subTest(environment=environment):
                with self.assertRaises(ValueError):
                    MemoryPolicy.from_env(environment)

    def test_active_reservations_reduce_total_and_available_capacity(self):
        policy = MemoryPolicy()
        snapshot = MemorySnapshot(10 * 1024 * MIB, 12 * 1024 * MIB, "fixture")
        decision = policy.decide(snapshot, hard_cap=5, active=2)
        self.assertEqual(decision.ceiling_capacity, 5)
        self.assertEqual(decision.available_capacity, 4)
        self.assertEqual(decision.starts, 2)
        self.assertEqual(decision.reason, "admitted")

    def test_hard_cap_remains_an_upper_bound(self):
        policy = MemoryPolicy(heavy_slot_bytes=MIB, host_guard_bytes=0)
        decision = policy.decide(
            MemorySnapshot(100 * MIB, 100 * MIB, "fixture"), hard_cap=3, active=2)
        self.assertEqual(decision.starts, 1)

    def test_unknown_metrics_fail_closed(self):
        decision = MemoryPolicy().decide(
            MemorySnapshot(None, None, "unavailable"), hard_cap=3, active=0)
        self.assertEqual(decision.starts, 0)
        self.assertEqual(decision.reason, "memory_metrics_unavailable")

    def test_pressure_subtracts_active_reservations_conservatively(self):
        policy = MemoryPolicy()
        snapshot = MemorySnapshot(5 * 1024 * MIB, 16 * 1024 * MIB, "fixture")
        decision = policy.decide(snapshot, hard_cap=8, active=2)
        self.assertEqual(decision.available_capacity, 2)
        self.assertEqual(decision.starts, 0)
        self.assertEqual(decision.reason, "memory_pressure")


if __name__ == "__main__":
    unittest.main()
