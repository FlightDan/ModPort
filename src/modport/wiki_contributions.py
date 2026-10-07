"""Host-owned editable wiki drafts and explicit GitHub Draft PR submission.

GitHub CLI configuration is separate from agent/Run inputs. gh normally uses the
OS credential store, whose service/account slots may be shared with other gh
installations. If unavailable, gh can fall back to plaintext hosts.yml inside
our private auth directory; we never request insecure storage or expose tokens.
Submission uses GitHub's Git API, never the project's local Git checkout. Server
object IDs are transaction references, not locally calculated checksums.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import threading
from urllib.parse import quote, urlencode, urlsplit
import uuid

from .knowledge_library import project_entries
from .platform_files import (FileLock, assert_host_owned, atomic_write,
                             make_private_directory, safe_open)

UPSTREAM = 'FlightDan/modport-wiki-for-agents'
MAX_DRAFT_BYTES = 512 * 1024
MAX_OUTPUT_BYTES = 2_097_152
LOGIN_TIMEOUT = 900
COMMAND_TIMEOUT = 45
CREDENTIAL_NOTICE = ('GitHub CLI uses the system credential store when available; '
    'its account slots can be shared with other GitHub CLI installations. If no '
    'credential store is available, GitHub CLI may save credentials in the private '
    'app authentication directory. Credentials are never included in drafts or exports.')
_LOGIN = re.compile(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}\Z')
_TOKEN = re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|sk-(?:proj-|live-|test-)?[A-Za-z0-9_-]{16,})\b')
_LOCAL = re.compile(r'(?:file://|\\\\[A-Za-z0-9]|(?<![A-Za-z0-9:])//[A-Za-z0-9]|(?<![A-Za-z0-9:/])/(?![/*\s"\'`<>()])[^\s"\'`<>()]+|(?<![A-Za-z0-9])[A-Za-z]:[\\/])')
_SECRET_FIELD = re.compile(r'(?i)(?:authorization\s*[:=]\s*(?:bearer\s+|token\s+)?|(?:gh_token|github_token|oauth_token|access_token|api_key|password)\s*["\']?\s*[:=]\s*["\']?)[^\s,"\'}]+')


def _json(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def _redact(value):
    value = str(value)
    for key, secret in os.environ.items():
        if ('TOKEN' in key.upper() or 'KEY' in key.upper() or 'PASSWORD' in key.upper()) and len(secret) >= 8:
            value = value.replace(secret, '[redacted]')
    value = _TOKEN.sub('[redacted]', value)
    value = _SECRET_FIELD.sub('[redacted credential]', value)
    # Basic-auth URLs may appear in lower-level proxy/network errors.
    value = re.sub(r'(https?://)[^\s/@]+:[^\s/@]+@', r'\1[redacted]@', value)
    return value[-16000:]


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _portable(value):
    # Inspect decoded text, preserving whitespace around paths and URLs. JSON
    # serialization escapes newlines, which would otherwise disguise boundaries.
    for text in _strings(value):
        inherited_secret = any(secret in text for key, secret in os.environ.items()
            if ('TOKEN' in key.upper() or 'KEY' in key.upper() or 'PASSWORD' in key.upper()) and len(secret) >= 8)
        without_urls = re.sub(r'https?://[^\s"<>]+', '(portable-url)', text)
        if inherited_secret or _TOKEN.search(text) or _SECRET_FIELD.search(text) or _LOCAL.search(without_urls):
            raise ValueError('Contribution contains credentials or a local project path')
        for url in re.findall(r'https?://[^\s"<>]+', text):
            parsed = urlsplit(url)
            if parsed.username is not None or parsed.password is not None:
                raise ValueError('Contribution URLs must not contain credentials')
            if re.search(r'(?i)(?:token|key|password|signature|credential)=', parsed.query):
                raise ValueError('Contribution URLs must not contain authentication parameters')


def _identity(kind, value):
    fields = {'java'} if kind == 'java' else {'minecraft', 'loader', 'loader_version'}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f'{kind} requires exact version fields: {", ".join(sorted(fields))}')
    for field, version in value.items():
        if not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+\-]{0,95}', version):
            raise ValueError(f'Invalid version identity: {field}')
        if version.lower() in {'latest', 'current', 'recommended', 'unknown'}:
            raise ValueError(f'Exact version required: {field}')
    return dict(value)


def _content(identifier, value):
    if not isinstance(value, dict) or set(value) != {'schema_version', 'id', 'kind', 'source', 'target', 'entries'}:
        raise ValueError('Contribution requires schema_version/id/kind/source/target/entries only')
    if type(value['schema_version']) is not int or value['schema_version'] != 1 or value['id'] != identifier:
        raise ValueError('Contribution schema or draft ID does not match')
    kind = value['kind']
    if kind not in {'java', 'platform'}:
        raise ValueError('Contribution kind must be java or platform')
    result = {'schema_version': 1, 'id': identifier, 'kind': kind,
              'source': _identity(kind, value['source']), 'target': _identity(kind, value['target']),
              'entries': project_entries(value['entries'])}
    _portable(result)
    if len(_json(result).encode('utf-8')) > MAX_DRAFT_BYTES:
        raise ValueError('Contribution exceeds its size limit')
    return result


class ContributionStore:
    def __init__(self, data_root):
        self.root = make_private_directory(Path(data_root).expanduser().absolute() / 'wiki-contributions')
        self.drafts = make_private_directory(self.root / 'drafts')
        self.metadata = make_private_directory(self.root / 'private')
        self.lock_path = self.root / 'store.lock'

    def _id(self, identifier):
        if not isinstance(identifier, str):
            raise ValueError('Invalid draft ID')
        try:
            if str(uuid.UUID(identifier)) != identifier:
                raise ValueError('Invalid draft ID')
        except (ValueError, AttributeError) as error:
            raise ValueError('Invalid draft ID') from error
        return identifier

    def _read_json(self, root, name, *, missing=None):
        assert_host_owned(root)
        try:
            descriptor = safe_open(root, name)
        except FileNotFoundError:
            return missing
        with os.fdopen(descriptor, 'rb') as stream:
            assert_host_owned(root / name)
            raw = stream.read(MAX_DRAFT_BYTES * 2 + 1)
        if len(raw) > MAX_DRAFT_BYTES * 2:
            raise ValueError('Saved draft exceeds its size limit')
        return json.loads(raw)

    def _save(self, root, identifier, value):
        atomic_write(root / (self._id(identifier) + '.json'), _json(value).encode('utf-8'))

    def _meta(self, identifier):
        return self._read_json(self.metadata, self._id(identifier) + '.json', missing={})

    def list_drafts(self):
        assert_host_owned(self.drafts)
        return [self.read(path.stem) for path in sorted(self.drafts.glob('*.json'))]

    def create(self, kind, source, target, entries, *, origin=None):
        # Only references needed for host replay are accepted; no free-form Run data.
        if origin is not None:
            if (not isinstance(origin, dict) or not origin or set(origin) - {'run_id', 'command_id', 'kind', 'instance_id'}
                    or any(not isinstance(v, str) or not v.strip() or len(v) > 256 for v in origin.values())):
                raise ValueError('Invalid private draft origin')
            _portable(origin)
            origin = dict(origin)
            if 'kind' in origin and origin['kind'] != kind:
                raise ValueError('Private draft origin kind does not match the contribution')
            origin['kind'] = kind
        with FileLock(self.lock_path):
            if origin is not None:
                for path in self.metadata.glob('*.json'):
                    if self._meta(path.stem).get('origin') == origin:
                        return self.read(path.stem)
            identifier = str(uuid.uuid4())
            value = _content(identifier, {'schema_version': 1, 'id': identifier, 'kind': kind,
                'source': source, 'target': target, 'entries': entries})
            pair = lambda item: ', '.join(f'{key} {version}' for key, version in sorted(item.items()))
            title = f'Add {kind} migration knowledge: {pair(source)} to {pair(target)}'
            body = ('Generic version-specific migration knowledge for ' + pair(source) + ' to ' + pair(target)
                + '.\n\nEach entry includes applicability, migration guidance, compatibility, '
                'verification or uncertainty, and portable primary-source references. '
                'Please review the knowledge and cited evidence before accepting this contribution.\n')
            self._save(self.drafts, identifier, {'title': title, 'body': body, 'content': value})
            self._save(self.metadata, identifier, {'origin': origin, 'status': 'draft'})
            return self.read(identifier)

    def read(self, draft_id):
        draft_id = self._id(draft_id)
        saved = self._read_json(self.drafts, draft_id + '.json')
        if saved is None:
            raise FileNotFoundError('Contribution draft does not exist')
        value = _content(draft_id, saved['content'])
        self._text(saved['title'], 'title', 256)
        self._text(saved['body'], 'body', 16000)
        meta = self._meta(draft_id)
        encoded = _json(value)
        return {'id': draft_id, 'title': saved['title'], 'body': saved['body'], 'content': encoded,
                'kind': value['kind'], 'source': value['source'], 'target': value['target'],
                'entries': value['entries'], 'files': {f'contributions/{value["kind"]}/{draft_id}.json': encoded},
                'status': meta.get('status', 'draft'), 'pr_url': meta.get('pr_url')}

    def _text(self, value, label, limit):
        if not isinstance(value, str) or not value.strip() or len(value) > limit or '\x00' in value:
            raise ValueError(f'Invalid contribution {label}')
        _portable(value)
        return value

    def update(self, draft_id, content):
        draft_id = self._id(draft_id)
        if not isinstance(content, dict) or set(content) - {'title', 'body', 'content'} or not content:
            raise ValueError('Edit title, body or contribution content only')
        with FileLock(self.lock_path):
            if self._meta(draft_id).get('status', 'draft') != 'draft':
                raise ValueError('Submission has started; create a new draft to change its content')
            draft = self.read(draft_id)
            title = self._text(content.get('title', draft['title']), 'title', 256)
            body = self._text(content.get('body', draft['body']), 'body', 16000)
            raw = content.get('content', draft['content'])
            if not isinstance(raw, str) or len(raw.encode('utf-8')) > MAX_DRAFT_BYTES:
                raise ValueError('Contribution content must be bounded UTF-8 JSON')
            value = _content(draft_id, json.loads(raw))
            self._save(self.drafts, draft_id, {'title': title, 'body': body, 'content': value})
            return self.read(draft_id)

    def export(self, draft_id, destination):
        """Export editable submission data only, without host receipts or origin."""
        draft = self.read(draft_id)
        destination = Path(destination).expanduser().absolute()
        payload = {key: draft[key] for key in ('id', 'title', 'body', 'content', 'files')}
        atomic_write(destination, _json(payload).encode('utf-8'))
        return {'id': draft['id'], 'path': str(destination), 'files': draft['files']}


class GitHubContributionError(RuntimeError):
    def __init__(self, message, *, status_code=None):
        self.status_code = status_code
        super().__init__(_redact(message))


class GitHubContributor:
    def __init__(self, data_root, *, executable=None):
        self.store = ContributionStore(data_root)
        self.auth_directory = make_private_directory(self.store.root / 'github-auth')
        self.executable = executable or shutil.which('gh')
        self._login_lock = threading.RLock()
        self._login = {'state': 'idle', 'verification_url': None, 'user_code': None}
        self._login_process = None
        self._login_thread = None
        self._credentials = threading.local()

    def _environment(self, *, api=False):
        # Allowlist OS/browser/proxy transport settings, never provider credentials.
        allowed = {'PATH', 'HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'SYSTEMROOT',
                   'WINDIR', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP', 'TMPDIR', 'DISPLAY',
                   'WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS', 'XDG_RUNTIME_DIR',
                   'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY', 'http_proxy',
                   'https_proxy', 'all_proxy', 'no_proxy', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
                   'LANG', 'LC_ALL', 'BROWSER'}
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed or key in allowed}
        env.update({'GH_CONFIG_DIR': str(self.auth_directory), 'GH_HOST': 'github.com',
                    'GH_PAGER': 'cat', 'GH_PROMPT_DISABLED': '1', 'GH_NO_UPDATE_NOTIFIER': '1',
                    'GH_NO_EXTENSION_UPDATE_NOTIFIER': '1', 'NO_COLOR': '1'})
        token = getattr(self._credentials, 'token', None)
        if api and token is not None:
            env['GH_TOKEN'] = token
        return env

    def _launch(self, args, stdin):
        if not self.executable:
            raise GitHubContributionError('GitHub CLI (gh) is not installed; install it to sign in or submit')
        assert_host_owned(self.auth_directory)
        argv = [str(self.executable), *args]
        environment = self._environment(api=bool(args) and args[0] == 'api')
        if os.name == 'nt':
            from .windows_process import launch
            return launch(argv, cwd=self.auth_directory, environment=environment, stdin=stdin)
        return subprocess.Popen(argv, cwd=self.auth_directory, env=environment, stdin=stdin,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)

    def _safe_text(self, value):
        token = getattr(self._credentials, 'token', None)
        text = str(value)
        if token:
            text = text.replace(token, '[redacted]')
        return _redact(text)

    def _read_token(self, login):
        token = self._call(['auth', 'token', '--hostname', 'github.com', '--user', login], sensitive=True).strip()
        if not token or len(token) > 4096 or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in token):
            raise GitHubContributionError('GitHub CLI returned an invalid app account credential; sign in again')
        return token

    @contextmanager
    def _frozen_credentials(self, login):
        # Selecting by user avoids the shared active-host keyring slot. Pin once
        # in this host thread; only API children receive the scoped override.
        token = self._read_token(login)
        previous = getattr(self._credentials, 'token', None)
        self._credentials.token = token
        try:
            yield
        finally:
            if previous is None:
                del self._credentials.token
            else:
                self._credentials.token = previous

    def _cleanup(self, process):
        if os.name == 'nt':
            process.close()
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)

    def _capture(self, process, *, on_output=None):
        buffers = [bytearray(), bytearray()]
        overflow = threading.Event()
        def drain(stream, output):
            while True:
                block = stream.read(1 if on_output else 8192)
                if not block:
                    return
                if len(output) + len(block) <= MAX_OUTPUT_BYTES:
                    output.extend(block)
                else:
                    overflow.set()
                if on_output:
                    on_output(block)
        threads = [threading.Thread(target=drain, args=(stream, buffer), daemon=True)
            for stream, buffer in zip((process.stdout, process.stderr), buffers)]
        for thread in threads:
            thread.start()
        return buffers, threads, overflow

    def _call(self, args, payload=None, *, sensitive=False):
        with tempfile.TemporaryFile() as stdin:
            if payload is not None:
                stdin.write(_json(payload).encode('utf-8'))
                stdin.seek(0)
            try:
                if threading.current_thread() is self._login_thread:
                    # Post-login account lookups belong to the same cancellable
                    # host operation. Publish each active child before releasing
                    # the lock; cancellation either reaps it or prevents dispatch.
                    with self._login_lock:
                        if self._login['state'] == 'cancelled':
                            raise GitHubContributionError('GitHub sign-in was cancelled')
                        process = self._launch(args, stdin)
                        self._login_process = process
                else:
                    process = self._launch(args, stdin)
            except OSError as error:
                raise GitHubContributionError(self._safe_text(f'Cannot start GitHub CLI: {error}')) from None
            buffers, threads, overflow = self._capture(process)
            timed_out = False
            try:
                process.wait(timeout=COMMAND_TIMEOUT)
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                self._cleanup(process)
                for thread in threads:
                    thread.join(timeout=2)
                for stream in (process.stdout, process.stderr):
                    if not stream.closed:
                        stream.close()
            stdout, stderr = (bytes(buffer).decode('utf-8', errors='replace') for buffer in buffers)
            if timed_out:
                detail = '' if sensitive else self._safe_text(stderr)
                raise GitHubContributionError('GitHub request timed out; retry the saved draft. ' + detail)
            if overflow.is_set():
                raise GitHubContributionError('GitHub response exceeded its size limit')
            if process.returncode:
                if sensitive:
                    # Credential commands can contain an opaque token in either
                    # stream, including when they fail. Do not expose either.
                    raise GitHubContributionError('Cannot retrieve the selected app account credential '
                        f'(GitHub CLI exit {process.returncode}); sign in again')
                detail = stderr or stdout or f'GitHub CLI exited with status {process.returncode}'
                match = re.search(r'\bHTTP (\d{3})\b', detail)
                raise GitHubContributionError(self._safe_text(detail), status_code=int(match[1]) if match else None)
            return stdout

    def _api(self, path, method='GET', payload=None):
        args = ['api', '--hostname', 'github.com', '--method', method,
                '-H', 'Accept: application/vnd.github+json', path]
        if payload is not None:
            args += ['--input', '-']
        raw = self._call(args, payload)
        try:
            return json.loads(raw)
        except ValueError as error:
            raise GitHubContributionError('GitHub returned invalid JSON') from error

    def _optional(self, path):
        try:
            return self._api(path)
        except GitHubContributionError as error:
            if error.status_code == 404:
                return None
            raise

    def _configured_login(self):
        # Older gh releases can fall back to a host-wide keyring slot even with
        # an empty GH_CONFIG_DIR. Require our own host registration, and compare
        # its selected account with the API identity instead of trusting that slot.
        try:
            descriptor = safe_open(self.auth_directory, 'hosts.yml')
        except FileNotFoundError as error:
            raise GitHubContributionError('Sign in to GitHub in this app before submitting') from error
        os.close(descriptor)
        assert_host_owned(self.auth_directory / 'hosts.yml')
        login = self._call(['config', 'get', 'user', '--host', 'github.com']).strip()
        if not _LOGIN.fullmatch(login):
            raise GitHubContributionError('GitHub CLI has no selected app account; sign in again')
        return login

    def status(self):
        result = {'available': bool(self.executable), 'authenticated': False, 'login': None,
                  'credential_notice': CREDENTIAL_NOTICE, 'repository': UPSTREAM}
        if not self.executable:
            result['error'] = 'GitHub CLI (gh) is not installed'
            return result
        try:
            selected = self._configured_login()
            user = self._api('user')
            login = user.get('login')
            if not isinstance(login, str) or not _LOGIN.fullmatch(login):
                raise GitHubContributionError('GitHub returned an invalid account identity')
            if login.lower() != selected.lower():
                raise GitHubContributionError('GitHub credential-store account changed outside this app; sign in again')
            result.update(authenticated=True, login=login)
        except GitHubContributionError as error:
            result['error'] = self._safe_text(error)
        return result

    def start_login(self):
        with self._login_lock:
            if self._login_process is not None:
                return self.login_status()
            self._login = {'state': 'starting', 'verification_url': None, 'user_code': None}
            guard = FileLock(self.store.root / 'login.lock', blocking=False)
            try:
                guard.__enter__()
            except BlockingIOError as error:
                self._login.update(state='failed', error='A GitHub sign-in is already running in another host window')
                raise GitHubContributionError('A GitHub sign-in is already running in another host window') from error
            stdin = tempfile.TemporaryFile()
            stdin.write(b'\n')
            stdin.seek(0)
            try:
                process = self._launch(['auth', 'login', '--hostname', 'github.com', '--git-protocol',
                    'https', '--web'], stdin)
            except BaseException as error:
                guard.__exit__(None, None, None)
                stdin.close()
                self._login.update(state='failed', error=_redact(error))
                raise
            self._login_process = process
            self._login_thread = threading.Thread(target=self._watch_login, args=(process, stdin, guard), daemon=True)
            self._login_thread.start()
            return self.login_status()

    def _watch_login(self, process, stdin, guard):
        output = bytearray()
        def observe(block):
            with self._login_lock:
                output.extend(block)
                if len(output) > 16000:
                    del output[:-16000]
                text = bytes(output).decode('utf-8', errors='replace')
                code = re.search(r'\b[A-Z0-9]{4}-[A-Z0-9]{4}\b', text)
                if code:
                    self._login.update(state='waiting', user_code=code[0],
                                       verification_url='https://github.com/login/device')
        buffers, threads, overflow = self._capture(process, on_output=observe)
        timeout = False
        error = None
        try:
            process.wait(timeout=LOGIN_TIMEOUT)
        except subprocess.TimeoutExpired:
            timeout = True
        except Exception as exception:
            error = _redact(exception)
        finally:
            try:
                self._cleanup(process)
                for thread in threads:
                    thread.join(timeout=2)
                for stream in (process.stdout, process.stderr):
                    if not stream.closed:
                        stream.close()
            except Exception as exception:
                error = 'GitHub sign-in cleanup is unconfirmed: ' + _redact(exception)
            stdin.close()
            guard.__exit__(None, None, None)
        identity = self.status() if process.returncode == 0 and not timeout and not error else None
        with self._login_lock:
            if error:
                self._login.update(state='failed', error=error)
            if self._login.get('state') != 'cancelled':
                if timeout:
                    self._login.update(state='expired', error='GitHub sign-in expired; start again')
                elif error:
                    self._login.update(state='failed', error=error)
                elif identity and identity['authenticated']:
                    self._login.update(state='authenticated', login=identity['login'])
                else:
                    self._login.update(state='failed', error=(identity or {}).get('error') or
                        _redact(bytes(output).decode('utf-8', errors='replace')) or 'GitHub sign-in failed')
            self._login_process = None

    def login_status(self):
        with self._login_lock:
            return dict(self._login)

    def cancel_login(self):
        with self._login_lock:
            process, thread = self._login_process, self._login_thread
            if process is None:
                return self.login_status()
            self._login['state'] = 'cancelled'
        self._cleanup(process)
        if thread is not None:
            thread.join(timeout=10)
            if thread.is_alive():
                raise GitHubContributionError('GitHub sign-in cancellation cleanup is unconfirmed')
        return self.login_status()

    def close(self):
        self.cancel_login()

    def submit(self, draft, *, expected_login):
        """Explicit user action: publish own-account branch and create a Draft PR.

        Replays use the saved account, branch and commit receipt. Editing after
        submission starts is disallowed; failures retain all local draft data.
        """
        identifier = self.store._id(draft['id'] if isinstance(draft, dict) else draft)
        if not isinstance(expected_login, str) or not _LOGIN.fullmatch(expected_login):
            raise ValueError('Confirm the signed-in GitHub account before submission')
        guard = FileLock(self.store.root / 'login.lock', blocking=False)
        try:
            guard.__enter__()
        except BlockingIOError as error:
            raise GitHubContributionError('Wait for the current GitHub sign-in before submitting') from error
        try:
            selected = self._configured_login()
            if selected.lower() != expected_login.lower():
                raise GitHubContributionError('Signed-in GitHub account changed; review and confirm the current account')
            with self._frozen_credentials(selected):
                return self._submit_locked(identifier, expected_login)
        finally:
            guard.__exit__(None, None, None)

    def _submit_locked(self, identifier, expected_login):
        with FileLock(self.store.lock_path):
            current = self.status()
            if not current['authenticated']:
                raise GitHubContributionError(current.get('error', 'Sign in to GitHub before submitting'))
            login = current['login']
            if login.lower() != expected_login.lower():
                raise GitHubContributionError('Signed-in GitHub account changed; review and confirm the current account')
            draft = self.store.read(identifier)
            meta = self.store._meta(identifier)
            if meta.get('account') and meta['account'].lower() != login.lower():
                raise GitHubContributionError('This draft is already bound to another GitHub account')
            repo = UPSTREAM if login.lower() == UPSTREAM.split('/')[0].lower() else login + '/' + UPSTREAM.split('/')[1]
            branch = 'release/wiki-' + identifier
            upstream = self._api('repos/' + UPSTREAM)
            base = meta.get('base') or upstream['default_branch']
            meta.update(status='submitting', account=login, repository=repo, branch=branch, base=base)
            self.store._save(self.store.metadata, identifier, meta)
            try:
                return self._submit(draft, meta)
            except Exception as error:
                meta['last_error'] = self._safe_text(error)
                self.store._save(self.store.metadata, identifier, meta)
                raise GitHubContributionError(self._safe_text(error),
                    status_code=getattr(error, 'status_code', None)) from None

    def _submit(self, draft, meta):
        identifier, repo, branch = draft['id'], meta['repository'], meta['branch']
        def save():
            self.store._save(self.store.metadata, identifier, meta)
        # Reconcile PR creation first, including lost responses and closed PRs.
        query = urlencode({'state': 'all', 'head': meta['account'] + ':' + branch, 'base': meta['base'], 'per_page': 100})
        pulls = self._api('repos/' + UPSTREAM + '/pulls?' + query)
        if pulls:
            pull = pulls[0]
            if pull.get('user', {}).get('login', '').lower() != meta['account'].lower():
                raise GitHubContributionError('Existing contribution PR belongs to another account')
            if pull.get('state') == 'open' and not pull.get('draft'):
                raise GitHubContributionError('Contribution PR is no longer a Draft; inspect it on GitHub')
            meta.update(status='submitted', pr_url=pull['html_url'], pr_number=pull['number'])
            meta.pop('last_error', None)
            save()
            return self.store.read(identifier)
        target_repo = self._optional('repos/' + repo)
        if target_repo is None and repo != UPSTREAM:
            self._api('repos/' + UPSTREAM + '/forks', 'POST', {'default_branch_only': True})
            target_repo = self._optional('repos/' + repo)
            if target_repo is None:
                raise GitHubContributionError('GitHub is still preparing your fork; retry this saved draft shortly')
        if not target_repo:
            raise GitHubContributionError('Contribution repository is unavailable')
        if repo != UPSTREAM and target_repo.get('parent', {}).get('full_name', '').lower() != UPSTREAM.lower():
            raise GitHubContributionError('Your account has a same-name repository that is not the expected wiki fork')
        if not target_repo.get('permissions', {}).get('push', False):
            raise GitHubContributionError('Signed-in account cannot write to its contribution repository; check GitHub permissions')
        ref_path = 'repos/' + repo + '/git/ref/heads/' + quote(branch, safe='/')
        ref = self._optional(ref_path)
        if not meta.get('parent_commit'):
            if ref is not None:
                # Never adopt an arbitrary pre-existing branch without our receipt.
                raise GitHubContributionError('Contribution branch exists without a local creation receipt')
            base_ref = self._api('repos/' + UPSTREAM + '/git/ref/heads/' + quote(meta['base'], safe='/'))
            meta['parent_commit'] = base_ref['object']['sha']
            save()
        if ref is None:
            self._api('repos/' + repo + '/git/refs', 'POST',
                      {'ref': 'refs/heads/' + branch, 'sha': meta['parent_commit']})
        if not meta.get('commit'):
            parent = self._api('repos/' + repo + '/git/commits/' + meta['parent_commit'])
            tree = self._api('repos/' + repo + '/git/trees', 'POST', {'base_tree': parent['tree']['sha'],
                'tree': [{'path': path, 'mode': '100644', 'type': 'blob', 'content': content}
                         for path, content in draft['files'].items()]})
            commit = self._api('repos/' + repo + '/git/commits', 'POST',
                {'message': draft['title'], 'tree': tree['sha'], 'parents': [meta['parent_commit']]})
            meta['commit'] = commit['sha']
            save()
        # Always restore the recorded commit, including a recreated branch after
        # a failed PR request. Non-forcing updates reject unrelated head changes
        # without adding a locally computed identity or checksum comparison.
        self._api('repos/' + repo + '/git/refs/heads/' + quote(branch, safe='/'), 'PATCH',
                  {'sha': meta['commit'], 'force': False})
        meta['branch_updated'] = True
        save()
        pull = self._api('repos/' + UPSTREAM + '/pulls', 'POST',
            {'title': draft['title'], 'body': draft['body'], 'head': meta['account'] + ':' + branch,
             'base': meta['base'], 'draft': True, 'maintainer_can_modify': True})
        meta.update(status='submitted', pr_url=pull['html_url'], pr_number=pull['number'])
        meta.pop('last_error', None)
        save()
        return self.store.read(identifier)
