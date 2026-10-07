"""Bound and observe host input preparation independently of model execution."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import json
import math
import os
from pathlib import Path
import signal
import threading
import time

from .contracts import OperationResult


DEFAULT_PREPARATION_TIMEOUT = 300.0
_CURRENT = ContextVar('modport_input_preparation', default=None)


class InputPreparationError(RuntimeError):
    code = 'input_preparation_audit_failed'


class InputPreparationTimeout(InputPreparationError):
    def __init__(self, message, code='input_preparation_timeout'):
        super().__init__(message)
        self.code = code


class InputPreparation:
    def __init__(self, command):
        raw = os.environ.get('MODPORT_INPUT_PREPARATION_TIMEOUT_SECONDS', str(DEFAULT_PREPARATION_TIMEOUT))
        try:
            self.limit = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError('input preparation timeout must be positive and finite') from exc
        if not math.isfinite(self.limit) or not 0 < self.limit <= DEFAULT_PREPARATION_TIMEOUT:
            raise ValueError('input preparation timeout must be positive and no more than 300 seconds')
        self.command_id = command.command_id
        self.run_deadline = command.options.get('deadline_epoch')
        if self.run_deadline is not None and (
                isinstance(self.run_deadline, bool) or not isinstance(self.run_deadline, (int, float))
                or not math.isfinite(self.run_deadline)):
            raise ValueError('Run deadline must be finite')
        self.root = Path(command.run_dir)
        self.path = self.root / 'artifacts' / 'executions' / command.command_id / 'input-preparation.json'
        self.started = time.monotonic()
        self.excluded = 0.0
        self.paused_at = None
        self.pause_depth = 0
        self.phase = 'initialization'
        self.phases = {}
        self.counters = {}
        self.last_save = 0.0
        self.status = 'preparing'
        self.owns_alarm = False
        self.interrupt_mode = 'checkpoints'
        self.old_alarm_handler = None
        self.audit_interrupted = False

    def elapsed(self):
        now = self.paused_at if self.paused_at is not None else time.monotonic()
        return max(0.0, now - self.started - self.excluded)

    def _expired(self):
        self.status = 'timed_out'
        if self.run_deadline is not None and time.time() >= self.run_deadline:
            return InputPreparationTimeout(
                f'Run deadline reached during host input preparation in {self.phase}; '
                f'progress: {self.path.relative_to(self.root)}', 'budget_exhausted')
        return InputPreparationTimeout(
            f'host input preparation exceeded {self.limit:g}s in {self.phase}; '
            f'progress: {self.path.relative_to(self.root)}')

    def _alarm(self, signum, frame):
        raise self._expired()

    def start(self):
        self.acquire_alarm()
        self.arm()
        self.checkpoint(force=True)

    def acquire_alarm(self):
        # Real SDK process workers can interrupt even a blocked preparation
        # operation. Thread-mode fixtures use the same cooperative checkpoints;
        # never replace another component's timer or signal handler.
        if (threading.current_thread() is threading.main_thread()
                and hasattr(signal, 'setitimer')
                and signal.getsignal(signal.SIGALRM) == signal.SIG_DFL
                and signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)):
            self.old_alarm_handler = signal.getsignal(signal.SIGALRM)
            signal.signal(signal.SIGALRM, self._alarm)
            self.owns_alarm = True
            self.interrupt_mode = 'signal'

    def has_alarm(self):
        return self.owns_alarm and signal.getsignal(signal.SIGALRM) == self._alarm

    def arm(self):
        if self.has_alarm():
            remaining = self.limit - self.elapsed()
            if self.run_deadline is not None:
                remaining = min(remaining, self.run_deadline - time.time())
            remaining = 0.0 if self.pause_depth else max(0.000001, remaining)
            signal.setitimer(signal.ITIMER_REAL, remaining)

    def close(self):
        if self.has_alarm():
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, self.old_alarm_handler)
        self.owns_alarm = False

    def save(self):
        record = {'schema_version': 1, 'command_id': self.command_id,
                  'status': self.status, 'phase': self.phase,
                  'timeout_seconds': self.limit, 'host_seconds': self.elapsed(),
                  'excluded_model_seconds': self.excluded + (
                      time.monotonic() - self.paused_at if self.paused_at is not None else 0),
                  'interrupt_mode': 'signal' if self.has_alarm() else 'checkpoints',
                  'phases': self.phases, 'counters': self.counters,
                  'updated_at_epoch': time.time()}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps(record, sort_keys=True) + '\n', encoding='utf-8')
            temporary.replace(self.path)
        except InputPreparationTimeout:
            self.audit_interrupted = True
            raise
        except OSError as exc:
            self.audit_interrupted = True
            self.status = 'audit_failed'
            raise InputPreparationError(f'input preparation progress could not be written (errno={exc.errno})') from exc
        self.last_save = time.monotonic()

    def terminal_diagnostic(self):
        if self.audit_interrupted:
            return  # Do not retry the filesystem operation that just hung.
        if self.has_alarm():
            # The work deadline may have fired already. Bound error reporting
            # separately and never let it replace the intended business result.
            signal.setitimer(signal.ITIMER_REAL, 0.25)
        try:
            self.save()
        except (InputPreparationError, OSError):
            pass

    def checkpoint(self, *, count=None, amount=1, force=False):
        if count is not None:
            self.counters[count] = self.counters.get(count, 0) + amount
        if not self.pause_depth and (self.elapsed() >= self.limit or (
                self.run_deadline is not None and time.time() >= self.run_deadline)):
            self.save()
            raise self._expired()
        if force or time.monotonic() - self.last_save >= 1.0:
            self.save()


@contextmanager
def preparation_phase(name):
    progress = _CURRENT.get()
    if progress is None:
        yield
        return
    previous = progress.phase
    progress.phase = name
    started = progress.elapsed()
    progress.checkpoint(force=True)
    try:
        yield
    finally:
        row = progress.phases.setdefault(name, {'calls': 0, 'host_seconds': 0.0})
        row['calls'] += 1
        row['host_seconds'] += max(0.0, progress.elapsed() - started)
        # Retain the failing phase for the terminal diagnostic.
        if progress.status not in {'timed_out', 'audit_failed'}:
            progress.checkpoint(force=True)
            progress.phase = previous


def preparation_step(name):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with preparation_phase(name):
                return function(*args, **kwargs)
        return wrapped
    return decorate


def preparation_checkpoint(*, count=None, amount=1):
    progress = _CURRENT.get()
    if progress is not None:
        progress.checkpoint(count=count, amount=amount)


@contextmanager
def model_work(name):
    """Model/goal execution keeps its existing deadline, outside the host cap."""
    progress = _CURRENT.get()
    if progress is None:
        yield
        return
    progress.checkpoint()
    previous = progress.phase
    progress.phase = name
    progress.status = 'model_running'
    if progress.pause_depth == 0:
        progress.save()  # Host diagnostic IO still runs under the host timer.
    if progress.pause_depth == 0:
        progress.paused_at = time.monotonic()
    progress.pause_depth += 1
    if progress.pause_depth == 1:
        progress.close()
    try:
        yield
    finally:
        progress.pause_depth -= 1
        if progress.pause_depth == 0:
            progress.excluded += time.monotonic() - progress.paused_at
            progress.paused_at = None
            progress.status = 'preparing'
            progress.acquire_alarm()
        progress.phase = previous
        progress.arm()
        progress.save()


def prepare_inputs(function):
    """Share one preparation clock through nested planning/agent handlers."""
    @wraps(function)
    def wrapped(self, command, *args, **kwargs):
        existing = _CURRENT.get()
        if existing is not None and existing.command_id == command.command_id:
            return function(self, command, *args, **kwargs)
        try:
            progress = InputPreparation(command)
        except ValueError as exc:
            return OperationResult('blocked', command.run_id, command.task_id,
                command.stage_id, command.command_id, detail=str(exc),
                error_code='input_preparation_configuration_invalid')
        token = _CURRENT.set(progress)
        try:
            progress.start()
            result = function(self, command, *args, **kwargs)
            progress.checkpoint()
            progress.status = 'finished'
            progress.save()
            return result
        except InputPreparationError as exc:
            progress.status = 'timed_out' if isinstance(exc, InputPreparationTimeout) else 'audit_failed'
            progress.terminal_diagnostic()
            return OperationResult('blocked', command.run_id, command.task_id,
                command.stage_id, command.command_id,
                outputs={'input_preparation': str(progress.path.relative_to(progress.root))},
                detail=str(exc), error_code=exc.code)
        except BaseException:
            progress.status = 'interrupted'
            progress.terminal_diagnostic()
            raise
        finally:
            try:
                progress.close()
            finally:
                _CURRENT.reset(token)
    return wrapped
