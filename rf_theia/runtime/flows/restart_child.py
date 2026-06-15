"""RestartChild flow — drive a supervisor child through a crash + restart cycle.

States::

    Idle ──crash event──► CrashRequested ──restart_done──► Restarted (final)
                                  │
                                  └──after 10s──► Failure (final)

The flow doesn't poll. Transitions fire on events the supervisor
adapter publishes onto the bus when its child-state polling sees a
change. (Phase 2: replace polling with a streaming gRPC subscription.)

Usage from a Robot scenario::

    Start State Machine    RestartChild    target=sm_daemon
    Emit Event             crash    on=sm_daemon
    Wait For State         Restarted    within=10s
"""
from __future__ import annotations

import logging

from ..flow_engine import Flow, State, Transition

logger = logging.getLogger("rf_theia.flow.restart_child")


class RestartChild(Flow):
    """Crash-and-restart cycle for a single supervised child.

    Parameters:
      target: the supervisor child name (matches ChildSpec.name)
    """

    initial = "Idle"

    def build(self) -> None:
        target = self.params.get("target")
        if not isinstance(target, str) or not target:
            raise ValueError("RestartChild: 'target' param is required")

        self.states["Idle"] = State(
            name="Idle",
            transitions=[
                Transition(target="CrashRequested",
                           event_name="crash",
                           guard=lambda f, ev: ev.payload.get("on") == target),
            ],
        )

        self.states["CrashRequested"] = State(
            name="CrashRequested",
            entry=self._enter_crash,
            transitions=[
                Transition(target="Restarted",
                           event_name="supervisor_child_running",
                           guard=lambda f, ev: ev.payload.get("name") == target),
                Transition(target="Failure", after_s=10.0),
            ],
        )

        self.states["Restarted"] = State(name="Restarted", final=True)
        self.states["Failure"] = State(name="Failure", final=True)

    # -- entry hooks ---------------------------------------------------

    def _enter_crash(self, flow: Flow) -> None:
        """Ask the supervisor to terminate-then-let-restart cycle the
        child. The supervisor's restart strategy decides whether the
        child gets re-spawned automatically; for a 'permanent' child
        (which sm_daemon is) the supervisor restarts it on its own.

        For test purposes, terminate + observe the natural restart is
        the cleanest path. If the supervisor strategy were 'temporary',
        we'd follow with a StartChild — but that's a different flow.
        """
        target = self.params["target"]
        sup = self.ctx.supervisor
        if sup is None:
            logger.warning(
                "RestartChild: no supervisor adapter bound; CrashRequested "
                "is a no-op (test should mock or skip the live tag)"
            )
            return
        sup.terminate_child(target)
