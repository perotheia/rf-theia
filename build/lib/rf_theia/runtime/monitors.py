"""Temporal monitors — Eventually / Always / Never.

Each monitor subscribes to the runtime event bus and re-evaluates a
predicate string (via ExprEvaluator) whenever a relevant event lands.
Semantics:

  Eventually(P, within=T)   — pass if P() == True at least once in T.
                              Block keyword caller until either P
                              becomes true (early-return: pass) or T
                              elapses (fail).

  Always(P, during=T)       — pass if P() == True at every evaluation
                              point in T. As soon as P() returns False
                              we fail-fast. Otherwise wait T and pass.

  Never(P, during=T)        — pass if P() never becomes True in T. As
                              soon as P() returns True we fail-fast.
                              Otherwise wait T and pass.

The evaluation triggers: any event published on the bus while the
monitor is armed. trace_record, state_entered, supervisor_child_*,
emit_event — all of them. We re-check on every event because the
predicate's bindings (trace.count, service.state, etc.) are derived
from event history.

This is the **event-driven** mode. A future sampling mode (for
continuous-time predicates like heartbeat_period < N) is a separate
monitor type; not in Pair 2.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

from .event_bus import Event, EventBus
from .expr import ExprEvaluator

logger = logging.getLogger("rf_theia.monitor")


@dataclass
class MonitorResult:
    """Outcome of one monitor run.

    `verdict`: "pass" | "fail"
    `reason`:  human-readable string; included in the AssertionError
               raised by the keyword when verdict == "fail".
    """
    verdict: str
    reason: str
    elapsed: float


class _BaseMonitor:
    """Common machinery: subscribe to all bus events, re-evaluate on
    each, wake the caller via a condition variable."""

    def __init__(self, bus: EventBus, evaluator: ExprEvaluator,
                 expr: str, timeout: float) -> None:
        self.bus = bus
        self.evaluator = evaluator
        self.expr = expr
        self.timeout = timeout
        self._cond = threading.Condition()
        self._result: Optional[MonitorResult] = None
        self._unsub = None
        self._start_ts = 0.0

    def run(self) -> MonitorResult:
        self._start_ts = time.monotonic()
        self._unsub = self.bus.subscribe(None, self._on_event)
        try:
            # Initial evaluation (cover the case where the predicate is
            # already satisfied/violated before any new event arrives).
            self._evaluate(_initial=True)
            with self._cond:
                while self._result is None:
                    remaining = self.timeout - (time.monotonic() - self._start_ts)
                    if remaining <= 0:
                        self._on_timeout()
                        break
                    self._cond.wait(timeout=remaining)
            assert self._result is not None
            return self._result
        finally:
            if self._unsub is not None:
                self._unsub()

    def _on_event(self, ev: Event) -> None:
        if self._result is not None:
            return
        self._evaluate(_initial=False)

    def _evaluate(self, _initial: bool) -> None:
        try:
            value = self.evaluator.eval(self.expr)
        except Exception as e:
            self._finish(MonitorResult(
                verdict="fail",
                reason=f"predicate eval error: {e}",
                elapsed=time.monotonic() - self._start_ts,
            ))
            return
        self._apply(value, initial=_initial)

    def _apply(self, value: bool, initial: bool) -> None:
        raise NotImplementedError

    def _on_timeout(self) -> None:
        raise NotImplementedError

    def _finish(self, result: MonitorResult) -> None:
        with self._cond:
            if self._result is None:
                self._result = result
                self._cond.notify_all()


class Eventually(_BaseMonitor):
    """Pass on first True; fail on timeout."""

    def _apply(self, value: bool, initial: bool) -> None:
        if value:
            self._finish(MonitorResult(
                verdict="pass",
                reason=f"satisfied at +{time.monotonic() - self._start_ts:.3f}s",
                elapsed=time.monotonic() - self._start_ts,
            ))

    def _on_timeout(self) -> None:
        self._finish(MonitorResult(
            verdict="fail",
            reason=(f"Eventually({self.expr!r}) within {self.timeout}s: "
                    "predicate never became True"),
            elapsed=self.timeout,
        ))


class Always(_BaseMonitor):
    """Pass if predicate remains True until timeout; fail-fast on False."""

    def _apply(self, value: bool, initial: bool) -> None:
        if not value:
            self._finish(MonitorResult(
                verdict="fail",
                reason=(f"Always({self.expr!r}) during {self.timeout}s: "
                        f"violated at +{time.monotonic() - self._start_ts:.3f}s"),
                elapsed=time.monotonic() - self._start_ts,
            ))

    def _on_timeout(self) -> None:
        self._finish(MonitorResult(
            verdict="pass",
            reason=(f"held for full window {self.timeout}s"),
            elapsed=self.timeout,
        ))


class Never(_BaseMonitor):
    """Pass if predicate stays False until timeout; fail-fast on True."""

    def _apply(self, value: bool, initial: bool) -> None:
        if value:
            self._finish(MonitorResult(
                verdict="fail",
                reason=(f"Never({self.expr!r}) during {self.timeout}s: "
                        f"became True at +{time.monotonic() - self._start_ts:.3f}s"),
                elapsed=time.monotonic() - self._start_ts,
            ))

    def _on_timeout(self) -> None:
        self._finish(MonitorResult(
            verdict="pass",
            reason=(f"stayed False for full window {self.timeout}s"),
            elapsed=self.timeout,
        ))
