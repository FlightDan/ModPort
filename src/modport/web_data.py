"""Bounded, read-only web projections of observed SDK state.

Only an online backup is read through the Orchestrator. No live runtime, handlers,
notification consumer, or migration operation is opened by this module.
"""
from copy import deepcopy
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time

from dispatcher_sdk.orchestrator import Orchestrator
from dispatcher_sdk.storage import inspect_storage

from .contracts import FORMAT_VERSION
from .sdk_compat import SDK_VERSION, sdk_release
from .snapshot_storage import SnapshotLimitError, snapshot_databases
from .workflow import MAIN_STAGES, REVIEW_STAGES, stage_routes
from .application_state_storage import hydrate_run_snapshot

_LABELS = {
    'skill_resolve': '解析现有迁移规则',
    'build_prepare': '准备目标构建',
    'codemod': '机械迁移',
    'early_compile': '早期目标编译',
    'source': '获取源码',
    'background': '背景资料',
    'preparation': '准备迁移',
    'project_init': '初始化项目',
    'environment': '准备环境',
    'baseline_build': '基线构建',
    'skill_lookup': '查找迁移知识',
    'skill_publish': '发布迁移知识',
    'mod_scan': '扫描模组',
    'mod_analysis': '分析模组',
    'contract_draft': '起草行为契约',
    'contract_verify': '验证行为契约',
    'contract_review': '审查行为契约',
    'contract_freeze': '冻结行为契约',
    'migration_inventory': '迁移清单',
    'migration_plan': '迁移方案',
    'migration_tasks': '拆分迁移任务',
    'parallel_review': '并行方案审查',
    'implementation': '执行迁移',
    'development_integrate': '集成开发结果',
    'target_build': '目标构建',
    'code_review': '代码审查',
    'test_design': '设计测试',
    'test_review': '测试审查',
    'test_execute': '执行测试',
    'acceptance_preflight': '验收预检',
    'acceptance_build': '验收构建',
    'client_smoke': '客户端冒烟测试',
    'gap_review': '缺口审查',
    'delivery': '交付',
}
_LABELS.update(platform_diff='平台差异研究', java_diff='Java 差异研究',
    platform_skill_review='平台知识审查', java_skill_review='Java 知识审查',
    gap_research='补充研究', coder='编码任务', goal_prepare='准备编码目标',
    development_prepare='开发准备', development_prepare_integrate='集成开发准备',
    contract_restore='恢复行为契约', contract_revise='修订行为契约', target_revise='修订目标实现',
    contract_repair_integrate='集成契约修复', target_repair_integrate='集成目标修复',
    gap_plan='缺口研究计划', gap_plan_review='缺口计划审查', research_review='研究结果审查',
    admin_review='管理员审查', knowledge_publish='发布知识', supervisor='监督检查')
for _scope, _name in [('contract', '契约'), ('target', '目标')]:
    for _suffix, _action in [('diagnose', '诊断'), ('repair_plan', '修复计划'),
                              ('repair_tasks', '修复任务'), ('repair_review', '修复审查')]:
        _LABELS[_scope + '_' + _suffix] = _name + _action
_GROUPS = [('prepare', '源码与环境准备', ('source', 'background', 'preparation', 'environment',
                                      'project_init', 'baseline_build', 'skill_lookup', 'skill_publish',
                                      'skill_resolve', 'build_prepare', 'codemod', 'early_compile')),
           ('analysis', '分析与契约', MAIN_STAGES[8:14]),
           ('planning', '迁移规划', MAIN_STAGES[14:18]),
           ('implementation', '迁移执行', MAIN_STAGES[18:20]),
           ('validation', '构建与验证', MAIN_STAGES[20:-1]),
           ('delivery', '交付', ('delivery',)), ('support', '研究、返工与支持', ())]
_ACTIVE = {'running', 'leased', 'queued', 'pending_dispatch', 'recovery_required'}
_TERMINAL = {'succeeded', 'failed', 'cancelled'}
_LIST_SNAPSHOT_MAX_BYTES = 64 * 1024 * 1024
_LARGE_RUN_MESSAGE = '运行数据库较大；打开运行详情以读取完整进度。'
_MAX_DISCOVERY_DIRECTORIES = 4000
_MAX_DISCOVERED_RUNS = 200
_MAX_STATUS_HEADER_BYTES = 256 * 1024
_MAX_MONITOR_STATUS_BYTES = 256 * 1024
_MAX_WEB_SAMPLES = 8
_STATUS_STALE_SECONDS = 90

_PROGRESS = {'running': '正在执行', 'leased': '执行器已领取任务', 'queued': '已排队等待执行',
             'pending_dispatch': '等待调度', 'planned': '等待依赖完成', 'succeeded': '执行完成',
             'failed': '执行失败', 'blocked': '业务步骤受阻', 'cancelled': '已取消',
             'recovery_required': '需要恢复处理', 'timed_out': '执行超时', 'dead': '执行已终止'}
_PURPOSES = {
    'skill_resolve': '按锁定版本解析现有规则，缺口如实传递，不自动重研究。',
    'build_prepare': '补齐可认证的缺失构建文件；定制配置保留并生成合并草案。',
    'codemod': '应用已核实的精确映射，保留补丁、未适用项与候选身份。',
    'early_compile': '在主规划前收集目标编译诊断，不以编译结果代替行为验收。',
    'source': '获取指定版本源码并记录提交身份。',
    'background': '整理项目背景与参考资料。',
    'preparation': '确认迁移前提和所需材料。',
    'project_init': '检查源码布局并记录项目初始化说明。',
    'environment': '准备锁定的 Java、构建工具与加载器环境。',
    'baseline_build': '构建原版模组以建立行为验证基线。',
    'skill_lookup': '查找源版本与目标版本的迁移知识。',
    'skill_publish': '发布经审查的迁移知识供后续任务使用。',
    'mod_scan': '扫描模组使用的 API、资源与配置。',
    'mod_analysis': '分析受版本差异影响的代码与行为。',
    'contract_draft': '定义迁移必须保留的可观察行为。',
    'contract_verify': '验证行为契约与原版模组的一致性。',
    'contract_review': '独立审查行为契约及其证据。',
    'contract_freeze': '冻结通过审查的行为契约作为验收依据。',
    'migration_inventory': '列出需迁移的模块、接口与资源。',
    'migration_plan': '制定迁移顺序和验证方案。',
    'migration_tasks': '将迁移方案拆成有明确依赖和验收要求的任务。',
    'parallel_review': '审查任务能否安全并行执行。',
    'implementation': '按审定的任务与契约实施迁移。',
    'development_integrate': '集成各开发任务的修改并检查兼容性。',
    'target_build': '使用目标加载器和工具链构建迁移结果。',
    'code_review': '独立审查代码改动及行为保持情况。',
    'test_design': '设计覆盖行为契约的测试。',
    'test_review': '独立审查测试断言、候选、范围与静态覆盖。',
    'test_execute': '执行测试并记录结果证据。',
    'acceptance_preflight': '检查验收环境、材料和证据是否齐备。',
    'acceptance_build': '执行交付前的验收构建。',
    'client_smoke': '启动客户端并验证基本交互与运行行为。',
    'gap_review': '审查未解决缺口是否影响交付。',
    'delivery': '整理迁移产物、验证证据与交付说明。',
}
_PURPOSES.update(coder='完成分配的编码任务并记录自检与回归验证结果。',
    goal_prepare='为编码任务准备经过校验的目标、依赖与上下文。',
    platform_diff='研究源平台与目标平台之间的 API 和行为差异。',
    java_diff='研究 Java 版本变化对模组的影响。',
    supervisor='检查迁移进展与证据，记录必要的协调建议。')


def _safe(value, limit=800):
    """Only scalar, bounded text leaves the provider; redact common credentials."""
    if not isinstance(value, (str, int, float, bool)):
        return ''
    value = str(value)[:limit * 2]
    value = re.sub(r'(?is)-----BEGIN [^-]*PRIVATE KEY-----.*', '[已隐藏密钥]', value)
    value = re.sub(r'(?i)(https?://)[^\s/@]+:[^\s/@]+@', r'\1[已隐藏]@', value)
    value = re.sub(r'(?i)\b(Bearer\s+)\S+', r'\1[已隐藏]', value)
    value = re.sub(r'(?i)((?:password|passwd|token|api[_-]?key|secret|authorization)\s*["\']?\s*[:=]\s*["\']?)[^\s,;"\']+', r'\1[已隐藏]', value)
    value = re.sub(r'\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,})\b', '[已隐藏]', value)
    return value[:limit] + ('…' if len(value) > limit else '')


class ProgressReadError(ValueError):
    """A bounded, public diagnostic; underlying exceptions stay server-side."""


def _oid(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def _step(stage, task=None):
    attempts = (task or {}).get('attempts', [])
    last = attempts[-1] if attempts else {}
    payload = last.get('command', {}).get('payload', {})
    result = (last.get('result') or {}).get('value') or {}
    if not isinstance(result, dict):
        result = {}
    state = last.get('state', 'pending')
    if state == 'succeeded' and result.get('status') in {'failed', 'blocked'}:
        state = result['status']
    if state == 'succeeded' and stage in REVIEW_STAGES and result.get('outputs', {}).get('verdict') != 'approved':
        state = 'failed'
    name = _LABELS.get(stage, _safe(stage, 100))
    task_id = (task or {}).get('task_id', stage)
    if task_id != stage:
        name += ' · ' + _safe(task_id, 100)
    rows = []
    for index, attempt in enumerate(attempts, 1):
        value = (attempt.get('result') or {}).get('value') or {}
        if not isinstance(value, dict):
            value = {}
        kernel = attempt.get('kernel_snapshot') or {}
        business_state = value.get('status')
        if business_state == 'completed' and stage in REVIEW_STAGES and value.get('outputs', {}).get('verdict') != 'approved':
            business_state = 'failed'
        rows.append({'label': '业务执行 %d%s' % (index, '（返工）' if index > 1 else ''),
                     'state': business_state if business_state in {'failed', 'blocked'} else attempt.get('state', 'pending'),
                     'detail': _safe(value.get('detail')) or '暂无结果',
                     'execution_attempt': kernel.get('attempt', 0),
                     'execution_retries': max(0, int(kernel.get('attempt', 0)) - 1)})
    outputs = result.get('outputs') or {}
    execution_error = (last.get('result') or {}).get('error') or {}
    brief = [_safe(result.get('detail')) or _safe(execution_error.get('message'))]
    if isinstance(outputs, dict):
        for key, title in [('verdict', '审查结论'), ('parallel_decision', '执行方式'),
                           ('summary', '摘要'), ('source_commit', '源码提交')]:
            if outputs.get(key) is not None:
                brief.append(title + '：' + _safe(outputs[key], 300))
    inputs = ['前置步骤：' + '、'.join(_LABELS.get(x, _safe(x, 100))
              for x in task.get('dependencies', [])[:20])] if task and task.get('dependencies') else []
    request = payload.get('payload', {}).get('request', {}) if isinstance(payload, dict) else {}
    for key, title in [('source_minecraft', '源版本'), ('target_minecraft', '目标版本')]:
        if request.get(key):
            inputs.append(title + '：' + _safe(request[key], 100))
    # Keep SDK task references intact across grouping and generation projection.
    # Missing nodes remain opaque refs; stage placeholders have no observed edges.
    dependencies = [_oid(dependency) for dependency in task.get('dependencies', [])] if task is not None else []
    return {'id': _oid(task_id), 'label': name, 'state': state,
            'scheduled': task is not None, 'dependencies': dependencies,
            'purpose': _PURPOSES.get(stage, '根据已有证据处理当前研究或修复任务，并产出供独立审查的结果。'),
            'inputs': inputs, 'progress': (_PROGRESS.get(state, '状态：' + _safe(state, 50)) +
                '（SDK 最近记录；业务执行 %d 次）' % len(attempts)) if attempts else '尚未调度',
            'result': '\n'.join(x for x in brief if x) or '暂无结果',
            'error': _safe(result.get('error_code') or execution_error.get('code')) or None, 'attempts': rows}


class ProgressStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self._lock = threading.RLock()
        self._cache = {}
        self._summary_refs = {}
        self._sdk_checked = False
        self._large_loads = {}
        self._large_retry_at = {}
        self._large_read_gate = threading.Lock()
        self._discovery_truncated = False
        self._evidence_cache = {}

    def _contained(self, path):
        path = Path(path)
        # Reject symlinks even when they point inside the root: discovery must not
        # alias Runs or allow SQLite sidecars to escape the configured boundary.
        relative = path.relative_to(self.root)
        cursor = self.root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ProgressReadError('运行路径包含符号链接')
        if not path.resolve().is_relative_to(self.root):
            raise ProgressReadError('运行路径超出根目录')
        return path

    def _discover(self):
        found = {}
        self._discovery_truncated = False
        if not self.root.is_dir():
            return found
        visited = 0
        for current, dirs, files in os.walk(self.root, followlinks=False):
            visited += 1
            if visited > _MAX_DISCOVERY_DIRECTORIES:
                self._discovery_truncated = True
                break
            dirs[:] = sorted(d for d in dirs if not d.startswith('.') and
                             d not in {'node_modules', '__pycache__', 'build', 'dist', 'deployments'} and
                             not Path(current, d).is_symlink())
            if len(Path(current).relative_to(self.root).parts) >= 4:
                dirs[:] = []
            if 'run.json' in files or 'control.sqlite3' in files:
                path = self._contained(Path(current))
                found[_oid(path.relative_to(self.root))] = path
                dirs[:] = []
                if len(found) >= _MAX_DISCOVERED_RUNS:
                    self._discovery_truncated = True
                    break
        return found

    def _fingerprint(self, path):
        records = []
        for name in ('run.json', 'orchestrator.sqlite3', 'orchestrator.sqlite3-wal',
                     'orchestrator.sqlite3-shm', 'orchestrator.sqlite3-journal'):
            file = self._contained(path / name)
            try:
                stat = file.stat()
                if name.endswith('-wal') and stat.st_size == 0:
                    records.append((name, None))
                elif not name.endswith('-shm'):
                    records.append((name, stat.st_ino, stat.st_size, stat.st_mtime_ns))
            except FileNotFoundError:
                if not name.endswith('-shm'):
                    records.append((name, None))
        return tuple(records)

    def _database_footprint(self, path):
        """Return the SQLite files that an online snapshot would have to read.

        The main database is not the whole consistency boundary: a non-empty
        WAL or journal can contain committed data too.  Resolve every sidecar
        through ``_contained`` before looking at its size so the fast list path
        cannot bypass the symlink boundary checks used by normal reads.
        """
        total = 0
        for name in ('orchestrator.sqlite3', 'orchestrator.sqlite3-wal',
                     'orchestrator.sqlite3-journal'):
            file = self._contained(path / name)
            try:
                total += file.stat().st_size
            except FileNotFoundError:
                continue
        return total

    def _read_header(self, path, *, max_bytes=4 * 1024 * 1024):
        """Read and validate the immutable Run header without opening SQLite."""
        if (path / 'control.sqlite3').exists():
            raise ProgressReadError('旧版运行记录：当前进度页不支持读取其步骤和状态')
        marker = self._contained(path / 'run.json')
        if marker.stat().st_size > max_bytes:
            raise ProgressReadError('运行头文件过大')
        header = json.loads(marker.read_text(encoding='utf-8'))
        if (path / 'control.sqlite3').exists() or header.get('format_version') != FORMAT_VERSION:
            raise ProgressReadError('不支持旧版运行格式')
        if header.get('sdk_identity', {}).get('source_version') != SDK_VERSION:
            raise ProgressReadError(f'运行未声明受支持的 SDK {SDK_VERSION} 身份')
        if header.get('run_dir') != str(path):
            raise ProgressReadError('运行目录与冻结输入不一致')
        return header

    @staticmethod
    def _finite_number(value):
        return (float(value) if not isinstance(value, bool)
                and isinstance(value, (int, float)) and math.isfinite(float(value)) else None)

    @staticmethod
    def _metric(value):
        if isinstance(value, bool):
            return value
        if type(value) is int:
            return value if -(2**63) <= value <= 2**63 - 1 else None
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        return None

    def _read_monitor_status(self, path, run_id):
        """Read the already-projected host status without opening an SDK store."""
        marker = self._contained(path / 'artifacts' / 'monitor' / 'monitor-status.json')
        try:
            descriptor = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        except FileNotFoundError:
            return None, '尚无监控状态记录。'
        except OSError as error:
            raise ProgressReadError('监控状态文件不可读取。') from error
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise ProgressReadError('监控状态必须是普通文件。')
            if info.st_size > _MAX_MONITOR_STATUS_BYTES:
                return None, '监控状态文件超过 256 KiB 上限。'
            with os.fdopen(descriptor, 'rb', closefd=False) as stream:
                content = stream.read(_MAX_MONITOR_STATUS_BYTES + 1)
        finally:
            os.close(descriptor)
        if len(content) > _MAX_MONITOR_STATUS_BYTES:
            return None, '监控状态文件超过 256 KiB 上限。'
        try:
            value = json.loads(content)
        except (UnicodeError, ValueError, TypeError):
            return None, '监控状态文件格式无效。'
        if not isinstance(value, dict) or value.get('run_id') != run_id:
            return None, '监控状态身份不匹配。'
        return value, None

    def _status_row(self, identifier, path):
        """Bounded UI projection from the immutable header and monitor cache."""
        try:
            header = self._read_header(path, max_bytes=_MAX_STATUS_HEADER_BYTES)
            run_id = _safe(header.get('run_id') or path.name, 150)
            request = header.get('request') if isinstance(header.get('request'), dict) else {}
            monitor, error = self._read_monitor_status(path, run_id)
            now = time.time()
            observed_at = self._finite_number(monitor.get('observed_at')) if monitor else None
            execution = monitor.get('execution') if isinstance(monitor, dict) else {}
            execution = execution if isinstance(execution, dict) else {}
            consistency = monitor.get('snapshot_consistency') if isinstance(monitor, dict) else None
            if consistency is None:
                consistency = execution.get('snapshot_consistency')
            complete = (monitor.get('complete') is True if isinstance(monitor, dict)
                        else False)
            if not complete and execution:
                complete = execution.get('complete') is True
            age = None if observed_at is None else max(0.0, now - observed_at)
            stale = (monitor is None or age is None or age > _STATUS_STALE_SECONDS
                     or not complete or consistency != 'consistent')
            run_state = (monitor.get('run_state') or execution.get('state') or 'unknown') if monitor else 'unknown'
            run_state = _safe(run_state, 64) or 'unknown'
            overall = monitor.get('overall_status', run_state) if monitor else 'unknown'
            acceptance_projection = monitor.get('acceptance') if isinstance(monitor, dict) else None
            acceptance_status = 'unknown'
            if isinstance(acceptance_projection, dict):
                proposed = acceptance_projection.get('status')
                source = acceptance_projection.get('source')
                if (isinstance(proposed, str) and proposed in {'unverified', 'verified', 'failed'}
                        and isinstance(source, str) and source != 'monitor_default'):
                    acceptance_status = proposed

            raw_counts = monitor.get('counts') if isinstance(monitor, dict) else None
            if not isinstance(raw_counts, dict):
                raw_counts = execution.get('counts') if isinstance(execution, dict) else {}
            if not isinstance(raw_counts, dict):
                raw_counts = {}
            count_keys = ('task_count', 'active_leases', 'queued_ready', 'expired_leases', 'future_retries',
                          'pending_commands', 'pending_result_sync', 'pending_result_delivery',
                          'open_waits', 'recovery_required', 'unknown_effects', 'missing_executions')
            counts = {key: (raw_counts.get(key) if type(raw_counts.get(key)) is int
                            and 0 <= raw_counts.get(key) <= 2**31 - 1 else None)
                      for key in count_keys}
            progress_rows = monitor.get('execution_progress') if isinstance(monitor, dict) else None
            progress_rows = progress_rows if isinstance(progress_rows, list) else []
            wait_samples = []
            for row in progress_rows[:_MAX_WEB_SAMPLES]:
                if not isinstance(row, dict) or not isinstance(row.get('wait'), dict):
                    continue
                wait = row['wait']
                sample_time = self._finite_number(wait.get('sampled_at'))
                if sample_time is None:
                    sample_time = self._finite_number(row.get('last_progress_at'))
                sample_age = None if sample_time is None else max(0.0, now - sample_time)
                fields = ('reason', 'source', 'stage', 'waited_seconds', 'required_bytes',
                          'reserved_bytes', 'available_bytes', 'ceiling_bytes')
                sample = {key: (_safe(wait.get(key), 256) if isinstance(wait.get(key), str)
                                else self._metric(wait.get(key))) for key in fields
                          if isinstance(wait.get(key), (str, int, float, bool))
                          and (not isinstance(wait.get(key), float)
                               or math.isfinite(wait.get(key)))
                          and (isinstance(wait.get(key), str)
                               or self._metric(wait.get(key)) is not None)}
                dynamic = wait.get('dynamic_memory')
                if isinstance(dynamic, dict):
                    sample['dynamic_memory'] = {
                        _safe(key, 64): (_safe(value, 128) if isinstance(value, str)
                                         else self._metric(value))
                        for key, value in list(dynamic.items())[:8]
                        if isinstance(key, str) and isinstance(value, (str, int, float, bool))
                        and (not isinstance(value, float) or math.isfinite(value))
                        and (isinstance(value, str) or self._metric(value) is not None)}
                sample.update({
                    'task_id': _safe(row.get('task_id'), 256),
                    'execution_id': _safe(row.get('execution_id'), 128),
                    'observed_at': sample_time,
                    'stale': stale or sample_age is None or sample_age > _STATUS_STALE_SECONDS,
                    'source': _safe(wait.get('source') or 'host execution-progress marker', 160),
                })
                wait_samples.append(sample)
            wait_samples = wait_samples[:_MAX_WEB_SAMPLES]
            open_count = counts['open_waits']
            wait_status = ('unknown' if stale else
                           'open' if any(not item['stale'] for item in wait_samples)
                           or (open_count is not None and open_count > 0) else
                           'none' if open_count == 0 and run_state in {'succeeded', 'failed', 'cancelled'}
                           else 'unknown')
            wait_stale = stale or wait_status == 'unknown'
            wait_source = ('host execution-progress markers'
                           if any(not item['stale'] for item in wait_samples)
                           else 'dispatcher-sdk inspect_work_availability')
            current_wait = {
                'status': wait_status, 'source': wait_source,
                'observed_at': max((item['observed_at'] for item in wait_samples
                                    if item['observed_at'] is not None), default=observed_at),
                'stale': wait_stale, 'samples': wait_samples,
                'detail': ('SDK reports an open wait; the monitor projection has no current wait reason.'
                           if wait_status == 'open' and not wait_samples else None),
            }
            active_samples = monitor.get('active_samples') if isinstance(monitor, dict) else None
            active_tasks = []
            for row in (active_samples if isinstance(active_samples, list) else [])[:_MAX_WEB_SAMPLES]:
                if not isinstance(row, dict):
                    continue
                active_tasks.append({key: _safe(row.get(key), limit)
                                     for key, limit in (('task_id', 256), ('execution_id', 128),
                                                        ('state', 64))})
            runner = monitor.get('runner_health') if isinstance(monitor, dict) else None
            runner = runner if isinstance(runner, dict) else {'status': 'unknown'}
            return {
                'id': identifier, 'run_id': run_id,
                'mod_id': _safe(request.get('mod_id'), 150),
                'source_version': _safe(request.get('source_minecraft'), 100),
                'target_version': _safe(request.get('target_minecraft'), 100),
                'state': run_state, 'overall_status': _safe(overall, 64) or 'unknown',
                'acceptance_status': acceptance_status,
                'created_at': self._finite_number(header.get('started_at')) or 0,
                'updated_at': observed_at or self._finite_number(header.get('started_at')) or 0,
                'observed_at': observed_at, 'stale': stale,
                'error': error or (_safe(monitor.get('error'), 300) if monitor else None),
                'active_steps': [], 'active_tasks': active_tasks, 'groups': [],
                'execution': {
                    'source': 'artifacts/monitor/monitor-status.json',
                    'state': run_state,
                    'revision': (self._metric(monitor.get('revision'))
                                 if isinstance(monitor, dict) else None),
                    'task_count': counts.get('task_count'),
                    'observed_at': observed_at, 'stale': stale,
                    'complete': complete, 'snapshot_consistency': consistency,
                    'counts': counts,
                },
                'availability': {
                    'source': 'dispatcher-sdk inspect_work_availability via RunMonitor',
                    'observed_at': observed_at, 'stale': stale, 'complete': complete,
                    'snapshot_consistency': consistency, 'counts': counts,
                },
                'current_wait': current_wait,
                'last_code_change': {
                    'status': 'unknown', 'source': 'SDK integration task detail not requested',
                    'observed_at': None, 'stale': True,
                },
                'last_successful_authenticated_verification': {
                    'status': 'unknown', 'source': 'SDK verification task detail not requested',
                    'observed_at': None, 'stale': True,
                },
                'recovery': {
                    'required': counts.get('recovery_required'),
                    'status': _safe(monitor.get('recovery_status'), 64) if monitor else None,
                },
                'runner_health': {
                    'status': _safe(runner.get('status'), 64) or 'unknown',
                    'reason': _safe(runner.get('reason'), 160) or None,
                    'heartbeat_age_seconds': self._finite_number(runner.get('heartbeat_age_seconds')),
                },
                'summary_pending': False,
                'discovery_truncated': self._discovery_truncated,
            }
        except Exception as error:
            reason = (_safe(str(error)) if isinstance(error, ProgressReadError)
                      else '无法读取运行摘要（%s）' % type(error).__name__)
            return {
                'id': identifier, 'run_id': _safe(path.name, 150),
                'mod_id': _safe(path.name, 150), 'source_version': '',
                'target_version': '', 'state': 'unavailable', 'overall_status': 'unknown',
                'acceptance_status': 'unknown', 'created_at': 0, 'updated_at': 0,
                'observed_at': None, 'stale': True, 'error': reason,
                'active_steps': [], 'active_tasks': [], 'groups': [],
                'current_wait': {'status': 'unknown', 'source': 'unavailable',
                                 'observed_at': None, 'stale': True, 'samples': []},
                'last_code_change': {'status': 'unknown', 'stale': True},
                'last_successful_authenticated_verification': {'status': 'unknown', 'stale': True},
                'discovery_truncated': self._discovery_truncated,
            }

    def _list_preview(self, identifier, path, message=_LARGE_RUN_MESSAGE):
        """Project a large Run from its bounded immutable header for the list.

        A list request must stay responsive even when producing an online SDK
        backup would require copying gigabytes.  This row deliberately carries
        no SDK state or steps; selecting it still follows the normal full
        snapshot path through ``get_run``.
        """
        try:
            header = self._read_header(path)
            request = header.get('request', {})
            return {
                'id': identifier,
                'run_id': _safe(header.get('run_id') or path.name, 150),
                'mod_id': _safe(request.get('mod_id'), 150),
                'source_version': _safe(request.get('source_minecraft'), 100),
                'target_version': _safe(request.get('target_minecraft'), 100),
                'state': 'unavailable', 'acceptance_status': 'unknown',
                'created_at': header.get('started_at', 0),
                'updated_at': header.get('started_at', 0), 'observed_at': None,
                'stale': True, 'error': message,
                'active_steps': [], 'groups': []
            }
        except Exception as error:
            reason = (_safe(str(error)) if isinstance(error, ProgressReadError)
                      else '无法读取运行数据（%s）' % type(error).__name__)
            return {
                'id': identifier, 'run_id': _safe(path.name, 150),
                'mod_id': _safe(path.name, 150), 'source_version': '',
                'target_version': '', 'state': 'unavailable',
                'acceptance_status': 'unknown', 'created_at': 0,
                'updated_at': 0, 'observed_at': None, 'stale': True,
                'error': reason, 'active_steps': [], 'groups': []
            }

    @contextmanager
    def _snapshot(self, identifier, path):
        """Keep only bounded disposable backups, never a cache of whole stores."""
        self._database_footprint(path)  # Validate every source sidecar boundary.
        try:
            with snapshot_databases((path / 'orchestrator.sqlite3',),
                                    prefix='modport-web-') as temporary:
                yield temporary / 'orchestrator.sqlite3'
        except SnapshotLimitError as error:
            raise ProgressReadError('状态读取超出资源限制，已停止复制运行数据库。') from error

    def _large_read_error(self, error):
        return (_safe(str(error)) if isinstance(error, ProgressReadError)
                else '无法读取运行数据（%s）' % type(error).__name__)

    def _load_large(self, identifier, path, fingerprint):
        """Populate the normal projection without blocking an HTTP worker."""
        try:
            # Keep one background observation per store; the shared snapshot
            # budget also covers CLI readers and other web processes.
            with self._large_read_gate:
                value = self._read(identifier, path)
                current_fingerprint = self._fingerprint(path)
                if current_fingerprint != fingerprint:
                    raise ProgressReadError('读取期间运行数据发生变化，请刷新后重试。')
        except Exception as error:
            value = None
            reason = self._large_read_error(error)
            current_fingerprint = fingerprint
        else:
            reason = None
        with self._lock:
            status = self._large_loads.get(identifier)
            if status is None or status[0] != fingerprint:
                return
            if value is None:
                self._large_loads[identifier] = (fingerprint, 'error', reason)
                self._large_retry_at[identifier] = time.monotonic() + 30
                return
            self._cache[identifier] = (time.monotonic(), current_fingerprint, value)
            self._large_loads[identifier] = (fingerprint, 'ready', None)

    def _large_detail(self, identifier, path):
        """Return a fast placeholder while a large Run is read in the background."""
        fingerprint = self._fingerprint(path)
        previous = self._cache.get(identifier)
        if previous and previous[1] == fingerprint and not previous[2].get('stale'):
            return self._get(identifier, path)
        status = self._large_loads.get(identifier)
        if (status is not None and status[1] == 'error'
                and time.monotonic() >= self._large_retry_at.get(identifier, 0)):
            status = None
        if status is not None and status[1] == 'running':
            # A live Run can change its fingerprint while the backup is being
            # copied.  Keep one worker for that Run; the next poll will start
            # a fresh fingerprinted read after this one finishes.
            row = self._list_preview(identifier, path,
                                     '正在读取大型运行详情；完成后会自动显示。')
            row['detail_pending'] = True
            return row
        if status is None or status[0] != fingerprint:
            status = (fingerprint, 'running', None)
            self._large_loads[identifier] = status
            self._large_retry_at.pop(identifier, None)
            threading.Thread(target=self._load_large,
                             args=(identifier, path, fingerprint),
                             name='modport-web-large-read', daemon=True).start()
        if status[1] == 'error':
            row = self._list_preview(identifier, path, status[2] or '暂时无法读取运行详情。')
            row['detail_pending'] = False
            return row
        row = self._list_preview(identifier, path,
                                 '正在读取大型运行详情；完成后会自动显示。')
        row['detail_pending'] = True
        return row

    def _read(self, identifier, path):
        header = self._read_header(path)
        if not self._sdk_checked:
            sdk_release()
            self._sdk_checked = True
        with self._snapshot(identifier, path) as snapshot_path:
            report = inspect_storage(snapshot_path)
            if not report['compatible'] or report['orchestrator_schema'] != 4:
                raise ProgressReadError('运行数据库格式不兼容或已损坏')
            sdk = Orchestrator(snapshot_path, None)
            try:
                snapshot = hydrate_run_snapshot(path, sdk.get_run(header['run_id']))
            finally:
                sdk.close()
        if snapshot.get('input') != header:
            raise ProgressReadError('运行头文件与数据库不一致，状态同步中')
        request = header.get('request', {})
        tasks = deepcopy(snapshot.get('tasks', {}))
        app = snapshot.get('application_state') or {}
        effective = app.get('effective', {})
        inherited = set()
        for alias, outcome in effective.items():
            if not isinstance(outcome, dict):
                continue
            task_id = outcome.get('task_id') or alias
            if task_id not in tasks:
                stage = outcome.get('stage_id') or alias
                tasks[task_id] = {'task_id': task_id, 'dependencies': [], 'attempts': [
                    {'state': 'succeeded', 'command': {'execution_id': outcome.get('command_id'),
                        'payload': {'stage_id': stage}}, 'result': {'value': outcome}}]}
                inherited.add(task_id)
        summary_refs = {}
        for task in tasks.values():
            attempts = task.get('attempts', [])
            value = ((attempts[-1].get('result') or {}).get('value') or {}) if attempts else {}
            outputs = value.get('outputs', {}) if isinstance(value, dict) else {}
            refs = outputs.get('artifact_refs', {})
            candidates = [ref for alias, ref in refs.items()
                          if alias.startswith('planning_summary:') and isinstance(ref, dict)]
            if isinstance(outputs.get('last_message'), str):
                message_path = outputs['last_message']
                authenticated = next((ref for ref in refs.values() if isinstance(ref, dict)
                                      and ref.get('path') == message_path), None)
                if not any(ref.get('path') == message_path for ref in candidates):
                    candidates.append(authenticated or {'path': message_path})
            summary_refs[_oid(task['task_id'])] = candidates[:4]
        grouped = {key: [] for key, _, _ in _GROUPS}
        stages = {}
        for task in tasks.values():
            attempts = task.get('attempts', [])
            stage = attempts[-1].get('command', {}).get('payload', {}).get('stage_id') if attempts else None
            stage = stage or task['task_id'].split('.')[0]
            stages.setdefault(stage, []).append(task)
        effective_commands = {outcome.get('command_id') for outcome in effective.values()
                              if isinstance(outcome, dict)}
        processed = set(app.get('processed', []))

        def generation(task):
            match = re.search(r'\.g(\d+)(?:\.|$)', task['task_id'])
            return int(match[1]) if match else 0

        latest_generations = {stage: max(generation(task) for task in members)
                              for stage, members in stages.items()}

        def project(stage, task):
            step = _step(stage, task)
            step['inherited'] = bool(task and task['task_id'] in inherited)
            if task and task['task_id'] in inherited:
                step['progress'] = '继承已有的工作流有效结果。'
                step['attempts'][0]['label'] = '继承的业务结果'
            elif task and task.get('attempts'):
                command_id = task['attempts'][-1].get('command', {}).get('execution_id')
                if (step['state'] in {'succeeded', 'failed', 'cancelled', 'blocked'}
                        and generation(task) < latest_generations.get(stage, 0)):
                    step['state'] = 'superseded'
                    step['progress'] = '已被后续执行替代；此处保留历史结果。'
                elif (step['state'] == 'succeeded' and command_id in processed
                        and command_id not in effective_commands):
                    step['state'] = 'pending'
                    step['progress'] = '历史结果已失效，等待返工；历史执行记录保留在下方。'
                    step['result'] = '暂无本轮有效结果'
            return step

        current_main = set(stage_routes(header)[0])
        for key, _, members in _GROUPS:
            for stage in members:
                if stage not in current_main and stage not in stages:
                    continue
                for task in stages.pop(stage, [None]):
                    grouped[key].append(project(stage, task))
        for stage, members in stages.items():
            grouped['support'].extend(project(stage, task) for task in members)
        groups = [{'id': key, 'label': label, 'steps': grouped[key]}
                  for key, label, _ in _GROUPS if grouped[key]]
        app = snapshot.get('application_state') or {}
        active = [s['label'] for g in groups for s in g['steps'] if s['state'] in _ACTIVE]
        waiting = any(wait.get('state') == 'open' for wait in snapshot.get('waits', {}).values())
        state = snapshot['state']
        if waiting:
            if not active and state == 'running':
                state = 'waiting'
            active.append('等待外部处理')
        if state in _TERMINAL:
            active = []
        timestamps = [header.get('started_at', 0)]
        for task in tasks.values():
            timestamps.extend((a.get('kernel_snapshot') or {}).get('updated_at') or 0
                              for a in task.get('attempts', []))
        self._summary_refs[identifier] = summary_refs
        acceptance_status = app.get('acceptance_status', 'unverified')
        if (not isinstance(acceptance_status, str)
                or acceptance_status not in {'unverified', 'verified', 'failed'}):
            acceptance_status = 'unknown'
        return {'id': identifier, 'run_id': _safe(header['run_id'], 150),
                'mod_id': _safe(request.get('mod_id'), 150),
                'source_version': _safe(request.get('source_minecraft'), 100),
                'target_version': _safe(request.get('target_minecraft'), 100),
                'state': state, 'acceptance_status': acceptance_status,
                'created_at': header.get('started_at', 0),
                'updated_at': max(timestamps), 'observed_at': time.time(), 'stale': False,
                'error': _safe(app.get('stop_reason') or app.get('terminal_reason')) or None, 'active_steps': active, 'groups': groups}

    def _get(self, identifier, path):
        now = time.monotonic()
        previous = self._cache.get(identifier)
        if previous and now - previous[0] < 3:
            return deepcopy(previous[2])
        fingerprint = None
        try:
            fingerprint = self._fingerprint(path)
            if previous and not previous[2]['stale'] and fingerprint == previous[1]:
                self._cache[identifier] = (now, fingerprint, previous[2])
                return deepcopy(previous[2])
            value = self._read(identifier, path)
        except Exception as error:
            # Do not expose exception text containing filesystem paths or SQL.
            reason = _safe(str(error)) if isinstance(error, ProgressReadError) else '无法读取运行数据（%s）' % type(error).__name__
            value = deepcopy(previous[2]) if previous else {
                'id': identifier, 'run_id': _safe(path.name, 150), 'mod_id': _safe(path.name, 150),
                'source_version': '', 'target_version': '', 'state': 'unavailable',
                'acceptance_status': 'unknown',
                'created_at': 0, 'updated_at': 0, 'observed_at': None,
                'active_steps': [], 'groups': []}
            value.update(stale=True, error=reason)
        self._cache[identifier] = (now, fingerprint, value)
        return deepcopy(value)

    def list_runs(self):
        with self._lock:
            paths = self._discover()
            runs = [self._with_driver_health(self._status_row(identifier, path), path)
                    for identifier, path in paths.items()]
            return sorted(runs, key=lambda row: (row['state'] in {'running', 'waiting'}, row['created_at']), reverse=True)

    def get_run(self, identifier):
        with self._lock:
            path = self._discover().get(identifier)
            if path is None:
                raise KeyError(identifier)
            return self._with_driver_health(self._status_row(identifier, path), path)

    def get_evidence(self, identifier):
        """Read one selected Run's bounded SDK evidence at most once per minute."""
        with self._lock:
            path = self._discover().get(identifier)
            if path is None:
                raise KeyError(identifier)
            header = self._read_header(path, max_bytes=_MAX_STATUS_HEADER_BYTES)
            cached = self._evidence_cache.get(identifier)
            if (cached is not None and cached[1] == str(path)
                    and cached[2].get('run_id') == header['run_id']
                    and time.monotonic() - cached[0] < 60):
                return deepcopy(cached[2])
        from .operations import MigrationOperations
        observed = MigrationOperations().status(path, header['run_id'])
        summary = observed.snapshot.get('status_summary', {})
        value = {
            'run_id': _safe(observed.run_id, 150),
            'source': 'dispatcher-sdk bounded status',
            'revision': summary.get('revision'),
            'observed_at': summary.get('observed_at'),
            'stale': summary.get('stale', True),
            'last_code_change': summary.get('last_code_change'),
            'last_successful_authenticated_verification': summary.get(
                'last_successful_authenticated_verification'),
            'current_wait': summary.get('current_wait'),
        }
        with self._lock:
            self._evidence_cache[identifier] = (time.monotonic(), str(path), value)
        return deepcopy(value)

    @staticmethod
    def _task_view(task):
        attempt = task.get('latest_attempt') if isinstance(task, dict) else None
        attempt = attempt if isinstance(attempt, dict) else {}
        command = attempt.get('command') if isinstance(attempt.get('command'), dict) else {}
        payload = command.get('payload') if isinstance(command.get('payload'), dict) else {}
        result = attempt.get('result') if isinstance(attempt.get('result'), dict) else {}
        value = result.get('value') if isinstance(result.get('value'), dict) else {}
        outputs = value.get('outputs') if isinstance(value.get('outputs'), dict) else {}
        refs = outputs.get('artifact_refs') if isinstance(outputs.get('artifact_refs'), dict) else {}
        kernel = attempt.get('kernel_snapshot') if isinstance(attempt.get('kernel_snapshot'), dict) else {}
        updated_at = ProgressStore._finite_number(kernel.get('updated_at'))
        return {
            'task_id': _safe(task.get('task_id'), 256),
            'stage_id': _safe(payload.get('stage_id'), 128),
            'execution_state': _safe(attempt.get('state'), 64) or 'unknown',
            'business_status': _safe(value.get('status'), 64) or 'unknown',
            'error_code': _safe(value.get('error_code'), 128) or None,
            'detail': _safe(value.get('detail'), 1000) or None,
            'attempt_count': (task.get('attempt_count') if type(task.get('attempt_count')) is int
                              and 0 <= task.get('attempt_count') <= 100000 else None),
            'execution_id': _safe(command.get('execution_id'), 128) or None,
            'updated_at': updated_at,
            'head': _safe(outputs.get('head'), 128) or None,
            'paths': [_safe(value, 256) for value in outputs.get('paths', [])[:20]
                      if isinstance(value, str)] if isinstance(outputs.get('paths'), list) else [],
            'artifact_refs': {
                _safe(name, 128): {
                    'path': _safe(ref.get('path'), 512) or None,
                    'sha256': _safe(ref.get('sha256'), 128) or None,
                } for name, ref in list(refs.items())[:16]
                if isinstance(name, str) and isinstance(ref, dict)
            },
        }

    def get_task(self, identifier, task_id):
        """On-demand single-task detail; never expands the rest of the Run."""
        if not isinstance(task_id, str) or not task_id or len(task_id) > 256:
            raise KeyError(task_id)
        with self._lock:
            path = self._discover().get(identifier)
            if path is None:
                raise KeyError(identifier)
            header = self._read_header(path, max_bytes=_MAX_STATUS_HEADER_BYTES)
            if not self._sdk_checked:
                sdk_release()
                self._sdk_checked = True
            with self._snapshot(identifier, path) as snapshot_path:
                report = inspect_storage(snapshot_path)
                if not report['compatible'] or report['orchestrator_schema'] != 4:
                    raise ProgressReadError('运行数据库格式不兼容或已损坏。')
                sdk = Orchestrator(snapshot_path, None)
                try:
                    summary = sdk.get_run_summary(header['run_id'])
                    if summary.get('run_id') != header['run_id']:
                        raise ProgressReadError('SDK 任务详情身份不匹配。')
                    try:
                        task = sdk.get_task(header['run_id'], task_id)
                    except Exception as error:
                        if 'unknown task' in str(error).lower():
                            raise KeyError(task_id) from error
                        raise
                finally:
                    sdk.close()
            return {
                'run_id': _safe(header['run_id'], 150),
                'task': self._task_view(task),
                'source': 'dispatcher-sdk public get_task',
                'observed_at': time.time(),
                'stale': False,
            }

    @staticmethod
    def _with_driver_health(row, path):
        # Health must be refreshed even when the SDK fingerprint is unchanged:
        # a dead driver cannot update the database to invalidate its cache.
        from .runner import read_driver_health
        from .run_monitor import _runner_health, driver_namespace_match, process_alive
        row = deepcopy(row)
        row['execution_state'] = row['state']
        try:
            snapshot = read_driver_health(path)
            if snapshot is None:
                row['runner_health'] = (_runner_health(
                    driver_alive=False, run_state=row['state'], now=time.time())
                    if row['state'] in _TERMINAL
                    else {'status': 'unknown', 'reason': 'heartbeat_missing'})
                return row
            namespace_match = driver_namespace_match(snapshot)
            row['runner_health'] = _runner_health(
                driver_alive=(namespace_match is not False and process_alive(
                    snapshot['pid'], snapshot['birth'])),
                run_state=row['state'], now=time.time(), snapshot=snapshot,
                namespace_match=namespace_match)
            if row['state'] in {'running', 'waiting'} and row['runner_health']['status'] in {'interrupted', 'unresponsive'}:
                row['state'] = 'interrupted'
                row['active_steps'] = ['执行驱动失联；需核对恢复状态，不能直接重跑任务。']
        except (OSError, ValueError, RuntimeError):
            row['runner_health'] = {'status': 'unknown', 'reason': 'heartbeat_unavailable'}
        return row

    def _summary(self, root, ref):
        relative = ref.get('path')
        if not isinstance(relative, str) or Path(relative).is_absolute():
            raise ProgressReadError('摘要引用路径无效')
        path = self._contained(root / relative)
        if not path.resolve().is_relative_to(root):
            raise ProgressReadError('摘要引用超出运行目录')
        # Never wait on a FIFO/device supplied as a supposed summary artifact.
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(descriptor, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ProgressReadError('摘要必须是普通文件')
            data = stream.read(65537)
        if len(data) > 65536:
            raise ProgressReadError('摘要文件超过 64 KiB')
        if ref.get('sha256') is not None and hashlib.sha256(data).hexdigest() != ref['sha256']:
            raise ProgressReadError('摘要校验不一致')
        return _safe(data.decode('utf-8'), 1600)

    def get_step(self, identifier, step_id):
        with self._lock:
            path = self._discover().get(identifier)
            if path is None:
                raise KeyError(identifier)
            run = self._read(identifier, path)
            for group in run['groups']:
                for step in group['steps']:
                    if step['id'] != step_id:
                        continue
                    # Existing public final messages supplement only absent or
                    # generic summaries. Logs, prompts and arbitrary refs are excluded.
                    generic = step['result'] in {'暂无结果', 'completed', 'agent completed',
                                                  'agent assignment completed', 'coding agent completed', 'fixture result'}
                    if generic:
                        root = self._discover().get(identifier)
                        if root is None:
                            raise KeyError(identifier)
                        for ref in self._summary_refs.get(identifier, {}).get(step_id, []):
                            try:
                                summary = self._summary(root, ref)
                            except (OSError, ValueError, UnicodeError):
                                step['summary_error'] = '已有摘要不可用（引用无效、校验失败或文件过大）'
                                continue
                            if summary:
                                step['result'] = summary
                                step.pop('summary_error', None)
                                break
                    return step
            raise KeyError(step_id)
