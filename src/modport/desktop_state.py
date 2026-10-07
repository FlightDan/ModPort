"""Application-owned registry and bounded UI projections; never SDK storage."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import stat
import os
import threading
import time
import uuid

from .evidence import atomic_json
from .token_budget import read_token_budget

TERMINAL = frozenset({'succeeded', 'failed', 'cancelled'})
INSTANCE_ID = re.compile(r'desktop-[a-f0-9]{32}\Z')
MAX_JSON_BYTES = 4 * 1024 * 1024
_published = {}
_publish_lock = threading.Lock()


def note_desktop_error(header, phase, error, *, revision=None):
    """Retain optional UI failures without changing SDK business execution."""
    try:
        from .telemetry import record_event, redact
        record_event(header['run_dir'],
            f"desktop-error:{header['run_id']}:{phase}:{revision}",
            'desktop.error', {'phase': phase, 'error_type': type(error).__name__,
                              'detail': redact(str(error))}, run_id=header['run_id'])
    except Exception:
        # Audit collection itself is best effort; no UI failure is authority
        # to cancel or restart business work.
        pass


def publish_snapshot_safely(header, snapshot, *, force=False):
    try:
        publish_snapshot(header, snapshot, force=force)
    except Exception as error:
        note_desktop_error(header, 'projection', error, revision=snapshot.get('revision'))
_managed_stores = {}


def read_json(path: Path, *, limit=MAX_JSON_BYTES):
    if path.is_symlink():
        raise ValueError('application state must not be a symbolic link')
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError('application state exceeds its size limit')
    return json.loads(data)


class DesktopState:
    def __init__(self, data_root):
        self.root = Path(data_root).expanduser().resolve()
        if any(ord(char) < 32 for char in str(self.root)):
            raise ValueError('application root must not contain control characters')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.instances = self.root / 'instances'
        self.instances.mkdir(exist_ok=True, mode=0o700)
        self.database = self.root / 'desktop.sqlite3'
        if self.database.is_symlink() or self.instances.is_symlink():
            raise ValueError('application directories must not be symbolic links')
        with self.transaction() as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS instances (id TEXT PRIMARY KEY, project_name TEXT NOT NULL, created REAL NOT NULL, launch_error TEXT, supervisor TEXT)')
            connection.execute('CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, instance TEXT NOT NULL, content TEXT NOT NULL, created REAL NOT NULL, state TEXT NOT NULL, task_id TEXT, reply TEXT, error TEXT)')
            connection.execute('CREATE TABLE IF NOT EXISTS workspace_reservations (path TEXT PRIMARY KEY, instance TEXT NOT NULL UNIQUE)')

    @contextmanager
    def transaction(self):
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('BEGIN IMMEDIATE')
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def run_dir(self, instance_id):
        if not isinstance(instance_id, str) or not INSTANCE_ID.fullmatch(instance_id):
            raise KeyError('unknown migration instance')
        path = self.instances / instance_id
        if path.is_symlink() or path.resolve().parent != self.instances.resolve():
            raise ValueError('unsafe migration instance directory')
        return path

    def get(self, instance_id):
        self.run_dir(instance_id)
        with self.transaction() as connection:
            row = connection.execute('SELECT * FROM instances WHERE id=?', (instance_id,)).fetchone()
        if row is None:
            raise KeyError('unknown migration instance')
        return dict(row)

    def register(self, instance_id, project_name, *, source_identity_notice=None, source_details=None):
        root = self.run_dir(instance_id)
        with self.transaction() as connection:
            connection.execute('INSERT INTO instances(id,project_name,created) VALUES (?,?,?)', (instance_id, project_name, time.time()))
        atomic_json(root / 'desktop-instance.json', {'id': instance_id, 'application_root': str(self.root), 'source_identity_notice': source_identity_notice, 'source_details': source_details})

    def reserve_workspace(self, instance_id, path):
        self.run_dir(instance_id)
        path = Path(path).resolve(strict=True)
        with self.transaction() as connection:
            for row in connection.execute('SELECT path,instance FROM workspace_reservations').fetchall():
                if row['instance'] == instance_id:
                    if Path(row['path']) != path:
                        raise ValueError('实例不能更换已经预留的开发目录。')
                    return
                other = Path(row['path'])
                if path == other or path in other.parents or other in path.parents:
                    raise ValueError('该目录与另一迁移实例的开发区重叠；请先结束该实例或选择独立开发区。')
            connection.execute('INSERT INTO workspace_reservations(path,instance) VALUES (?,?)', (str(path), instance_id))

    def release_workspace(self, instance_id):
        with self.transaction() as connection:
            connection.execute('DELETE FROM workspace_reservations WHERE instance=?', (instance_id,))

    def update_launch(self, instance_id, *, supervisor=None, error=None):
        with self.transaction() as connection:
            connection.execute('UPDATE instances SET supervisor=COALESCE(?,supervisor), launch_error=? WHERE id=?', (json.dumps(supervisor) if supervisor else None, error, instance_id))

    def recent(self, limit=15):
        with self.transaction() as connection:
            rows = connection.execute('SELECT * FROM instances ORDER BY created DESC LIMIT ?', (limit,)).fetchall()
        result = []
        for record in rows:
            try:
                status = self.status(record['id'])
                result.append({key: status[key] for key in ('id', 'project_name', 'status')})
            except (OSError, ValueError, KeyError):
                result.append({'id': record['id'], 'project_name': record['project_name'], 'status': 'unknown'})
        return result

    def enqueue_message(self, instance_id, content):
        self.get(instance_id)
        if not isinstance(content, str) or not content.strip() or len(content) > 12000:
            raise ValueError('message must contain 1–12000 characters')
        projection = self.status(instance_id)
        if projection['status'] in TERMINAL:
            raise ValueError('此实例已结束，不能再调度监督对话。')
        identifier = uuid.uuid4().hex
        with self.transaction() as connection:
            pending = connection.execute("SELECT count(*) FROM messages WHERE instance=? AND state IN ('queued','running')", (instance_id,)).fetchone()[0]
            if pending:
                raise ValueError('Supervisor 正在处理上一条消息，请稍后再发送。')
            connection.execute("INSERT INTO messages(id,instance,content,created,state) VALUES (?,?,?,?,'queued')", (identifier, instance_id, content.strip(), time.time()))
        return {'id': identifier, 'role': 'user', 'content': content.strip(), 'state': 'queued'}

    def queued_message(self, instance_id):
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM messages WHERE instance=? AND state='queued' ORDER BY created LIMIT 1", (instance_id,)).fetchone()
        return dict(row) if row else None

    def fail_message(self, message_id, reason):
        with self.transaction() as connection:
            connection.execute("UPDATE messages SET state='failed',error=? WHERE id=? AND state='queued'", (reason, message_id))

    def messages(self, instance_id):
        with self.transaction() as connection:
            rows = connection.execute('SELECT * FROM messages WHERE instance=? ORDER BY created DESC LIMIT 50', (instance_id,)).fetchall()
        result = []
        for row in reversed(rows):
            result.append({'id': row['id'], 'role': 'user', 'content': row['content'], 'state': row['state']})
            if row['reply'] or row['error']:
                result.append({'id': row['id'] + '.reply', 'role': 'supervisor', 'content': row['reply'] or row['error'], 'state': row['state']})
        return result

    def settle_messages(self, snapshot, *, instance_id=None):
        instance_id = instance_id or snapshot['run_id']
        tasks = snapshot.get('tasks', {})
        with self.transaction() as connection:
            rows = connection.execute("SELECT * FROM messages WHERE instance=? AND state IN ('queued','running')", (instance_id,)).fetchall()
            for row in rows:
                task_id = 'desktop.chat.' + row['id']
                task = tasks.get(task_id)
                if not task:
                    if snapshot['state'] in TERMINAL:
                        detail = ('实例已结束，消息未获调度；没有延长执行预算。' if row['state'] == 'queued'
                                  else '实例已结束，监督任务结果未恢复；消息处理是否完成尚未验证。')
                        connection.execute("UPDATE messages SET state='failed',error=? WHERE id=?", (detail, row['id']))
                    continue
                attempts = task.get('attempts', [])
                if not attempts:
                    if snapshot['state'] in TERMINAL:
                        connection.execute("UPDATE messages SET state='failed',error=? WHERE id=?",
                            ('实例已结束，监督任务结果未恢复；消息处理是否完成尚未验证。', row['id']))
                    continue
                attempt = attempts[-1]
                result = (attempt.get('result') or {}).get('value') or {}
                result = result if isinstance(result, dict) else {}
                execution_state = attempt.get('state')
                if execution_state == 'succeeded' and result.get('status') == 'completed':
                    reply = str(result.get('outputs', {}).get('desktop_chat_reply', result.get('detail') or 'Supervisor 已完成处理。'))[:24000]
                    connection.execute("UPDATE messages SET state='completed',task_id=?,reply=? WHERE id=?", (task_id, reply, row['id']))
                elif execution_state in {'failed', 'cancelled', 'rejected', 'timed_out'} or result.get('status') in {'failed', 'blocked'}:
                    detail = result.get('detail') or json.dumps(attempt.get('error') or {}, ensure_ascii=False) or execution_state
                    connection.execute("UPDATE messages SET state='failed',task_id=?,error=? WHERE id=?", (task_id, str(detail)[:12000], row['id']))
                elif execution_state != 'planned':
                    if snapshot['state'] in TERMINAL:
                        connection.execute("UPDATE messages SET state='failed',task_id=?,error=? WHERE id=?",
                            (task_id, '实例已结束，监督任务结果未恢复；消息处理是否完成尚未验证。', row['id']))
                    else:
                        connection.execute("UPDATE messages SET state='running',task_id=? WHERE id=?", (task_id, row['id']))

    def settle_terminal_messages(self, instance_id, *, task_ids, task_scan_complete):
        """End pending UI messages without fabricating unavailable SDK results."""
        with self.transaction() as connection:
            rows = connection.execute("SELECT * FROM messages WHERE instance=? AND state IN ('queued','running')", (instance_id,)).fetchall()
            for row in rows:
                task_id = 'desktop.chat.' + row['id']
                unexecuted = row['state'] == 'queued' and task_scan_complete and task_id not in task_ids
                detail = ('实例已结束，消息未获调度；没有延长执行预算。' if unexecuted
                          else '实例已结束，监督任务结果未恢复；消息处理是否完成尚未验证。')
                connection.execute("UPDATE messages SET state='failed',error=? WHERE id=?", (detail, row['id']))

    def status(self, instance_id):
        record = self.get(instance_id)
        root = self.run_dir(instance_id)
        header = read_json(root / 'run.json')
        try:
            value = read_json(root / 'desktop-status.json')
        except FileNotFoundError:
            value = {'id': instance_id, 'project_name': record['project_name'],
                'status': 'unknown', 'elapsed_seconds': max(0, time.time() - header['started_at']),
                'budget': {'max_seconds': header['request']['budget']['max_seconds'],
                           'max_tokens': header['request']['budget'].get('max_tokens')},
                'stages': {key: {'label': label, 'state': 'pending', 'items': []}
                    for key, label in [('preparation', 'Preparation'),
                                       ('implementation', 'Implementation'), ('testing', 'Testing')]},
                'notice': '实例已保存；尚未收到任务状态投影，当前执行状态未知。',
                'observed_at': 0, 'workflow_version': header['definition']['workflow_version']}
        if value.get('execution_run_id', value.get('id')) != header['run_id']:
            # A predecessor projection is historical evidence. Until the new
            # driver publishes, observe only the current SDK lifecycle state.
            from .run_monitor import read_run_availability
            try:
                observed = read_run_availability(root, header['run_id'], sample_limit=0, effect_scan_limit=0)
                current_state = ('waiting' if observed.run_state not in TERMINAL and observed.open_waits
                                 else observed.run_state)
                revision, observed_at = observed.run_revision, observed.observed_at
                notice = '当前 SDK 执行尚无任务状态投影；行为验收仍未验证。'
            except (OSError, ValueError, RuntimeError, KeyError) as error:
                current_state, revision, observed_at = 'unknown', None, 0
                notice = '当前 SDK 执行状态未知；旧执行结果仅为历史证据。' + str(error)[:1000]
            value = {'id': instance_id, 'execution_run_id': header['run_id'], 'project_name': record['project_name'],
                'status': current_state, 'elapsed_seconds': max(0, time.time() - header['started_at']),
                'budget': {'max_seconds': header['request']['budget']['max_seconds'],
                           'max_tokens': header['request']['budget'].get('max_tokens')},
                'stages': {key: {'label': label, 'state': 'pending', 'items': []}
                    for key, label in [('preparation', 'Preparation'), ('implementation', 'Implementation'), ('testing', 'Testing')]},
                'acceptance_status': 'unverified', 'notice': notice, 'sdk_revision': revision,
                'observed_at': observed_at, 'workflow_version': header['definition']['workflow_version']}
        value['id'] = instance_id
        value['execution_run_id'] = header['run_id']
        value['messages'] = self.messages(instance_id)
        value['supervisor'] = {'busy': any(message['state'] in {'queued', 'running'} for message in value['messages'])}
        token = read_token_budget(root)
        value['budget']['used_tokens'] = token['used_tokens']
        value['budget']['token_usage_complete'] = token['usage_complete']
        metadata = read_json(root / 'desktop-instance.json', limit=8192)
        from .workspace import validate_workspace_spec
        workspace = header.get('request', {}).get('local_workspace')
        if workspace is not None:
            validate_workspace_spec(workspace)
        # Status remains readable after a user moves/deletes a finished project.
        # Execution and the native opener validate the live directory separately.
        value['workspace'] = workspace
        notice_parts = [
            {'kind': 'framework', 'text': value.get('notice')},
            {'kind': 'launch_error', 'text': record.get('launch_error')},
            {'kind': 'source', 'text': metadata.get('source_identity_notice')},
        ]
        observed = value.get('observed_at', 0)
        if value['status'] not in TERMINAL and time.time() - observed > 30:
            notice_parts.append({'kind': 'framework', 'text': '状态投影超过 30 秒未更新；这不证明进程已退出。请查看持久监督器或环境检查。'})
        value['_desktop_notice_parts'] = [part for part in notice_parts if part['text']]
        value['notice'] = '\n'.join(str(part['text']) for part in notice_parts if part['text']) or None
        return value


def managed_state(header):
    root = Path(header['run_dir'])
    marker = root / 'desktop-instance.json'
    if not marker.exists():
        return None
    info = marker.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('desktop instance marker must be a regular file')
    key = (info.st_mtime_ns, info.st_size)
    cached = _managed_stores.get(str(root))
    if cached and cached[0] == key:
        return cached[1]
    value = read_json(marker, limit=4096)
    state = DesktopState(value['application_root'])
    if state.run_dir(value['id']).resolve() != root.resolve():
        raise ValueError('desktop instance marker lies outside its application root')
    state.get(value['id'])
    _managed_stores[str(root)] = (key, state)
    return state


def managed_instance_id(header):
    """Resolve the registered desktop identity independently of SDK segments."""
    return Path(header['run_dir']).name if managed_state(header) is not None else None


def group_for_stage(stage):
    if stage in {'supervisor'}:
        return 'preparation'
    if stage in {'coder', 'agent_rework', 'goal_prepare', 'implementation', 'development_prepare', 'development_prepare_integrate', 'development_integrate', 'code_cleanup', 'target_revise', 'target_repair_integrate', 'contract_revise', 'contract_repair_integrate', 'target_contract_draft'} or stage.startswith(('target_repair_', 'contract_repair_')):
        return 'implementation'
    if stage in {'target_build', 'code_review', 'target_contract_freeze', 'target_contract_review', 'artifact_test_design', 'artifact_test_review', 'artifact_verified', 'delivery', 'final_cleanup', 'gap_review'} or stage.startswith(('test_', 'target_test', 'acceptance_', 'client_', 'artifact_')):
        return 'testing'
    return 'preparation'


def publish_snapshot(header, snapshot, *, force=False):
    """Called with the driver's authoritative hydrated SDK state, never a new read."""
    state = managed_state(header)
    if state is None:
        return
    if snapshot.get('run_id', header['run_id']) != header['run_id']:
        raise ValueError('desktop snapshot names a different execution Run')
    now = time.time()
    key = (header['run_dir'], header['run_id'], snapshot.get('revision'), snapshot['state'])
    with _publish_lock:
        previous = _published.get(header['run_dir'])
        if not force and previous and previous[0] == key and now - previous[1] < 3:
            return
        _published[header['run_dir']] = (key, now)
    instance_id = managed_instance_id(header)
    state.settle_messages(snapshot, instance_id=instance_id)
    from .workflow import AGENT_STAGES
    stages = {key: {'label': label, 'state': 'pending', 'items': []} for key, label in [('preparation', 'Preparation'), ('implementation', 'Implementation'), ('testing', 'Testing')]}
    tasks = snapshot.get('tasks', {})
    for task_id, task in tasks.items():
        if task_id.startswith('desktop.chat.'):
            continue
        attempts = task.get('attempts', [])
        if not attempts:
            continue
        attempt = attempts[-1]
        operation = attempt.get('command', {}).get('payload', {})
        stage = str(operation.get('stage_id') or task_id)
        outcome = (attempt.get('result') or {}).get('value') or {}
        outcome = outcome if isinstance(outcome, dict) else {}
        execution = attempt.get('state', 'unknown')
        execution_states = {'planned': 'queued', 'leased': 'queued', 'running': 'running', 'succeeded': 'completed', 'failed': 'failed', 'cancelled': 'cancelled', 'recovery_required': 'waiting', 'rejected': 'failed', 'timed_out': 'failed'}
        display_state = execution_states.get(execution, 'waiting')
        if outcome.get('status') == 'failed':
            display_state = 'failed'
        elif outcome.get('status') == 'blocked':
            display_state = 'waiting'
        payload = operation.get('payload', {})
        definition = payload.get('development_task') or payload.get('task', {})
        authored_label = definition.get('title') or definition.get('objective')
        label = authored_label or stage.replace('_', ' ')
        error = attempt.get('error')
        detail = str(outcome.get('detail') or (json.dumps(error, ensure_ascii=False) if error else ''))[:2000]
        is_agent = bool(operation.get('options', {}).get('model')) or stage in AGENT_STAGES | {'supervisor'}
        item = {'id': task_id, 'label': str(label)[:240], 'state': display_state, 'detail': detail, 'error_code': outcome.get('error_code'), 'active_agents': 1 if execution == 'running' and is_agent else 0, 'active_subagents': None, 'execution_id': attempt.get('command', {}).get('execution_id')}
        # Translate framework stage names at the API boundary, preserving authored titles.
        item.update(stage_id=stage, label_is_stage=not bool(authored_label))
        stages[group_for_stage(stage)]['items'].append(item)
    for group in stages.values():
        states = {item['state'] for item in group['items']}
        group['state'] = next((candidate for candidate in ['failed', 'waiting', 'running', 'queued'] if candidate in states), 'completed' if states else 'pending')
    budget = header['request']['budget']
    record = state.get(instance_id)
    run_state = snapshot['state']
    if run_state not in TERMINAL:
        run_state = 'waiting' if any(wait.get('state') == 'open' for wait in snapshot.get('waits', {}).values()) else 'running' if tasks else 'queued'
    app = snapshot.get('application_state') or {}
    acceptance = app.get('acceptance_status', 'unverified')
    value = {'id': instance_id, 'execution_run_id': header['run_id'], 'project_name': record['project_name'], 'status': run_state, 'elapsed_seconds': max(0, now - header['started_at']), 'budget': {'max_seconds': budget['max_seconds'], 'max_tokens': budget.get('max_tokens'), 'used_tokens': None, 'token_usage_complete': False}, 'stages': stages, 'messages': [], 'supervisor': {'busy': False}, 'notice': ('执行已完成；行为验收仍未验证。' if run_state == 'succeeded' and acceptance != 'passed' else None), 'acceptance_status': acceptance, 'observed_at': now, 'sdk_revision': snapshot.get('revision'), 'workflow_version': header['definition']['workflow_version']}
    projection = Path(header['run_dir']) / 'desktop-status.json'
    if run_state in TERMINAL and projection.exists():
        previous = read_json(projection)
        if (previous.get('status') in TERMINAL and previous.get('execution_run_id') == header['run_id']
                and previous.get('sdk_revision') == snapshot.get('revision')):
            value['elapsed_seconds'] = previous['elapsed_seconds']
    atomic_json(projection, value)
    marker = Path(header['run_dir']) / 'desktop-driver-state.json'
    if marker.exists():
        from .platform_runtime import process_birth
        driver = read_json(marker, limit=65536)
        birth = process_birth(os.getpid())
        if driver.get('pid') == os.getpid() and birth and driver.get('birth') == birth:
            if snapshot['state'] in TERMINAL:
                driver.update(state='terminal', at=now)
                atomic_json(marker, driver)
            else:
                from .runner import read_driver_health
                health = read_driver_health(Path(header['run_dir']))
                if health and health['pid'] == os.getpid() and health['birth'] == birth and health['status'] == 'running':
                    driver.update(state='running', at=now)
                    atomic_json(marker, driver)
