"""Bridge live supervisor state into the event bus.

The supervisor adapter (``adapters.supervisor_grpc.SupervisorClient``)
is poll-based — every gRPC tree snapshot returns one frame. Flows are
event-driven. This watcher polls the supervisor in a background thread
and publishes a ``supervisor_child_*`` event whenever any tracked
child's state changes.

Events published:

  - ``supervisor_child_running``    when a child enters RUNNING
  - ``supervisor_child_stopped``    when a child enters STOPPED
  - ``supervisor_child_crashed``    when a child enters CRASHED
  - ``supervisor_child_starting``   when a child enters STARTING
  - ``supervisor_child_stopping``   when a child enters STOPPING

Each event's payload includes ``{name, prev_state, restart_count, pid}``.

Phase-2 (later): replace polling with the supervisor's streaming
subscription RPC the probe-backed SupervisorClient drives via tdb.art.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from .event_bus import EventBus

logger = logging.getLogger("rf_theia.sup_watch")


class SupervisorWatcher:
    """Polls a SupervisorClient at ``interval`` seconds, publishing
    state-change events onto an EventBus."""

    def __init__(
        self,
        bus: EventBus,
        supervisor,           # adapters.supervisor_grpc.SupervisorClient
        interval: float = 0.2,
    ) -> None:
        self.bus = bus
        self.sup = supervisor
        self.interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # name → last observed state string ("RUNNING", "STOPPED", ...)
        self._last_state: dict[str, str] = {}
        # name → last observed restart_count
        self._last_rc: dict[str, int] = {}

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="sup-watcher", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                topo = self.sup.get_topology()
            except Exception as e:
                logger.debug("topology fetch failed: %s", e)
                if self._stop.wait(self.interval):
                    return
                continue
            for child in topo.get("children", []):
                self._observe(child)
            if self._stop.wait(self.interval):
                return

    def _observe(self, child: dict) -> None:
        name = child.get("name")
        if not name:
            return
        state = child.get("state", "UNKNOWN")
        rc = int(child.get("restart_count", 0))
        prev = self._last_state.get(name)
        if state != prev:
            # State change — publish appropriate event.
            ev_type = {
                "RUNNING":  "supervisor_child_running",
                "STOPPED":  "supervisor_child_stopped",
                "CRASHED":  "supervisor_child_crashed",
                "STARTING": "supervisor_child_starting",
                "STOPPING": "supervisor_child_stopping",
            }.get(state)
            if ev_type is not None:
                self.bus.publish(
                    ev_type,
                    name=name,
                    prev_state=prev,
                    restart_count=rc,
                    pid=child.get("pid", 0),
                )
                logger.info("sup: %s %s → %s (rc=%d)",
                            name, prev or "<initial>", state, rc)
            self._last_state[name] = state
        if rc != self._last_rc.get(name, 0):
            self._last_rc[name] = rc
