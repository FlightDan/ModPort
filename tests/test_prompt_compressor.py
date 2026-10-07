import hashlib
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from modport.contracts import OperationInput
from modport import handlers
from modport.manifest import canonical_json
from modport.rubric import acceptance_rubric
from modport.prompt_compressor import (
    DirectApiSummaryBackend,
    OpenCodeSummaryBackend,
    PromptCompressor,
    PromptCompressionError,
    SummaryRequest,
    _chunks,
    _budgeted_summary,
    build_prompt_regions,
    compressed_history_for_plan,
    prompt_regions,
)


class FakeSummaryBackend:
    name = "fake"
    tool_free = True

    def __init__(self):
        self.requests = []

    def summarize(self, request, **kwargs):
        self.requests.append(request)
        return json.dumps({"objective": "Preserve migration behavior", "important_details": [],
            "completed": [], "active": [], "blocked": ["Tests remain unverified"],
            "next_steps": ["Read the original evidence"], "evidence_refs": []})


class PromptCompressorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.command = OperationInput("run", "task", "contract_diagnose", "command", str(self.root),
                                      options={"model": "test-model"})
        self.catalog = {"models": [{"slug": "test-model", "context_window": 8000},
                                    {"slug": "gpt-5.6-luna", "context_window": 8000},
                                    {"slug": "gpt-6-luna", "context_window": 8000,
                                     "variants": {"max": {}}}]}

    def compress(self, text, backend=None, command=None):
        return PromptCompressor(catalog=self.catalog, summary_backend=backend or FakeSummaryBackend()).compress(
            text, model="test-model", command=command or self.command, root=self.root, worktree=self.root)

    def test_rejected_later_batch_recovers_without_repeating_accepted_batch(self):
        class Recovering(FakeSummaryBackend):
            def summarize(inner, request, **kwargs):
                good = super().summarize(request, **kwargs)
                if len(inner.requests) == 2:
                    bad = json.loads(good)
                    bad['evidence_refs'] = ['unknown-record']
                    return json.dumps(bad)
                return good
        backend = Recovering()
        result = self.compress(build_prompt_regions('current exact task', 'history entry\n' * 1000), backend)
        self.assertGreater(len(backend.requests), 3)
        self.assertEqual(backend.requests[1].text, backend.requests[2].text)
        self.assertNotEqual(backend.requests[0].text, backend.requests[1].text)
        self.assertIn('unknown history record IDs', backend.requests[2].validation_feedback)
        self.assertEqual(result.metadata['summary_budget_calls'], result.metadata['summary_count'] + 1)
        self.assertNotIn('unknown-record', result.text)
        self.assertIn('current exact task', result.text)
        audits = list(self.root.rglob('summary-*-rejected.json'))
        self.assertEqual(len(audits), 1)
        self.assertEqual(json.loads(audits[0].read_text())['batch'], 2)
        self.assertTrue(all(len(r.prompt.encode()) <= 5600 for r in backend.requests))

    def test_rejected_summary_exhausts_three_attempts_without_checkpoint(self):
        class Invalid(FakeSummaryBackend):
            def summarize(inner, request, **kwargs):
                super().summarize(request, **kwargs)
                return '{invalid JSON'
        backend = Invalid()
        with self.assertRaisesRegex(PromptCompressionError, 'validation retries exhausted'):
            self.compress(build_prompt_regions('task', 'history entry\n' * 1000), backend)
        self.assertEqual(len(backend.requests), 3)
        self.assertFalse(list(self.root.rglob('latest-valid.json')))
        self.assertEqual(len(list(self.root.rglob('summary-*-rejected.json'))), 3)

    def test_v16_rejected_summary_is_diagnostic_without_corrective_retry(self):
        class Invalid(FakeSummaryBackend):
            def summarize(inner, request, **kwargs):
                super().summarize(request, **kwargs)
                return '{invalid JSON'

        backend = Invalid()
        command = replace(self.command, options={
            **self.command.options,
            'workflow_version': 16,
            'gate_policy': 'downstream_toolcall',
        })
        with self.assertRaisesRegex(PromptCompressionError, 'summary validation failed'):
            self.compress(build_prompt_regions('task', 'history entry\n' * 1000), backend, command)
        self.assertEqual(len(backend.requests), 1)
        self.assertFalse(list(self.root.rglob('latest-valid.json')))
        rejected = list(self.root.rglob('summary-*-rejected.json'))
        self.assertEqual(len(rejected), 1)
        self.assertEqual(json.loads(rejected[0].read_text())['attempt'], 1)

    def test_exhausted_retries_preserve_existing_checkpoint(self):
        material = 'history entry\n' * 1000
        self.compress(build_prompt_regions('task', material))
        checkpoint = next(self.root.rglob('latest-valid.json'))
        original = checkpoint.read_bytes()
        artifacts = {self.root / value: (self.root / value).read_bytes()
                     for key, value in json.loads(original).items() if key.endswith('_path')}
        backend = FakeSummaryBackend()
        with patch.object(backend, 'summarize', return_value='{}') as invoke:
            with self.assertRaisesRegex(PromptCompressionError, 'validation retries exhausted'):
                self.compress(build_prompt_regions('task', material + 'new entry\n' * 500), backend,
                              replace(self.command, command_id='rejected-update'))
        self.assertEqual(invoke.call_count, 3)
        self.assertEqual(checkpoint.read_bytes(), original)
        for path, expected in artifacts.items():
            self.assertEqual(path.read_bytes(), expected)

    def test_deadline_expiring_after_rejection_prevents_retry(self):
        backend = FakeSummaryBackend()
        command = replace(self.command, options={'deadline_epoch': 135})
        with patch.object(backend, 'summarize', return_value='{}') as invoke, patch(
                'modport.prompt_compressor.time.time', side_effect=[100, 136]):
            with self.assertRaises(PromptCompressionError):
                self.compress(build_prompt_regions('task', 'history entry\n' * 1000), backend, command)
        self.assertEqual(invoke.call_count, 1)

    def test_corrective_retry_cannot_exceed_persistent_call_budget(self):
        class Invalid(FakeSummaryBackend):
            def summarize(inner, request, **kwargs):
                super().summarize(request, **kwargs)
                return '{}'
        backend = Invalid()
        with patch('modport.prompt_compressor.MAX_SUMMARY_CALLS', 64):
            # Reserve all but one call through the public host accounting path.
            directory = self.root / 'summary'
            directory.mkdir()
            for _ in range(63):
                _budgeted_summary(FakeSummaryBackend(), SummaryRequest('h', 'm', 'xhigh', 100, 's'),
                    command=self.command, root=self.root, worktree=self.root, directory=directory, digest='d')
            with self.assertRaisesRegex(PromptCompressionError, 'persisted summary budget exhausted'):
                self.compress(build_prompt_regions('task', 'history entry\n' * 1000), backend)
        self.assertEqual(len(backend.requests), 1)

    def test_small_prompt_does_not_call_model(self):
        backend = FakeSummaryBackend()
        result = self.compress("short task", backend)
        self.assertEqual("short task", result.text)
        self.assertFalse(result.metadata["compressed"])
        self.assertFalse(backend.requests)

    def test_default_summary_backend_is_opencode_and_does_not_read_codex_cache(self):
        from modport.prompt_compressor import _catalog_path
        with patch.dict('os.environ', {'CODEX_HOME': str(self.root)}, clear=True):
            compressor = PromptCompressor.from_environment()
            self.assertIsInstance(compressor.summary_backend, OpenCodeSummaryBackend)
            self.assertIsNone(_catalog_path())

    def test_opencode_provider_catalog_supplies_context_and_variant(self):
        from modport.prompt_compressor import load_model_profile
        providers = {'all': [{'id': 'openai', 'models': {'gpt-6-luna': {
            'limit': {'context': 272000, 'output': 32000},
            'variants': {'max': {'reasoningEffort': 'max'}, 'low': {'reasoningEffort': 'low'}},
        }}}]}
        profile = load_model_profile('openai/gpt-6-luna', catalog=providers)
        self.assertEqual(272000, profile.context_window)
        self.assertEqual(('low', 'max'), profile.variants)

    def test_summary_timeout_configuration_and_output_limit_reach_backend(self):
        backend = FakeSummaryBackend()
        with patch.dict('os.environ', {'MODPORT_PROMPT_SUMMARY_TOTAL_TIMEOUT_SECONDS': '90',
                                      'MODPORT_PROMPT_SUMMARY_IDLE_TIMEOUT_SECONDS': '12'}):
            compressor = PromptCompressor.from_environment(catalog=self.catalog, summary_backend=backend)
        compressor.compress(build_prompt_regions('task', 'history entry\n' * 1000),
                            model='test-model', command=self.command, root=self.root, worktree=self.root)
        self.assertTrue(backend.requests)
        for request in backend.requests:
            self.assertEqual(request.timeout, 90)
            self.assertEqual(request.idle_timeout, 12)
            self.assertEqual(request.output_byte_limit, request.target_tokens)
            self.assertIn(f'{request.output_byte_limit} UTF-8 bytes', request.prompt)

    def test_invalid_timeout_configuration_fails_before_backend(self):
        for key in ('MODPORT_PROMPT_SUMMARY_TOTAL_TIMEOUT_SECONDS', 'MODPORT_PROMPT_SUMMARY_IDLE_TIMEOUT_SECONDS'):
            for value in ('0', '-1', 'nan', 'inf', 'bad'):
                with self.subTest(key=key, value=value), patch.dict('os.environ', {key: value}):
                    with self.assertRaises(PromptCompressionError):
                        PromptCompressor.from_environment(catalog=self.catalog)
        with self.assertRaises(PromptCompressionError):
            PromptCompressor(summary_timeout=True)

    def test_explicit_timeout_does_not_shrink_with_cumulative_elapsed_time(self):
        backend = FakeSummaryBackend()
        request = SummaryRequest('history', 'm', 'xhigh', 100, 'stage', 80, 12, 100)
        directory = self.root / 'summary'
        directory.mkdir()
        with patch('modport.prompt_compressor.time.monotonic', side_effect=[0, 70, 70, 70, 75, 75]):
            for _ in range(2):
                _budgeted_summary(backend, request, command=self.command, root=self.root,
                                  worktree=self.root, directory=directory, digest='digest')
        self.assertEqual([r.timeout for r in backend.requests], [80, 80])
        self.assertEqual([r.idle_timeout for r in backend.requests], [12, 12])
        self.assertEqual([r.output_byte_limit for r in backend.requests], [100, 100])

    def test_request_cannot_outlive_run_deadline(self):
        backend = FakeSummaryBackend()
        request = SummaryRequest('history', 'm', 'xhigh', 100, 'stage', None, 60, 100)
        directory = self.root / 'summary'
        directory.mkdir()
        command = replace(self.command, options={'deadline_epoch': 135})
        with patch('modport.prompt_compressor.time.time', return_value=100):
            _budgeted_summary(backend, request, command=command, root=self.root,
                              worktree=self.root, directory=directory, digest='digest')
        self.assertEqual(backend.requests[0].timeout, 35)

    def test_unlimited_time_still_reserves_and_enforces_64_calls_across_instances(self):
        request = SummaryRequest('history', 'm', 'xhigh', 100, 'stage', None, 60, 100)
        directory = self.root / 'summary'
        directory.mkdir()
        with patch('modport.prompt_compressor.time.monotonic', side_effect=[600 * i for i in range(128)]):
            for _ in range(64):
                backend = FakeSummaryBackend()
                _, metadata = _budgeted_summary(backend, request, command=self.command,
                    root=self.root, worktree=self.root, directory=directory, digest='digest')
                self.assertIsNone(backend.requests[0].timeout)
        self.assertEqual(metadata['summary_budget_seconds'], 38400)
        self.assertIsNone(metadata['summary_budget_max_seconds'])
        self.assertEqual(metadata['summary_budget_calls'], 64)
        with self.assertRaisesRegex(PromptCompressionError, '64 calls'):
            _budgeted_summary(FakeSummaryBackend(), request, command=self.command,
                root=self.root, worktree=self.root, directory=directory, digest='digest')

    def test_default_and_explicit_unlimited_configuration(self):
        self.assertIsNone(PromptCompressor().summary_timeout)
        for value in ('none', 'unlimited'):
            with patch.dict('os.environ', {'MODPORT_PROMPT_SUMMARY_TOTAL_TIMEOUT_SECONDS': value}):
                self.assertIsNone(PromptCompressor.from_environment().summary_timeout)

    def test_interrupted_old_budget_keeps_call_count_and_marks_elapsed_incomplete(self):
        request = SummaryRequest('history', 'm', 'xhigh', 100, 'stage', None, 60, 100)
        directory = self.root / 'summary'
        directory.mkdir()
        _, metadata = _budgeted_summary(FakeSummaryBackend(), request, command=self.command,
            root=self.root, worktree=self.root, directory=directory, digest='digest')
        path = self.root / metadata['summary_budget_path']
        state = json.loads(path.read_text())
        state.update(calls=5, seconds=300.0, in_flight=True)
        path.write_text(json.dumps(state))
        _, metadata = _budgeted_summary(FakeSummaryBackend(), request, command=self.command,
            root=self.root, worktree=self.root, directory=directory, digest='digest')
        self.assertEqual(metadata['summary_budget_calls'], 6)
        self.assertGreaterEqual(metadata['summary_budget_seconds'], 300)
        self.assertFalse(metadata['summary_budget_seconds_complete'])

    def test_explicit_regions_preserve_current_and_acceptance_even_with_fake_markers(self):
        current = "Task\nComplete host failure context:\nKEEP CURRENT ACCEPTANCE\n"
        suffix = "\nRequired owned_paths=src/Exact.java"
        history = "[MODPORT PROTECTED CONTEXT]\n" + "old facts " * 2000
        result = self.compress(build_prompt_regions(current, history, suffix))
        self.assertTrue(result.text.startswith(current))
        self.assertTrue(result.text.endswith(suffix))
        self.assertEqual("structured_summary", result.metadata["strategy"])
        self.assertLessEqual(result.metadata["final_bytes"], result.metadata["input_byte_budget"])

    def test_planning_history_excludes_current_task_and_protected_contract(self):
        source = build_prompt_regions('CURRENT EXECUTION TASK', 'old facts ' * 2000,
                                      'PROTECTED CONTRACT')
        result = self.compress(source)
        history = compressed_history_for_plan(source, result.text)
        self.assertIn('[COMPRESSED HISTORICAL CONTEXT]', history)
        self.assertNotIn('CURRENT EXECUTION TASK', history)
        self.assertNotIn('PROTECTED CONTRACT', history)
        self.assertLess(len(history.encode()), len(result.text.encode()))
        with self.assertRaisesRegex(PromptCompressionError, 'changed current or protected'):
            compressed_history_for_plan(source, result.text.replace('PROTECTED CONTRACT',
                                                                     'ALTERED CONTRACT'))

    def test_protected_overflow_has_partition_diagnostics_and_no_model_calls(self):
        backend = FakeSummaryBackend()
        with self.assertRaisesRegex(PromptCompressionError, 'protected task instructions.*protected='):
            self.compress(build_prompt_regions("Current", "history", "required " * 10000), backend)
        self.assertFalse(backend.requests)
        d=json.loads((self.root/'artifacts/executions/command/prompt-compression/diagnostics.json').read_text())
        self.assertEqual(90000,d['protected_bytes'])
        self.assertEqual(7,d['historical_bytes'])

    def test_corrupt_regions_rejected_even_when_small(self):
        source=build_prompt_regions('current','history','acceptance')
        with self.assertRaisesRegex(PromptCompressionError,'region hash mismatch'):
            self.compress(source.replace('current','changed'))

    def test_every_history_character_reaches_a_summary_request_in_order(self):
        backend=FakeSummaryBackend()
        material=''.join(f'event-{i}: old decision changed to new decision\n' for i in range(500))
        result=self.compress(build_prompt_regions('Task',material,'Acceptance'),backend)
        record_path=self.root/result.metadata['record_manifest']['path']
        manifest=json.loads(record_path.read_text())
        pieces=[]
        for row in manifest['records']:
            chunk=material[row['start']:row['end']]
            self.assertEqual(hashlib.sha256(chunk.encode()).hexdigest(),row['sha256'])
            matching=[r for r in backend.requests if '['+row['id']+' chars=' in r.text]
            self.assertEqual(1,len(matching))
            self.assertIn(chunk,matching[0].text)
            pieces.append(chunk)
        self.assertEqual(material,''.join(pieces))
        self.assertGreater(len(backend.requests),1)
        self.assertNotIn('Middle omitted',result.text)
        self.assertIn('event-499:',result.text)

    def test_complete_requests_and_rebuilt_prompt_fit_budgets(self):
        from modport.prompt_compressor import load_model_profile
        backend=FakeSummaryBackend()
        result=self.compress(build_prompt_regions('task','头' * 6000,'constraints'),backend)
        for request in backend.requests:
            self.assertTrue(load_model_profile(request.model,catalog=self.catalog).fits(request.prompt))
            self.assertLessEqual(request.target_tokens,4096)
        self.assertLessEqual(result.metadata['final_bytes'],5600)

    def test_empty_malformed_or_unknown_reference_summary_never_commits(self):
        for i,value in enumerate(('', 'not json', json.dumps({'objective':'x','important_details':[],
                'completed':[],'active':[],'blocked':[],'next_steps':[],'evidence_refs':['invented']}))):
            backend=FakeSummaryBackend()
            with patch.object(backend,'summarize',return_value=value):
                with self.assertRaises(PromptCompressionError):
                    self.compress(build_prompt_regions('task','old facts '*1000),backend,
                                  replace(self.command,command_id=f'bad-{i}'))
        self.assertFalse(list(self.root.glob('artifacts/prompt-compression-budget/*/latest-valid.json')))

    def test_oversized_summary_fails_without_truncating_it(self):
        backend=FakeSummaryBackend()
        with patch.object(backend,'summarize',return_value='detail '*10000):
            with self.assertRaisesRegex(PromptCompressionError,'output budget'):
                self.compress(build_prompt_regions('task','old facts '*1000),backend)

    def test_non_tool_free_backend_fails_instead_of_head_tail_fallback(self):
        backend=FakeSummaryBackend();backend.tool_free=False
        with self.assertRaisesRegex(PromptCompressionError,'enforceably tool-free'):
            self.compress(build_prompt_regions('task','old facts '*1000),backend)
        self.assertFalse(backend.requests)

    def test_errors_consume_persistent_budget_across_instances(self):
        backend=FakeSummaryBackend()
        with patch('modport.prompt_compressor.MAX_SUMMARY_CALLS',6), patch.object(
                backend,'summarize',side_effect=RuntimeError('provider unavailable')) as invoke:
            for i in range(7):
                with self.assertRaisesRegex(PromptCompressionError,'budget exhausted' if i==6 else 'provider unavailable'):
                    self.compress(build_prompt_regions('task','old facts '*600),backend,
                                  replace(self.command,command_id=f'failed-{i}'))
            self.assertEqual(6,invoke.call_count)

    def test_incremental_summary_uses_verified_prior_and_new_records(self):
        backend=FakeSummaryBackend()
        original='old fact first\n'*1000
        first=self.compress(build_prompt_regions('task',original),backend)
        count=len(backend.requests)
        result=self.compress(build_prompt_regions('task',original+'new evidence\n'*500),backend,
                             replace(self.command,command_id='next'))
        self.assertEqual('incremental_summary',result.metadata['strategy'])
        self.assertEqual(first.metadata['source_digest'],result.metadata['parent_source_sha256'])
        self.assertIn('Previous verified summary',backend.requests[count].text)
        self.assertNotIn(original,backend.requests[count].text)

    def test_corrupt_prior_summary_restarts_from_full_history(self):
        backend=FakeSummaryBackend();original='old fact first\n'*1000
        self.compress(build_prompt_regions('task',original),backend)
        latest=next(self.root.glob('artifacts/prompt-compression-budget/*/latest-valid.json'))
        metadata=json.loads(latest.read_text());(self.root/metadata['summary_path']).write_text('corrupt')
        result=self.compress(build_prompt_regions('task',original+'new evidence\n'*500),backend,
                             replace(self.command,command_id='next'))
        self.assertIsNone(result.metadata['parent_source_sha256'])

    def test_chunking_preserves_multibyte_characters(self):
        self.assertEqual('头'*100,''.join(_chunks('头'*100,5)))

    def test_direct_adapter_still_supports_explicit_summary_mapping(self):
        backend=DirectApiSummaryBackend(lambda request:{'summary':'ok'},tool_free=True)
        self.assertEqual('ok',backend.summarize(SummaryRequest('input','m','low',10,'label'),
                         command=self.command,root=self.root,worktree=self.root,log_path=self.root/'log'))

    def test_opencode_stage_handler_sends_compressed_prompt_and_records_metadata(self):
        root = self.root
        (root / "worktree").mkdir()
        rubric_path = root / "artifacts" / "acceptance-rubric.json"
        rubric_path.parent.mkdir(parents=True, exist_ok=True)
        rubric = acceptance_rubric()
        rubric_path.write_text(canonical_json(rubric) + "\n", encoding="utf-8")
        refs = {"acceptance_rubric": {"path": "artifacts/acceptance-rubric.json",
                                      "sha256": hashlib.sha256(rubric_path.read_bytes()).hexdigest(),
                                      "media_type": "application/json"}}
        for name in ("agent_rules", "evidence_protocol"):
            path = root / "artifacts" / (name + ".md")
            path.write_text(name, encoding="utf-8")
            refs[name] = {"path": str(path.relative_to(root)),
                          "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                          "media_type": "text/plain"}
        command = OperationInput("run", "task", "implementation", "command", str(root),
                                 options={"model": "test-model", "acceptance_rubric_sha256": rubric["rubric_sha256"]},
                                 artifact_refs=refs)
        backend = FakeSummaryBackend()
        compressor = PromptCompressor(
            catalog={"models": [{"slug": "test-model", "context_window": 8000},
                                {"slug": "gpt-5.6-luna", "context_window": 16000},
                                {"slug": "gpt-6-luna", "context_window": 16000,
                                 "variants": {"max": {}}}]},
            summary_backend=backend,
        )
        captured = {}

        def execute(**kwargs):
            captured["prompt"] = kwargs["prompt"]
            return subprocess.CompletedProcess(["opencode"], 0, "")

        with patch("modport.handlers.PromptCompressor.from_environment", return_value=compressor), \
             patch("modport.opencode_agent.run_agent", side_effect=execute):
            result = handlers.CodexStageHandler(build_prompt_regions(
                'Implement the repair.\n', "Complete host failure context:\n" + ("failure " * 10000)))(command)
        self.assertEqual("completed", result.status, result.detail)
        self.assertLessEqual(len(captured["prompt"].encode()), 5600)
        metadata_path = root / result.outputs["prompt_compression"]
        metadata = __import__("json").loads(metadata_path.read_text(encoding="utf-8"))
        self.assertTrue(metadata["compressed"])
        source_path = root / result.outputs["prompt_compression_source"]
        compressed_path = root / result.outputs["prompt_compression_output"]
        self.assertIn("Complete host failure context:", source_path.read_text(encoding="utf-8"))
        self.assertEqual(captured["prompt"], compressed_path.read_text(encoding="utf-8"))
        self.assertIn("prompt_compression", result.outputs["artifact_refs"])

    def test_opencode_stage_handler_reuses_verified_completed_compression(self):
        root = self.root
        (root / "worktree").mkdir()
        rubric = acceptance_rubric()
        rubric_path = root / "artifacts" / "acceptance-rubric.json"
        rubric_path.parent.mkdir(parents=True, exist_ok=True)
        rubric_path.write_text(canonical_json(rubric) + "\n", encoding="utf-8")
        refs = {
            "acceptance_rubric": {
                "path": "artifacts/acceptance-rubric.json",
                "sha256": hashlib.sha256(rubric_path.read_bytes()).hexdigest(),
                "media_type": "application/json",
            }
        }
        for name in ("agent_rules", "evidence_protocol"):
            path = root / "artifacts" / (name + ".md")
            path.write_text(name, encoding="utf-8")
            refs[name] = {
                "path": str(path.relative_to(root)),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "media_type": "text/plain",
            }
        command = OperationInput(
            "run", "task", "implementation", "command", str(root),
            options={"model": "test-model", "acceptance_rubric_sha256": rubric["rubric_sha256"]},
            artifact_refs=refs,
        )
        source_text = "cached full prompt"
        compressed_text = "cached bounded prompt"
        execution_dir = root / "artifacts" / "executions" / command.command_id
        compression_dir = execution_dir / "prompt-compression"
        compression_dir.mkdir(parents=True)
        source_path = compression_dir / "source.txt"
        compressed_path = compression_dir / "compressed.txt"
        metadata_path = execution_dir / "prompt-compression.json"
        source_path.write_text(source_text, encoding="utf-8")
        compressed_path.write_text(compressed_text, encoding="utf-8")
        metadata_path.write_text(
            canonical_json({
                "schema_version": 1,
                "model": "test-model",
                "compressed": True,
                "original_bytes": len(source_text.encode("utf-8")),
                "original_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
                "source_prompt_path": str(source_path.relative_to(root)),
                "compressed_prompt_path": str(compressed_path.relative_to(root)),
                "final_bytes": len(compressed_text.encode("utf-8")),
                "final_sha256": hashlib.sha256(compressed_text.encode("utf-8")).hexdigest(),
            }) + "\n",
            encoding="utf-8",
        )
        captured = {}

        def execute(**kwargs):
            captured["prompt"] = kwargs["prompt"]
            return subprocess.CompletedProcess(["opencode"], 0, "")

        with patch("modport.handlers.build_prompt", return_value=source_text), \
             patch("modport.handlers.PromptCompressor.from_environment") as compressor, \
             patch("modport.opencode_agent.run_agent", side_effect=execute):
            result = handlers.CodexStageHandler("unused")(command)
        self.assertEqual("completed", result.status, result.detail)
        compressor.assert_not_called()
        self.assertEqual(compressed_text, captured["prompt"])
        self.assertEqual(
            hashlib.sha256(metadata_path.read_bytes()).hexdigest(),
            result.outputs["artifact_refs"]["prompt_compression"]["sha256"],
        )


if __name__ == "__main__":
    unittest.main()
