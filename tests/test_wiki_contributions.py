import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from modport import wiki_contributions as wiki
from modport.platform_files import FileLock


def entry():
    return {'id': 'java.changed', 'category': 'api', 'summary': 'A versioned API changed',
            'applicability': 'Uses the documented API', 'migration': 'Use the documented replacement',
            'compat': 'Use a version-specific adapter',
            'verification': 'Read the locked JDK source; runtime behavior remains unverified',
            'evidence': [{'source': 'https://openjdk.org/projects/jdk/25/',
                          'locator': 'JDK 25 source', 'supports': 'Version applicability'}]}


class DraftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = wiki.ContributionStore(self.root)

    def create(self, **kwargs):
        return self.store.create('java', {'java': '17'}, {'java': '25'}, [entry()], **kwargs)

    def test_edits_export_and_replay_preserve_private_origin(self):
        origin = {'run_id': 'private-run', 'command_id': 'private-command', 'kind': 'java'}
        draft = self.create(origin=origin)
        value = json.loads(draft['content'])
        value['entries'][0]['summary'] = 'Reviewed API observation'
        changed = self.store.update(draft['id'], {'title': 'Reviewed generic Java knowledge',
            'body': 'Read JDK 25 source; runtime behavior remains unverified.', 'content': json.dumps(value)})
        self.assertEqual(self.create(origin=origin), changed)
        self.assertEqual(self.store.list_drafts(), [changed])
        exported = self.root / 'export.json'
        self.store.export(draft['id'], exported)
        raw = exported.read_text()
        self.assertNotIn('private-run', raw)
        self.assertNotIn('private-command', raw)
        self.assertNotIn('origin', raw)
        payload = json.loads(raw)
        self.assertEqual(payload['content'], changed['content'])
        self.assertEqual(set(json.loads(changed['content'])),
                         {'schema_version', 'id', 'kind', 'source', 'target', 'entries'})
        self.assertEqual(list(changed['files']), [f'contributions/java/{draft["id"]}.json'])
        if os.name != 'nt':
            self.assertEqual(exported.stat().st_mode & 0o777, 0o600)

    def test_draft_identity_and_paths_cannot_be_changed(self):
        draft = self.create()
        value = json.loads(draft['content'])
        value['id'] = 'other'
        for updates in ({'content': json.dumps(value)}, {'files': {'../secret': 'text'}}, {'origin': {}}):
            with self.assertRaises(ValueError):
                self.store.update(draft['id'], updates)
        for identifier in ('../private', '/etc/passwd', '', draft['id'].upper()):
            with self.assertRaises(ValueError):
                self.store.read(identifier)
        self.assertEqual(self.store.read(draft['id']), draft)

    def test_generic_fields_and_version_identity_are_required(self):
        candidate = entry()
        candidate['run_id'] = 'private'
        with self.assertRaisesRegex(ValueError, 'project fields'):
            self.store.create('java', {'java': '17'}, {'java': '25'}, [candidate])
        for source in ({'java': '17', 'path': '/root/project'}, {'java': '../17'}, {'java': '17', 'project': 'demo'}):
            with self.assertRaises(ValueError):
                self.store.create('java', source, {'java': '25'}, [entry()])
        draft = self.store.create('platform', {'minecraft': '1.20.1', 'loader': 'forge', 'loader_version': '47.4.0'},
            {'minecraft': '26.1.2', 'loader': 'neoforge', 'loader_version': '26.1.2.1'}, [entry()])
        self.assertEqual(draft['kind'], 'platform')

    def test_credentials_and_local_paths_are_rejected_without_echo(self):
        draft = self.create()
        for private in ('ghp_' + 'secret' * 8, 'Authorization: Bearer supersecret', '/root/private/project',
                        'See [local source](/root/private/project)', '/opt/private/project',
                        'Source: https://example.org\n/root/private/project',
                        'Source: https://example.org \n/root/private/project',
                        'Source:\n/opt/private/project',
                        r'\\private-host\share\project', '//private-host/share/project',
                        'file:///private/project', 'C:\\Users\\private\\project',
                        'https://user:secret@example.org/path', 'https://example.org?token=secret'):
            with self.subTest(private=private):
                with self.assertRaises(ValueError) as caught:
                    self.store.update(draft['id'], {'body': private})
                self.assertNotIn('supersecret', str(caught.exception))
        candidate = entry()
        candidate['evidence'][0]['source'] = 'https://user:secret@example.org/'
        with self.assertRaises(ValueError):
            self.store.create('java', {'java': '17'}, {'java': '25'}, [candidate])
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'opaque-inherited-secret'}):
            with self.assertRaises(ValueError):
                self.store.update(draft['id'], {'body': 'Details: opaque-inherited-secret'})

    def test_origin_replay_is_separate_for_each_knowledge_kind(self):
        origin = {'run_id': 'private-run', 'command_id': 'research-command'}
        java = self.create(origin=origin)
        platform = self.store.create('platform',
            {'minecraft': '1.20.1', 'loader': 'forge', 'loader_version': '47.4.0'},
            {'minecraft': '26.1.2', 'loader': 'neoforge', 'loader_version': '26.1.2.1'}, [entry()], origin=origin)
        self.assertNotEqual(java['id'], platform['id'])
        self.assertEqual(java, self.create(origin=origin))

    def test_produced_contribution_obeys_wiki_reader_document_bound(self):
        from modport.wiki_knowledge import MAX_DOCUMENT, validate_contribution
        candidate = entry()
        candidate['summary'] = 'g' * (MAX_DOCUMENT - 4096)
        draft = self.store.create('java', {'java': '17'}, {'java': '25'}, [candidate])
        self.assertLessEqual(len(draft['content'].encode('utf-8')), MAX_DOCUMENT)
        validate_contribution(json.loads(draft['content']))
        candidate['summary'] = 'g' * MAX_DOCUMENT
        with self.assertRaisesRegex(ValueError, 'size limit'):
            self.store.create('java', {'java': '17'}, {'java': '25'}, [candidate])

    @unittest.skipIf(os.name == 'nt', 'POSIX symlink and hardlink authoring test')
    def test_links_cannot_redirect_draft_read_or_export(self):
        draft = self.create()
        path = self.store.drafts / (draft['id'] + '.json')
        real = self.root / 'saved.json'
        path.rename(real)
        path.symlink_to(real)
        with self.assertRaises((OSError, ValueError)):
            self.store.read(draft['id'])
        path.unlink()
        os.link(real, path)
        with self.assertRaises((OSError, ValueError)):
            self.store.read(draft['id'])
        destination = self.root / 'redirect'
        destination.symlink_to(real)
        path.unlink()
        real.rename(path)
        with self.assertRaises((OSError, ValueError)):
            self.store.export(draft['id'], destination)


class FakeGitHub(wiki.GitHubContributor):
    """Stateful GitHub simulation, including mutations with lost responses."""
    def __init__(self, root, *, login='reader', own_repo=False):
        super().__init__(root, executable='simulated-gh')
        self.login = login
        self.calls = []
        self.ref = None
        self.pull = None
        self.fork = own_repo or login == 'FlightDan'
        self.permissions = True
        self.parent = wiki.UPSTREAM
        self.lost = None
        self.commits = 0
        self.queries_fail = False
        self.fail_pr_before_acceptance = False

    def _configured_login(self):
        return self.login

    def _read_token(self, login):
        return 'simulated-credential-for-' + login

    def _api(self, path, method='GET', payload=None):
        self.calls.append((path, method, payload))
        repo = self.login + '/modport-wiki-for-agents'
        if path == 'user':
            return {'login': self.login}
        if path == 'repos/' + wiki.UPSTREAM and method == 'GET':
            return {'default_branch': 'main', 'permissions': {'push': self.permissions}}
        if path.startswith('repos/' + wiki.UPSTREAM + '/pulls?'):
            if self.queries_fail:
                raise wiki.GitHubContributionError('gh: API rate limit exceeded (HTTP 403)', status_code=403)
            return [self.pull] if self.pull else []
        if path == 'repos/' + repo and method == 'GET':
            if not self.fork:
                raise wiki.GitHubContributionError('gh: Not Found (HTTP 404)', status_code=404)
            return {'parent': {'full_name': self.parent}, 'permissions': {'push': self.permissions}}
        if path.endswith('/forks'):
            self.fork = True
            result = {'full_name': repo}
        elif '/git/ref/heads/' in path:
            if path == 'repos/' + wiki.UPSTREAM + '/git/ref/heads/main':
                return {'object': {'sha': 'server-base'}}
            if self.ref is None:
                raise wiki.GitHubContributionError('gh: Not Found (HTTP 404)', status_code=404)
            return {'object': {'sha': self.ref}}
        elif path.endswith('/git/refs') and method == 'POST':
            self.ref = payload['sha']
            result = {'object': {'sha': self.ref}}
        elif path.endswith('/git/commits/server-base'):
            return {'tree': {'sha': 'server-base-tree'}}
        elif path.endswith('/git/trees'):
            result = {'sha': 'server-new-tree'}
        elif path.endswith('/git/commits'):
            self.commits += 1
            result = {'sha': 'server-new-commit'}
        elif '/git/refs/heads/' in path and method == 'PATCH':
            self.ref = payload['sha']
            result = {'object': {'sha': self.ref}}
        elif path.endswith('/pulls') and method == 'POST':
            if self.fail_pr_before_acceptance:
                self.fail_pr_before_acceptance = False
                raise wiki.GitHubContributionError('Network unavailable before PR creation')
            self.pull = {'html_url': 'https://github.com/' + wiki.UPSTREAM + '/pull/3', 'number': 3,
                         'user': {'login': self.login}, 'draft': True, 'state': 'open'}
            result = self.pull
        else:
            raise AssertionError((path, method, payload))
        if self.lost == (method, path.split('/')[-1]):
            self.lost = None
            raise wiki.GitHubContributionError('Connection reset after request; outcome unknown')
        return result


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host = FakeGitHub(self.temp.name)
        self.draft = self.host.store.create('java', {'java': '17'}, {'java': '25'}, [entry()],
                                           origin={'run_id': 'host-private-run'})

    def test_own_account_fork_and_draft_pr_without_project_checkout(self):
        result = self.host.submit(self.draft, expected_login='reader')
        self.assertEqual(result['status'], 'submitted')
        mutations = [(path, method, data) for path, method, data in self.host.calls if method != 'GET']
        self.assertTrue(any(path.endswith('/forks') for path, _, _ in mutations))
        pull = [data for path, _, data in mutations if path.endswith('/pulls')][0]
        self.assertTrue(pull['draft'])
        self.assertTrue(pull['head'].startswith('reader:release/wiki-'))
        self.assertNotIn('host-private-run', json.dumps(mutations))
        self.assertFalse(any(path.endswith('/merges') for path, _, _ in mutations))
        count = len(mutations)
        self.assertEqual(self.host.submit(self.draft['id'], expected_login='reader'), result)
        self.assertEqual(sum(method != 'GET' for _, method, _ in self.host.calls), count)
        with self.assertRaisesRegex(ValueError, 'Submission has started'):
            self.host.store.update(self.draft['id'], {'body': 'Changed'})

    def test_upstream_owner_uses_owned_repo_without_fork(self):
        host = FakeGitHub(self.temp.name, login='FlightDan')
        host.submit(self.draft, expected_login='FlightDan')
        self.assertFalse(any(path.endswith('/forks') for path, _, _ in host.calls))
        self.assertTrue(all(path.startswith('repos/' + wiki.UPSTREAM) or path == 'user'
                            for path, _, _ in host.calls))

    def test_lost_pr_response_resumes_without_duplicate_commit_or_pr(self):
        self.host.lost = ('POST', 'pulls')
        with self.assertRaisesRegex(wiki.GitHubContributionError, 'outcome unknown'):
            self.host.submit(self.draft, expected_login='reader')
        self.assertEqual(self.host.store.read(self.draft['id'])['status'], 'submitting')
        self.assertTrue(self.host.store.read(self.draft['id'])['content'])
        result = self.host.submit(self.draft, expected_login='reader')
        self.assertEqual(result['status'], 'submitted')
        self.assertEqual(self.host.commits, 1)
        self.assertEqual(sum(path.endswith('/pulls') and method == 'POST'
                            for path, method, _ in self.host.calls), 1)

    def test_lost_branch_creation_response_reuses_branch(self):
        self.host.lost = ('POST', 'refs')
        with self.assertRaises(wiki.GitHubContributionError):
            self.host.submit(self.draft, expected_login='reader')
        self.host.submit(self.draft, expected_login='reader')
        self.assertEqual(sum(path.endswith('/git/refs') and method == 'POST'
                            for path, method, _ in self.host.calls), 1)

    def test_lost_branch_update_response_reuses_saved_commit(self):
        self.host.lost = ('PATCH', 'wiki-' + self.draft['id'])
        with self.assertRaises(wiki.GitHubContributionError):
            self.host.submit(self.draft, expected_login='reader')
        self.host.submit(self.draft, expected_login='reader')
        self.assertEqual(self.host.commits, 1)
        self.assertEqual(self.host.ref, 'server-new-commit')

    def test_retry_restores_deleted_branch_before_creating_pr(self):
        self.host.fail_pr_before_acceptance = True
        with self.assertRaises(wiki.GitHubContributionError):
            self.host.submit(self.draft, expected_login='reader')
        self.assertEqual(self.host.ref, 'server-new-commit')
        self.host.ref = None
        result = self.host.submit(self.draft, expected_login='reader')
        self.assertEqual(result['status'], 'submitted')
        self.assertEqual(self.host.ref, 'server-new-commit')
        self.assertEqual(self.host.commits, 1)

    def test_sign_in_cannot_change_account_during_submission(self):
        with FileLock(self.host.store.root / 'login.lock', blocking=False):
            with self.assertRaisesRegex(wiki.GitHubContributionError, 'current GitHub sign-in'):
                self.host.submit(self.draft, expected_login='reader')
        self.assertFalse(self.host.calls)

    def test_submission_lock_prevents_other_window_sign_in(self):
        other = wiki.GitHubContributor(self.temp.name, executable='never-launched')
        original = self.host._submit
        def submit_while_checking(draft, meta):
            with self.assertRaisesRegex(wiki.GitHubContributionError, 'another host window'):
                other.start_login()
            return original(draft, meta)
        with patch.object(self.host, '_submit', side_effect=submit_while_checking):
            self.host.submit(self.draft, expected_login='reader')

    def test_account_confirmation_and_account_binding(self):
        with self.assertRaisesRegex(wiki.GitHubContributionError, 'account changed'):
            self.host.submit(self.draft, expected_login='someone-else')
        self.assertFalse(any(method != 'GET' for _, method, _ in self.host.calls))
        self.host.lost = ('POST', 'refs')
        with self.assertRaises(wiki.GitHubContributionError):
            self.host.submit(self.draft, expected_login='reader')
        self.host.login = 'another'
        with self.assertRaisesRegex(wiki.GitHubContributionError, 'another GitHub account'):
            self.host.submit(self.draft, expected_login='another')

    def test_permissions_wrong_fork_and_raw_rate_limit_remain_actionable(self):
        self.host.fork = True
        self.host.permissions = False
        with self.assertRaisesRegex(wiki.GitHubContributionError, 'cannot write'):
            self.host.submit(self.draft, expected_login='reader')
        self.host.permissions = True
        self.host.parent = 'someone/unrelated'
        with self.assertRaisesRegex(wiki.GitHubContributionError, 'not the expected wiki fork'):
            self.host.submit(self.draft, expected_login='reader')
        self.host.queries_fail = True
        with self.assertRaisesRegex(wiki.GitHubContributionError, 'rate limit exceeded.*403'):
            self.host.submit(self.draft, expected_login='reader')
        self.assertTrue(self.host.store.read(self.draft['id'])['content'])
        self.assertFalse(any(method != 'GET' for _, method, _ in self.host.calls))


@unittest.skipIf(os.name == 'nt', 'Fake gh launcher uses a POSIX script; Windows Job runtime requires Windows')
class GitHubProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.gh = self.root / 'fake-gh'
        self.gh.write_text('#!' + sys.executable + '\n' + '''import json, os, sys, time
args = sys.argv[1:]
if args[0] == "auth":
    if "--skip-ssh-key" in args:
        print("unknown flag: --skip-ssh-key", file=sys.stderr)
        sys.exit(2)
    print("First copy your one-time code: ABCD-1234", file=sys.stderr, flush=True)
    time.sleep(30)
elif args[0] == "config":
    print("browser-user")
else:
    if "user" in args:
        print(json.dumps({"login":"browser-user"}))
    else:
        print("gh: Network error (HTTP 403) Authorization: Bearer ghp_privateToken", file=sys.stderr)
        sys.exit(1)
''')
        self.gh.chmod(0o700)
        self.host = wiki.GitHubContributor(self.root, executable=self.gh)
        (self.host.auth_directory / 'hosts.yml').write_text('github.com:\n    user: browser-user\n')
        (self.host.auth_directory / 'hosts.yml').chmod(0o600)
        self.addCleanup(self.host.close)

    def wait_state(self, expected):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status = self.host.login_status()
            if status['state'] == expected:
                return status
            time.sleep(0.01)
        self.fail(self.host.login_status())

    def test_login_exposes_only_safe_code_and_cancel_reaps_process(self):
        self.host.start_login()
        status = self.wait_state('waiting')
        self.assertEqual(status['user_code'], 'ABCD-1234')
        self.assertEqual(status['verification_url'], 'https://github.com/login/device')
        process = self.host._login_process
        cancelled = self.host.cancel_login()
        self.assertEqual(cancelled['state'], 'cancelled')
        self.assertIsNotNone(process.poll())
        self.assertIsNone(self.host._login_process)
        self.assertFalse(self.host._login_thread.is_alive())
        self.assertFalse(list(self.host.store.drafts.iterdir()))

    def test_app_environment_strips_tokens_credentials_and_other_gh_host(self):
        with patch.dict(os.environ, {'GH_TOKEN': 'private-gh', 'GITHUB_TOKEN': 'private-github',
            'OPENAI_API_KEY': 'private-openai', 'GH_HOST': 'other.example', 'GH_CONFIG_DIR': '/other',
            'GH_DEBUG': 'api', 'GH_ENTERPRISE_TOKEN': 'private-enterprise', 'GH_BROWSER': 'danger'}):
            env = self.host._environment()
            self.assertFalse({'GH_TOKEN', 'GITHUB_TOKEN', 'OPENAI_API_KEY', 'GH_DEBUG',
                              'GH_ENTERPRISE_TOKEN', 'GH_BROWSER'} & set(env))
            self.assertEqual(env['GH_HOST'], 'github.com')
            self.assertEqual(env['GH_CONFIG_DIR'], str(self.host.auth_directory))
            status = self.host.status()
            self.assertEqual(status['login'], 'browser-user')
            self.assertNotIn('private-', json.dumps(status))
        self.assertTrue(self.host.auth_directory.is_dir())
        self.assertNotIn('github-auth', str(self.host.store.drafts))

    def test_failed_requests_preserve_raw_reason_but_redact_credentials(self):
        with self.assertRaises(wiki.GitHubContributionError) as caught:
            self.host._api('simulate-failure')
        self.assertEqual(caught.exception.status_code, 403)
        self.assertIn('Network error', str(caught.exception))
        self.assertNotIn('ghp_privateToken', str(caught.exception))

    def test_login_timeout_cleans_exact_process(self):
        with patch.object(wiki, 'LOGIN_TIMEOUT', 0.15):
            self.host.start_login()
            self.wait_state('expired')
            self.assertIsNone(self.host._login_process)
        self.assertFalse(self.host._login_thread.is_alive())

    def test_request_timeout_is_bounded_and_reaps_process(self):
        self.gh.write_text('#!' + sys.executable + '\nimport time\ntime.sleep(30)\n')
        with patch.object(wiki, 'COMMAND_TIMEOUT', 0.1):
            start = time.monotonic()
            with self.assertRaisesRegex(wiki.GitHubContributionError, 'timed out'):
                self.host._api('simulate-timeout')
            self.assertLess(time.monotonic() - start, 2)

    def test_completed_login_observes_authenticated_identity(self):
        self.gh.write_text('#!' + sys.executable + '\nimport json, sys\n'
            'if "--skip-ssh-key" in sys.argv:\n'
            '    print("unknown flag: --skip-ssh-key", file=sys.stderr)\n'
            '    sys.exit(2)\n'
            'print("browser-user" if sys.argv[1] == "config" else json.dumps({"login":"browser-user"}))\n')
        self.host.start_login()
        status = self.wait_state('authenticated')
        self.assertEqual(status['login'], 'browser-user')

    def test_cancel_reaps_hung_post_login_account_lookup(self):
        marker = self.host.auth_directory / 'identity-started'
        self.gh.write_text('#!' + sys.executable + '\nimport sys, time\nfrom pathlib import Path\n'
            'if sys.argv[1] == "config":\n'
            '    Path("identity-started").write_text("started")\n'
            '    time.sleep(30)\n')
        self.host.start_login()
        deadline = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(marker.exists())
        process = self.host._login_process
        start = time.monotonic()
        self.assertEqual(self.host.cancel_login()['state'], 'cancelled')
        self.assertLess(time.monotonic() - start, 2)
        self.assertIsNotNone(process.poll())
        self.assertFalse(self.host._login_thread.is_alive())
        self.assertIsNone(self.host._login_process)

    @unittest.skipUnless(shutil.which('gh'), 'GitHub CLI is not installed')
    def test_installed_gh_reads_only_offline_app_account_configuration(self):
        (self.host.auth_directory / 'hosts.yml').write_text(
            'github.com:\n    user: offline-config-user\n    users:\n        offline-config-user: {}\n')
        config = self.host.auth_directory / 'config.yml'
        config.write_text('version: "1"\n')
        config.chmod(0o600)
        installed = wiki.GitHubContributor(self.root)
        self.addCleanup(installed.close)
        self.assertEqual(installed._configured_login(), 'offline-config-user')

    def test_empty_app_config_cannot_reuse_global_keyring_account(self):
        (self.host.auth_directory / 'hosts.yml').unlink()
        with patch.object(self.host, '_api') as api:
            status = self.host.status()
        self.assertFalse(status['authenticated'])
        self.assertIn('Sign in', status['error'])
        api.assert_not_called()

    def test_shared_keyring_switch_cannot_silently_select_another_account(self):
        with patch.object(self.host, '_api', return_value={'login': 'another-account'}):
            status = self.host.status()
        self.assertFalse(status['authenticated'])
        self.assertIsNone(status['login'])
        self.assertIn('credential-store account changed', status['error'])

    def frozen_token_script(self, *, credential_error=False):
        token = 'opaque-app-secret-without-a-known-prefix'
        script = '''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
secret = "opaque-app-secret-without-a-known-prefix"
if args[0] == "config":
    if "GH_TOKEN" in os.environ:
        raise SystemExit("credential supplied to a non-API child")
    print("browser-user")
elif args[:2] == ["auth", "token"]:
    if args[args.index("--user") + 1] != "browser-user" or "GH_TOKEN" in os.environ:
        raise SystemExit("incorrect credential selection")
    with Path("token-reads").open("a") as stream:
        stream.write("read\\n")
    print(secret if not Path("keyring-switched").exists() else "changed-external-keyring-secret")
    if CREDENTIAL_ERROR:
        print("Credential command failed: " + secret, file=sys.stderr)
        raise SystemExit(1)
elif args[0] == "api":
    path = args[args.index("-H") + 2]
    pinned = os.environ.get("GH_TOKEN") == secret
    login = "browser-user" if pinned else "external-account"
    if path == "user":
        Path("keyring-switched").write_text("external sign-in replaced shared slot")
        print(json.dumps({"login": login}))
    elif path.startswith("repos/"):
        print(json.dumps({"default_branch":"main"}))
    elif path == "record-mutation":
        print(json.dumps({"login":login,"pinned":pinned,"external_change":Path("keyring-switched").exists()}))
    elif path == "failed-mutation":
        print("gh: Permission denied (HTTP 403): " + os.environ.get("GH_TOKEN", ""), file=sys.stderr)
        raise SystemExit(1)
'''.replace('CREDENTIAL_ERROR', str(credential_error))
        self.gh.write_text('#!' + sys.executable + '\n' + script)
        return token

    def test_submission_pins_selected_credential_after_external_keyring_switch(self):
        token = self.frozen_token_script()
        draft = self.host.store.create('java', {'java': '17'}, {'java': '25'}, [entry()])
        def mutation(draft, meta):
            # The earlier API user check changed the simulated shared keyring.
            return self.host._api('record-mutation', 'POST', {})
        with patch.dict(os.environ, {'GH_TOKEN': 'inherited-unrelated-account-token'}):
            with patch.object(self.host, '_submit', side_effect=mutation):
                result = self.host.submit(draft, expected_login='browser-user')
            self.assertNotIn('GH_TOKEN', self.host._environment(api=True))
        self.assertEqual(result, {'login': 'browser-user', 'pinned': True, 'external_change': True})
        self.assertEqual((self.host.auth_directory / 'token-reads').read_text().splitlines(), ['read'])
        self.assertFalse(self.host.status()['authenticated'])
        self.assertNotIn(token, json.dumps(self.host.store.read(draft['id'])))
        self.assertNotIn(token, json.dumps(self.host.store._meta(draft['id'])))

    def test_pinned_opaque_credential_is_redacted_from_errors_and_receipts(self):
        token = self.frozen_token_script()
        draft = self.host.store.create('java', {'java': '17'}, {'java': '25'}, [entry()])
        with patch.object(self.host, '_submit', side_effect=lambda draft, meta:
                               self.host._api('failed-mutation', 'POST', {})):
            with self.assertRaises(wiki.GitHubContributionError) as caught:
                self.host.submit(draft, expected_login='browser-user')
        self.assertEqual(caught.exception.status_code, 403)
        self.assertIn('Permission denied', str(caught.exception))
        self.assertNotIn(token, str(caught.exception))
        self.assertNotIn(token, json.dumps(self.host.store._meta(draft['id'])))
        self.assertNotIn('GH_TOKEN', self.host._environment(api=True))
        self.assertNotIn(token, json.dumps(self.host.store.read(draft['id'])))

    def test_failed_credential_command_never_exposes_its_output(self):
        token = self.frozen_token_script(credential_error=True)
        draft = self.host.store.create('java', {'java': '17'}, {'java': '25'}, [entry()])
        with self.assertRaises(wiki.GitHubContributionError) as caught:
            self.host.submit(draft, expected_login='browser-user')
        self.assertIn('Cannot retrieve', str(caught.exception))
        self.assertNotIn(token, str(caught.exception))
        self.assertFalse((self.host.auth_directory / 'keyring-switched').exists())
        self.assertNotIn('GH_TOKEN', self.host._environment(api=True))

    def test_frozen_credential_is_scoped_to_api_children_on_owning_thread(self):
        self.frozen_token_script()
        other = []
        with self.host._frozen_credentials('browser-user'):
            self.assertIn('GH_TOKEN', self.host._environment(api=True))
            self.assertNotIn('GH_TOKEN', self.host._environment())
            thread = threading.Thread(target=lambda: other.append(
                (self.host.status(), self.host._environment(api=True))))
            thread.start()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        self.assertFalse(other[0][0]['authenticated'])
        self.assertNotIn('GH_TOKEN', other[0][1])
        self.assertNotIn('GH_TOKEN', self.host._environment(api=True))

    def test_separate_windows_cannot_start_simultaneous_login(self):
        self.host.start_login()
        self.wait_state('waiting')
        other = wiki.GitHubContributor(self.root, executable=self.gh)
        self.addCleanup(other.close)
        with self.assertRaisesRegex(wiki.GitHubContributionError, 'another host window'):
            other.start_login()


if __name__ == '__main__':
    unittest.main()
