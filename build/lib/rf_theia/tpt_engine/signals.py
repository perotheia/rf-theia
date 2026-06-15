"""SignalStore: centralized signal registry with timestamped history."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class SignalRecord:
    """A single signal sample."""
    timestamp: float
    name: str
    value: float


class SignalStore:
    """Centralized signal registry with timestamped history.

    Thread-safe: stimuli generators write from their own threads while
    guards read from the evaluation loop.
    """

    def __init__(self) -> None:
        self._signals: dict[str, float] = {}
        self._history: list[SignalRecord] = []
        self._lock = threading.Lock()

    def set(self, name: str, value: float) -> None:
        """Set a signal value and record in history."""
        ts = time.monotonic()
        with self._lock:
            self._signals[name] = value
            self._history.append(SignalRecord(ts, name, value))

    def get(self, name: str, default: float = 0.0) -> float:
        """Get the current value of a signal."""
        with self._lock:
            return self._signals.get(name, default)

    def has(self, name: str) -> bool:
        """Check if a signal has been set."""
        with self._lock:
            return name in self._signals

    def get_history(self, name: str | None = None) -> list[SignalRecord]:
        """Get signal history, optionally filtered by name."""
        with self._lock:
            if name is None:
                return list(self._history)
            return [r for r in self._history if r.name == name]

    def clear(self) -> None:
        """Clear all signals and history."""
        with self._lock:
            self._signals.clear()
            self._history.clear()

    def snapshot(self) -> dict[str, float]:
        """Return a copy of current signal values."""
        with self._lock:
            return dict(self._signals)

    def __repr__(self) -> str:
        with self._lock:
            return f"SignalStore({len(self._signals)} signals, {len(self._history)} samples)"


class EventStore:
    """Event registry for discrete events (not continuous signals).

    Events have a name and a payload dict. The store supports queries
    by event name and optional target field.
    """

    def __init__(self) -> None:
        self._events: list[tuple[float, str, dict]] = []
        self._lock = threading.Lock()

    def emit(self, name: str, payload: dict | None = None) -> None:
        """Record an event."""
        ts = time.monotonic()
        with self._lock:
            self._events.append((ts, name, payload or {}))

    def check(self, name: str, target: str | None = None) -> bool:
        """Check if an event exists matching name and optional target."""
        with self._lock:
            for _, ename, payload in self._events:
                if ename == name:
                    if target is None or payload.get("target") == target:
                        return True
            return False

    def check_since(self, name: str, since: float, target: str | None = None) -> bool:
        """Check if event occurred after a monotonic timestamp."""
        with self._lock:
            for ts, ename, payload in self._events:
                if ts >= since and ename == name:
                    if target is None or payload.get("target") == target:
                        return True
            return False

    def get_events(self, name: str | None = None) -> list[tuple[float, str, dict]]:
        """Get all events, optionally filtered by name."""
        with self._lock:
            if name is None:
                return list(self._events)
            return [(ts, n, p) for ts, n, p in self._events if n == name]

    def clear(self) -> None:
        """Clear all events."""
        with self._lock:
            self._events.clear()

    def __repr__(self) -> str:
        with self._lock:
            return f"EventStore({len(self._events)} events)"
