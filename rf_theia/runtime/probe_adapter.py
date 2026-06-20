"""ProbeAdapter — inject typed events at a statem FC's gate via artheia.probe.

A gen_statem node takes no wire messages; its gate (a receiver node) does, and
post_event()s each into the FSM in-process. This adapter binds a tester
identity from a `.art` (a sender node, e.g. an app's MyFsmTester) and casts
events at the gate over ONE persistent, ORDERED connection — so a sequence of
events reaches the FSM in order (separate per-event sockets race, dropping
events; see project-probe-connect-stale-bindings).

Boundary: rf-theia may import ``artheia.gen_server.probe`` (a stable artheia
contract — see docs/tasks/BACKLOG/rf-theia-v3-probe-augment.md §boundary); it
must NOT import ``artheia.model`` / ``artheia.generators``. Quarantined here.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger("rf_theia.probe_adapter")

# Repo root: runtime[0] rf_theia[1] testing[2] theia[3].
_WS = Path(__file__).resolve().parents[3]
_ARTHEIA = _WS / "artheia"
if str(_ARTHEIA) not in sys.path:
    sys.path.insert(0, str(_ARTHEIA))

_PROTO = _WS / "platform" / "proto"


class ProbeAdapter:
    """Casts events at a statem gate via a probe bound to a tester node.

    ``art`` is the `.art` carrying the tester (sender) node + the gate, loaded
    through its canonical ``system/`` path so cross-package imports resolve.
    ``tester`` is the sender node name (the cast SOURCE identity); ``gate`` is
    the receiver node the events are cast at.
    """

    def __init__(self, *, art: Path, tester: str, gate: str,
                 proto_root: Optional[Path] = None) -> None:
        self._art = str(art)
        self._proto = str(proto_root or _PROTO)
        self.tester = tester
        self.gate = gate
        self._ctx = None
        self._probe = None

    def start(self) -> "ProbeAdapter":
        from artheia.gen_server.probe import ArtheiaContext
        self._ctx = ArtheiaContext(self._art, proto_root=self._proto)
        # Bind the tester node as the cast SOURCE; one probe = one ordered
        # connection to the gate.
        self._probe = self._ctx.probe(self.tester).start()
        g = self._ctx.ref(self.gate)
        logger.info("ProbeAdapter: %s → gate %s @ tipc 0x%08x",
                    self.tester, self.gate, g.tipc_type)
        return self

    def emit(self, event: str, **fields: object) -> None:
        """Cast ``event`` (a MyFsmIn data element name) at the gate."""
        if self._probe is None:
            raise RuntimeError("ProbeAdapter.emit before start()")
        self._probe.cast(self.gate, event, **fields)
        logger.info("ProbeAdapter: cast %s → %s", event, self.gate)

    def stop(self) -> None:
        if self._probe is not None:
            self._probe.stop()
            self._probe = None
