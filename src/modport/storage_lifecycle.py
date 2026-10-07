"""Standard retention checkpoints outside SDK scheduling and business gates."""
from pathlib import Path
import re
import stat
from typing import Mapping

from .artifact_retention import apply_retention, plan_retention
from .evidence import atomic_json, digest, read_json, verified_path, workspace_lock
from .user_paths import archives_root

_RECORD = 'artifacts/storage/settled-segments.json'
_REPORT = 'artifacts/storage/retention-status.json'
_MAX_RECORD_BYTES = 2 * 1024 * 1024
_TERMINAL = {'succeeded', 'failed', 'cancelled'}
_DONE = _TERMINAL | {'timed_out', 'dead'}
_SEGMENT = re.compile(r'[A-Za-z0-9_.:-]+')
_SHA256 = re.compile(r'[0-9a-f]{64}')


def _natural(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f'invalid settlement {name}')
    return value


def _validate_record(segment, record):
    if not isinstance(segment, str) or _SEGMENT.fullmatch(segment) is None:
        raise ValueError('invalid settlement segment identity')
    if (not isinstance(record, dict)
            or _SHA256.fullmatch(str(record.get('header_sha256', ''))) is None
            or record.get('state') not in _TERMINAL):
        raise ValueError('invalid settlement record')
    _natural(record.get('generation'), 'generation')
    _natural(record.get('revision'), 'revision')


def _storage_directory(root, *, create=False):
    current = Path(root)
    for name in ('artifacts', 'storage'):
        current = current / name
        try:
            info = current.lstat()
        except FileNotFoundError:
            if not create:
                return None
            current.mkdir()
            info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError('storage metadata directory is unsafe')
    return current


def _records(root):
    directory = _storage_directory(root)
    if directory is None:
        return {}
    path = Path(root) / _RECORD
    if not path.exists() and not path.is_symlink():
        return {}
    path = verified_path(Path(root), {'path': _RECORD})
    if path.stat().st_size > _MAX_RECORD_BYTES:
        raise ValueError('settlement registry exceeds its bounded size')
    value = read_json(path)
    if not isinstance(value, dict) or value.get('schema_version') != 1 or not isinstance(value.get('segments'), dict):
        raise ValueError('invalid settlement registry')
    records = value['segments']
    for segment, record in records.items():
        _validate_record(segment, record)
    return dict(records)


def _frozen_header(root, segment, *, current=None):
    if current is None:
        current = read_json(verified_path(root, {'path': 'run.json'}))
    relative = ('run.json' if current.get('run_id') == segment else
                f'artifacts/continuations/{segment}/run.json')
    return read_json(verified_path(root, {'path': relative}))


def record_settled_segment(root, snapshot):
    """Record only a terminal authoritative SDK observation with settled attempts.

    Callers pass a fresh SDK get_run result from their validated writer session.
    Frozen headers bind these small records to the continuation chain. They are
    not a replacement for SDK authority and cannot authorize an SDK mutation.
    """
    root = Path(root).resolve()
    if not isinstance(snapshot, Mapping):
        raise ValueError('settled snapshot must be an object')
    if snapshot.get('state') not in _TERMINAL:
        return False
    tasks = snapshot.get('tasks')
    if not isinstance(tasks, dict):
        raise ValueError('settled snapshot tasks are invalid')
    for task in tasks.values():
        if (not isinstance(task, dict) or not isinstance(task.get('attempts'), list)
                or not task['attempts']
                or any(not isinstance(attempt, dict)
                       or attempt.get('state') not in _DONE
                       for attempt in task['attempts'])):
            return False
    header = snapshot.get('input')
    if not isinstance(header, dict) or header.get('run_id') != snapshot.get('run_id'):
        raise ValueError('settled snapshot does not match its frozen header')
    unsigned = dict(header)
    checksum = unsigned.pop('header_sha256', None)
    run_dir = header.get('run_dir')
    if (checksum != digest(unsigned) or not isinstance(run_dir, str)
            or Path(run_dir).resolve() != root):
        raise ValueError('settled snapshot header is invalid')
    generation = _natural(snapshot.get('generation', 0), 'generation')
    revision = _natural(snapshot.get('revision'), 'revision')
    with workspace_lock(root):
        frozen = _frozen_header(root, snapshot['run_id'])
        if frozen != header:
            raise ValueError('settled snapshot differs from the frozen local header')
        records = _records(root)
        previous = records.get(snapshot['run_id'])
        if previous is not None:
            old_position = (previous['generation'], previous['revision'])
            new_position = (generation, revision)
            if new_position < old_position:
                return False
            if new_position == old_position and previous['state'] != snapshot['state']:
                raise ValueError('settled snapshot terminal state changed')
        records[snapshot['run_id']] = {
            'header_sha256': checksum, 'state': snapshot['state'],
            'generation': generation, 'revision': revision,
        }
        # Small terminal metadata is retained; refuse growth rather than silently
        # lose authority or turn this index into another unbounded history log.
        if len(records) > 4096:
            raise ValueError('settlement registry capacity reached')
        _storage_directory(root, create=True)
        atomic_json(root / _RECORD, {'schema_version': 1, 'segments': records})
    return True


def settled_segments(root):
    root = Path(root).resolve()
    current = read_json(verified_path(root, {'path': 'run.json'}))
    result = []
    for segment, record in _records(root).items():
        try:
            header = _frozen_header(root, segment, current=current)
        except (OSError, ValueError):
            continue
        unsigned = dict(header)
        checksum = unsigned.pop('header_sha256', None)
        if (header.get('run_id') == segment and checksum == digest(unsigned)
                and isinstance(record, dict) and record.get('header_sha256') == checksum
                and record.get('state') in _TERMINAL):
            result.append(segment)
    return result


def retention_checkpoint(root, *, archive_root=None, apply=True):
    """Plan or run conservative maintenance without opening SDK databases."""
    root = Path(root).resolve()
    destination = archives_root(archive_root)
    plan = plan_retention(root, keep_rounds=3, settled_segments=settled_segments(root))
    if not apply:
        return plan
    if not any(group['status'] == 'eligible' for group in plan['candidates']):
        report = {'schema_version': 1, 'status': 'no_eligible_artifacts',
                  'protected_segments': plan.get('retained_segments', []),
                  'blocked': plan.get('blocked', []), 'released_bytes': 0}
    else:
        report = apply_retention(root, plan, destination)
    _storage_directory(root, create=True)
    atomic_json(root / _REPORT, report)
    return report


def automatic_retention(root, snapshot=None):
    """Lifecycle hook: keep maintenance failures visible without fabricating results."""
    root = Path(root)
    try:
        if snapshot is not None:
            record_settled_segment(root, snapshot)
        return retention_checkpoint(root)
    except Exception as error:
        report = {'schema_version': 1, 'status': 'maintenance_required',
                  'error': f'{type(error).__name__}: {error}'[:2048], 'released_bytes': 0}
        try:
            _storage_directory(root, create=True)
            atomic_json(root / _REPORT, report)
        except (OSError, ValueError) as write_error:
            report['report_write_error'] = (
                f'{type(write_error).__name__}: {write_error}'[:2048])
        return report
