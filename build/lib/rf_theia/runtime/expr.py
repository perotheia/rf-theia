"""Expression sublanguage for temporal assertions.

The `Assert Eventually / Always / Never` keywords take a Python-ish
predicate string. It's evaluated by asteval (safe sandbox — no
globals, no imports, no exec) against a binding context that exposes
the live system as Python callables and attribute chains.

Available bindings (built on demand from the runtime context):

  trace.event(name, on=...)        # latest trace record matching
  trace.last(event, node=...)      # same; returns TraceRecord or None
  trace.count(event, node=...)     # total matching records so far
  service.state(name)              # ChildState.state string
  service.restart_count(name)      # int
  service(name).heartbeat_period   # ms — for `Always` sampling later
  flow(name).active                # bool — flow is running
  flow(name).state                 # current state name or None
  supervision.state(domain)        # composite — phase 3

Phase-2 implements: trace.*, service.state, service.restart_count,
flow.active, flow.state. Sampled bindings (heartbeat_period) come
when temporal monitors gain sampling mode.

Boolean operators in expressions are Python's: and / or / not.
Comparisons: == != < > <= >=.

Example::

    trace.count('send', node='sm_daemon') >= 1
    service.state('sm_daemon') == 'RUNNING' and flow('RestartChild').active
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from asteval import Interpreter

from .event_bus import EventBus


# -----------------------------------------------------------------------------
# Binding objects — Python-side proxies that the expression strings call into.
# -----------------------------------------------------------------------------


class _Trace:
    """trace.event(...), trace.last(...), trace.count(...) bindings.

    Backed by a list of trace_record events the bus has seen. Each
    record arrives as a payload dict {event, node, msg_type, corr_id,
    ts_ms, payload_hex}.
    """

    def __init__(self, bus: EventBus) -> None:
        self._records: list[dict] = []
        self._unsub = bus.subscribe(
            "trace_record",
            lambda ev: self._records.append(ev.payload),
        )

    def close(self) -> None:
        self._unsub()

    def event(self, name: str, on: Optional[str] = None) -> bool:
        """Truthy if at least one matching record has arrived."""
        return self.last(name, node=on) is not None

    def last(self, event: str, node: Optional[str] = None) -> Optional[dict]:
        for r in reversed(self._records):
            if r.get("event") != event:
                continue
            if node is not None and r.get("node") != node:
                continue
            return r
        return None

    def count(self, event: str, node: Optional[str] = None) -> int:
        n = 0
        for r in self._records:
            if r.get("event") != event:
                continue
            if node is not None and r.get("node") != node:
                continue
            n += 1
        return n


class _ServiceQuery:
    """Container for `service.state(...)`, `service.restart_count(...)`.

    Backed by the SupervisorWatcher's last-seen state cache, which the
    watcher updates on every poll. We read from the same data the
    watcher uses to publish events — so the binding sees what the bus
    sees, without re-polling.
    """

    def __init__(self, watcher) -> None:  # SupervisorWatcher | None
        self._watcher = watcher

    def state(self, name: str) -> Optional[str]:
        if self._watcher is None:
            return None
        return self._watcher._last_state.get(name)

    def restart_count(self, name: str) -> int:
        if self._watcher is None:
            return 0
        return self._watcher._last_rc.get(name, 0)


class _FlowQuery:
    """flow(name).active, flow(name).state from the flow engine."""

    def __init__(self, engine) -> None:  # FlowEngine | None
        self._engine = engine

    def __call__(self, name: str) -> "_FlowView":
        return _FlowView(self._engine, name)


class _FlowView:
    def __init__(self, engine, name: str) -> None:
        self._engine = engine
        self._name = name

    @property
    def active(self) -> bool:
        if self._engine is None:
            return False
        f = self._engine.get(self._name)
        if f is None:
            return False
        if f.current is None:
            return True
        return not f.states[f.current].final

    @property
    def state(self) -> Optional[str]:
        if self._engine is None:
            return None
        f = self._engine.get(self._name)
        if f is None:
            return None
        return f.current


# -----------------------------------------------------------------------------
# Evaluator
# -----------------------------------------------------------------------------


class ExprEvaluator:
    """Safe predicate evaluator for temporal assertions.

    Construct once per RuntimeContext. The bindings (trace, service,
    flow) capture references to the runtime's live data, so each
    eval() call sees the current state without rebuilding the context.
    """

    def __init__(self, bus: EventBus, supervisor_watcher=None,
                 flow_engine=None) -> None:
        self.trace = _Trace(bus)
        self.service = _ServiceQuery(supervisor_watcher)
        # `flow` is a callable: flow("name").active
        self.flow: Callable[[str], _FlowView] = _FlowQuery(flow_engine)
        self._interp = Interpreter(
            symtable={
                "trace": self.trace,
                "service": self.service,
                "flow": self.flow,
            },
            use_numpy=False,
            minimal=True,
        )

    def eval(self, expr: str) -> bool:
        """Evaluate a predicate. Returns the bool() of the result."""
        result = self._interp.eval(expr, raise_errors=True)
        return bool(result)

    def close(self) -> None:
        self.trace.close()
