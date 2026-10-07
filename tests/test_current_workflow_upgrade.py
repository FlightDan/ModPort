"""Only the host-selected predecessor can enter the current workflow."""
from copy import deepcopy
import os
import unittest
from unittest.mock import patch

from modport.models import Budget, MigrationRequest
from modport.workflow import WORKFLOW_VERSION, WorkflowDefinition
from modport import workflow_upgrade
from modport.workflow_upgrade import validate_upgrade_definition


TEST_PREDECESSOR_RUN_ID = 'selected-predecessor'


class CurrentWorkflowUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.selection = patch.object(workflow_upgrade, 'UPGRADE_SOURCE_RUN_ID', TEST_PREDECESSOR_RUN_ID)
        self.selection.start()
        self.addCleanup(self.selection.stop)

    def header(self, version=40):
        request = MigrationRequest('fixture', 'https://example.invalid/fixture.git',
            '1.21', '26.1', source_loader='neoforge', budget=Budget(max_seconds=86400))
        return {'run_id': TEST_PREDECESSOR_RUN_ID, 'request': request.to_dict(),
                'definition': WorkflowDefinition(request.to_dict(), version=version).to_dict()}

    def test_selected_predecessor_upgrades_without_changing_original(self):
        header = self.header()
        original = deepcopy(header)
        current = validate_upgrade_definition(header)
        self.assertEqual(WORKFLOW_VERSION, current['workflow_version'])
        self.assertEqual(original, header)

    def test_peers_and_modified_predecessors_remain_unsupported(self):
        peer = self.header()
        peer['run_id'] = 'another-instance'
        with self.assertRaisesRegex(ValueError, 'explicitly carried'):
            validate_upgrade_definition(peer)
        changed = self.header()
        changed['definition']['stages'] = []
        with self.assertRaisesRegex(ValueError, 'exact supported'):
            validate_upgrade_definition(changed)

    def test_current_workflow_can_continue_its_own_frozen_definition(self):
        header = self.header(WORKFLOW_VERSION)
        header['run_id'] = 'current-instance'
        self.assertEqual(header['definition'], validate_upgrade_definition(header))

    def test_no_selected_predecessor_rejects_old_runs_but_allows_current(self):
        with patch.object(workflow_upgrade, 'UPGRADE_SOURCE_RUN_ID', None):
            for identifier in (TEST_PREDECESSOR_RUN_ID, None):
                header = self.header()
                header['run_id'] = identifier
                with self.assertRaisesRegex(ValueError, 'explicitly carried'):
                    validate_upgrade_definition(header)
            current = self.header(WORKFLOW_VERSION)
            self.assertEqual(current['definition'], validate_upgrade_definition(current))

    def test_current_optional_wiki_omission_preserves_default_semantics(self):
        header = self.header(WORKFLOW_VERSION)
        header.pop('run_id')
        header['request'].pop('wiki_enabled')
        header['definition']['request'].pop('wiki_enabled')
        with patch.object(workflow_upgrade, 'UPGRADE_SOURCE_RUN_ID', None):
            self.assertEqual(header['definition'], validate_upgrade_definition(header))

    def test_carried_successor_preserves_absent_optional_wiki_setting(self):
        header = self.header()
        header['logical_run_id'] = 'earlier-historical-segment'
        header['request'].pop('wiki_enabled')
        header['definition']['request'].pop('wiki_enabled')
        original = deepcopy(header)
        current = validate_upgrade_definition(header)
        self.assertEqual(WORKFLOW_VERSION, current['workflow_version'])
        self.assertNotIn('wiki_enabled', current['request'])
        self.assertEqual(original, header)
        successor = {**header, 'run_id': 'current-successor', 'definition': current}
        self.assertEqual(current, validate_upgrade_definition(successor))
        header['run_id'] = 'retired-peer'
        with self.assertRaisesRegex(ValueError, 'not canonical'):
            validate_upgrade_definition(header)

    def test_older_workflows_are_not_supported_by_current_upgrade(self):
        with self.assertRaisesRegex(ValueError, 'exact supported'):
            validate_upgrade_definition(self.header(38))


class PrivateUpgradePolicyTests(unittest.TestCase):
    def select(self):
        return workflow_upgrade._selected_upgrade_source_run_id()

    def test_environment_takes_precedence_without_reading_private_file(self):
        with patch.dict(os.environ, {'MODPORT_CARRIED_RUN_ID': TEST_PREDECESSOR_RUN_ID}), \
                patch.object(workflow_upgrade.Path, 'read_text') as read:
            self.assertEqual(TEST_PREDECESSOR_RUN_ID, self.select())
        read.assert_not_called()

    def test_absent_environment_and_private_file_select_no_predecessor(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(workflow_upgrade.Path, 'read_text', side_effect=FileNotFoundError):
            self.assertIsNone(self.select())

    def test_private_json_selects_predecessor_at_project_root(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(workflow_upgrade.Path, 'read_text', autospec=True,
                             return_value='{"carried_run_id":"selected-predecessor"}') as read:
            self.assertEqual(TEST_PREDECESSOR_RUN_ID, self.select())
        self.assertEqual(workflow_upgrade.Path(workflow_upgrade.__file__).resolve().parents[2]
                         / 'private-upgrade-policy.json', read.call_args.args[0])

    def test_private_json_rejects_invalid_shape_duplicates_and_identity(self):
        for text in ('invalid', 'null', '[]', '{}',
                     '{"carried_run_id":"one","extra":true}',
                     '{"carried_run_id":"one","carried_run_id":"two"}',
                     '{"carried_run_id":null}', '{"carried_run_id":1}',
                     '{"carried_run_id":"../private"}', '{"carried_run_id":""}'):
            with self.subTest(text=text), patch.dict(os.environ, {}, clear=True), \
                    patch.object(workflow_upgrade.Path, 'read_text', return_value=text):
                with self.assertRaises(ValueError):
                    self.select()

    def test_invalid_environment_does_not_fall_back_to_private_file(self):
        for identifier in ('', '.', '..', '../private', 'with space', 'with/slash'):
            with self.subTest(identifier=identifier), \
                    patch.dict(os.environ, {'MODPORT_CARRIED_RUN_ID': identifier}), \
                    patch.object(workflow_upgrade.Path, 'read_text') as read:
                with self.assertRaises(ValueError):
                    self.select()
            read.assert_not_called()
