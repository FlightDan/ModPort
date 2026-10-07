"""Persistent host supervision of an exact registered desktop migration Run."""
from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time
from xml.etree import ElementTree as ET

from .desktop_state import DesktopState, note_desktop_error, read_json, publish_snapshot_safely, TERMINAL
from .evidence import atomic_json
from .run_monitor import read_run_availability
from .user_paths import configured_path

HOST_ENV_KEYS = frozenset({'PATH', 'Path', 'HOME', 'USER', 'LOGNAME', 'SHELL', 'LANG', 'LC_ALL', 'LC_CTYPE',
                           'TERM', 'TMPDIR', 'TMP', 'TEMP', 'NO_COLOR', 'XDG_DATA_HOME', 'MODPORT_OPENCODE_BIN',
                           'MODPORT_DATA_ROOT', 'MODPORT_OUTPUT_ROOT', 'MODPORT_SKILL_STORE', 'MODPORT_ARCHIVE_ROOT',
                           'OPENCODE_BIN', 'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'OPENAI_ORG_ID', 'OPENAI_ORGANIZATION',
                           'OPENAI_PROJECT_ID', 'OPENAI_PROJECT', 'BAILIAN_BASE_URL', 'BAILIAN_API_KEY',
                           'SystemRoot', 'WINDIR', 'USERPROFILE', 'LOCALAPPDATA', 'APPDATA', 'JAVA_HOME'})
_HOST_PATH_OVERRIDES = frozenset({'MODPORT_DATA_ROOT', 'MODPORT_OUTPUT_ROOT',
                                 'MODPORT_SKILL_STORE', 'MODPORT_ARCHIVE_ROOT'})


def save_host_environment(store, instance_id, environment=None):
    from .platform_files import atomic_write, make_private_directory, assert_host_owned
    from .desktop_model_settings import provider_environment_key
    store.get(instance_id)
    private = make_private_directory(store.root / 'host-runtime')
    source = dict(os.environ)
    if environment is not None:
        source.update(environment)
    names = {key.upper(): key for key in HOST_ENV_KEYS}
    names['PATH'] = 'PATH'
    values = {names.get(key.upper(), key): str(value) for key, value in source.items()
              if key.upper() in names or provider_environment_key(key)}
    for key in _HOST_PATH_OVERRIDES:
        if values.get(key):
            values[key] = str(configured_path(values[key]))
    encoded = json.dumps(values, ensure_ascii=False).encode('utf-8')
    if len(encoded) > 65536:
        raise ValueError('Private host environment exceeds its size limit')
    destination = private / (instance_id + '.json')
    atomic_write(destination, encoded, mode=0o600)
    assert_host_owned(destination)
    return destination


def restore_host_environment(store, instance_id):
    from .platform_files import safe_open, assert_host_owned
    from .desktop_model_settings import provider_environment_key
    store.get(instance_id)
    private = store.root / 'host-runtime'
    path = private / (instance_id + '.json')
    assert_host_owned(private)
    assert_host_owned(path)
    descriptor = safe_open(private, path.name)
    with os.fdopen(descriptor, 'rb') as stream:
        data = stream.read(65537)
    if len(data) > 65536:
        raise ValueError('Private host environment exceeds its size limit')
    values = json.loads(data)
    if (not isinstance(values, dict)
            or any(key not in HOST_ENV_KEYS and not provider_environment_key(key) for key in values)
            or any(not isinstance(value, str) or '\0' in value for value in values.values())):
        raise ValueError('Invalid private host environment snapshot')
    for key in list(os.environ):
        if key not in HOST_ENV_KEYS and not provider_environment_key(key):
            continue
        os.environ.pop(key, None)
    os.environ.update(values)
    return values


class PersistentSupervisor:
    def __init__(self, state, *, platform_name=None, execute=subprocess.run):
        self.state = state
        self.platform = platform_name or platform.system()
        self.execute = execute
        self.user = self.platform == 'Linux' and hasattr(os, 'geteuid') and os.geteuid() != 0

    def _systemctl(self, *args):
        return ['systemctl', *(['--user'] if self.user else []), *args]

    def _run(self, argv):
        result = self.execute(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30, check=False)
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout or f'{argv[0]} failed')[-3000:].strip())
        return result.stdout

    def check(self):
        if self.platform == 'Linux':
            if not shutil.which('systemctl'):
                return False, 'systemd is required for a durable migration driver'
            try:
                self._run(self._systemctl('show-environment'))
                return True, 'systemd persistent service supervision is available'
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
                return False, str(error)
        if self.platform == 'Windows':
            if not shutil.which('schtasks'):
                return False, 'Windows Task Scheduler is unavailable'
            try:
                self._run(['schtasks', '/Query', '/FO', 'CSV', '/NH'])
                return True, 'Windows Task Scheduler is available; native acceptance pending'
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
                return False, str(error)
        return False, 'This desktop package supports Windows and Linux supervision'

    def command(self, instance_id):
        return self._command(instance_id, 'desktop_driver')

    def watchdog_command(self, instance_id):
        return self._command(instance_id, 'watchdog_process')

    def _command(self, instance_id, module):
        self.state.get(instance_id)
        source = str(Path(__file__).resolve().parents[1])
        code = 'import sys; sys.path.insert(0, ' + repr(source) + f'); from modport.{module} import main; raise SystemExit(main())'
        return [sys.executable, '-I', '-c', code, '--data-root', str(self.state.root), '--instance', instance_id]

    @staticmethod
    def _unit_argument(value):
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$').replace('\n', '\\n') + '"'

    def launch(self, instance_id, *, environment=None):
        save_host_environment(self.state, instance_id, environment)
        atomic_json(self.state.run_dir(instance_id) / 'desktop-watchdog-control.json',
                    {'instance': instance_id, 'suppressed': False, 'reason': 'launch', 'at': time.time()})
        result = self.launch_driver(instance_id)
        try:
            watchdog = self._launch_component(instance_id, 'watchdog')
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            result['watchdog'] = {**self._component_identity(instance_id, 'watchdog'),
                                  'state': 'failed', 'error': str(error)[-3000:]}
            self.state.update_launch(instance_id, supervisor=result,
                                     error='Independent watchdog did not start: ' + str(error)[-3000:])
            raise RuntimeError('Independent watchdog did not start: ' + str(error)) from error
        result['watchdog'] = watchdog
        self.state.update_launch(instance_id, supervisor=result)
        return result

    def launch_driver(self, instance_id, *, environment=None):
        """Resume only the driver, preserving watchdog suppression and credentials."""
        self.state.get(instance_id)
        if environment is not None:
            save_host_environment(self.state, instance_id, environment)
        result = self._launch_component(instance_id, 'driver')
        previous = self._receipt(instance_id)
        if previous.get('watchdog'):
            result['watchdog'] = previous['watchdog']
        self.state.update_launch(instance_id, supervisor=result)
        return result

    def _receipt(self, instance_id):
        value = self.state.get(instance_id).get('supervisor')
        return json.loads(value) if value else {}

    def _component_identity(self, instance_id, component):
        suffix = '-watchdog' if component == 'watchdog' else ''
        if self.platform == 'Linux':
            return {'kind': 'systemd', 'unit': f'modport-{instance_id}{suffix}.service',
                    'scope': 'user' if self.user else 'system'}
        if self.platform == 'Windows':
            return {'kind': 'task_scheduler', 'task': f'ModPort-{instance_id}{suffix}'}
        raise RuntimeError('No durable supervisor for this platform')

    def _launch_component(self, instance_id, component):
        argv = self.watchdog_command(instance_id) if component == 'watchdog' else self.command(instance_id)
        result = self._component_identity(instance_id, component)
        requested_at = time.time()
        supervision = self.state.root / 'supervision'
        supervision.mkdir(exist_ok=True, mode=0o700)
        if self.platform == 'Linux':
            unit_name = result['unit']
            unit = supervision / unit_name
            directory = str(self.state.root)
            if any(ord(character) < 32 for character in directory):
                raise ValueError('The desktop data directory contains unsupported control characters')
            log = str(self.state.run_dir(instance_id) / f'desktop-{component}.log').replace('%', '%%')
            content = (f'[Unit]\nDescription=ModPort desktop {component}\nStartLimitIntervalSec=15min\nStartLimitBurst=3\n'
                       '[Service]\nType=simple\nRestart=on-failure\n'
                       + ('RestartPreventExitStatus=78\n' if component == 'driver' else '')
                       + 'RestartSec=30\n'
                       'KillMode=control-group\nTimeoutStopSec=30\n'
                       f'WorkingDirectory={directory.replace("%", "%%")}\n'
                       f'ExecStart={" ".join(self._unit_argument(arg) for arg in argv)}\n'
                       f'StandardOutput=append:{log}\nStandardError=append:{log}\n'
                       '[Install]\nWantedBy=' + ('default.target' if self.user else 'multi-user.target') + '\n')
            unit.write_text(content, encoding='utf-8')
            self._run(self._systemctl('link', str(unit)))
            self._run(self._systemctl('daemon-reload'))
            self._run(self._systemctl('enable', '--now', unit_name))
            try:
                active = self._run(self._systemctl('is-active', unit_name)).strip()
            except RuntimeError:
                active = 'inactive'
            if component == 'watchdog':
                value = self._wait_entry(instance_id, component, requested_at=requested_at)
                if active != 'active' and value['state'] != 'terminal':
                    raise RuntimeError('Independent watchdog service did not remain active after launch')
                result['state'] = value['state']
            elif active != 'active':
                marker_path = self.state.run_dir(instance_id) / 'desktop-driver-state.json'
                marker = read_json(marker_path, limit=65536) if marker_path.exists() else {}
                if not (marker.get('instance') == instance_id and marker.get('state') == 'terminal'
                        and time.time() - marker.get('at', 0) < 30
                        and self._terminal_observed(instance_id, marker)):
                    raise RuntimeError('Persistent driver did not remain active after launch')
        elif self.platform == 'Windows':
            task_name = result['task']
            task = supervision / (task_name + '.xml')
            namespace = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
            ET.register_namespace('', namespace)
            def node(parent, name, value=None, **attributes):
                element = ET.SubElement(parent, '{' + namespace + '}' + name, attributes)
                if value is not None:
                    element.text = value
                return element
            document = ET.Element('{' + namespace + '}Task', {'version': '1.4'})
            registration = node(document, 'RegistrationInfo'); node(registration, 'Description', 'Resume the same frozen ModPort instance; never create another Run')
            identity = list(csv.reader(self._run(['whoami', '/user', '/fo', 'csv', '/nh']).strip().splitlines()))
            sid = identity[0][1] if identity and len(identity[0]) > 1 else ''
            if not re.fullmatch(r'S-1-[0-9-]+', sid):
                raise RuntimeError('Could not resolve the current Windows user SID')
            triggers = node(document, 'Triggers'); trigger = node(triggers, 'LogonTrigger'); node(trigger, 'Enabled', 'true'); node(trigger, 'UserId', sid)
            principals = node(document, 'Principals'); principal = node(principals, 'Principal', id='Author'); node(principal, 'UserId', sid); node(principal, 'LogonType', 'InteractiveToken'); node(principal, 'RunLevel', 'LeastPrivilege')
            settings = node(document, 'Settings'); node(settings, 'MultipleInstancesPolicy', 'IgnoreNew'); node(settings, 'DisallowStartIfOnBatteries', 'false'); node(settings, 'StopIfGoingOnBatteries', 'false'); node(settings, 'ExecutionTimeLimit', 'PT0S'); node(settings, 'StartWhenAvailable', 'true')
            restart = node(settings, 'RestartOnFailure'); node(restart, 'Interval', 'PT1M'); node(restart, 'Count', '999')
            actions = node(document, 'Actions', Context='Author'); execution = node(actions, 'Exec'); node(execution, 'Command', argv[0]); node(execution, 'Arguments', subprocess.list2cmdline(argv[1:])); node(execution, 'WorkingDirectory', str(self.state.root))
            ET.ElementTree(document).write(task, encoding='utf-16', xml_declaration=True)
            self._run(['schtasks', '/Create', '/TN', task_name, '/XML', str(task), '/F'])
            self._run(['schtasks', '/Run', '/TN', task_name])
            value = self._wait_entry(instance_id, component, requested_at=requested_at)
            result.update({('driver_state' if component == 'driver' else 'state'): value['state'],
                           'native_acceptance': 'pending'})
        else:
            raise RuntimeError('No durable supervisor for this platform')
        return result

    def _wait_entry(self, instance_id, component, *, requested_at):
        """Service admission is insufficient: observe the independently owned process."""
        from .platform_runtime import process_birth
        marker = self.state.run_dir(instance_id) / f'desktop-{component}-state.json'
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if marker.exists():
                value = read_json(marker, limit=65536)
                if value.get('instance') == instance_id:
                    if value.get('state') == 'failed' and value.get('at', 0) >= requested_at:
                        raise RuntimeError(f'{component} startup failed: ' + str(value.get('error') or 'unknown host failure')[:3000])
                    if (value.get('state') == 'terminal' and value.get('at', 0) >= requested_at
                            and self._terminal_observed(instance_id, value)):
                        return value
                    states = {'running'} if component == 'watchdog' else {'launching', 'running'}
                    if value.get('state') in states:
                        birth = process_birth(value.get('pid'))
                        if birth and birth == value.get('birth'):
                            if component == 'watchdog':
                                return value
                            from .runner import read_driver_health
                            health = read_driver_health(self.state.run_dir(instance_id))
                            if (health and health['pid'] == value['pid']
                                    and health['birth'] == birth and health['status'] == 'running'):
                                return value
            time.sleep(.2)
        raise RuntimeError(f'Persistent supervisor accepted launch but {component} entry was not observed')

    def _terminal_observed(self, instance_id, marker):
        root = self.state.run_dir(instance_id)
        run_id = read_json(root / 'run.json')['run_id']
        if marker.get('execution_run_id') != run_id:
            return False
        observed = read_run_availability(root, run_id, sample_limit=0, effect_scan_limit=0)
        return observed.run_state in TERMINAL

    def _stop_component(self, instance_id, component):
        receipt = self._component_identity(instance_id, component)
        registered = self._receipt(instance_id)
        if component == 'watchdog':
            registered = registered.get('watchdog') or {}
        if self.platform == 'Linux':
            unit = receipt['unit']
            installed = self.state.root / 'supervision' / unit
            if installed.exists() or registered.get('unit') == unit:
                self._run(self._systemctl('disable', '--now', unit))
        elif self.platform == 'Windows':
            task = receipt['task']
            installed = self.state.root / 'supervision' / (task + '.xml')
            if installed.exists() or registered.get('task') == task:
                self._run(['schtasks', '/Change', '/TN', task, '/DISABLE'])
                # COM avoids localized CSV states and treats completed tasks
                # as already stopped. The name is bound to a validated instance.
                code = ("$ErrorActionPreference='Stop'; $scheduler=New-Object -ComObject Schedule.Service; "
                        "$scheduler.Connect(); $task=$scheduler.GetFolder('\\').GetTask('" + task + "'); "
                        "if ($task.GetInstances(0).Count -gt 0) { $task.Stop(0) }")
                self._run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', code])
        return {**receipt, 'state': 'stopped'}

    def suppress_watchdog(self, instance_id, *, reason='user_stop'):
        self.state.get(instance_id)
        atomic_json(self.state.run_dir(instance_id) / 'desktop-watchdog-control.json',
                    {'instance': instance_id, 'suppressed': True, 'reason': reason, 'at': time.time()})
        previous = self._receipt(instance_id)
        try:
            self._stop_component(instance_id, 'watchdog')
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            previous['watchdog'] = {**self._component_identity(instance_id, 'watchdog'),
                                    'state': 'suppressed', 'reason': reason, 'stop_error': str(error)[-3000:]}
            self.state.update_launch(instance_id, supervisor=previous,
                                     error='Watchdog suppression persisted; service stop failed: ' + str(error)[-3000:])
            return previous['watchdog']
        previous['watchdog'] = {**self._component_identity(instance_id, 'watchdog'),
                                'state': 'suppressed', 'reason': reason}
        self.state.update_launch(instance_id, supervisor=previous)
        return previous['watchdog']

    def stop_driver(self, instance_id):
        self.state.get(instance_id)
        result = self._stop_component(instance_id, 'driver')
        receipt = self._receipt(instance_id)
        receipt['driver_state'] = 'stopped'
        self.state.update_launch(instance_id, supervisor=receipt)
        return result

    def stop(self, instance_id, *, reason='user_stop'):
        self.suppress_watchdog(instance_id, reason=reason)
        return self.stop_driver(instance_id)


def _scheduler_exit_code(code, *, platform_name=None):
    # Task Scheduler restarts every nonzero exit; business outcomes remain in
    # the SDK/projection/marker rather than authorizing another execution.
    return 0 if code == 78 and (platform_name or platform.system()) == 'Windows' else code


def _result_marker(snapshot):
    business_state = snapshot['state']
    app = snapshot.get('application_state') or {}
    waiting = any(wait.get('state') == 'open' for wait in snapshot.get('waits', {}).values())
    code = 0 if business_state == 'succeeded' else 78 if business_state in TERMINAL or waiting else 1
    failures = []
    for task_id, task in snapshot.get('tasks', {}).items():
        attempts = task.get('attempts', [])
        if not attempts:
            continue
        attempt = attempts[-1]
        outcome = (attempt.get('result') or {}).get('value') or {}
        outcome = outcome if isinstance(outcome, dict) else {}
        if outcome.get('status') in {'failed', 'blocked'} or attempt.get('state') in {'failed', 'rejected', 'timed_out'}:
            failures.append({'task_id': task_id[:256], 'error_code': str(outcome['error_code'])[:256] if outcome.get('error_code') else None,
                             'detail': str(outcome.get('detail') or attempt.get('error') or '')[:2000]})
            if len(failures) == 3:
                break
    return {'state': 'terminal' if business_state in TERMINAL else 'waiting' if waiting else 'failed',
            'business_state': business_state, 'sdk_revision': snapshot.get('revision'),
            'terminal_reason': str(app.get('terminal_reason') or app.get('stop_reason'))[:2000]
                if app.get('terminal_reason') or app.get('stop_reason') else None, 'failures': failures,
            'acceptance_status': app.get('acceptance_status', 'unverified'),
            'exit_code': code, 'process_exit_code': _scheduler_exit_code(code),
            'exit_classification': 'success' if code == 0 else 'coordination' if code == 78 else 'unexpected_nonterminal_exit'}


def run_instance(data_root, instance_id, *, operations=None):
    store = DesktopState(data_root)
    store.get(instance_id)
    from .operations import MigrationOperations
    from .sdk_compat import inspect_runtime, require_compatible_storage
    root = store.run_dir(instance_id)
    header = read_json(root / 'run.json')
    from .desktop_state import managed_instance_id
    if managed_instance_id(header) != instance_id or Path(header['run_dir']).resolve() != root.resolve():
        raise ValueError('Registered instance does not match the frozen Run')
    execution_run_id = header['run_id']
    # Preserve public SDK identity/storage diagnostics before opening writers.
    report = inspect_runtime(root)
    atomic_json(root / 'desktop-sdk-inspection.json', report)
    require_compatible_storage(root)
    marker = root / 'desktop-driver-state.json'
    from .platform_runtime import process_birth
    from .run_monitor import pid_namespace
    birth = process_birth(os.getpid())
    namespace = pid_namespace()
    prior = read_json(marker, limit=65536) if marker.exists() else {}
    atomic_json(marker, {'instance': instance_id, 'execution_run_id': execution_run_id,
                         'pid': os.getpid(), 'birth': birth, 'pid_namespace': namespace,
                         'at': time.time(), 'state': 'launching'})
    observed = None
    try:
        # Inspect existing stores through the public read-only SDK API. A
        # settled Run never opens an execution writer or requires credentials.
        observed = read_run_availability(root, execution_run_id, sample_limit=0, effect_scan_limit=0)
        if observed.run_state in TERMINAL:
            if any(message['state'] in {'queued', 'running'} for message in store.messages(instance_id)):
                # The public read-only API supplies execution identity/state,
                # not business reply payloads. Preserve that uncertainty.
                try:
                    message_state = read_run_availability(root, execution_run_id, sample_limit=1000, effect_scan_limit=0)
                    task_ids = {sample.task_id for sample in message_state.summaries}
                    task_scan_complete = not message_state.summaries_truncated and message_state.missing_executions == 0
                except (OSError, ValueError, RuntimeError, KeyError) as error:
                    note_desktop_error(header, 'terminal_messages', error, revision=observed.run_revision)
                    task_ids, task_scan_complete = set(), False
                store.settle_terminal_messages(instance_id, task_ids=task_ids, task_scan_complete=task_scan_complete)
            projection = root / 'desktop-status.json'
            value = read_json(projection) if projection.exists() else store.status(instance_id)
            same_segment = value.get('execution_run_id', value.get('id')) == execution_run_id
            if not same_segment:
                # Results from a predecessor are historical, not this segment's
                # task or acceptance evidence.
                value['acceptance_status'] = 'unverified'
                value['notice'] = None
                for group in value.get('stages', {}).values():
                    group.update(state='pending', items=[])
            value.update(id=instance_id, execution_run_id=execution_run_id,
                         status=observed.run_state, sdk_revision=observed.run_revision, observed_at=time.time())
            atomic_json(projection, value)
            store.release_workspace(instance_id)
            outcome = _result_marker({'state': observed.run_state, 'revision': observed.run_revision})
            if same_segment:
                outcome['acceptance_status'] = value.get('acceptance_status', 'unverified')
            # Preserve prior detailed business evidence, without treating a
            # projection or marker as authority for lifecycle decisions.
            if (prior.get('execution_run_id') == execution_run_id
                    and prior.get('business_state') == observed.run_state and prior.get('sdk_revision') == observed.run_revision):
                for key in ('terminal_reason', 'failures', 'acceptance_status'):
                    outcome[key] = prior.get(key, outcome[key])
            atomic_json(marker, {'instance': instance_id, 'execution_run_id': execution_run_id,
                                 'pid': os.getpid(), 'birth': birth, 'pid_namespace': namespace,
                                 'at': time.time(), **outcome})
            return outcome['exit_code']
        restore_host_environment(store, instance_id)
        operations = operations or MigrationOperations()
        # resume applies the existing startup recovery protocol. It retains the
        # original Run deadline and cumulative task/assignment/token usage.
        result = operations.resume(root, execution_run_id)
        if result.snapshot.get('run_id', execution_run_id) != execution_run_id:
            raise ValueError('SDK result belongs to a different execution Run')
        publish_snapshot_safely(header, result.snapshot, force=True)
        if result.snapshot['state'] in TERMINAL:
            store.release_workspace(instance_id)
        outcome = _result_marker(result.snapshot)
        atomic_json(marker, {'instance': instance_id, 'execution_run_id': execution_run_id,
                             'pid': os.getpid(), 'birth': birth, 'pid_namespace': namespace,
                             'at': time.time(), **outcome})
        if outcome['exit_code'] == 1:
            store.update_launch(instance_id, error='Driver returned without a terminal SDK outcome or an open coordination wait')
        return outcome['exit_code']
    except BaseException as error:
        atomic_json(marker, {'instance': instance_id, 'execution_run_id': execution_run_id,
                             'business_state': observed.run_state if observed is not None else None,
                             'pid': os.getpid(), 'birth': birth, 'pid_namespace': namespace,
                             'at': time.time(), 'state': 'failed',
                             'error': str(error)[:6000], 'exit_code': 1, 'process_exit_code': 1, 'exit_classification': 'host_failure'})
        store.update_launch(instance_id, error=str(error)[:6000])
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description='Drive one registered ModPort desktop instance')
    parser.add_argument('--data-root', required=True)
    parser.add_argument('--instance', required=True)
    args = parser.parse_args(argv)
    return _scheduler_exit_code(run_instance(args.data_root, args.instance))


if __name__ == '__main__':
    raise SystemExit(main())
