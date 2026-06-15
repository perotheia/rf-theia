"""Guard factories: time, event, signal, and combined guards."""

from __future__ import annotations

import operator
import time
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .signals import EventStore, SignalStore

Guard = Callable[[], bool]

# Safe operator dispatch — no eval()
_OPS: dict[str, Callable[[float, float], bool]] = {
    ">": operator.gt,
    "<": operator.lt,
    ">=": operator.ge,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
}


def time_guard(seconds: float) -> Guard:
    """Fire after `seconds` have elapsed since first evaluation.

    Uses time.monotonic() for deterministic timing. Start time is
    captured lazily on first call.
    """
    start: list[float | None] = [None]  # mutable container for closure

    def _guard() -> bool:
        if start[0] is None:
            start[0] = time.monotonic()
        return (time.monotonic() - start[0]) >= seconds

    _guard.__name__ = f"time_guard({seconds}s)"
    return _guard


def event_guard(
    event_store: EventStore,
    event_name: str,
    target: str | None = None,
) -> Guard:
    """Fire when a matching event exists in the EventStore."""
    def _guard() -> bool:
        return event_store.check(event_name, target)

    _guard.__name__ = f"event_guard({event_name}, {target})"
    return _guard


def signal_guard(
    signal_store: SignalStore,
    signal_name: str,
    op: str,
    threshold: float,
) -> Guard:
    """Fire when a signal value satisfies a comparison.

    Uses safe operator dispatch (not eval).
    Supported operators: '>', '<', '>=', '<=', '==', '!='.
    """
    if op not in _OPS:
        raise ValueError(f"Unsupported operator: {op!r}. Use one of {list(_OPS.keys())}")
    cmp = _OPS[op]

    def _guard() -> bool:
        val = signal_store.get(signal_name)
        return cmp(val, threshold)

    _guard.__name__ = f"signal_guard({signal_name} {op} {threshold})"
    return _guard


def combined_guard(*guards: Guard) -> Guard:
    """Fire when ALL guards are True (logical AND)."""
    def _guard() -> bool:
        return all(g() for g in guards)

    names = ", ".join(getattr(g, "__name__", "?") for g in guards)
    _guard.__name__ = f"combined_guard({names})"
    return _guard


def any_guard(*guards: Guard) -> Guard:
    """Fire when ANY guard is True (logical OR)."""
    def _guard() -> bool:
        return any(g() for g in guards)

    names = ", ".join(getattr(g, "__name__", "?") for g in guards)
    _guard.__name__ = f"any_guard({names})"
    return _guard
