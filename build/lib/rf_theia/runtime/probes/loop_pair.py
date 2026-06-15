"""Loop-pair probes — Echo + Receiver, for hermetic Pair-4 selftests.

Echo sends messages on a named loop port. Receiver consumes them and
exposes ``expect_message`` for the scenario to assert against.

The Robot scenario starts both, sends from Echo, asserts on Receiver.
Verifies the Pair-4 surface end-to-end without any live SUT.
"""
from __future__ import annotations

import logging

from ..components import Component, PortSpec

logger = logging.getLogger("rf_theia.probe.loop_pair")


class Echo(Component):
    """Sends arbitrary messages on a loop port."""

    ports = [
        PortSpec(name="out", kind="loop", direction="out"),
    ]

    def emit(self, msg: str) -> None:
        self.out.send(msg)
        logger.info("Echo: sent %r", msg)


class Sink(Component):
    """Receives messages on the matching ``out`` loop port (loop ports
    pair by name)."""

    ports = [
        PortSpec(name="out", kind="loop", direction="in"),
    ]

    def expect_message(self, expected: str, within: str = "1s") -> str:
        from .sm_prober import _seconds
        got = self.out.receive(timeout=_seconds(within))
        if got != expected:
            raise AssertionError(
                f"Sink: expected {expected!r}, got {got!r}"
            )
        logger.info("Sink: got expected %r", expected)
        return got
