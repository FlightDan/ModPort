"""Keep a workload inside its SDK execution window, with room to settle it.

The command is immutable evidence.  A context variable carries the local
deadline to nested handlers without changing that command or its receipt hash.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import math
import time
from typing import Any, Callable, Mapping

from dispatcher_sdk.execution_kernel import BudgetClockUnknownError


_RECEIPT_ALLOWANCE_SECONDS = 60.0
_SDK_RECEIPT_FRACTION = 0.5

# Lease renewal controls execution authority; it is independent of the public
# SDK execution budget and never supplies a workload deadline.
SDK_LEASE_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class DeadlineBudget:
    """Absolute deadlines with one, explicitly recorded settlement reserve."""

    run_deadline: float | None
    stage_deadline: float
    effective_deadline: float
    work_deadline: float
    settlement_reserve: float

    def remaining_work(self, *, now: float | None = None) -> float:
        current = time.time() if now is None else _finite(now, "current time")
        return self.work_deadline - current

    def remaining_settlement(self, *, now: float | None = None) -> float:
        current = time.time() if now is None else _finite(now, "current time")
        return self.effective_deadline - current

    def timeout(self, maximum: float, *, now: float | None = None) -> float:
        limit = _positive(maximum, "maximum workload timeout")
        remaining = self.remaining_work(now=now)
        if remaining <= 0:
            raise TimeoutError("execution has no remaining workload time before settlement")
        return min(limit, remaining)


@dataclass(frozen=True, slots=True)
class SettlementBudget:
    """One allocation split between model, capture, publication, and SDK receipt."""

    effective_deadline: float
    model_deadline: float
    capture_deadline: float
    publication_deadline: float
    receipt_deadline: float
    capture_reserve: float
    publication_reserve: float
    receipt_reserve: float

    @property
    def settlement_reserve(self) -> float:
        return self.capture_reserve + self.publication_reserve + self.receipt_reserve

    def timeout(self, phase: str, maximum: float, *, now: float | None = None) -> float:
        limit = _positive(maximum, "maximum workload timeout")
        current = time.time() if now is None else _finite(now, "current time")
        deadlines = {
            "model": self.model_deadline,
            "capture": self.capture_deadline,
            "publication": self.publication_deadline,
            "receipt": self.receipt_deadline,
        }
        if phase not in deadlines:
            raise ValueError("unknown execution budget phase")
        remaining = deadlines[phase] - current
        if remaining <= 0:
            raise TimeoutError(f"execution has no remaining {phase} time")
        return min(limit, remaining)


@dataclass(frozen=True, slots=True)
class _ExecutionBudgetState:
    execution_id: str
    budget: DeadlineBudget
    now: Callable[[], float]
    settlement: SettlementBudget | None = None
    phase: str = "model"


def _finite(value: Any, name: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be finite")
    return parsed


def _positive(value: Any, name: str) -> float:
    parsed = _finite(value, name)
    if parsed <= 0:
        raise ValueError(f"{name} must be positive and finite")
    return parsed


def deadline_budget(*, run_deadline: float | None, stage_deadline: float,
                    settlement_reserve: float) -> DeadlineBudget:
    """Resolve the shared Run/stage window without deducting reserve twice.

    All values are absolute epoch deadlines except ``settlement_reserve``.
    Consumers reuse the returned ``work_deadline``; they must not derive a new
    deadline by subtracting the reserve again.
    """

    stage = _finite(stage_deadline, "stage deadline")
    run = None if run_deadline is None else _finite(run_deadline, "run deadline")
    reserve = _finite(settlement_reserve, "settlement reserve")
    if reserve < 0:
        raise ValueError("settlement reserve must be nonnegative")
    effective = stage if run is None else min(run, stage)
    return DeadlineBudget(run, stage, effective, effective - reserve, reserve)


def _narrow_run_deadline(original: float | None, supplied: Any) -> float | None:
    current = None if supplied is None else _finite(supplied, "run deadline")
    if original is None:
        return current
    return original if current is None else min(original, current)


_current_execution: ContextVar[_ExecutionBudgetState | None] = ContextVar(
    "modport_execution_budget", default=None
)
_current_sdk_context: ContextVar[Any | None] = ContextVar(
    "modport_sdk_context", default=None
)


def current_sdk_context() -> Any | None:
    """Return the active public handler context, independently of budget phases."""

    return _current_sdk_context.get()


@contextmanager
def execution_budget(context: Any, *, now: Callable[[], float] | None = None):
    """Bound nested calls before the SDK's outer handler timer can fire.

    The public ``HandlerContext.budget`` carries actual handler entry and
    inherited Run/parent constraints. Its work cutoff already preserves SDK
    reserves. Keep ModPort capture/publication/receipt time within that cutoff,
    and sample its authoritative clock at blocking calls rather than guessing
    from a lease or granting a new ``now + timeout`` window.
    """

    command = context.command
    duration = _positive(command.timeout_seconds, "SDK command timeout")
    allowance = min(_RECEIPT_ALLOWANCE_SECONDS, duration * 0.2)
    def sdk_clock() -> float:
        view = context.budget
        if view.clock_status != "trusted" or view.observed_at is None:
            raise BudgetClockUnknownError(view.unknown_reason or "SDK budget clock is unknown")
        observed = _finite(view.observed_at, "SDK observed time")
        return observed if now is None else max(observed, _finite(now(), "current time"))

    view = context.budget
    if view.clock_status != "trusted" or view.observed_at is None:
        raise BudgetClockUnknownError(view.unknown_reason or "SDK budget clock is unknown")
    if view.effective_work_deadline_at is None:
        raise ValueError("SDK execution budget requires a finite work deadline")
    stage_deadline = _finite(view.effective_work_deadline_at, "SDK work deadline")
    payload = command.payload if isinstance(getattr(command, "payload", None), Mapping) else {}
    options = payload.get("options") if isinstance(payload.get("options"), Mapping) else {}
    run_deadline = options.get("deadline_epoch")
    budget = deadline_budget(
        run_deadline=run_deadline, stage_deadline=stage_deadline,
        settlement_reserve=allowance,
    )
    token = _current_execution.set(
        _ExecutionBudgetState(command.execution_id, budget, sdk_clock)
    )
    sdk_token = _current_sdk_context.set(context)
    try:
        yield
    finally:
        _current_sdk_context.reset(sdk_token)
        _current_execution.reset(token)


def remaining_timeout(command: Any, maximum: float) -> float:
    """Return a workload timeout bounded by the Run and active SDK execution."""

    maximum = _positive(maximum, "maximum workload timeout")
    payload = getattr(command, "payload", {})
    payload = payload if isinstance(payload, Mapping) else {}
    options = getattr(command, "options", None) or payload.get("options", {})
    run_deadline = options.get("deadline_epoch") if isinstance(options, Mapping) else None
    model_deadline = (options.get("model_deadline_epoch")
                      if isinstance(options, Mapping) else None)
    if model_deadline is not None:
        model_deadline = _finite(model_deadline, "model deadline")
    execution = _current_execution.get()
    if execution is not None:
        if execution.execution_id != command.command_id:
            raise ValueError("SDK execution budget does not match operation identity")
        active = execution.budget
        if execution.settlement is not None:
            phase_deadline = {
                "model": execution.settlement.model_deadline,
                "capture": execution.settlement.capture_deadline,
                "publication": execution.settlement.publication_deadline,
                "receipt": execution.settlement.receipt_deadline,
            }[execution.phase]
            if run_deadline is not None:
                phase_deadline = min(phase_deadline, _finite(run_deadline, "run deadline"))
            if execution.phase == "model" and model_deadline is not None:
                phase_deadline = min(phase_deadline, model_deadline)
            remaining = phase_deadline - execution.now()
            if remaining <= 0:
                raise TimeoutError(
                    f"execution has no remaining {execution.phase} time")
            return min(maximum, remaining)
        # The active stage carries the one authoritative reserve. Re-resolving
        # against a narrower Run deadline changes the effective deadline, not
        # the reserve, so nested callers cannot compound the deduction.
        active = deadline_budget(
            run_deadline=_narrow_run_deadline(active.run_deadline, run_deadline),
            stage_deadline=active.stage_deadline,
            settlement_reserve=active.settlement_reserve,
        )
        timeout = active.timeout(maximum, now=execution.now())
        if model_deadline is not None:
            remaining = model_deadline - execution.now()
            if remaining <= 0:
                raise TimeoutError("execution has no remaining model time")
            timeout = min(timeout, remaining)
        return timeout
    if run_deadline is not None:
        remaining = _finite(run_deadline, "run deadline") - time.time()
        if remaining <= 0:
            raise TimeoutError("run wall-clock budget is exhausted")
        maximum = min(maximum, remaining)
    if model_deadline is not None:
        remaining = model_deadline - time.time()
        if remaining <= 0:
            raise TimeoutError("execution has no remaining model time")
        maximum = min(maximum, remaining)
    return maximum


def current_deadline_budget(command: Any) -> DeadlineBudget | None:
    """Return the active absolute budget for integration code and diagnostics."""

    execution = _current_execution.get()
    if execution is None:
        return None
    if execution.execution_id != command.command_id:
        raise ValueError("SDK execution budget does not match operation identity")
    active = execution.budget
    payload = getattr(command, "payload", {})
    payload = payload if isinstance(payload, Mapping) else {}
    options = getattr(command, "options", None) or payload.get("options", {})
    run_deadline = options.get("deadline_epoch") if isinstance(options, Mapping) else None
    return deadline_budget(
        run_deadline=_narrow_run_deadline(active.run_deadline, run_deadline),
        stage_deadline=active.stage_deadline,
        settlement_reserve=active.settlement_reserve,
    )


def reserve_settlement(command: Any, capture_reserve: float) -> SettlementBudget | None:
    """Allocate capture once and partition the existing tail reserve once.

    Direct unit callers outside :func:`execution_budget` receive ``None`` and
    keep legacy behavior. Inside an SDK execution, repeated or nested requests
    use the largest requested capture reserve; they never add publication or
    SDK receipt reserves to the authoritative tail.
    """

    requested = _finite(capture_reserve, "capture reserve")
    if requested < 0:
        raise ValueError("capture reserve must be nonnegative")
    execution = _current_execution.get()
    if execution is None:
        return None
    if execution.execution_id != command.command_id:
        raise ValueError("SDK execution budget does not match operation identity")
    active = current_deadline_budget(command)
    if active is None:  # pragma: no cover - guarded by the ContextVar above
        return None
    prior = execution.settlement.capture_reserve if execution.settlement is not None else 0.0
    capture = max(prior, requested)
    tail = active.settlement_reserve
    receipt = tail * _SDK_RECEIPT_FRACTION
    publication = tail - receipt
    capture_deadline = active.work_deadline
    publication_deadline = active.effective_deadline - receipt
    settlement = SettlementBudget(
        effective_deadline=active.effective_deadline,
        model_deadline=capture_deadline - capture,
        capture_deadline=capture_deadline,
        publication_deadline=publication_deadline,
        receipt_deadline=active.effective_deadline,
        capture_reserve=capture,
        publication_reserve=publication,
        receipt_reserve=receipt,
    )
    _current_execution.set(_ExecutionBudgetState(
        execution.execution_id, active, execution.now, settlement, execution.phase))
    return settlement


def current_settlement_budget(command: Any) -> SettlementBudget | None:
    """Return the model/capture/publication/SDK-receipt split."""

    execution = _current_execution.get()
    if execution is None:
        return None
    if execution.execution_id != command.command_id:
        raise ValueError("SDK execution budget does not match operation identity")
    return execution.settlement


def require_remaining_time(command: Any, *, now: float | None = None) -> float | None:
    """Check the active host phase before and after non-subprocess work.

    The returned seconds are diagnostic.  Callers must not turn them into a
    new deadline or reserve.  A direct caller without an SDK execution context
    is checked only against an explicit command Run deadline.
    """

    execution = _current_execution.get()
    current = execution.now() if execution is not None else time.time()
    if now is not None:
        supplied = _finite(now, "current time")
        current = supplied if execution is None else max(current, supplied)
    payload = getattr(command, "payload", {})
    payload = payload if isinstance(payload, Mapping) else {}
    options = getattr(command, "options", None) or payload.get("options", {})
    run_deadline = options.get("deadline_epoch") if isinstance(options, Mapping) else None
    if execution is None:
        if run_deadline is None:
            return None
        deadline = _finite(run_deadline, "run deadline")
        phase = "run"
    else:
        if execution.execution_id != command.command_id:
            raise ValueError("SDK execution budget does not match operation identity")
        phase = execution.phase
        if execution.settlement is None:
            deadline = current_deadline_budget(command).work_deadline
        else:
            deadline = {
                "model": execution.settlement.model_deadline,
                "capture": execution.settlement.capture_deadline,
                "publication": execution.settlement.publication_deadline,
                "receipt": execution.settlement.receipt_deadline,
            }[phase]
            if run_deadline is not None:
                deadline = min(deadline, _finite(run_deadline, "run deadline"))
    remaining = deadline - current
    if remaining <= 0:
        raise TimeoutError(f"execution has no remaining {phase} time")
    return remaining


@contextmanager
def settlement_phase(command: Any):
    """Spend only capture time, preserving publication and SDK receipt tails."""

    execution = _current_execution.get()
    if execution is None:
        # Raw handler unit tests historically run without an SDK context.
        yield None
        return
    if execution.execution_id != command.command_id:
        raise ValueError("SDK execution budget does not match operation identity")
    if execution.settlement is None:
        raise RuntimeError("settlement reserve must be configured before capture")
    token = _current_execution.set(_ExecutionBudgetState(
        execution.execution_id, execution.budget, execution.now,
        execution.settlement, "capture"))
    try:
        yield execution.settlement
    finally:
        _current_execution.reset(token)


@contextmanager
def publication_phase(command: Any):
    """Publish host artifacts while preserving the true SDK receipt tail."""

    execution = _current_execution.get()
    if execution is None:
        yield None
        return
    if execution.execution_id != command.command_id:
        raise ValueError("SDK execution budget does not match operation identity")
    if execution.settlement is None:
        raise RuntimeError("settlement reserve must be configured before publication")
    token = _current_execution.set(_ExecutionBudgetState(
        execution.execution_id, execution.budget, execution.now,
        execution.settlement, "publication"))
    try:
        yield execution.settlement
    finally:
        _current_execution.reset(token)


@contextmanager
def receipt_phase(command: Any):
    """Compatibility name for bounded host publication, not SDK receipt time."""

    with publication_phase(command) as settlement:
        yield settlement


__all__ = [
    "DeadlineBudget", "SettlementBudget", "current_deadline_budget", "current_sdk_context",
    "current_settlement_budget", "deadline_budget", "execution_budget",
    "publication_phase", "receipt_phase", "remaining_timeout", "require_remaining_time",
    "reserve_settlement", "settlement_phase", "SDK_LEASE_SECONDS",
]
