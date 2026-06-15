"""Flow engine — hybrid automata for theia tests.

A Flow is a class subclass of :class:`Flow`. It declares states as
methods + a state graph. The engine runs the flow on a background
thread driven by events from the bus — not polling.

Two kinds of transitions:

  - ``on_event(name)`` — fires when an event of ``name`` reaches the
    bus while the flow is in this state.
  - ``after(seconds)`` — fires when the flow has been in this state
    for at least ``seconds`` of wall time.

The flow:

  - publishes ``state_entered`` events on the bus when it changes
    states (so `Wait For State` can react)
  - publishes ``flow_terminated`` when it reaches a final state
  - has access to ``self.ctx`` (the runtime context — Rig, supervisor
    adapter, etc.) for actions in state ``entry`` hooks

This is intentionally a small reactive FSM, not a full SCXML
interpreter. We grow it as later tests demand more.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .event_bus import Event, EventBus

logger = logging.getLogger("rf_theia.flow")


@dataclass
class Transition:
    """One edge out of a state.

    Either ``event_name`` is set (event-driven) or ``after_s`` is set
    (time-driven). The engine picks whichever fires first.
    """
    target: str
    event_name: Optional[str] = None
    after_s: Optional[float] = None
    guard: Optional[Callable[["Flow", Optional[Event]], bool]] = None


@dataclass
class State:
    name: str
    entry: Optional[Callable[["Flow"], None]] = None
    transitions: list[Transition] = field(default_factory=list)
    final: bool = False


class Flow:
    """Base class for flows. Subclasses set ``initial`` and override
    :meth:`build` to populate ``self.states``.

    The engine owns the lifecycle (thread, event subscription,
    state-change publication). Subclasses just declare structure.
    """

    initial: str = ""

    def __init__(self, name: str, ctx: "RuntimeContext", **params: object) -> None:
        self.name = name
        self.ctx = ctx
        self.params = params
        self.states: dict[str, State] = {}
        self.current: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._state_entered_at: float = 0.0
        self.build()

    # -- subclass hook -------------------------------------------------

    def build(self) -> None:
        """Override: populate ``self.states`` and set ``self.initial``."""
        raise NotImplementedError

    # -- lifecycle (engine-owned) -------------------------------------

    def start(self) -> None:
        if self.initial not in self.states:
            raise ValueError(
                f"flow {self.name!r}: initial state {self.initial!r} not "
                f"in states {list(self.states)}"
            )
        self._thread = threading.Thread(
            target=self._run, name=f"flow-{self.name}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        self._enter(self.initial)
        while not self._stop.is_set():
            st = self.states[self.current or self.initial]
            if st.final:
                self.ctx.bus.publish(
                    "flow_terminated",
                    flow=self.name, state=st.name,
                )
                return
            # Wait for the next transition trigger. We subscribe inside
            # the loop so each state can have its own set of triggers.
            chosen = self._await_transition(st)
            if chosen is None:
                # _stop was set; bail.
                return
            self._enter(chosen)

    def _enter(self, state_name: str) -> None:
        self.current = state_name
        self._state_entered_at = time.monotonic()
        st = self.states[state_name]
        if st.entry is not None:
            try:
                st.entry(self)
            except Exception as e:
                logger.exception("flow %s: entry of %s raised: %s",
                                 self.name, state_name, e)
        self.ctx.bus.publish(
            "state_entered",
            flow=self.name, name=state_name,
        )
        logger.info("flow %s → %s", self.name, state_name)

    def _await_transition(self, state: State) -> Optional[str]:
        """Block until one of the state's transitions fires. Returns
        the target state name, or None if the engine was stopped."""
        if not state.transitions:
            # Dead-end non-final state: just sit until stopped.
            self._stop.wait(timeout=None)
            return None

        result: list[str] = []
        cond = threading.Condition()

        def fire(target: str) -> None:
            with cond:
                if not result:
                    result.append(target)
                    cond.notify_all()

        unsubs: list[Callable[[], None]] = []

        # Event-driven transitions.
        for tr in state.transitions:
            if tr.event_name is None:
                continue
            tname = tr.event_name
            ttarget = tr.target
            tguard = tr.guard

            def _on_event(ev: Event, _target=ttarget, _guard=tguard) -> None:
                if _guard is not None and not _guard(self, ev):
                    return
                fire(_target)

            unsubs.append(self.ctx.bus.subscribe(tname, _on_event))

        # Time-driven transitions: each gets its own helper thread that
        # sleeps then fires (if nothing else has fired).
        timer_threads: list[threading.Thread] = []
        for tr in state.transitions:
            if tr.after_s is None:
                continue
            ttarget = tr.target
            delay = tr.after_s

            def _timer(_target=ttarget, _delay=delay) -> None:
                if self._stop.wait(timeout=_delay):
                    return
                fire(_target)

            th = threading.Thread(target=_timer, daemon=True)
            th.start()
            timer_threads.append(th)

        # Block until something fires or engine is stopped.
        with cond:
            while not result and not self._stop.is_set():
                cond.wait(timeout=0.25)

        for u in unsubs:
            u()
        return result[0] if result else None


class RuntimeContext:
    """The "world" each flow sees.

    Phase-1 fields: bus, rig (typed), supervisor adapter. Later pairs
    add trace feed, fault registry, etc. Single object instead of many
    keyword arguments so flows can be written without juggling.
    """

    def __init__(self, bus: EventBus, rig, supervisor=None) -> None:
        self.bus = bus
        self.rig = rig
        self.supervisor = supervisor


class FlowEngine:
    """Tracks instantiated flows by name. Multiplexes the event bus."""

    def __init__(self, ctx: RuntimeContext) -> None:
        self.ctx = ctx
        self._flows: dict[str, Flow] = {}

    def start(self, flow_cls: type[Flow], name: Optional[str] = None,
              **params: object) -> Flow:
        flow_name = name or flow_cls.__name__
        if flow_name in self._flows:
            raise RuntimeError(f"flow {flow_name!r} already running")
        flow = flow_cls(flow_name, self.ctx, **params)
        flow.start()
        self._flows[flow_name] = flow
        return flow

    def stop(self, name: str) -> None:
        flow = self._flows.pop(name, None)
        if flow is not None:
            flow.stop()

    def stop_all(self) -> None:
        for name in list(self._flows):
            self.stop(name)

    def get(self, name: str) -> Optional[Flow]:
        return self._flows.get(name)
