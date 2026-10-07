"""Independent desktop watchdog: durable SDK wakeups and exact-driver recovery.

This process never executes workers or edits SDK storage directly. Business
recovery belongs to MigrationOperations; the service restarts only a driver
whose recorded process identity is proved dead in the same PID namespace.
"""
from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import signal
import threading
import time
from uuid import uuid4

from dispatcher_sdk.orchestrator import Orchestrator

from .desktop_driver import PersistentSupervisor, restore_host_environment
from .desktop_state import DesktopState, managed_instance_id, read_json
from .evidence import atomic_json
from .platform_runtime import process_birth
from .run_monitor import driver_namespace_match, process_identity_state
from .runner import read_driver_health
from .sdk_compat import require_compatible_storage
from .token_budget import read_token_budget


class DesktopWatchdog:
    def __init__(self, store, instance_id, sdk, *, operations=None, supervisor=None,
                 accept_notification=None, clock=time.time):
        self.store = store
        self.instance = instance_id
        self.root = store.run_dir(instance_id)
        self.header = read_json(self.root / 'run.json')
        if (managed_instance_id(self.header) != instance_id
                or Path(self.header['run_dir']).resolve() != self.root.resolve()):
            raise ValueError('Registered instance does not match the frozen Run')
        self.run_id = self.header['run_id']
        initial = sdk.get_run_summary(self.run_id)
        if initial['run_id'] != self.run_id:
            raise ValueError('SDK watchdog observation belongs to another Run')
        self.business_state = initial['state']
        self.sdk = sdk
        self.operations = operations
        self.supervisor = supervisor or PersistentSupervisor(store)
        if accept_notification is None:
            from .watchdog_events import accept_notification
        self.accept_notification = accept_notification
        self.clock = clock
        self.owner = 'modport-watchdog-' + uuid4().hex
        self.marker = self.root / 'desktop-watchdog-state.json'
        previous = read_json(self.marker, limit=65536) if self.marker.exists() else {}
        self.last_restart = previous.get('last_driver_restart') if previous.get('instance') == instance_id else None
        self.recovery_retry = previous.get('recovery_retry') if previous.get('instance') == instance_id else None
        self.notification_error = previous.get('notification_error') if previous.get('instance') == instance_id else None
        self.birth = process_birth(os.getpid())
        if not self.birth:
            raise RuntimeError('Watchdog process birth identity is unavailable')

    def publish(self, state='running', **facts):
        value = {'instance': self.instance, 'execution_run_id': self.run_id,
                 'pid': os.getpid(), 'birth': self.birth, 'at': self.clock(),
                 'state': state, 'last_driver_restart': self.last_restart,
                 'business_state': self.business_state,
                 'recovery_retry': self.recovery_retry,
                 'notification_error': self.notification_error, **facts}
        atomic_json(self.marker, value)
        return value

    def suppressed(self):
        path = self.root / 'desktop-watchdog-control.json'
        if not path.exists():
            return False
        value = read_json(path, limit=65536)
        if value.get('instance') != self.instance or type(value.get('suppressed')) is not bool:
            raise ValueError('Invalid watchdog suppression control')
        return value['suppressed']

    def driver_identity(self):
        health = read_driver_health(self.root)
        marker_path = self.root / 'desktop-driver-state.json'
        marker = read_json(marker_path, limit=65536) if marker_path.exists() else None
        if (marker and marker.get('instance') == self.instance
                and marker.get('execution_run_id') == self.run_id):
            if driver_namespace_match(marker) is True:
                return process_identity_state(marker.get('pid'), marker.get('birth')), marker
            if health and (marker.get('pid'), marker.get('birth')) != (health.get('pid'), health.get('birth')):
                # A fresh launcher must not be mistaken for its predecessor.
                return None, marker
        if not health or health.get('run_id') != self.run_id:
            return None, health
        if driver_namespace_match(health) is not True:
            return None, health
        return process_identity_state(health.get('pid'), health.get('birth')), health

    def _notification(self, notification):
        try:
            self.accept_notification(self.root, notification)
            self.notification_error = None
        except Exception as error:
            self.notification_error = {'notification_id': notification.get('notification_id'),
                                       'type': type(error).__name__, 'detail': str(error)[:3000]}
            raise

    def _driver_loss(self, summary, health):
        identity = {'run_id': self.run_id, 'generation': summary['generation'],
                    'pid': health['pid'], 'birth': health['birth']}
        marker_path = self.root / 'desktop-driver-state.json'
        marker = read_json(marker_path, limit=65536) if marker_path.exists() else {}
        notification = {'notification_id': f"driver-lost:{self.run_id}:{summary['generation']}:{health['pid']}:{health['birth']}",
                        'run_id': self.run_id, 'generation': summary['generation'], 'kind': 'driver_lost',
                        'payload': {'driver': identity, 'health': health, 'driver_marker': marker}}
        # Durable acceptance precedes restart. A callback failure remains a
        # concrete host error, never a synthesized missing-context diagnosis.
        self.accept_notification(self.root, notification)
        return identity, marker

    def step(self):
        if self.suppressed():
            return self.publish('terminal', reason='user_suppressed')
        delivered = self.sdk.deliver_notifications(self._notification, owner=self.owner,
                                                    lease_seconds=30, retry_delay=1, limit=32)
        summary = self.sdk.get_run_summary(self.run_id)
        if summary['run_id'] != self.run_id:
            raise ValueError('SDK watchdog observation belongs to another Run')
        self.business_state = summary['state']
        if summary['state'] in {'succeeded', 'cancelled'}:
            return self.publish('terminal', business_state=summary['state'], notifications_delivered=delivered)
        deadline = self.header.get('deadline_epoch')
        if (type(deadline) not in {int, float} or not math.isfinite(deadline)):
            raise ValueError('Watchdog requires the original finite Run deadline')
        exhausted = self.clock() >= deadline or read_token_budget(self.root)['exhausted']
        if exhausted and summary['state'] == 'failed':
            return self.publish('terminal', reason='original_budget_exhausted', business_state='failed')
        alive, health = self.driver_identity()
        if alive is not False:
            return self.publish(driver_identity='alive' if alive is True else 'unknown',
                                business_state=summary['state'], notifications_delivered=delivered,
                                reason='original_budget_exhausted' if exhausted else None)
        identity, marker = self._driver_loss(summary, health)
        if self.suppressed():
            return self.publish('terminal', reason='user_suppressed')
        coordination = marker.get('instance') == self.instance and marker.get('state') == 'waiting'
        if not exhausted and (summary['state'] == 'failed' or coordination):
            observation = {key: summary.get(key) for key in ('state', 'generation', 'revision')}
            if (self.recovery_retry and self.recovery_retry.get('observation') == observation
                    and self.clock() < self.recovery_retry.get('at', 0) and delivered == 0):
                return self.publish(reason='recovery_blocked', driver_identity='dead',
                                    recovery=self.recovery_retry.get('result'))
            if self.operations is None:
                from .operations import MigrationOperations
                restore_host_environment(self.store, self.instance)
                self.operations = MigrationOperations()
            recovery = self.operations.watchdog_recover(self.root, self.run_id)
            if not isinstance(recovery, dict) or recovery.get('status') not in {'reopened', 'terminal', 'blocked'}:
                raise ValueError('Watchdog recovery returned an invalid status')
            if recovery['status'] == 'blocked':
                latest = self.sdk.get_run_summary(self.run_id)
                self.business_state = latest['state']
                self.recovery_retry = {'observation': {key: latest.get(key) for key in ('state', 'generation', 'revision')},
                                       'at': self.clock() + 30, 'result': recovery}
                return self.publish(reason='recovery_blocked', driver_identity='dead', recovery=recovery)
            self.recovery_retry = None
            if recovery['status'] == 'terminal':
                self.business_state = self.sdk.get_run_summary(self.run_id)['state']
                return self.publish('terminal',
                                    reason=recovery.get('reason') or recovery['status'], recovery=recovery)
            summary = self.sdk.get_run_summary(self.run_id)
            self.business_state = summary['state']
            if summary['run_id'] != self.run_id or summary['state'] != 'running':
                raise ValueError('Recovered SDK Run is not the same running execution')
            identity['generation'] = summary['generation']
        if self.last_restart == identity:
            return self.publish(reason='driver_restart_already_requested', driver_identity='dead')
        # Recheck after diagnosis/recovery so a concurrent user cancellation or
        # systemd restart cannot become another restart authorization.
        if self.suppressed():
            return self.publish('terminal', reason='user_suppressed')
        current = self.sdk.get_run_summary(self.run_id)
        alive, current_health = self.driver_identity()
        if (current['state'] != 'running' or current['generation'] != identity['generation']
                or alive is not False or not current_health
                or current_health.get('pid') != identity['pid'] or current_health.get('birth') != identity['birth']):
            return self.publish(reason='restart_observation_changed')
        self.supervisor.launch_driver(self.instance)
        self.last_restart = identity
        return self.publish(reason='original_budget_settlement_requested' if exhausted else 'dead_driver_restarted',
                            driver_identity='dead')


def run_watchdog(data_root, instance_id, *, stop_event=None, poll_seconds=1.0,
                 sdk_factory=Orchestrator, operations=None, supervisor=None,
                 accept_notification=None):
    if isinstance(poll_seconds, bool) or not math.isfinite(poll_seconds) or poll_seconds <= 0:
        raise ValueError('watchdog poll interval must be positive and finite')
    store = DesktopState(data_root)
    store.get(instance_id)
    root = store.run_dir(instance_id)
    sdk = None
    watchdog = None
    stop_event = stop_event or threading.Event()
    try:
        for name in ('kernel.sqlite3', 'orchestrator.sqlite3'):
            path = root / name
            if path.is_symlink() or not path.is_file():
                raise ValueError('Watchdog requires existing SDK stores: ' + name)
        require_compatible_storage(root)
        sdk = sdk_factory(root / 'orchestrator.sqlite3', None)
        watchdog = DesktopWatchdog(store, instance_id, sdk, operations=operations,
                                   supervisor=supervisor, accept_notification=accept_notification)
        watchdog.publish()
        while not stop_event.is_set():
            if watchdog.step()['state'] == 'terminal':
                return 0
            stop_event.wait(poll_seconds)
        watchdog.publish('stopped', reason='service_stopped')
        return 0
    except BaseException as error:
        if watchdog is not None:
            watchdog.publish('failed', error=str(error)[:6000], error_type=type(error).__name__)
        else:
            atomic_json(root / 'desktop-watchdog-state.json',
                        {'instance': instance_id, 'pid': os.getpid(), 'birth': process_birth(os.getpid()),
                         'at': time.time(), 'state': 'failed', 'error': str(error)[:6000],
                         'error_type': type(error).__name__})
        store.update_launch(instance_id, error='Independent watchdog failed: ' + str(error)[:6000])
        raise
    finally:
        if sdk is not None:
            sdk.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Independently supervise one registered ModPort desktop instance')
    parser.add_argument('--data-root', required=True)
    parser.add_argument('--instance', required=True)
    parser.add_argument('--poll-seconds', type=float, default=1.0)
    args = parser.parse_args(argv)
    stop = threading.Event()
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
    return run_watchdog(args.data_root, args.instance, stop_event=stop, poll_seconds=args.poll_seconds)


if __name__ == '__main__':
    raise SystemExit(main())
