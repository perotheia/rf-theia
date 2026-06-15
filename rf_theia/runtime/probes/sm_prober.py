"""SmProber — active probe co-located with sm_daemon.

Two ports:

  - ``sm_state``  outbound. Sends control messages to sm_daemon
                  (e.g. ``set_state``). On a live cluster this is a
                  TIPC SEQPACKET client; for Pair 4 v1 it's a loop
                  port that pairs with a test-side responder.

  - ``broadcast`` inbound. Subscribes to the cluster broadcast
                  sm_daemon emits when its state changes. Used to
                  verify that a set_state command actually propagates
                  through the cluster.

The probe doesn't carry verdicts — Robot does. Operation methods
raise :class:`AssertionError` when the SUT misbehaves; Robot fails
the keyword call; the scenario records ``fail``.
"""
from __future__ import annotations

import logging
from typing import Any

from ..components import Component, PortSpec

logger = logging.getLogger("rf_theia.probe.sm_prober")


class SmProber(Component):
    """Co-resident probe for sm_daemon.

    Live transport: TIPC ports — kind switches to ``"tipc"`` once
    AF_TIPC binding is wired into LocalTransport. Today (Pair 4 v1)
    both ports are ``"loop"`` so the probe drives hermetically with
    a test-side responder on the same names.
    """

    ports = [
        PortSpec(
            name="sm_state",
            kind="loop",
            direction="out",
            config={
                # Future TIPC binding: type/instance from
                # services.sm/SmDaemon's tipc declaration in the .art.
                "tipc_type":     0x8001000D,
                "tipc_instance": 0,
            },
        ),
        PortSpec(
            name="broadcast",
            kind="loop",
            direction="in",
            config={
                # sm_daemon emits SmStateMsg broadcasts via netgraph;
                # the receiver here listens on the per-FC sender port.
                "msg_type": "SmStateMsg",
            },
        ),
    ]

    # ----- operations Robot calls -----------------------------------

    def set_state(self, state: str) -> None:
        """Send a set_state command. Returns when the message is
        accepted by the transport (sm_daemon-side ack is not part of
        the contract — use ``expect_broadcast`` to verify propagation).
        """
        self.sm_state.send({"op": "set_state", "state": state})
        logger.info("SmProber: sent set_state(%s)", state)

    def expect_broadcast(self, state: str, within: str = "2s") -> None:
        """Block until the broadcast port delivers a message whose
        ``state`` field matches. Raise if nothing matches in window.
        """
        timeout = _seconds(within)
        msg = self.broadcast.receive(timeout=timeout)
        observed = (msg or {}).get("state")
        if observed != state:
            raise AssertionError(
                f"SmProber: expected broadcast state={state!r}, "
                f"got {observed!r} (full msg: {msg!r})"
            )
        logger.info("SmProber: observed broadcast state=%s", state)


def _seconds(spec: Any) -> float:
    """Local copy — keep the probe import-light."""
    if isinstance(spec, (int, float)):
        return float(spec)
    s = str(spec).strip().lower()
    if s.endswith("ms"):
        return float(s[:-2]) / 1000.0
    if s.endswith("s"):
        return float(s[:-1])
    return float(s)
