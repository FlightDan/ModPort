"""Exact-version selection, optional failure, offline packs and real host handoff."""
from http.client import IncompleteRead
from urllib.error import HTTPError
import json
from pathlib import Path
import tempfile
import time
import subprocess
import unittest
from unittest.mock import patch

from modport import wiki_knowledge as wiki
from modport.contracts import OperationInput, OperationResult
from modport.desktop_service import DesktopApplication
from modport.evidence import atomic_json
from modport.kernel_runtime import open_runtime
from modport.opencode_shell_mcp import _read_run_artifact, prepare_sandbox_tool
from modport.prompts import STAGE_PROMPTS, build_prompt
from modport.workflow import WORKFLOW_VERSION


def page(identifier='java-17-25', target='25'):
    return {'schema_version': 1, 'id': identifier, 'kind': 'java',
        'source': {'java': '17'}, 'target': {'java': target}, 'entries': [{
            'id': 'java.versioned', 'category': 'api', 'summary': 'Documented version change',
            'applicability': 'Exact JDK versions only', 'migration': 'Read the versioned declarations',
            'compat': 'Use a version-specific adapter', 'verification': 'Source evidence; runtime unverified',
            'evidence': [{'source': 'https://openjdk.org/projects/jdk/25/',
                          'locator': 'JDK 25 source', 'supports': 'Version-specific applicability'}]}]}


def index(*pages):
    return {'schema_version': 1, 'entries': [{key: value[key] for key in
        ('id', 'kind', 'source', 'target')} | {'path': 'java/' + value['id'] + '.json'} for value in pages]}


class WikiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = self.root / 'cache'
        self.run = self.root / 'run'
        self.run.mkdir()
        self.identity = {'java': {'source': {'java': '17'}, 'target': {'java': '25'}}}

    def reader(self, value=None):
        value = value or page()
        def read(url, **kwargs):
            if '/commits/' in url:
                return json.dumps({'sha': 'repository-revision-one'})
            if url.endswith('index.json'):
                return json.dumps(index(value, page('java-17-21', '21')))
            return json.dumps(page('java-17-21', '21') if url.endswith('java-17-21.json') else value)
        return read

    def test_exact_selection_is_saved_and_revision_stays_fixed(self):
        refs = wiki.prepare_references(self.run, self.identity, revision='main', reader=self.reader(), cache_root=self.cache)
        self.assertEqual({'wiki:java', 'wiki:page:java-17-25'}, set(refs))
        selected = json.loads((self.run / refs['wiki:java']['path']).read_text())
        self.assertEqual('repository-revision-one', selected['revision'])
        self.assertEqual(1, selected['matching_entries'])
        with patch.object(wiki, 'read_remote', side_effect=AssertionError('Frozen selection must not refetch')):
            self.assertEqual(refs, wiki.prepare_references(self.run, self.identity, revision='next', cache_root=self.cache))

    def test_partial_http_response_never_blocks_and_is_recorded(self):
        def broken(*args, **kwargs):
            raise IncompleteRead(b'', 10)
        refs = wiki.prepare_references(self.run, self.identity, reader=broken, cache_root=self.cache)
        selection = json.loads((self.run / refs['wiki:java']['path']).read_text())
        self.assertEqual('unavailable', selection['status'])
        self.assertIn('IncompleteRead', selection['reason'])

    def test_page_version_mismatch_is_a_diagnostic_not_reusable_knowledge(self):
        read = self.reader()
        def mismatch(url, **kwargs):
            if url.endswith('java-17-25.json'):
                return json.dumps(page(target='26'))
            return read(url, **kwargs)
        refs = wiki.prepare_references(self.run, self.identity, revision='main', reader=mismatch, cache_root=self.cache)
        self.assertNotIn('wiki:page:java-17-25', refs)
        selected = json.loads((self.run / refs['wiki:java']['path']).read_text())
        self.assertIn('selected version pair', selected['diagnostics'][0]['detail'].replace('indexed', 'selected'))

    def test_update_caches_body_for_new_offline_run(self):
        wiki.refresh_cache(revision='main', cache_root=self.cache, reader=self.reader())
        refs = wiki.prepare_references(self.run, self.identity, offline=True, cache_root=self.cache)
        self.assertIn('wiki:page:java-17-25', refs)
        self.assertTrue(json.loads((self.run / refs['wiki:java']['path']).read_text())['cache_used'])

    def test_pack_import_distinguishes_revision_names_and_rejects_extra_files(self):
        from zipfile import ZipFile
        library = self.root / 'library'
        atomic_json(library / 'java' / 'research.json', page())
        first, second = self.root / 'first.zip', self.root / 'second.zip'
        wiki.build_pack(library, first, revision='release/v1')
        wiki.build_pack(library, second, revision='release_v1')
        wiki.import_pack(first, cache_root=self.cache)
        first_pointer = json.loads((self.cache / 'current.json').read_text())['path']
        wiki.import_pack(second, cache_root=self.cache)
        current = json.loads((self.cache / 'current.json').read_text())
        self.assertNotEqual(first_pointer, current['path'])
        refs = wiki.prepare_references(self.run, self.identity, revision='release_v1', offline=True, cache_root=self.cache)
        self.assertIn('wiki:page:java-17-25', refs)
        with ZipFile(second, 'a') as archive:
            archive.writestr('../private.json', '{}')
        with self.assertRaisesRegex(ValueError, 'undeclared'):
            wiki.import_pack(second, cache_root=self.cache)

    def test_optional_findings_export_replays_preserving_edits_and_colon_id(self):
        command = OperationInput('run', 'background', 'background', 'run:background:1', str(self.run),
            payload={'source_java': '17', 'target_java': '25'})
        workspace = self.run / 'baseline'
        atomic_json(workspace / wiki.finding_path(command), {'java': page()['entries']})
        found, diagnostics = wiki.export_findings(command, workspace=workspace, store_root=self.root / 'app')
        self.assertFalse(diagnostics)
        self.assertEqual(1, len(found))
        from modport.wiki_contributions import ContributionStore
        store = ContributionStore(self.root / 'app')
        store.update(found[0]['id'], {'title': 'User-edited title'})
        again, _ = wiki.export_findings(command, workspace=workspace, store_root=self.root / 'app')
        self.assertEqual('User-edited title', again[0]['title'])

    def test_desktop_actual_routes_accept_created_uuid_and_reject_hidden_fields(self):
        app = DesktopApplication(self.root / 'app')
        self.addCleanup(app.github.close)
        draft = app.wiki_contributions.create('java', {'java': '17'}, {'java': '25'}, page()['entries'])
        route = '/api/wiki/contributions/' + draft['id']
        preview = app.request('GET', '/api/wiki/contributions')['drafts'][0]
        self.assertEqual(draft['id'], preview['id'])
        self.assertNotIn('content', preview)
        self.assertNotIn('files', preview)
        self.assertEqual(draft, app.request('GET', route))
        changed = app.request('POST', route, {'title': 'Reviewed title'})
        self.assertEqual('Reviewed title', changed['title'])
        with self.assertRaises(ValueError):
            app.request('POST', route, {'origin': {'run_id': 'changed'}})
        with self.assertRaises(ValueError):
            app.request('POST', '/api/github/login', {'token': 'not-accepted'})

    def test_large_or_nested_optional_findings_become_diagnostics(self):
        command = OperationInput('run', 'background', 'background', 'run:background:1', str(self.run),
            payload={'source_java': '17', 'target_java': '25'})
        workspace = self.run / 'baseline'
        path = workspace / wiki.finding_path(command)
        path.parent.mkdir(parents=True)
        for raw in ('[' * 1500 + '0' + ']' * 1500, ' ' * (wiki.MAX_DOCUMENT + 1)):
            path.write_text(raw)
            drafts, diagnostics = wiki.export_findings(command, workspace=workspace, store_root=self.root / 'app')
            self.assertFalse(drafts)
            self.assertTrue(diagnostics)

    def test_slow_download_is_bounded_and_optional(self):
        import subprocess
        with patch.object(wiki.subprocess, 'run', side_effect=subprocess.TimeoutExpired('download', 0.1)):
            refs = wiki.prepare_references(self.run, self.identity, cache_root=self.cache)
        selected = json.loads((self.run / refs['wiki:java']['path']).read_text())
        self.assertEqual('unavailable', selected['status'])
        self.assertIn('time limit', selected['reason'])

    def test_locked_version_conflicts_do_not_select_a_different_pair(self):
        atomic_json(self.run / 'artifacts/locked-manifest.json', {'java_version': 25})
        command = OperationInput('run', 'background', 'background', 'run:background:1', str(self.run),
            payload={'source_java': '17', 'target_java': '26'})
        identities, _ = wiki.identities_for(command)
        self.assertNotIn('java', identities)

    def test_failed_update_preserves_previous_offline_research(self):
        wiki.refresh_cache(revision='main', cache_root=self.cache, reader=self.reader())
        original = (self.cache / 'current.json').read_text()
        def broken(url, **kwargs):
            if url.endswith('java-17-25.json'):
                raise OSError('page download interrupted')
            return self.reader()(url, **kwargs)
        with self.assertRaises(OSError):
            wiki.refresh_cache(revision='main', cache_root=self.cache, reader=broken)
        self.assertEqual(original, (self.cache / 'current.json').read_text())
        refs = wiki.prepare_references(self.run, self.identity, offline=True, cache_root=self.cache)
        self.assertIn('wiki:page:java-17-25', refs)

    def test_bad_optional_kind_does_not_hide_a_good_export(self):
        command = OperationInput('run', 'background', 'background', 'run:background:1', str(self.run),
            payload={'source_java': '17', 'target_java': '25', 'source_minecraft': '1.20.1',
                'source_loader_version': '47.3.0', 'target_minecraft': '26.1.2',
                'target_loader_version': '26.1.2.0'})
        drafts, diagnostics = wiki.export_findings(command,
            entries={'platform': [{}], 'java': page()['entries']}, store_root=self.root / 'app')
        self.assertEqual(1, len(drafts))
        self.assertTrue(diagnostics[0].startswith('platform:'))

    def test_rate_limited_metadata_uses_public_transport_without_rest_retry(self):
        calls = []
        def reader(url, **kwargs):
            calls.append(url)
            if url.startswith('https://api.github.com/'):
                raise HTTPError(url, 403, 'Anonymous rate limit exhausted', None, None)
            self.assertIn('/server-revision/index.json', url)
            return json.dumps(index())
        with patch.object(wiki, 'public_revision', return_value={'requested_revision': 'main',
            'revision': 'server-revision', 'metadata_transport': 'public_git'}) as resolver:
            state = wiki._fetch_library(self.root / 'library', reader=reader)
        self.assertEqual(1, sum(url.startswith('https://api.github.com/') for url in calls))
        resolver.assert_called_once()
        self.assertEqual('public_git', state['metadata_transport'])
        self.assertIn('403', state['metadata_diagnostic'])

    def test_public_transport_selects_peeled_release_tag_and_rejects_other_hosts(self):
        def packet(text):
            return f'{len(text.encode("utf-8")) + 4:04x}' + text
        advertisement = packet('# service=git-upload-pack\n') + '0000' + packet(
            'tag-object refs/tags/research-v1\n') + packet(
            'server-commit refs/tags/research-v1^{}\n') + packet('main-commit refs/heads/main\n') + '0000'
        location = wiki.REPOSITORY_URL + '/releases/tag/research-v1'
        with patch.object(wiki, '_request_remote', return_value={'status': 302, 'headers': {'Location': location}}), \
             patch.object(wiki, 'read_remote', return_value=advertisement):
            selection = wiki.public_revision()
        self.assertEqual('research-v1', selection['requested_revision'])
        self.assertEqual('server-commit', selection['revision'])
        with patch.object(wiki, '_request_remote', return_value={'status': 302,
                'headers': {'Location': 'https://evil.example/releases/tag/v1'}}), \
             patch.object(wiki, 'read_remote') as read:
            with self.assertRaisesRegex(ValueError, 'Unexpected'):
                wiki.public_revision()
            read.assert_not_called()

    def test_missing_index_reports_public_revision_instead_of_old_api_error(self):
        def reader(url, **kwargs):
            code = 403 if url.startswith('https://api.github.com/') else 404
            raise HTTPError(url, code, 'request failed', None, None)
        with patch.object(wiki, 'public_revision', return_value={'requested_revision': 'main',
            'revision': 'server-revision', 'metadata_transport': 'public_git'}):
            refs = wiki.prepare_references(self.run, self.identity, reader=reader, cache_root=self.cache)
        selection = json.loads((self.run / refs['wiki:java']['path']).read_text())
        self.assertEqual('server-revision', selection['revision'])
        self.assertEqual('public_git', selection['metadata_transport'])
        self.assertIn('index.json is not published', selection['reason'])
        self.assertIn('403', selection['metadata_diagnostic'])

    def test_http_rate_limit_headers_are_retained_as_diagnostics(self):
        with patch.object(wiki, '_request_remote', return_value={'status': 403,
            'headers': {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '12345'}}):
            with self.assertRaises(HTTPError) as raised:
                wiki.read_remote('https://api.github.com/repos/example/wiki')
        self.assertIn('rate limit exhausted', str(raised.exception))
        self.assertEqual('12345', raised.exception.headers['X-RateLimit-Reset'])

    def test_public_fallback_keeps_original_workload_time_bound(self):
        atomic_json(self.run / 'run.json', {'request': {'source_java': '17', 'target_java': '25'}})
        command = OperationInput('run', 'background', 'background', 'run:background:1', str(self.run))
        def reader(url, **kwargs):
            if url.startswith('https://api.github.com/'):
                raise HTTPError(url, 403, 'rate limit', None, None)
            return json.dumps(index())
        observed = []
        def resolver(**kwargs):
            observed.append(kwargs['timeout'])
            return {'requested_revision': 'main', 'revision': 'server-revision', 'metadata_transport': 'public_git'}
        with patch('modport.execution_budget.remaining_timeout', return_value=1.5), \
             patch.object(wiki, 'read_remote', side_effect=reader), \
             patch.object(wiki, 'public_revision', side_effect=resolver):
            wiki.prepare_for_command(command)
        self.assertEqual(1, len(observed))
        self.assertGreater(observed[0], 0)
        self.assertLessEqual(observed[0], 1.5)

    def test_missing_public_branch_is_reported_without_guessing_revision(self):
        with patch.object(wiki, 'read_remote', return_value='0000'):
            with self.assertRaisesRegex(ValueError, 'not advertised'):
                wiki.public_revision(revision='missing-branch')


class HandoffHandler:
    __execution_kernel_revision__ = 'wiki-handoff-test-v1'

    def __call__(self, command):
        from modport.execution_budget import current_deadline_budget
        assert current_deadline_budget(command) is not None
        refs = wiki.prepare_for_command(command)
        prompt = build_prompt(STAGE_PROMPTS['gap_research'], command, Path(command.run_dir), {}, {})
        instructions = _read_run_artifact({'root': command.run_dir, 'deadline_epoch': time.time() + 10},
            'artifacts/executions/' + command.command_id + '/task-instructions.json')
        selection = _read_run_artifact({'root': command.run_dir, 'deadline_epoch': time.time() + 10},
                                      refs['wiki:java']['path'])
        from modport.execution_budget import remaining_timeout
        workspace = Path(command.run_dir) / 'worktree'
        config = prepare_sandbox_tool(Path(command.run_dir), workspace, command.command_id,
            remaining_timeout(command, 10), read_only=True, allow_project_commands=False)
        rpc = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {
                'name': 'read_run_artifact', 'arguments': {'path': refs['wiki:java']['path']}}}]
        completed = subprocess.run(config['modport_sandbox']['command'],
            input='\n'.join(json.dumps(row) for row in rpc) + '\n', text=True,
            capture_output=True, timeout=remaining_timeout(command, 10), check=True)
        responses = [json.loads(line) for line in completed.stdout.splitlines()]
        assert any(tool['name'] == 'read_run_artifact' for tool in responses[0]['result']['tools'])
        assert not responses[1]['result'].get('isError')
        material = json.loads(responses[1]['result']['content'][0]['text'])
        return OperationResult('failed', command.run_id, command.task_id, command.stage_id, command.command_id,
            outputs={'refs': refs, 'prompt': prompt, 'instructions': json.loads(instructions['content_utf8']),
                     'selection': json.loads(selection['content_utf8']),
                     'mcp_selection': json.loads(material['content_utf8'])},
            error_code='research_unresolved')


class ActualWikiHandoffTests(unittest.TestCase):
    def test_production_background_dispatch_exports_editable_draft_with_saved_wiki_citations(self):
        """Real host/SDK/MCP path; the model transport is a controlled fixture."""
        import os
        import re
        import uuid
        from modport.execution_budget import current_deadline_budget, remaining_timeout
        from modport.models import Budget, MigrationRequest
        from modport.operations import MigrationOperations
        from modport.prompt_compressor import PromptCompressor

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'run'
            baseline = root / 'baseline'
            app_root = Path(directory) / 'app'
            request = MigrationRequest('wiki-fixture', 'https://github.com/example/wiki-fixture',
                '1.20.1', '26.1.2', source_loader_version='47.3.0',
                target_loader_version='26.1.2.0', source_java='17', target_java='25',
                budget=Budget(max_seconds=60, max_agent_assignments=1),
                skill_store=str(Path(directory) / 'skills'))
            with patch.dict(os.environ, {'MODPORT_DATA_ROOT': str(app_root)}):
                MigrationOperations(isolation_mode='thread').submit(request, run_dir=root,
                                                                   run_id='wiki-production-run')
            header = json.loads((root / 'run.json').read_text())
            definition = header['definition']
            self.assertEqual(WORKFLOW_VERSION, definition['workflow_version'])
            baseline.mkdir(parents=True)
            library = root / 'artifacts/wiki/library'
            atomic_json(library / 'repository.json', {'status': 'available',
                'repository': wiki.REPOSITORY_URL, 'revision': 'saved-revision',
                'retrieved_at': 'host-recorded'})
            atomic_json(library / 'index.json', index(page()))
            atomic_json(library / 'java/java-17-25.json', page())
            options = {key: definition[key] for key in
                       ('workflow_version', 'agent_dialogue_policy', 'validation_policy')}
            options.update(deadline_epoch=header['deadline_epoch'], model_policy=header['model_policy'])
            operation = OperationInput('wiki-production-run', 'background', 'background',
                'wiki-production-run:background:1', str(root), payload={'request': header['request']},
                options=options, artifact_refs=header['initial_refs'])
            observed = {}

            def model_transport(**kwargs):
                self.assertIsNotNone(current_deadline_budget(operation))
                self.assertEqual(baseline, kwargs['cwd'])
                self.assertIsNotNone(kwargs['planning_prompt'])
                config = prepare_sandbox_tool(root, kwargs['cwd'], kwargs['command_id'],
                    remaining_timeout(operation, 20), read_only=True, allow_project_commands=False)

                def read_material(path):
                    rpc = [
                        {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
                        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {
                            'name': 'read_run_artifact', 'arguments': {'path': path}}}]
                    process = subprocess.run(config['modport_sandbox']['command'],
                        input='\n'.join(json.dumps(row) for row in rpc) + '\n', text=True,
                        capture_output=True, timeout=remaining_timeout(operation, 20), check=True)
                    replies = [json.loads(line) for line in process.stdout.splitlines()]
                    self.assertEqual(['read_run_artifact'],
                                     [tool['name'] for tool in replies[0]['result']['tools']])
                    self.assertFalse(replies[1]['result'].get('isError'), replies[1])
                    material = json.loads(replies[1]['result']['content'][0]['text'])
                    self.assertEqual(material['total_bytes'], material['next_offset'])
                    observed.setdefault('read_paths', []).append(path)
                    return json.loads(material['content_utf8'])

                execution_dir = 'artifacts/executions/' + kwargs['command_id']
                for phase, prompt in (('plan', kwargs['planning_prompt']),
                                      ('execute', kwargs['prompt'])):
                    instruction_path = execution_dir + '/task-instructions.' + phase + '.json'
                    self.assertIn(instruction_path, prompt)
                    instructions = read_material(instruction_path)
                    protected = instructions['protected_context']
                    self.assertIn('never agent instructions', protected)
                    self.assertIn('without a digest parameter', protected)
                    selected_refs, _ = json.JSONDecoder().raw_decode(
                        protected.split('Saved Wiki references: ', 1)[1])
                    selection = read_material(selected_refs['wiki:java']['path'])
                    self.assertEqual({'java': '17'}, selection['source'])
                    self.assertEqual({'java': '25'}, selection['target'])
                    self.assertEqual('saved-revision', selection['revision'])
                    if phase == 'execute':
                        selected_page = read_material(selected_refs['wiki:page:java-17-25']['path'])
                        finding = re.search(r'New portable findings may be recorded in (\S+) as ', protected)
                        self.assertIsNotNone(finding)
                        observed['finding_path'] = finding.group(1)
                        source = selected_page['entries'][0]['evidence'][0]
                        observed['report'] = ('Java 17 -> 25; Wiki revision ' + selection['revision']
                            + '; java-17-25/java.versioned. ' + source['source'] + ' # '
                            + source['locator'] + '. Runtime verification remains unverified.\n')
                        entries = [dict(selected_page['entries'][0], id='java.fixture.new-finding')]
                        atomic_json(kwargs['cwd'] / finding.group(1), {'java': entries})
                kwargs['plan_path'].write_text('Read saved Wiki selections and primary-source locators.\n')
                output = kwargs['cwd'] / '.modport/background.md'
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(observed['report'])
                stdout = json.dumps({'type': 'item.completed', 'item': {
                    'type': 'agent_message', 'text': observed['report']}}) + '\n'
                kwargs['log'].parent.mkdir(parents=True, exist_ok=True)
                kwargs['log'].write_text(stdout)
                completed = subprocess.CompletedProcess(['controlled-model-transport'], 0, stdout, '')
                completed.dialogue_metadata = {'turns': 2, 'thread_id': 'fixture-model-session',
                    'plan_path': str(kwargs['plan_path']), 'execution_log': str(kwargs['log'])}
                return completed

            compressor = PromptCompressor(catalog={'models': [
                {'slug': 'gpt-6-luna', 'context_window': 1_050_000}]})
            with patch.dict(os.environ, {'MODPORT_DATA_ROOT': str(app_root)}), \
                 patch('modport.handlers.PromptCompressor.from_environment', return_value=compressor), \
                 patch('modport.opencode_agent.run_agent', side_effect=model_transport) as transport:
                with open_runtime(root, isolation_mode='thread') as runtime:
                    command = runtime.command('modport.background', execution_id=operation.command_id,
                        idempotency_key=operation.command_id, correlation_id=operation.run_id,
                        timeout_seconds=header['deadline_epoch'] - time.time(), payload=operation.to_dict())
                    runtime.submit(command)
                    result = runtime.run_once(execution_id=operation.command_id)
                    self.assertEqual('succeeded', result.state, repr(result))
                    value = result.result.value
                    self.assertEqual('completed', value['status'], value)
                    self.assertFalse(value['outputs']['wiki_contribution_diagnostics'])
                    drafts = value['outputs']['wiki_contribution_drafts']
                    self.assertEqual(1, len(drafts))
                    draft_id = drafts[0]['id']
                    self.assertEqual(draft_id, str(uuid.UUID(draft_id)))
                    self.assertEqual(1, transport.call_count)
                    report_ref = value['outputs']['artifact_refs'][
                        'stage_output:background:.modport/background.md']
                    self.assertEqual(observed['report'].strip(),
                                     (root / report_ref['path']).read_text().strip())
                    self.assertEqual(wiki.finding_path(operation), observed['finding_path'])
                    app = DesktopApplication(app_root)
                    self.addCleanup(app.github.close)
                    route = '/api/wiki/contributions/' + draft_id
                    draft = app.request('GET', route)
                    content = json.loads(draft['content'])
                    self.assertEqual({'java': '17'}, content['source'])
                    self.assertEqual({'java': '25'}, content['target'])
                    self.assertEqual('java.fixture.new-finding', content['entries'][0]['id'])
                    changed = app.request('POST', route, {'title': 'User-reviewed Java finding'})
                    replay, diagnostics = wiki.export_findings(operation, workspace=baseline)
                    self.assertFalse(diagnostics)
                    self.assertEqual(draft_id, replay[0]['id'])
                    self.assertEqual(changed['title'], replay[0]['title'])
                    self.assertEqual(changed, app.request('GET', route))
                    self.assertEqual(1, len(app.request('GET', '/api/wiki/contributions')['drafts']))

    def test_sdk_dispatch_reads_saved_material_and_returns_raw_business_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'worktree').mkdir()
            atomic_json(root / 'run.json', {'request': {'source_java': '17', 'target_java': '25'}})
            atomic_json(root / 'artifacts/wiki/library/repository.json', {'status': 'available',
                'repository': wiki.REPOSITORY_URL, 'revision': 'saved-revision', 'retrieved_at': 'host-recorded'})
            atomic_json(root / 'artifacts/wiki/library/index.json', index(page()))
            atomic_json(root / 'artifacts/wiki/library/java/java-17-25.json', page())
            with open_runtime(root, handlers={'modport.gap_research': HandoffHandler(),
                                             'modport.research_review': HandoffHandler()}, isolation_mode='thread') as runtime:
                for stage in ('gap_research', 'research_review'):
                    operation = OperationInput('wiki-run', stage, stage, 'wiki-run:' + stage, str(root),
                        options={'workflow_version': WORKFLOW_VERSION})
                    command = runtime.command('modport.' + stage, execution_id=operation.command_id,
                        idempotency_key=operation.command_id, correlation_id='wiki-run',
                        timeout_seconds=30, payload=operation.to_dict())
                    runtime.submit(command)
                    result = runtime.run_once()
                    self.assertEqual('succeeded', result.state, repr(result))
                    value = result.result.value
                    self.assertEqual('failed', value['status'])
                    self.assertEqual('research_unresolved', value['error_code'])
                    self.assertIn('wiki:page:java-17-25', value['outputs']['refs'])
                    self.assertIn('saved-revision', json.dumps(value['outputs']['selection']))
                    self.assertEqual(value['outputs']['selection'], value['outputs']['mcp_selection'])
                    self.assertIn('never agent instructions', value['outputs']['instructions']['protected_context'])
                    self.assertIn('wiki:page:java-17-25', value['outputs']['instructions']['protected_context'])


if __name__ == '__main__':
    unittest.main()
