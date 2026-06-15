"""SmStub — stand-in for sm_daemon, used in Pair-4 hermetic selftests.

Pairs with SmProber via matching loop port names:

    SmStub.sm_state     in   — receives the set_state command
    SmStub.broadcast    out  — emits the broadcast SmProber asserts on

For live theia integration the SmStub goes away — SmProber's loop
ports flip to ``kind="tipc"`` and talk to the actual sm_daemon.
"""
from __future__ import annotations

import logging

from ..components import Component, PortSpec

logger = logging.getLogger("rf_theia.probe.sm_stub")


class SmStub(Component):
    """sm_daemon impersonator. Robot wires it next to SmProber and
    calls ``receive_command`` + ``emit_broadcast`` to script the
    exchange."""

    ports = [
        PortSpec(name="sm_state",  kind="loop", direction="in"),
        PortSpec(name="broadcast", kind="loop", direction="out"),
    ]

    def receive_command(self, within: str = "1s") -> str:
        from .sm_prober import _seconds
        msg = self.sm_state.receive(timeout=_seconds(within))
        op = (msg or {}).get("op")
        state = (msg or {}).get("state")
        if op != "set_state":
            raise AssertionError(
                f"SmStub: expected op=set_state, got {msg!r}"
            )
        logger.info("SmStub: received set_state(%s)", state)
        return str(state)

    def emit_broadcast(self, state: str) -> None:
        self.broadcast.send({"type": "SmStateMsg", "state": state})
        logger.info("SmStub: emitted broadcast state=%s", state)
