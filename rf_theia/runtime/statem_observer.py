"""StatemObserver — bridge the live STATEM trace onto the runtime event bus.

A gen_statem FC emits a STATEM trace record on every committed transition,
carrying from_state/to_state + the FSM `data` (OTP `{State, Data}` — the Data
term, decoded from the trace payload). This observer subscribes to that
firehose via :class:`artheia.observer.TraceObserver` (TIPC, the probe-native
path — no gRPC/com dependency), filters records for one statem node, and
republishes each transition as a ``statem_transition`` event on the bus:

    bus.publish("statem_transition",
                node=..., from_state=..., to_state=..., data={...})

so ``Wait For Fsm State`` reacts (no polling) and ``Assert Fsm Data`` reads the
decoded data dict. The TraceObserver addresses + Subscribe service_id are
resolved from the log `.art` — never hardcoded.

Boundary: rf-theia may import ``artheia.observer`` (a stable artheia contract,
alongside the probe — see docs/tasks/BACKLOG/rf-theia-v3-probe-augment.md); it
must NOT import ``artheia.model`` / ``artheia.generators``. Quarantined here.
"""
from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from typing import Optional

from .event_bus import EventBus

logger = logging.getLogger("rf_theia.statem_observer")

# Repo root: runtime[0] rf_theia[1] testing[2] theia[3].
_WS = Path(__file__).resolve().parents[3]
_ARTHEIA = _WS / "artheia"
if str(_ARTHEIA) not in sys.path:
    sys.path.insert(0, str(_ARTHEIA))

# The log .art the observer resolves the collector + Subscribe service_id from,
# and the proto root for decode. Stable artheia outputs, not the toolchain.
_LOG_ART = _WS / "services" / "log" / "system" / "log" / "component.art"
_PROTO = _WS / "platform" / "proto"

# TraceKind ordinal for STATEM (platform_runtime.TraceKind / the .art enum).
TK_STATEM = 5


class StatemObserver:
    """Tails the STATEM trace for one node, republishing transitions on a bus.

    ``node_filter`` is the statem node's kNodeName (e.g. "my_fsm"); only its
    records are forwarded. The kind filter is set to STATEM so the observer
    Subscribes for just the FSM transitions.
    """

    def __init__(self, bus: EventBus, *, node: str,
                 log_art: Optional[Path] = None,
                 proto_root: Optional[Path] = None) -> None:
        self.bus = bus
        self.node = node
        self._log_art = str(log_art or _LOG_ART)
        self._proto = str(proto_root or _PROTO)
        self._obs = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self, timeout: float = 3.0) -> "StatemObserver":
        from artheia.observer import TraceObserver
        # kind=STATEM so the hub only fans STATEM records to us; node_filter is
        # advisory (we re-check below) since the hub filter is best-effort.
        self._obs = TraceObserver.from_log_art(
            self._log_art, proto_root=self._proto,
            kind_filter=TK_STATEM, node_filter=self.node)
        self._obs.start(timeout=timeout)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name=f"statem-obs-{self.node}", daemon=True)
        self._thread.start()
        logger.info("StatemObserver(%s): subscribed to STATEM trace", self.node)
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._obs is not None:
            self._obs.stop()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _loop(self) -> None:
        assert self._obs is not None
        while not self._stop.is_set():
            rec = self._obs.next_record(timeout=0.5)
            if rec is None:
                continue
            # Only this FSM's STATEM transitions (src == node, has a to_state).
            if rec.src != self.node or not rec.to_state:
                continue
            self.bus.publish(
                "statem_transition",
                node=rec.src,
                event=rec.msg_type,          # the triggering event (or <init>)
                from_state=rec.from_state,
                to_state=rec.to_state,
                data=rec.data or {},         # decoded FSM data (OTP Data term)
            )
            logger.info("StatemObserver(%s): %s→%s data=%r",
                        self.node, rec.from_state, rec.to_state, rec.data)
