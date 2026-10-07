import os
from pathlib import Path
import tempfile
import unittest
from dispatcher_sdk.execution_kernel import ExecutionCommandV2, RetryPolicy
from modport.contracts import OperationInput
from modport.kernel_runtime import open_runtime
from modport.evidence import workspace_lock
from fixtures_modport import FixtureHandler, SlowFixture


@unittest.skipUnless(os.name == 'posix', 'ModPort production containment requires POSIX')
class ProcessRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def command(self, runtime, timeout=5):
        return ExecutionCommandV2(
            execution_id='process-source', idempotency_key='process-source',
            registry_revision=runtime.registry_revision, correlation_id='process', causation_id=None,
            handler_id='modport.source', handler_contract_version=1,
            retry_policy=RetryPolicy(max_attempts=3), timeout_seconds=timeout,
            payload=OperationInput('process', 'source', 'source', 'process-source', str(self.root)).to_dict())

    def test_queued_command_reopens_and_executes_in_real_process_isolation(self):
        handlers = {'modport.source': FixtureHandler()}
        with open_runtime(self.root, handlers=handlers) as runtime:
            runtime.submit(self.command(runtime))
        with open_runtime(self.root, handlers=handlers) as runtime:
            result = runtime.run_once()
            self.assertEqual(result.state, 'succeeded')
            self.assertEqual(result.result.value['status'], 'completed')
            self.assertTrue((self.root / 'artifacts/executions/process-source/receipt.json').is_file())
            self.assertIsNone(runtime.run_once())

    def test_timeout_kills_external_writer_and_keeps_effect_uncertain(self):
        with open_runtime(self.root, handlers={'modport.source': SlowFixture()}) as runtime:
            runtime.submit(self.command(runtime, timeout=1.5))
            result = runtime.run_once()
            self.assertEqual(result.state, 'recovery_required')
            self.assertIsNone(result.result)
            pid = int((self.root / 'child-pid').read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
            with workspace_lock(self.root, blocking=False):
                pass

    def test_production_skill_handlers_execute_through_spawn_supervisor(self):
        # Use the full production registry, not a fixture registry that hides
        # nested callables. Missing versions fail before any model/network call.
        with open_runtime(self.root) as runtime:
            for stage in ('platform_diff', 'platform_skill_review', 'java_diff', 'java_skill_review'):
                with self.subTest(stage=stage):
                    operation = OperationInput('process', stage, stage, stage, str(self.root))
                    command = runtime.command('modport.' + stage,
                        execution_id=stage, idempotency_key=stage, correlation_id='process',
                        timeout_seconds=10, payload=operation.to_dict())
                    runtime.submit(command)
                    result = runtime.run_once()
                    self.assertEqual(result.state, 'succeeded', result)
                    self.assertEqual(result.result.value['status'], 'blocked')
                    self.assertEqual(result.result.value['error_code'], 'skill_input_invalid')
