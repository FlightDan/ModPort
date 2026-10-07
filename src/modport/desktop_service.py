"""Authenticated loopback API for the native ModPort renderer."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

from .desktop_i18n import (t, stage_label, language_scope, request_language,
                           owned_display, validation_message, localize_run_display,
                           git_reason_display, warning_display)
from .desktop_driver import PersistentSupervisor
from .desktop_model_settings import ModelSettingsStore
from .desktop_local_source import inspect_local_source, prepare_local_workspace
from .desktop_state import DesktopState, INSTANCE_ID, read_json, publish_snapshot_safely
from .model_policy import validate_model_config, resolve_model_selection, ROLE_STAGES
from .models import Budget, MigrationRequest
from .workflow import WORKFLOW_VERSION, AGENT_STAGES, REVIEW_STAGES
from .user_paths import configured_path

MAX_BODY = 65536
MAX_RESPONSE = 4 * 1024 * 1024


def repository_address(value):
    if not isinstance(value, str) or len(value) > 500 or any(ord(c) < 32 for c in value):
        raise ValueError(t('请提供有效的 GitHub 或 Gitee HTTPS 仓库地址。'))
    parsed = urlsplit(value.strip())
    if parsed.scheme != 'https' or parsed.hostname not in {'github.com', 'gitee.com'} or parsed.username or parsed.password or parsed.port is not None or parsed.query or parsed.fragment:
        raise ValueError(t('仅支持不含凭据、端口或参数的 GitHub / Gitee HTTPS 仓库地址。'))
    match = re.fullmatch(r'/([A-Za-z0-9][A-Za-z0-9_.-]{0,99})/([A-Za-z0-9][A-Za-z0-9_.-]{0,99})/?', parsed.path)
    if not match:
        raise ValueError(t('仓库地址应为 https://github.com/owner/repository 或 Gitee 同类地址。'))
    owner, name = match.groups()
    name = name[:-4] if name.endswith('.git') else name
    if owner in {'.', '..'} or name in {'', '.', '..'}:
        raise ValueError(t('仓库路径不正确。'))
    return parsed.hostname, owner, name


def revision_name(value):
    if value is None or value == '':
        return None
    if not isinstance(value, str) or len(value) > 200 or value.startswith('-') or any(c.isspace() or ord(c) < 32 for c in value) or any(part in value for part in ('..', '@{', '\\')):
        raise ValueError(t('分支或标签名称不正确。'))
    if any(c in value for c in '~^:?*['):
        raise ValueError(t('请提供分支或标签名称，不使用 Git 版本表达式。'))
    return value


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError(t('Repository metadata redirects are not followed'))


def _read_remote(url, *, timeout=8, limit=512 * 1024):
    opener = build_opener(_NoRedirect())
    request = Request(url, headers={'User-Agent': 'ModPort-Desktop', 'Accept': 'text/plain'})
    try:
        with opener.open(request, timeout=timeout) as response:
            content = response.read(limit + 1)
            if len(content) > limit:
                raise ValueError(t('Repository metadata file exceeds the read limit'))
            return content.decode('utf-8', errors='replace')
    except HTTPError as error:
        if error.code == 404:
            return ''
        raise


def detect_versions(documents):
    """Read literals only. Gradle, wrappers and repository scripts never run."""
    text = '\n'.join(documents.values())
    values = {}
    for key, value in re.findall(r'^\s*([A-Za-z_][\w.-]*)\s*[=:]\s*["\']?([^\s"\'\r\n]+)', text, re.MULTILINE):
        if re.fullmatch(r'[0-9][0-9A-Za-z_.+-]*', value):
            values.setdefault(key.lower(), value)
    result = {}
    identifiers = re.findall(r'^\s*mod_id\s*[=:]\s*["\']?([a-z][a-z0-9_]{0,63})["\']?\s*(?:#.*)?$', text, re.MULTILINE)
    if not identifiers:
        identifiers = re.findall(r'^\s*modId\s*=\s*["\']([a-z][a-z0-9_]{0,63})["\']', text, re.MULTILINE)
    if identifiers:
        result['mod_id'] = identifiers[0]
    minecraft = next((values[key] for key in ['minecraft_version', 'minecraftversion', 'mc_version'] if key in values), None)
    neo = next((values[key] for key in ['neo_version', 'neoforge_version'] if key in values), None)
    forge = values.get('forge_version')
    fabric = values.get('fabric_loader_version') or values.get('loader_version')
    direct = re.search(r'net\.minecraftforge:forge:([0-9.]+)-([0-9.]+)', text)
    if direct:
        minecraft = minecraft or direct.group(1)
        forge = forge or direct.group(2)
    if minecraft:
        result['source_minecraft'] = minecraft
    if neo or 'net.neoforged' in text:
        result['source_loader'] = 'neoforge'
        if neo:
            result['source_loader_version'] = neo
    elif forge or 'net.minecraftforge' in text:
        result['source_loader'] = 'forge'
        if forge:
            result['source_loader_version'] = forge
    elif 'fabric-loom' in text or 'net.fabricmc' in text or values.get('fabric_loader_version'):
        result['source_loader'] = 'fabric'
        if fabric:
            result['source_loader_version'] = fabric
    return result


def inspect_repository(repository, revision=None, *, cwd, reader=_read_remote):
    from .platform_runtime import capture_process
    host, owner, name = repository_address(repository)
    revision = revision_name(revision)
    remote = f'https://{host}/{owner}/{name}.git'
    environment = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull}
    observed = capture_process(['git', '-c', 'http.followRedirects=false', 'ls-remote', '--symref', remote],
                               cwd=cwd, timeout=30, environment=environment, max_output_bytes=1024 * 1024)
    if observed.timed_out or observed.drain_incomplete or observed.returncode != 0:
        raise ValueError(t('无法读取远程仓库：{detail}', detail=observed.stderr.decode('utf-8', errors='replace')[-1500:]))
    if observed.stdout_truncated:
        raise ValueError(t('仓库引用列表超出读取上限，请指定较小的仓库或手动填写版本。'))
    raw = observed.stdout.decode('utf-8', errors='replace')
    branches, tags = [], []
    default = None
    warnings = []
    for line in raw.splitlines():
        if line.startswith('ref: refs/heads/') and line.endswith('\tHEAD'):
            default = line.split('\t')[0].removeprefix('ref: refs/heads/')
        elif '\trefs/heads/' in line:
            branches.append(line.split('\trefs/heads/', 1)[1])
        elif '\trefs/tags/' in line and not line.endswith('^{}'):
            tags.append(line.split('\trefs/tags/', 1)[1])
    if not default:
        warnings.append('未读到仓库默认分支，请明确选择分支或标签。')
    if len(branches) > 1000 or len(tags) > 1000:
        warnings.append('分支或标签较多，仅展示前 1000 项；仍可直接输入名称。')
    selected = revision or default
    detected = {}
    if selected:
        ref = quote(selected, safe='')
        paths = ['gradle.properties', 'build.gradle', 'build.gradle.kts', 'gradle/libs.versions.toml', 'src/main/resources/META-INF/mods.toml', 'src/main/resources/META-INF/neoforge.mods.toml']
        prefix = f'https://raw.githubusercontent.com/{owner}/{name}/{ref}/' if host == 'github.com' else f'https://gitee.com/{owner}/{name}/raw/{ref}/'
        def read(path):
            try:
                return path, reader(prefix + path)
            except (OSError, ValueError) as error:
                return path, ''
        with ThreadPoolExecutor(max_workers=4) as pool:
            documents = dict(pool.map(read, paths))
        detected = detect_versions(documents)
        if not documents.get('gradle.properties') and not documents.get('build.gradle') and not documents.get('build.gradle.kts'):
            warnings.append('未读取到构建版本声明。仓库可能需要认证，或使用其他目录结构；请手动确认版本。')
    if not detected.get('source_minecraft'):
        warnings.append('未能可靠识别源 Minecraft 版本，请手动填写。')
    return {'branches': sorted(set(branches))[:1000], 'tags': sorted(set(tags))[:1000], 'default_revision': default,
            'detected': detected, 'warnings': warnings}


class DesktopApplication:
    def __init__(self, data_root, *, operations=None, supervisor=None):
        self.state = DesktopState(data_root)
        self.model_settings = ModelSettingsStore(self.state.root)
        self.operations = operations
        self.supervisor = supervisor or PersistentSupervisor(self.state)
        self._creation = threading.Lock()
        self._repository_cache = {}
        self._repository_lock = threading.Lock()
        self._local_sources = {}
        from .wiki_contributions import ContributionStore, GitHubContributor
        self.wiki_contributions = ContributionStore(self.state.root)
        self.github = GitHubContributor(self.state.root)

    def environment(self):
        from .sdk_compat import sdk_release
        checks = [{'id': 'python', 'label': 'Python 3.10+', 'ready': sys.version_info >= (3, 10), 'detail': sys.version.split()[0]}]
        try:
            checks.append({'id': 'sdk', 'label': 'Dispatcher SDK', 'ready': True, 'detail': sdk_release()})
        except (ValueError, RuntimeError) as error:
            checks.append({'id': 'sdk', 'label': 'Dispatcher SDK', 'ready': False, 'detail': str(error)})
        for binary, label, guidance in [('git', 'Git', t('安装 Git 并加入 PATH；仓库识别只读取代码声明。')), ('opencode', 'OpenCode', t('需要 OpenCode 1.18.32，并在本机配置模型提供方凭据。')), ('java', 'Java', t('安装迁移版本需要的 JDK；具体版本在实例环境准备中解析。'))]:
            found = (os.environ.get('MODPORT_OPENCODE_BIN') or os.environ.get('OPENCODE_BIN') or shutil.which(binary)) if binary == 'opencode' else shutil.which(binary)
            valid = bool(found)
            detail = found or guidance
            if binary == 'opencode' and found:
                from .opencode_runtime import EXPECTED_OPENCODE_VERSION
                from .platform_runtime import capture_process
                try:
                    observed = capture_process([found, '--version'], cwd=self.state.root, timeout=5, max_output_bytes=2048)
                    version = observed.stdout.decode('utf-8', errors='replace').strip()
                    valid = observed.returncode == 0 and not observed.timed_out and version == EXPECTED_OPENCODE_VERSION
                    detail = t('{path} · {version} · 需要 {required}', path=found, version=version or t('版本未知'), required=EXPECTED_OPENCODE_VERSION)
                except (OSError, ValueError) as error:
                    valid, detail = False, str(error)
            checks.append({'id': binary, 'label': label, 'ready': valid, 'detail': detail})
        if platform.system() == 'Linux':
            sandbox = shutil.which('bwrap')
            checks.append({'id': 'sandbox', 'label': t('项目代码隔离执行'), 'ready': bool(sandbox), 'detail': sandbox or t('需要 bubblewrap；项目代码只能在无凭据沙箱中执行。')})
        elif platform.system() == 'Windows':
            checks.append({'id': 'sandbox', 'label': t('Windows AppContainer 沙箱'), 'ready': sys.getwindowsversion().major >= 10, 'detail': t('执行前会验证原生隔离和清理能力；Windows 真实主机验收尚未完成。')})
        ready, detail = self.supervisor.check()
        checks.append({'id': 'supervisor', 'label': t('持久执行监督器'), 'ready': ready, 'detail': owned_display(detail)})
        checks.append({'id': 'workspace', 'label': t('独立实例目录'), 'ready': True, 'detail': str(self.state.instances), 'action': 'prepare_workspace', 'action_label': t('准备工作目录')})
        return {'ready': all(check['ready'] for check in checks), 'checks': checks}

    def bootstrap(self):
        config = self.model_settings.read()['model_config']
        roles = [{'id': 'default', 'label': t('默认任务'), 'group': 'author', 'target': 'default', 'selection': config['default']}]
        names = {'planner': t('规划'), 'coder': t('代码编写'), 'supervisor': t('监督'), 'contract_review': t('需求审查'), 'summary': t('上下文整理'), 'subagent': t('子任务')}
        for identifier in ROLE_STAGES:
            roles.append({'id': identifier, 'label': names.get(identifier, stage_label(identifier)), 'group': 'review' if identifier in {'supervisor', 'contract_review'} else 'author', 'target': 'role', 'selection': resolve_model_selection(config, 'subagent') if identifier == 'subagent' else config['roles'].get(identifier, config['default'])})
        owned = set().union(*ROLE_STAGES.values())
        for stage in sorted(AGENT_STAGES - owned):
            roles.append({'id': 'stage:' + stage, 'stage': stage, 'label': stage_label(stage), 'group': 'review' if stage in REVIEW_STAGES else 'author', 'target': 'stage', 'selection': resolve_model_selection(config, stage)})
        return {'workflow_version': WORKFLOW_VERSION, 'platform': platform.system(), 'defaults': {'max_seconds': Budget().max_seconds, 'max_tokens': 2000000, 'source_loader': 'forge', 'target_loader': 'neoforge'}, 'model_config': config, 'roles': roles, 'environment': self.environment(), 'recent_runs': self.state.recent()}

    def create_run(self, body):
        from .operations import MigrationOperations
        allowed = {'project_name', 'source_mode', 'local_source_token', 'source_repository', 'source_revision', 'source_minecraft', 'target_minecraft', 'source_loader', 'target_loader', 'source_loader_version', 'target_loader_version', 'max_seconds', 'max_tokens', 'model_config', 'max_parallel_coders', 'local_workspace_mode', 'local_branch_name', 'direct_workspace_confirmed'}
        if set(body) - allowed:
            raise ValueError(t('不支持的项目参数：{fields}', fields=', '.join(sorted(set(body) - allowed))))
        name = body.get('project_name')
        if not isinstance(name, str) or not name.strip() or len(name) > 120 or any(ord(c) < 32 for c in name):
            raise ValueError(t('项目名称须包含 1–120 个字符。'))
        source_mode = body.get('source_mode', 'remote')
        if source_mode not in ('remote', 'local'):
            raise ValueError(t('请选择远程仓库或本地代码库。'))
        local = None
        if source_mode == 'local':
            if body.get('source_repository') or body.get('source_revision'):
                raise ValueError(t('本地模式使用所选目录的当前文件，不接受远程地址或分支。'))
            token = body.get('local_source_token')
            with self._repository_lock:
                local = self._local_sources.get(token) if isinstance(token, str) else None
            if local is None:
                raise ValueError(t('请通过“选择文件夹”重新选择本地代码库。'))
            inspected = inspect_local_source(local['path'], application_root=self.state.root)
            workspace_mode = body.get('local_workspace_mode') or ('git_worktree' if inspected['git']['can_branch'] else 'copy')
            if workspace_mode not in {'git_worktree', 'copy', 'direct'}:
                raise ValueError(t('请选择新分支、复制工作区或直接开发。'))
            branch = body.get('local_branch_name')
            if workspace_mode != 'git_worktree' and branch:
                raise ValueError(t('只有新建 Git 分支模式接受分支名称。'))
            if workspace_mode == 'direct' and body.get('direct_workspace_confirmed') is not True:
                raise ValueError(t('请确认直接修改所选目录，不新建分支、不复制备份。'))
            if workspace_mode != 'direct' and body.get('direct_workspace_confirmed'):
                raise ValueError(t('直接开发确认只能用于直接开发模式。'))
            detected = detect_versions(inspected['documents'])
            repository = inspected['name']
            source_repository, revision = Path(inspected['path']).as_uri(), None
        else:
            if any(key in body for key in ('local_source_token', 'local_workspace_mode', 'local_branch_name', 'direct_workspace_confirmed')):
                raise ValueError(t('远程模式不接受本地目录参数。'))
            host, owner, repository = repository_address(body.get('source_repository'))
            source_repository = f'https://{host}/{owner}/{repository}.git'
            revision = revision_name(body.get('source_revision'))
            with self._repository_lock:
                cached = self._repository_cache.get((host, owner, repository, revision))
            detected = cached['detected'] if cached and time.time() - cached['observed_at'] < 3600 else {}
        for field in ('max_seconds', 'max_tokens'):
            if type(body.get(field)) is not int or not 0 < body[field] <= 2 ** 53 - 1:
                raise ValueError(t('{field} 必须是正整数。', field=field))
        selected = validate_model_config(body.get('model_config'))
        parallel = body.get('max_parallel_coders', 3)
        if type(parallel) is not int or not 1 <= parallel <= 16:
            raise ValueError(t('并行代码任务数须为 1–16。'))
        mod_id = detected.get('mod_id') or re.sub(r'[^a-z0-9_]', '_', repository.lower()).strip('_') or 'modport_project'
        identity_notice = None if detected.get('mod_id') else '未识别到源码中的 mod_id 字面量。本次暂用规范化仓库名称 ' + mod_id + '；项目显示名称独立保存，实际源码身份仍由迁移源码读取确认。'
        request = MigrationRequest(mod_id=mod_id, source_repository=source_repository, source_revision=revision,
                                   source_minecraft=body.get('source_minecraft'), target_minecraft=body.get('target_minecraft'),
                                   source_loader=body.get('source_loader', 'forge'), target_loader=body.get('target_loader', 'neoforge'),
                                   source_loader_version=body.get('source_loader_version') or None, target_loader_version=body.get('target_loader_version') or None,
                                   skill_store=str(configured_path(os.environ.get('MODPORT_SKILL_STORE') or self.state.root / 'migration-skills')),
                                   budget=Budget(max_seconds=body['max_seconds'], max_tokens=body['max_tokens']), max_parallel_coders=parallel)
        request.validate()
        if request.source_loader not in {'forge', 'neoforge', 'fabric'} or request.target_loader not in {'forge', 'neoforge', 'fabric'}:
            raise ValueError(t('加载器须为 Forge、NeoForge 或 Fabric。'))
        with self._creation:
            environment = self.environment()
            if not environment['ready']:
                raise ValueError(t('运行环境尚未就绪，请先查看运行环境。'))
            instance_id = 'desktop-' + uuid.uuid4().hex
            root = self.state.run_dir(instance_id)
            source_details = None
            if local is not None:
                # Reserve originals before preparation so two desktop windows
                # cannot submit writers for overlapping direct workspaces.
                if workspace_mode == 'direct':
                    self.state.reserve_workspace(instance_id, local['path'])
                try:
                    source_details = prepare_local_workspace(local['path'], application_root=self.state.root,
                        snapshot_id=instance_id, mode=workspace_mode, branch=branch)
                    self.state.reserve_workspace(instance_id, source_details['workspace']['path'])
                except BaseException:
                    self.state.release_workspace(instance_id)
                    raise
                request = replace(request, source_repository=source_details['source_repository'],
                                  source_revision=source_details['source_revision'],
                                  source_snapshot=source_details['source_snapshot'],
                                  local_workspace=source_details['workspace'])
                request.validate()
                notices = [identity_notice, f"本地源码快照包含 {source_details['file_count']} 个文件，已排除 {source_details['excluded_count']} 项。", *source_details['warnings']]
                identity_notice = '\n'.join(note for note in notices if note)
            operations = self.operations or MigrationOperations()
            try:
                run = operations.submit(request, run_dir=root, run_id=instance_id, model_policy=selected)
            except BaseException:
                self.state.release_workspace(instance_id)
                raise
            self.state.register(instance_id, name.strip(), source_identity_notice=identity_notice, source_details=source_details)
            header = read_json(root / 'run.json')
            publish_snapshot_safely(header, run.snapshot, force=True)
            try:
                environment = {**self.model_settings.runtime_environment(),
                               'MODPORT_DATA_ROOT': str(self.state.root)}
                self.supervisor.launch(instance_id, environment=environment)
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
                self.state.update_launch(instance_id, error='持久监督器启动失败：' + str(error))
                raise RuntimeError(t('实例 {instance_id} 已保存，但持久驱动未能启动：{detail}', instance_id=instance_id, detail=error)) from error
            status = self.state.status(instance_id)
            return {key: status[key] for key in ('id', 'project_name', 'status')}

    def setup(self, body):
        if set(body) != {'action'} or body['action'] not in {'prepare_workspace', 'check_environment'}:
            raise ValueError(t('不支持此环境准备操作。'))
        if body['action'] == 'prepare_workspace':
            for name in ['instances', 'supervision', 'cache']:
                path = self.state.root / name
                if path.is_symlink():
                    raise ValueError(t('工作目录不能是符号链接。'))
                path.mkdir(exist_ok=True, mode=0o700)
        return {'message': t('工作目录已准备。') if body['action'] == 'prepare_workspace' else t('环境检查已完成。'), 'environment': self.environment()}

    def request(self, method, path, body=None):
        if method not in {'GET', 'POST'} or not isinstance(path, str) or '?' in path or '#' in path or '%' in path:
            raise ValueError(t('Unsupported application request'))
        if path.startswith('/api/github/'):
            if path == '/api/github/status' and method == 'GET':
                return self.github.status()
            if path == '/api/github/login':
                if method == 'GET':
                    return self.github.login_status()
                if body not in (None, {}):
                    raise ValueError('GitHub login does not accept credentials')
                return self.github.start_login()
            if path == '/api/github/login/cancel' and method == 'POST':
                if body not in (None, {}):
                    raise ValueError('GitHub login cancellation does not accept parameters')
                return self.github.cancel_login()
            raise KeyError(path)
        if path == '/api/wiki/contributions' and method == 'GET':
            # List previews rather than duplicating every research body and file
            # payload; the renderer loads the selected draft through its ID route.
            keys = ('id', 'title', 'kind', 'source', 'target', 'status', 'pr_url')
            return {'drafts': [{key: draft[key] for key in keys if key in draft}
                              for draft in self.wiki_contributions.list_drafts()]}
        if path == '/api/wiki/export' and method == 'POST':
            if not isinstance(body, dict) or set(body) != {'instance_id'}:
                raise ValueError('Contribution export requires an instance ID')
            instance_id = body['instance_id']
            self.state.status(instance_id)
            from .wiki_knowledge import export_run_findings
            return export_run_findings(self.state.run_dir(instance_id), store_root=self.state.root)
        if path == '/api/wiki/update' and method == 'POST':
            if not isinstance(body, dict) or set(body) - {'revision'}:
                raise ValueError('Wiki update accepts only a knowledge revision')
            from .wiki_knowledge import refresh_cache
            return refresh_cache(revision=body.get('revision'), cache_root=self.state.root / 'wiki-cache')
        contribution = re.fullmatch(r'/api/wiki/contributions/([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})(/submit)?', path)
        if contribution:
            draft_id, action = contribution.groups()
            if action and method == 'POST':
                if not isinstance(body, dict) or set(body) != {'expected_login'}:
                    raise ValueError('Contribution submission requires the displayed GitHub account')
                return self.github.submit(draft_id, expected_login=body['expected_login'])
            if not action and method == 'GET':
                return self.wiki_contributions.read(draft_id)
            if not action and method == 'POST':
                return self.wiki_contributions.update(draft_id, body)
            raise KeyError(path)
        if method == 'GET' and path == '/api/bootstrap':
            return self.bootstrap()
        if method == 'GET' and path == '/api/model-settings':
            return self.model_settings.read()
        if method == 'POST':
            if not isinstance(body, dict):
                raise ValueError(t('请求必须是 JSON 对象。'))
            if path == '/api/model-settings':
                with self._creation:
                    return self.model_settings.save(body)
            if path == '/api/local-source':
                # Native main process only: excluded from the renderer's API allowlist.
                if set(body) != {'path'}:
                    raise ValueError(t('不支持的本地目录参数。'))
                inspected = inspect_local_source(body['path'], application_root=self.state.root)
                token = uuid.uuid4().hex
                with self._repository_lock:
                    if len(self._local_sources) >= 32:
                        self._local_sources.pop(next(iter(self._local_sources)))
                    self._local_sources[token] = {'path': inspected['path']}
                return {'token': token, 'display_path': inspected['path'], 'name': inspected['name'],
                        'detected': detect_versions(inspected['documents']), **warning_display(inspected['warnings']),
                        'git': git_reason_display(inspected['git'])}
            if path == '/api/repository':
                if set(body) - {'repository', 'revision'}:
                    raise ValueError(t('Unsupported repository parameters'))
                repository = body.get('repository')
                result = inspect_repository(repository, body.get('revision'), cwd=self.state.root)
                host, owner, name = repository_address(repository)
                selected = revision_name(body.get('revision')) or result.get('default_revision')
                with self._repository_lock:
                    if len(self._repository_cache) >= 32:
                        self._repository_cache.pop(next(iter(self._repository_cache)))
                    self._repository_cache[(host, owner, name, selected)] = {'detected': dict(result['detected']), 'observed_at': time.time()}
                return {**result, **warning_display(result.get('warnings', []))}
            if path == '/api/runs':
                return self.create_run(body)
            if path == '/api/setup':
                return self.setup(body)
        match = re.fullmatch(r'/api/runs/(desktop-[a-f0-9]{32})(?:/(chat|cancel))?', path)
        if not match:
            raise KeyError(t('Unknown application endpoint'))
        instance_id, action = match.groups()
        self.state.get(instance_id)
        if method == 'GET' and action is None:
            return localize_run_display(self.state.status(instance_id))
        if method == 'POST' and action == 'chat' and set(body) == {'message'}:
            return self.state.enqueue_message(instance_id, body['message'])
        if method == 'POST' and action == 'cancel' and set(body) == {'confirmed'} and body['confirmed'] is True:
            from .operations import MigrationOperations
            operations = self.operations or MigrationOperations()
            root = self.state.run_dir(instance_id)
            header = read_json(root / 'run.json')
            self.supervisor.suppress_watchdog(instance_id, reason='user_cancelled')
            run = operations.cancel(root, header['run_id'])
            publish_snapshot_safely(header, run.snapshot, force=True)
            return localize_run_display(self.state.status(instance_id))
        raise ValueError(t('Unsupported lifecycle request'))


class DesktopServer(ThreadingHTTPServer):
    # Accepted mutations belong to the service even after an HTTP disconnect.
    # server_close waits for their completion before the native shell exits.
    daemon_threads = False
    allow_reuse_address = False

    def server_close(self):
        try:
            github = getattr(self.application, 'github', None)
            if github is not None:
                github.close()
        finally:
            super().server_close()

    def __init__(self, address, application, token):
        if address[0] != '127.0.0.1' or not isinstance(token, str) or not re.fullmatch(r'[a-f0-9]{64}', token):
            raise ValueError('desktop service requires IPv4 loopback and a random 32-byte token')
        self.application = application
        self.token = token
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(address, DesktopHandler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class DesktopHandler(BaseHTTPRequestHandler):
    server_version = 'ModPortDesktop'
    sys_version = ''

    def setup(self):
        self.request.settimeout(10)
        super().setup()

    def log_message(self, *args):
        pass

    def reply(self, code, data, content_type='application/json; charset=utf-8'):
        payload = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(payload) > MAX_RESPONSE:
            code, payload = 503, json.dumps({'error': t('Application response exceeds its limit')}, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(payload)

    def handle_api(self):
        # Each handler thread owns its language; concurrent requests cannot
        # change one another's labels or validation feedback.
        with language_scope(request_language(self.headers.get('Accept-Language'))):
            self._handle_api()

    def _handle_api(self):
        expected_host = f'127.0.0.1:{self.server.server_address[1]}'
        if self.headers.get('Host') != expected_host or self.headers.get('Origin') not in (None, 'http://' + expected_host) or self.headers.get('Sec-Fetch-Site') == 'cross-site':
            self.reply(403, {'error': t('请求来源不正确。')})
            return
        supplied = self.headers.get('Authorization', '')
        if len(supplied) > 100 or not hmac.compare_digest(supplied.encode('utf-8'), ('Bearer ' + self.server.token).encode('ascii')):
            self.reply(401, {'error': t('Application authentication required')})
            return
        try:
            body = None
            if self.command == 'POST':
                if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
                    self.reply(415, {'error': t('请求须使用 JSON。')})
                    return
                length = int(self.headers.get('Content-Length', '0'))
                body_limit = 2 * 1024 * 1024 if self.path.startswith('/api/wiki/contributions/') else MAX_BODY
                if length <= 0 or length > body_limit:
                    self.reply(413, {'error': t('请求大小不正确。')})
                    return
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError(t('Incomplete request body'))
                body = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(t('JSON numbers must be finite'))))
            data = self.server.application.request(self.command, self.path, body)
            self.reply(200, data)
        except KeyError:
            self.reply(404, {'error': t('实例或接口不存在。')})
        except (BrokenPipeError, ConnectionResetError):
            # The accepted operation has completed; a disconnected renderer
            # cannot receive a reply and does not require another response.
            return
        except (ValueError, TypeError) as error:
            self.reply(400, {'error': validation_message(error)})
        except (OSError, RuntimeError, subprocess.TimeoutExpired, socket.timeout) as error:
            self.reply(503, {'error': str(error)})
        except Exception:
            self.reply(503, {'error': t('本机服务无法完成此请求，请查看实例诊断。')})

    def do_GET(self):
        self.handle_api()

    def do_POST(self):
        self.handle_api()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Authenticated ModPort desktop application service')
    parser.add_argument('--data-root', required=True)
    parser.add_argument('--token-stdin', action='store_true', required=True)
    args = parser.parse_args(argv)
    root = Path(args.data_root)
    if not root.is_absolute():
        parser.error('--data-root must be absolute')
    token = sys.stdin.readline(129).strip()
    application = DesktopApplication(root)
    with DesktopServer(('127.0.0.1', 0), application, token) as server:
        def close_with_parent():
            # Only the inherited native-shell pipe controls service shutdown.
            # EOF stops admission; accepted requests drain via server_close.
            while sys.stdin.buffer.read(4096):
                pass
            server.shutdown()
        threading.Thread(target=close_with_parent, name='desktop-parent-pipe', daemon=True).start()
        print(json.dumps({'port': server.server_address[1]}), flush=True)
        try:
            server.serve_forever(poll_interval=.25)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
