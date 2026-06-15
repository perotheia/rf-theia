"""Observe child-state transitions on the bus, reconstruct restart order.

The supervisor_watcher (Pair 1) already publishes:

    supervisor_child_starting
    supervisor_child_running
    supervisor_child_stopping
    supervisor_child_stopped
    supervisor_child_crashed

This observer subscribes to all of them and stores a per-child
timeline. Pair-3 assertions query it:

    - "what's the first restart sequence after time T?"
    - "did <child> ever reach RUNNING within window W?"
    - "did <child> restart more than N times in window W?"

The observer is event-driven, history-buffered. ``observe_from(t)``
pins a reference time so an assertion can scope its query to "events
after I asked the supervisor to crash this child".
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from .event_bus import Event, EventBus

logger = logging.getLogger("rf_theia.restart_obs")


# State names map: which event corresponds to which observed state.
# (Kept consistent with adapters/supervisor_grpc._state_name)
_EVENT_TO_STATE = {
    "supervisor_child_starting":  "STARTING",
    "supervisor_child_running":   "RUNNING",
    "supervisor_child_stopping":  "STOPPING",
    "supervisor_child_stopped":   "STOPPED",
    "supervisor_child_crashed":   "CRASHED",
}


@dataclass
class StateChange:
    """One observed child-state transition."""
    name: str            # child name
    state: str           # new state ("RUNNING", "CRASHED", ...)
    prev_state: Optional[str]
    restart_count: int
    pid: int
    ts: float            # monotonic timestamp


@dataclass
class _ChildHistory:
    name: str
    changes: list[StateChange] = field(default_factory=list)


class RestartObserver:
    """Subscribes to supervisor_child_* events and records state changes.

    Thread-safe. ``snapshot()`` returns a list copy for read-only queries.
    """

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._lock = threading.Lock()
        self._by_child: dict[str, _ChildHistory] = {}
        self._unsubs: list = []
        for ev_type in _EVENT_TO_STATE:
            self._unsubs.append(bus.subscribe(ev_type, self._on_event))

    def close(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs = []

    def _on_event(self, ev: Event) -> None:
        state = _EVENT_TO_STATE.get(ev.type)
        if state is None:
            return
        name = ev.payload.get("name")
        if not name:
            return
        change = StateChange(
            name=name,
            state=state,
            prev_state=ev.payload.get("prev_state"),
            restart_count=int(ev.payload.get("restart_count", 0)),
            pid=int(ev.payload.get("pid", 0)),
            ts=ev.ts,
        )
        with self._lock:
            hist = self._by_child.setdefault(name, _ChildHistory(name=name))
            hist.changes.append(change)

    # ----- queries ----------------------------------------------------

    def snapshot(self) -> dict[str, list[StateChange]]:
        with self._lock:
            return {n: list(h.changes) for n, h in self._by_child.items()}

    def changes_for(self, name: str, since: float = 0.0) -> list[StateChange]:
        with self._lock:
            hist = self._by_child.get(name)
            if hist is None:
                return []
            return [c for c in hist.changes if c.ts >= since]

    def restart_count(self, name: str) -> int:
        """Latest restart_count seen on the bus. Zero if never observed."""
        with self._lock:
            hist = self._by_child.get(name)
            if not hist or not hist.changes:
                return 0
            return hist.changes[-1].restart_count

    def reached_running(self, name: str, since: float = 0.0) -> bool:
        return any(c.state == "RUNNING"
                   for c in self.changes_for(name, since=since))

    def restart_order(self, since: float = 0.0) -> list[str]:
        """Return children in the order they first reached STARTING (or
        if STARTING wasn't observed, RUNNING) after ``since``.

        Used by `Assert Restart Order` to compare against the expected
        sequence from ``expected_restart_order(tree, crashed_child)``.
        """
        seen: list[tuple[float, str]] = []
        with self._lock:
            for name, hist in self._by_child.items():
                first: Optional[StateChange] = None
                for c in hist.changes:
                    if c.ts < since:
                        continue
                    if c.state in ("STARTING", "RUNNING"):
                        first = c
                        break
                if first is not None:
                    seen.append((first.ts, name))
        seen.sort(key=lambda x: x[0])
        return [name for (_, name) in seen]
