"""Hermetic Robot library: publish synthetic supervisor events.

Used only by rf-theia self-test scenarios. The library reaches into
the TheiaTestLibrary's runtime context to inject events that a live
supervisor watcher would otherwise produce.

NOT for tests of theia — those should never need this. If you find
yourself wanting it for a real scenario, that's a signal the runtime
needs a new bus publisher of its own.
"""
from __future__ import annotations

from robot.api.deco import keyword, library
from robot.libraries.BuiltIn import BuiltIn


@library(scope="SUITE")
class SupervisorEventPublisher:
    """Wires through to TheiaTestLibrary's bus."""

    ROBOT_LIBRARY_SCOPE = "SUITE"

    def _bus(self):
        # Reach the live TheiaTestLibrary instance by importing it from
        # Robot's library registry — not a global, so Robot picks the
        # one tied to this suite.
        theia_lib = BuiltIn().get_library_instance("rf_theia.TheiaTestLibrary")
        bus = theia_lib._ctx_bus()  # raises if Load Rig wasn't called
        return bus

    @keyword("Publish Child Running")
    def publish_child_running(
        self, name: str, restart_count: int = 0, pid: int = 0,
    ) -> None:
        self._bus().publish(
            "supervisor_child_running",
            name=name,
            prev_state="STARTING",
            restart_count=int(restart_count),
            pid=int(pid),
        )

    @keyword("Publish Child Crashed")
    def publish_child_crashed(
        self, name: str, restart_count: int = 0, pid: int = 0,
    ) -> None:
        self._bus().publish(
            "supervisor_child_crashed",
            name=name,
            prev_state="RUNNING",
            restart_count=int(restart_count),
            pid=int(pid),
        )

    @keyword("Publish Child Starting")
    def publish_child_starting(
        self, name: str, restart_count: int = 0,
    ) -> None:
        self._bus().publish(
            "supervisor_child_starting",
            name=name,
            prev_state="CRASHED",
            restart_count=int(restart_count),
            pid=0,
        )
