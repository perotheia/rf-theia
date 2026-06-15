"""Bridge tracer_jsonl.TraceFeed onto the runtime event bus.

The TraceFeed adapter (phase 1) tails a file and parses TRC v1 lines
into TraceRecord objects, but its consumers are direct (filter/expect
calls). Pair 2's temporal monitors instead need every record to land
on the event bus so monitors and ExprEvaluator can react.

This watcher polls TraceFeed.snapshot() at a small interval and
publishes any new records as ``trace_record`` events. We use polling
rather than a push hook because the existing TraceFeed.background-
thread reader is internal; adding a callback to it is a future
refactor if polling becomes too lossy.

Events published:

    bus.publish("trace_record",
                event=...,   node=...,   msg_type=...,
                corr_id=...,  ts_ms=...,  payload_hex=...)
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from ..adapters.tracer_jsonl import TraceFeed
from .event_bus import EventBus

logger = logging.getLogger("rf_theia.trace_watch")


class TraceWatcher:
    """Tails a TraceFeed and republishes onto an EventBus."""

    def __init__(self, bus: EventBus, feed: TraceFeed,
                 interval: float = 0.05) -> None:
        self.bus = bus
        self.feed = feed
        self.interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._published = 0  # how many records we've already forwarded

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="trace-watcher", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            snap = self.feed.snapshot()
            if len(snap) > self._published:
                # Forward the new ones in order.
                for rec in snap[self._published:]:
                    self.bus.publish(
                        "trace_record",
                        event=rec.event,
                        node=rec.node,
                        msg_type=rec.msg_type,
                        corr_id=rec.corr_id,
                        ts_ms=rec.ts_ms,
                        payload_hex=rec.payload_hex,
                    )
                self._published = len(snap)
            if self._stop.wait(self.interval):
                return
