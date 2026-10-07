"""Continuation rework targets stream large packed source catalogs."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from modport.contracts import OperationInput, OperationResult
from modport.evidence import atomic_json, file_digest
from modport.payload_storage import INLINE_LIMIT, pack_input
from modport.rework_tools import rework_targets


class ReworkSourcesStreamingTests(TestCase):
    def test_catalog_is_streamed_and_retains_only_wanted_author_closure(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            segment = 'segment-two'
            options = {'workflow_version': 17, 'business_gates_disabled': True}

            author = OperationInput(
                'logical', 'coder.one', 'coder', 'coder.one:1', directory,
                options=options,
                payload={'development_task': {'objective': 'Repair source'}},
            )
            child = OperationInput(
                'logical', 'agent_rework.one', 'coder', 'agent_rework.one:1',
                directory, options=options,
                payload={'reviewer_rework': {
                    'source_execution_id': author.command_id,
                }},
            )
            unrelated = OperationInput(
                'logical', 'coder.unused', 'coder', 'coder.unused:1', directory,
                options=options, payload={'large': 'x' * INLINE_LIMIT},
            )

            def row(command):
                return {
                    'target_agent': command.task_id,
                    'execution_id': command.command_id,
                    'task_id': command.task_id,
                    'stage_id': command.stage_id,
                    'terminal_state': 'succeeded',
                    'operation': pack_input(root, command.to_dict()),
                    'result_identity': {
                        name: getattr(command, name)
                        for name in ('run_id', 'task_id', 'stage_id', 'command_id')
                    },
                }

            metadata = {
                'continuation_rework_sources': True,
                'schema_version': 1,
                'previous_run_id': 'segment-one',
                'previous_revision': 9,
                'previous_generation': 0,
                'next_run_id': segment,
            }
            catalog = root / 'artifacts' / 'continuations' / segment / 'rework-sources.json'
            atomic_json(catalog, {
                'schema_version': 1,
                'previous_run_id': 'segment-one',
                'previous_revision': 9,
                'previous_generation': 0,
                'next_run_id': segment,
                # Put the original first so discovering it from the child
                # requires the archive reader's bounded second pass.
                'sources': [row(author), row(unrelated), row(child)],
            })
            ref = {
                'path': catalog.relative_to(root).as_posix(),
                'sha256': file_digest(catalog),
                'metadata': metadata,
            }
            atomic_json(root / 'run.json', {
                'run_id': segment,
                'logical_run_id': 'logical',
                'continuation': {
                    'previous_run_id': 'segment-one',
                    'support_refs': {'continuation:rework_sources': ref},
                },
            })
            result = OperationResult(
                'failed', 'logical', child.task_id, child.stage_id,
                child.command_id, error_code='review_failed',
            )
            caller = OperationInput(
                'logical', 'code_review', 'code_review', 'review:1', directory,
                options=options,
                upstream_results={'failed_child': result.to_dict()},
                artifact_refs={'continuation:rework_sources': ref},
            )

            original_read_text = Path.read_text

            def reject_whole_catalog(path, *args, **kwargs):
                if path == catalog:
                    raise AssertionError('catalog must be streamed')
                return original_read_text(path, *args, **kwargs)

            with patch.object(Path, 'read_text', reject_whole_catalog):
                targets = rework_targets({'run_id': segment, 'tasks': {}}, caller)

            self.assertEqual(1, len(targets))
            self.assertEqual('coder.one', targets[0]['target_agent'])
            self.assertEqual(author.command_id, targets[0]['execution_id'])
            self.assertNotIn('coder.unused', {target['target_agent'] for target in targets})
