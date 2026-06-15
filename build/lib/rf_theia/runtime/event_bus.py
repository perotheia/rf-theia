"""Minimal in-process event bus.

Reactive substrate for flows and assertion monitors. Producers call
``publish(event_name, **payload)``; consumers register a listener with
``subscribe(...)`` and either consume callbacks or use ``wait_for(...)``
to block until a matching event appears.

Thread-safe. No persistent buffering — listeners registered AFTER an
event fires don't see it (that's the temporal-monitor's job, not the
bus's). The bus is intentionally dumb so consumers can layer their own
buffering semantics on top.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class Event:
    """One emitted event. Type is the routing key; payload is opaque."""
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    ts: float = 0.0


Listener = Callable[[Event], None]


class EventBus:
    """Thread-safe pub/sub. Listeners run on the publisher's thread, so
    they must not block — ``wait_for`` uses a condition variable for
    blocking semantics instead.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._listeners: list[tuple[Optional[str], Listener]] = []
        self._history: list[Event] = []   # bounded for wait_for back-search

    def publish(self, type_: str, **payload: Any) -> None:
        """Fire an event. All listeners with a matching filter run on
        the publishing thread before the call returns."""
        ev = Event(type=type_, payload=dict(payload), ts=time.monotonic())
        with self._cond:
            self._history.append(ev)
            if len(self._history) > 1024:
                self._history = self._history[-1024:]
            for filt, fn in list(self._listeners):
                if filt is None or filt == type_:
                    try:
                        fn(ev)
                    except Exception:
                        # Swallow listener errors — we don't want a
                        # bad subscriber to break the publisher chain.
                        pass
            self._cond.notify_all()

    def subscribe(
        self, event_type: Optional[str], fn: Listener
    ) -> Callable[[], None]:
        """Register a callback. Pass ``event_type=None`` to receive all
        events. Returns an unsubscribe function."""
        with self._lock:
            entry = (event_type, fn)
            self._listeners.append(entry)
        def _unsub() -> None:
            with self._lock:
                if entry in self._listeners:
                    self._listeners.remove(entry)
        return _unsub

    def wait_for(
        self,
        type_: str,
        match: Optional[Callable[[Event], bool]] = None,
        timeout: float = 5.0,
        since: Optional[float] = None,
    ) -> Optional[Event]:
        """Block until an event of ``type_`` arrives, or until
        ``timeout`` seconds elapse. Returns the event, or None on timeout.

        ``since`` (a monotonic timestamp) limits the back-search of
        already-emitted events; useful for "did X happen after this
        moment" assertions. Defaults to ``-inf`` — i.e. the entire
        history. Pass an explicit ``time.monotonic()`` snapshot to
        scope the search to "after this moment".

        ``match`` lets the caller filter on payload contents:

            bus.wait_for("state_entered", lambda e: e.payload["name"] == "Restarted")
        """
        deadline = time.monotonic() + timeout
        anchor = since if since is not None else float("-inf")
        with self._cond:
            while True:
                # Scan history first — covers the race where the event
                # fired between flow start and our wait_for call.
                for ev in self._history:
                    if ev.ts < anchor:
                        continue
                    if ev.type != type_:
                        continue
                    if match is not None and not match(ev):
                        continue
                    return ev
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(timeout=remaining)
