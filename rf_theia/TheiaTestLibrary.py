"""TheiaTestLibrary — Robot keyword surface for rf-theia.

The library is **routing only**. All semantics live in
:mod:`rf_theia.runtime`. Each keyword is a small method that forwards
to a runtime call; this keeps the library readable as a catalogue and
lets the runtime evolve without touching keyword definitions.

Phase-1 keywords (``T Sup *``, ``T Sig *``) remain importable for
backward compatibility but are no longer auto-exposed as Robot
keywords — they're plain methods now, used internally by adapters
that the role-named keywords drive.

Keyword catalogue (Pair 1 — hybrid automata):

  - ``Load Rig``           — bind a typed Rig context to the suite
  - ``Tear Down Rig``      — close adapters
  - ``Start State Machine``— instantiate a flow from the registry
  - ``Stop State Machine`` — stop a running flow
  - ``Emit Event``         — publish onto the runtime event bus
  - ``Wait For State``     — reactive block on FSM state entry
  - ``Verdict``            — TTCN-3-style outcome
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from robot.api.deco import keyword, library

# Vendored TPT engine (Phase 1 keywords still kept — useful for direct
# TPT scenarios that don't need the full flow engine).
from .tpt_engine import (
    EventStore,
    Partition,
    RampStimulus,
    SignalStore,
    TimeEngine,
    event_guard,
    signal_guard,
    time_guard,
)

# Pair-1 runtime — typed Rig, event bus, flow engine, supervisor watcher.
from .runtime import load_rig
from .runtime.event_bus import EventBus
from .runtime.expr import ExprEvaluator
from .runtime.flow_engine import FlowEngine, RuntimeContext
from .runtime.flows import RestartChild
from .runtime.components import (
    Component,
    ComponentRuntime,
    LocalTransport,
    SSHTransport,
)
from .runtime.monitors import Always, Eventually, Never
from .runtime.probes import Echo, Sink, SmProber, SmStub
from .runtime.restart_observer import RestartObserver
from .runtime.supervision import (
    SupervisorNode,
    expected_restart_order,
    load_supervision,
)
from .runtime.supervisor_watcher import SupervisorWatcher
from .runtime.topology import Topology, load_topology
from .runtime.topology_check import Issue, validate_against_rig
from .runtime.trace_watcher import TraceWatcher
from .adapters.hybrid_automata import HybridAutomata

logger = logging.getLogger("rf_theia")


# Registry of available flows. New flows added here become callable
# via `Start State Machine <Name>` without touching keyword wiring.
FLOW_REGISTRY: dict[str, type] = {
    "RestartChild": RestartChild,
}

# Registry of available components. Same pattern as FLOW_REGISTRY —
# adding a new probe means adding one line here, no keyword changes.
COMPONENT_REGISTRY: dict[str, type] = {
    "Echo":     Echo,
    "Sink":     Sink,
    "SmProber": SmProber,
    "SmStub":   SmStub,
}


@library(scope="SUITE")
class TheiaTestLibrary:
    """Single-library entry point for rf-theia scenarios.

    Robot scenarios import this with::

        Library    rf_theia.TheiaTestLibrary
    """

    ROBOT_LIBRARY_SCOPE = "SUITE"

    def __init__(self) -> None:
        # TPT engine — vendored, domain-agnostic. Kept for direct
        # TPT scenarios.
        self.signals = SignalStore()
        self.events = EventStore()
        self.engine = TimeEngine()

        # Pair-1 runtime — instantiated lazily by Load Rig.
        self._ctx: Optional[RuntimeContext] = None
        self._flow_engine: Optional[FlowEngine] = None
        self._sup_watcher: Optional[SupervisorWatcher] = None
        self._verdict: str = "none"
        # Pair-2 — trace feed + watcher + expression evaluator. Trace
        # feed is opened by `Open Trace` (lazy), evaluator constructed
        # on first Assert call.
        self._trace_feed: Any = None
        self._trace_watcher: Optional[TraceWatcher] = None
        self._evaluator: Optional[ExprEvaluator] = None
        # Pair-3 — supervision tree + restart observer. Tree loaded by
        # `Load Supervision`; observer subscribes to bus on Load Rig
        # (so it's ready to record before any crash happens).
        self._supervision_tree: Optional[SupervisorNode] = None
        self._restart_observer: Optional[RestartObserver] = None
        # Crash anchor — set by `Crash Child`, read by
        # `Assert Restart Order` to scope its query window.
        self._crash_anchor: float = 0.0
        self._last_crashed: Optional[str] = None
        # Pair-4 — distributed components. Runtime is created by
        # `Load Rig` so `Run Component` can register instances.
        self._components: Optional[ComponentRuntime] = None
        self._local_transport: Optional[LocalTransport] = None
        # Pair-5 — typed topology graph + cross-check issues from the
        # last `Assert Netgraph Matches Rig` call (for inspection).
        self._topology: Optional[Topology] = None
        self._last_issues: list[Issue] = []
        # Hybrid-automata (gen_statem) testing — drive+observe one statem FC
        # standalone (probe injects events, observer reads the STATEM trace).
        # Created by `Start Statem Stack`; parameterized by the FC's nodes/.art
        # so the SAME keywords drive my_fsm, sm, and any statem FC.
        self._statem: Optional[HybridAutomata] = None

    # ──────────────────────────────────────────────────────────────────
    # Pair-1 lifecycle: Load Rig / Tear Down Rig.
    # ──────────────────────────────────────────────────────────────────

    @keyword("Load Rig")
    def load_rig_kw(
        self,
        path: str,
        supervisor: str = "localhost:5051",
    ) -> None:
        """Bind a typed Rig context from artheia's rig-deps JSON.

        Args:
          path:       Path to the rig.json (artheia rig-deps --out).
          supervisor: gRPC endpoint of services/com SupervisorView.

        Opens the supervisor channel lazily — connection failures
        surface on first use, not here, so a hermetic test can load
        a rig fixture without a live supervisor.
        """
        rig = load_rig(path)
        bus = EventBus()
        ctx = RuntimeContext(bus=bus, rig=rig, supervisor=None)

        # Lazy supervisor connect: try, but don't fail the load if no
        # supervisor is reachable. Flows that need it will fail their
        # entry action with a clear error.
        try:
            from .adapters.supervisor_grpc import SupervisorClient
            sup = SupervisorClient(supervisor)
            sup.connect(timeout=2.0)
            ctx.supervisor = sup
            watcher = SupervisorWatcher(bus, sup)
            watcher.start()
            self._sup_watcher = watcher
            logger.info("Load Rig: supervisor at %s connected", supervisor)
        except (AssertionError, Exception) as e:
            logger.warning(
                "Load Rig: supervisor at %s unreachable (%s) — "
                "flows requiring supervisor will fail at entry",
                supervisor, e,
            )

        self._ctx = ctx
        self._flow_engine = FlowEngine(ctx)
        # Restart observer subscribes immediately so it captures every
        # supervisor_child_* event from Pair-1's watcher (when present)
        # AND from any test-side stub publishers.
        self._restart_observer = RestartObserver(bus)
        # Component runtime — LocalTransport is the default. Robot
        # scenarios pass `on=<machine>` to switch to SSHTransport when
        # that's implemented; for now both local and same-host targets
        # use LocalTransport.
        self._local_transport = LocalTransport()
        self._components = ComponentRuntime(bus, self._local_transport)
        self._verdict = "none"
        logger.info("Load Rig: %s (vehicle=%s, machines=%d, components=%d)",
                    path, rig.vehicle.name, len(rig.machines),
                    len(rig.all_components()))

    @keyword("Tear Down Rig")
    def tear_down_rig(self) -> None:
        """Stop flows, close adapters, drop the runtime context."""
        if self._components is not None:
            self._components.stop_all()
            self._components = None
            self._local_transport = None
        if self._flow_engine is not None:
            self._flow_engine.stop_all()
            self._flow_engine = None
        if self._sup_watcher is not None:
            self._sup_watcher.stop()
            self._sup_watcher = None
        if self._trace_watcher is not None:
            self._trace_watcher.stop()
            self._trace_watcher = None
        if self._trace_feed is not None:
            try:
                self._trace_feed.close()
            except Exception:
                pass
            self._trace_feed = None
        if self._evaluator is not None:
            self._evaluator.close()
            self._evaluator = None
        if self._restart_observer is not None:
            self._restart_observer.close()
            self._restart_observer = None
        self._supervision_tree = None
        self._crash_anchor = 0.0
        self._last_crashed = None
        self._topology = None
        self._last_issues = []
        if self._ctx is not None and self._ctx.supervisor is not None:
            try:
                self._ctx.supervisor.close()
            except Exception:
                pass
        self._ctx = None

    # ──────────────────────────────────────────────────────────────────
    # Pair-1 keywords: hybrid automata surface.
    # ──────────────────────────────────────────────────────────────────

    @keyword("Start State Machine")
    def start_state_machine(self, flow_name: str, **params: Any) -> None:
        """Instantiate and run a flow from the registry.

        Example::

            Start State Machine    RestartChild    target=sm_daemon

        The flow runs on a background thread. Use ``Wait For State``
        to react to its state transitions and ``Stop State Machine``
        to terminate it explicitly. Final states (e.g. ``Restarted``,
        ``Failure``) stop the flow naturally.
        """
        self._require_runtime()
        flow_cls = FLOW_REGISTRY.get(flow_name)
        if flow_cls is None:
            raise KeyError(
                f"Start State Machine: {flow_name!r} not in registry "
                f"(have: {sorted(FLOW_REGISTRY)})"
            )
        assert self._flow_engine is not None
        self._flow_engine.start(flow_cls, **params)
        logger.info("Start State Machine: %s started with params=%r",
                    flow_name, params)

    @keyword("Stop State Machine")
    def stop_state_machine(self, flow_name: str) -> None:
        self._require_runtime()
        assert self._flow_engine is not None
        self._flow_engine.stop(flow_name)

    @keyword("Emit Event")
    def emit_event(self, event_name: str, **payload: Any) -> None:
        """Publish an event onto the runtime bus.

        Flows whose current state has an ``event_name``-keyed
        transition will react. Payload keys flow through verbatim; flow
        guards can match on them (e.g. ``ev.payload.get('on') == target``).
        """
        self._require_runtime()
        assert self._ctx is not None
        self._ctx.bus.publish(event_name, **payload)

    @keyword("Wait For State")
    def wait_for_state(
        self,
        state_name: str,
        within: str = "5s",
        flow: str = "",
    ) -> None:
        """Block until a flow enters ``state_name``, or fail.

        With ``flow=""``, matches the first flow whose state machine
        enters ``state_name``. Set ``flow=<name>`` to scope the match.
        """
        self._require_runtime()
        assert self._ctx is not None
        target_flow = flow or None
        timeout = _seconds(within)

        def _match(ev) -> bool:
            if ev.payload.get("name") != state_name:
                return False
            if target_flow is not None and ev.payload.get("flow") != target_flow:
                return False
            return True

        # Anchor at the moment we begin waiting — earlier state entries
        # for previous tests shouldn't satisfy a fresh `Wait For State`.
        ev = self._ctx.bus.wait_for(
            "state_entered", match=_match, timeout=timeout,
            since=time.monotonic(),
        )
        if ev is None:
            raise AssertionError(
                f"Wait For State: did not see {state_name!r}"
                + (f" in flow {target_flow!r}" if target_flow else "")
                + f" within {within}"
            )
        logger.info("Wait For State: %s reached (flow=%s)",
                    state_name, ev.payload.get("flow"))

    @keyword("Verdict")
    def verdict(self, outcome: str) -> None:
        """Record the test verdict. TTCN-3 ordering applies:
        ``error > fail > inconclusive > pass > none``.

        Multiple calls within a suite merge: the worst verdict wins.
        Robot's own pass/fail still applies — Verdict is the framework's
        own outcome record, useful for cross-test aggregation in MCP /
        pandas analysis later.
        """
        order = {"none": 0, "pass": 1, "inconclusive": 2,
                 "fail": 3, "error": 4}
        new = outcome.strip().lower()
        if new not in order:
            raise ValueError(
                f"Verdict: {outcome!r} not in {sorted(order)}"
            )
        if order[new] > order.get(self._verdict, 0):
            self._verdict = new
        logger.info("Verdict: %s (suite verdict now %s)", new, self._verdict)

    # ──────────────────────────────────────────────────────────────────
    # Hybrid-automata (gen_statem) keywords. Drive + observe ANY statem FC
    # standalone: the probe injects events at the FC's gate, the observer
    # reads the STATEM trace. Parameterized by the FC's node names + .art so
    # the SAME keywords drive my_fsm, sm, and any future statem FC. Backed
    # by adapters/hybrid_automata.HybridAutomata.
    # ──────────────────────────────────────────────────────────────────

    @keyword("Start Statem Stack")
    def start_statem_stack(self, node: str, gate: str, tester: str,
                           art: str, ready: str = "",
                           within: str = "12s") -> None:
        """Stage + run the central supervisor (THEIA_TRACE=1), enable STATEM
        trace on ``node``, and attach the event-injecting probe + the trace
        observer for one gen_statem FC.

        Example::

            Start Statem Stack    node=my_fsm    gate=MyFsmGate
            ...    tester=MyFsmTester    art=system/app/package.art

        ``art`` is the FC's .art (canonical system/ path); ``tester`` a sender
        node in it the probe binds as the cast source. Readiness is checked by
        polling the gate's TIPC binding (uniform across FCs); ``ready`` is an
        optional supervisor-log substring for extra diagnostics."""
        self._statem = HybridAutomata(
            node=node, gate=gate, tester=tester, art=art, ready=ready)
        self._statem.start(timeout=_seconds(within))

    @keyword("Stop Statem Stack")
    def stop_statem_stack(self) -> None:
        if self._statem is not None:
            self._statem.stop()
            self._statem = None

    @keyword("Emit Statem Event")
    def emit_statem_event(self, event: str, **fields: Any) -> None:
        """Cast a gate-interface event (e.g. MyStart / StartupComplete) at
        the FC's gate, in order, over the probe's one connection."""
        self._require_statem()
        assert self._statem is not None
        self._statem.emit(event, **fields)

    @keyword("Wait For Statem State")
    def wait_for_statem_state(self, state: str, within: str = "2s") -> dict:
        """Block until the FSM enters ``state`` (reactive, via the STATEM
        trace). Returns the transition payload (incl. decoded ``data``)."""
        self._require_statem()
        assert self._statem is not None
        return self._statem.wait_for_state(state, within=within)

    @keyword("Assert Statem Data")
    def assert_statem_data(self, **expected: Any) -> None:
        """Assert fields of the LAST observed transition's FSM data (OTP Data
        term). e.g. ``Assert Statem Data    reason=PROCESSING``."""
        self._require_statem()
        assert self._statem is not None
        self._statem.assert_data(**expected)

    def _require_statem(self) -> None:
        if self._statem is None:
            raise AssertionError(
                "no statem stack — call `Start Statem Stack` first")

    # ──────────────────────────────────────────────────────────────────
    # Pair-2 keywords: temporal logic + trace lifecycle.
    # ──────────────────────────────────────────────────────────────────

    @keyword("Open Trace")
    def open_trace(self, source: str) -> None:
        """Open a trace feed and start forwarding records to the bus.

        ``source`` is a file path (or ``file://...``). The watcher tails
        the file and republishes each TRC v1 record as a
        ``trace_record`` event for monitors + ExprEvaluator to consume.

        Once a feed is open, ``trace.event(...)`` / ``trace.count(...)``
        expressions in Assert keywords see live data.
        """
        self._require_runtime()
        from .adapters.tracer_jsonl import TraceFeed
        assert self._ctx is not None
        feed = TraceFeed(source)
        feed.open()
        watcher = TraceWatcher(self._ctx.bus, feed)
        watcher.start()
        self._trace_feed = feed
        self._trace_watcher = watcher
        logger.info("Open Trace: tailing %s", source)

    @keyword("Close Trace")
    def close_trace(self) -> None:
        if self._trace_watcher is not None:
            self._trace_watcher.stop()
            self._trace_watcher = None
        if self._trace_feed is not None:
            self._trace_feed.close()
            self._trace_feed = None

    @keyword("Assert Eventually")
    def assert_eventually(self, expr: str, within: str = "5s") -> None:
        """Block until ``expr`` evaluates True at least once, or fail.

        The expression sees the current world via bindings:
          - ``trace.event(name, on=node)``,  ``trace.count(name)``
          - ``service.state(name)``,  ``service.restart_count(name)``
          - ``flow(name).active``, ``flow(name).state``

        Example::

            Assert Eventually    trace.event('send', on='sm_daemon')    within=5s
        """
        result = Eventually(
            self._ctx_bus(), self._eval(),
            expr=expr, timeout=_seconds(within),
        ).run()
        if result.verdict != "pass":
            raise AssertionError(result.reason)
        logger.info("Assert Eventually: %s [%s]", expr, result.reason)

    @keyword("Assert Always")
    def assert_always(self, expr: str, during: str = "1s") -> None:
        """Pass if ``expr`` stays True throughout the window, else fail
        on the first False."""
        result = Always(
            self._ctx_bus(), self._eval(),
            expr=expr, timeout=_seconds(during),
        ).run()
        if result.verdict != "pass":
            raise AssertionError(result.reason)
        logger.info("Assert Always: %s [%s]", expr, result.reason)

    @keyword("Assert Never")
    def assert_never(self, expr: str, during: str = "1s") -> None:
        """Pass if ``expr`` stays False throughout the window, else fail
        on the first True."""
        result = Never(
            self._ctx_bus(), self._eval(),
            expr=expr, timeout=_seconds(during),
        ).run()
        if result.verdict != "pass":
            raise AssertionError(result.reason)
        logger.info("Assert Never: %s [%s]", expr, result.reason)

    # ──────────────────────────────────────────────────────────────────
    # Pair-3 keywords: supervision graph.
    # ──────────────────────────────────────────────────────────────────

    @keyword("Load Supervision")
    def load_supervision_kw(self, path: str) -> None:
        """Load artheia's emitted ``executor.yaml`` for the current
        machine. Strategy + child order become queryable.

        Typical path:
          ``deploy/.staging/<machine>/ipk/executor.yaml``

        Must follow `Load Rig` — uses the same suite-scope runtime."""
        self._require_runtime()
        self._supervision_tree = load_supervision(path)
        logger.info("Load Supervision: %s (root=%s)",
                    path, self._supervision_tree.name)

    @keyword("Get Supervisor Tree")
    def get_supervisor_tree(self) -> dict:
        """Return the supervisor's current TreeSnapshot as a flat
        dict keyed by child/node name. Each value is itself a dict
        with at least ``parent`` (parent name), ``state`` (string),
        ``restart_count`` (int), ``uptime_ms`` (int), and — for
        supervisor rows — ``strategy`` (one_for_one / rest_for_one /
        one_for_all).

        Synthetic node_sup + node rows (from the per-node supervision
        feature, #364) appear here once #364 lands; scenarios written
        against the eventual shape can assert on them today and fail
        in spec-shape until then.

        Reads via the existing supervisor gRPC adapter (Pair 1's
        supervisor_watcher topology snapshot).
        """
        self._require_runtime()
        assert self._ctx is not None
        if self._ctx.supervisor is None:
            raise AssertionError(
                "Get Supervisor Tree: no live supervisor reachable "
                "(Load Rig couldn't connect on localhost:5051)"
            )
        topo = self._ctx.supervisor.get_topology()
        # topo from supervisor_grpc adapter is shaped:
        #   {"machine": "...", "supervisors": [...], "children": [...]}
        # Flatten supervisors + children into one name-keyed dict so
        # scenarios can do `${row}=  Get From Dictionary  ${tree}  sm`.
        flat: dict[str, dict] = {}
        for s in topo.get("supervisors", []):
            flat[s["name"]] = {**s, "kind": "supervisor"}
        for c in topo.get("children", []):
            flat[c["name"]] = {**c, "kind": "worker"}
        return flat

    @keyword("Crash Child")
    def crash_child(self, name: str) -> None:
        """Ask the supervisor to terminate ``name``, anchoring the
        restart-order observation window.

        Subsequent `Assert Restart Order` calls scope their query to
        events that arrive after this anchor — so prior child churn
        doesn't contaminate the assertion.

        Without a live supervisor adapter, this is a no-op anchor (just
        records the time + child name). Test-side publishers can stand
        in for the supervisor — same observer semantics.
        """
        self._require_runtime()
        import time as _time
        self._crash_anchor = _time.monotonic()
        self._last_crashed = name
        if self._ctx is not None and self._ctx.supervisor is not None:
            try:
                self._ctx.supervisor.terminate_child(name)
                logger.info("Crash Child: terminated %s via supervisor", name)
            except Exception as e:
                logger.warning(
                    "Crash Child: terminate %s failed (%s) — proceeding "
                    "with anchor only", name, e
                )
        else:
            logger.warning(
                "Crash Child: no supervisor — anchor set, "
                "stub-mode (test must publish supervisor_child_* itself)"
            )

    @keyword("Assert Restart Order")
    def assert_restart_order(
        self,
        *expected: str,
        crashed: str = "",
        within: str = "10s",
    ) -> None:
        """Assert children restarted in the expected order.

        Two calling forms:

          Assert Restart Order    crypto    sm    network_sup
              # explicit expected sequence; compared against observer.

          Assert Restart Order
              # derive expected from the supervision tree + the last
              # `Crash Child` target. The tree's parent_of(crashed)
              # determines the strategy; rest_for_one yields children
              # from the crashed one onward, etc.

        Waits up to ``within`` for enough restart events to land before
        comparing. The match is a PREFIX check: the observed sequence
        is allowed to contain trailing children (e.g. unrelated
        peers) the assertion doesn't name.
        """
        self._require_runtime()
        assert self._restart_observer is not None
        timeout = _seconds(within)

        # Derive expected sequence from the tree if not given.
        exp: list[str]
        if expected:
            exp = list(expected)
        else:
            if self._supervision_tree is None:
                raise RuntimeError(
                    "Assert Restart Order: no expected sequence and no "
                    "supervision tree loaded. Call `Load Supervision` "
                    "or pass the sequence explicitly."
                )
            target = crashed or self._last_crashed
            if not target:
                raise RuntimeError(
                    "Assert Restart Order: no `crashed=` arg and no "
                    "previous `Crash Child` — can't derive expected order"
                )
            exp = expected_restart_order(self._supervision_tree, target)

        # Wait up to `within` for the observed prefix to match.
        import time as _time
        deadline = _time.monotonic() + timeout
        anchor = self._crash_anchor or 0.0
        last_observed: list[str] = []
        while _time.monotonic() < deadline:
            observed = self._restart_observer.restart_order(since=anchor)
            last_observed = observed
            if observed[:len(exp)] == exp:
                logger.info(
                    "Assert Restart Order: observed prefix %r matches "
                    "expected %r", observed[:len(exp)], exp,
                )
                return
            _time.sleep(0.05)
        raise AssertionError(
            f"Assert Restart Order: expected prefix {exp!r}; "
            f"observed {last_observed!r} within {within}"
        )

    @keyword("Assert Healthy")
    def assert_healthy(self, name: str, within: str = "5s") -> None:
        """Pass if ``name`` reaches RUNNING within the window.

        Convenience: equivalent to
        ``Assert Eventually    service.state('X') == 'RUNNING'``
        but reads better and doesn't require the expression evaluator
        to be wired."""
        self._require_runtime()
        assert self._restart_observer is not None
        timeout = _seconds(within)
        import time as _time
        deadline = _time.monotonic() + timeout
        anchor = self._crash_anchor or 0.0
        while _time.monotonic() < deadline:
            if self._restart_observer.reached_running(name, since=anchor):
                return
            _time.sleep(0.05)
        raise AssertionError(
            f"Assert Healthy: {name!r} did not reach RUNNING within {within}"
        )

    @keyword("Assert Restart Within Limit")
    def assert_restart_within_limit(self, name: str) -> None:
        """Assert that ``name``'s restart_count <= max_restarts of its
        owning supervisor. Catches runaway-crash bugs where the limit
        is breached and escalation should have fired."""
        self._require_runtime()
        if self._supervision_tree is None:
            raise RuntimeError(
                "Assert Restart Within Limit: no supervision tree "
                "loaded. Call `Load Supervision` first."
            )
        assert self._restart_observer is not None
        parent = self._supervision_tree.parent_of(name)
        if parent is None:
            raise AssertionError(
                f"Assert Restart Within Limit: {name!r} not in tree"
            )
        observed = self._restart_observer.restart_count(name)
        if observed > parent.max_restarts:
            raise AssertionError(
                f"Assert Restart Within Limit: {name!r} restart_count="
                f"{observed} exceeds parent {parent.name!r} "
                f"max_restarts={parent.max_restarts}"
            )
        logger.info("Assert Restart Within Limit: %s rc=%d <= %d",
                    name, observed, parent.max_restarts)

    # ──────────────────────────────────────────────────────────────────
    # Pair-4 keywords: distributed components.
    # ──────────────────────────────────────────────────────────────────

    @keyword("Run Component")
    def run_component(
        self,
        component_name: str,
        on: str = "",
        as_: str = "",
    ) -> None:
        """Instantiate a component from the registry on a target machine.

        Args:
          component_name: Name in the COMPONENT_REGISTRY (e.g. ``SmProber``).
          on:             Target machine. Empty or the harness's own
                          machine → LocalTransport (in-process). Any
                          other name → SSHTransport (stubbed; raises).
          as_:            Instance name for Component Call (defaults to
                          ``component_name``). Use this when running
                          multiple instances of the same component.

        Example::

            Run Component    SmProber    on=central_host    as_=probe
        """
        self._require_runtime()
        assert self._components is not None
        comp_cls = COMPONENT_REGISTRY.get(component_name)
        if comp_cls is None:
            raise KeyError(
                f"Run Component: {component_name!r} not in registry "
                f"(have: {sorted(COMPONENT_REGISTRY)})"
            )
        transport = self._pick_transport(on)
        instance = as_ or component_name
        self._components.run(comp_cls, instance, transport)
        logger.info("Run Component: %s on=%s as=%s",
                    component_name, on or "<local>", instance)

    @keyword("Stop Component")
    def stop_component(self, instance: str) -> None:
        self._require_runtime()
        assert self._components is not None
        self._components.stop(instance)

    @keyword("Component Call")
    def component_call(self, instance: str, method: str, **kwargs: Any) -> Any:
        """Invoke a method on a running component.

        The component's method signature determines the keyword args.
        Robot passes everything as strings; the method does its own
        coercion. Method-raised AssertionError surfaces as a Robot
        keyword failure (test FAIL); other exceptions surface as ERROR.

        Example::

            Component Call    probe    set_state    state=RUN
        """
        self._require_runtime()
        assert self._components is not None
        return self._components.call(instance, method, **kwargs)

    @keyword("Component Expect")
    def component_expect(
        self, instance: str, method: str, **kwargs: Any
    ) -> Any:
        """Same as `Component Call`, but reads better when the operation
        is an assertion. Documents intent: this call is the test's
        observable check.
        """
        return self.component_call(instance, method, **kwargs)

    # ──────────────────────────────────────────────────────────────────
    # Pair-5 keywords: topology graph.
    # ──────────────────────────────────────────────────────────────────

    @keyword("Load Topology")
    def load_topology_kw(self, path: str) -> None:
        """Load artheia's emitted ``netgraph.json`` for the current
        package. Static graph queries are then available via the
        topology assertions.

        Typical path: ``<package>/system/netgraph.json`` (built by
        ``artheia gen-netgraph``) or a captured fixture under
        ``scenarios/fixtures/``.

        Must follow `Load Rig` so cross-checks can compare against
        the deployment.
        """
        self._require_runtime()
        self._topology = load_topology(path)
        logger.info("Load Topology: %s (nodes=%d, compositions=%d)",
                    path, len(self._topology.nodes),
                    len(self._topology.compositions))

    @keyword("Assert Routes To")
    def assert_routes_to(
        self,
        source: str,
        msg: str,
        *expected: str,
    ) -> None:
        """Assert that ``source`` emits ``msg`` to EXACTLY the given
        set of destinations (order-insensitive).

        Example::

            Assert Routes To    MyNode    MyReply    OtherNode    ThirdNode
        """
        topo = self._require_topology()
        actual = sorted(topo.destinations_of(source, msg))
        wanted = sorted(expected)
        if actual != wanted:
            raise AssertionError(
                f"Assert Routes To: {source}.{msg} routes to {actual!r}, "
                f"expected {wanted!r}"
            )
        logger.info("Assert Routes To: %s.%s = %s", source, msg, actual)

    @keyword("Assert Reachable")
    def assert_reachable(self, source: str, target: str) -> None:
        """Assert ``target`` is in the transitive outbound closure of
        ``source``. Catches "this signal can never reach that consumer"
        bugs after a refactor."""
        topo = self._require_topology()
        reached = topo.reachable_from(source)
        if target not in reached:
            raise AssertionError(
                f"Assert Reachable: {target!r} not reachable from "
                f"{source!r} (reached: {sorted(reached)})"
            )
        logger.info("Assert Reachable: %s → %s", source, target)

    @keyword("Assert Not Reachable")
    def assert_not_reachable(self, source: str, target: str) -> None:
        """Assert ``target`` is NOT in ``source``'s transitive closure.
        Useful for isolation invariants (e.g. "the sandbox node must
        not reach production services")."""
        topo = self._require_topology()
        reached = topo.reachable_from(source)
        if target in reached:
            raise AssertionError(
                f"Assert Not Reachable: {target!r} IS reachable from "
                f"{source!r} (path through outbound destinations)"
            )

    @keyword("Assert Netgraph Matches Rig")
    def assert_netgraph_matches_rig(
        self, severity: str = "error",
    ) -> None:
        """Cross-check the loaded topology against the loaded rig.

        ``severity="error"`` (default): fails the test if any check
        returns an error. Warnings (e.g. silent_node, orphan_node_type)
        are surfaced via the log but don't fail.

        ``severity="warning"``: fails on warnings too. Use this when
        you want stricter hygiene gates.

        Inspect the issue list afterwards via :meth:`Get Topology Issues`.
        """
        self._require_runtime()
        topo = self._require_topology()
        assert self._ctx is not None
        rig = self._ctx.rig
        self._last_issues = validate_against_rig(rig, topo)

        fail_on = {"error"} if severity == "error" else {"error", "warning"}
        offenders = [i for i in self._last_issues if i.severity in fail_on]
        for i in self._last_issues:
            logger.info("Assert Netgraph Matches Rig: %s", i)
        if offenders:
            lines = ["Assert Netgraph Matches Rig: cross-check failed:"]
            lines.extend(f"  {i}" for i in offenders)
            raise AssertionError("\n".join(lines))

    @keyword("Get Topology Issues")
    def get_topology_issues(self) -> list[str]:
        """Return the last cross-check's issue list as formatted strings.

        Use after `Assert Netgraph Matches Rig` to log/inspect findings
        even when the assertion passed (e.g. for warnings the test
        chose not to fail on).
        """
        return [str(i) for i in self._last_issues]

    # ──────────────────────────────────────────────────────────────────
    # TPT engine passthroughs — kept from Phase 1; declarative-shape.
    # ──────────────────────────────────────────────────────────────────

    @keyword("Create Partition")
    def create_partition(self, name: str) -> None:
        self.engine.add_partition(Partition(name))

    @keyword("Add Transition")
    def add_transition(self, source: str, target: str, condition: str) -> None:
        guard = self._parse_condition(condition)
        self.engine.add_transition(source, target, guard, condition)

    @keyword("Set Signal")
    def set_signal(self, name: str, value: float) -> None:
        self.signals.set(name, float(value))

    @keyword("Apply Ramp")
    def apply_ramp(
        self, signal_name: str, start: float, end: float, duration: str
    ) -> None:
        self.engine.add_stimulus(
            RampStimulus(
                self.signals, signal_name,
                float(start), float(end), _seconds(duration),
            )
        )

    @keyword("Run Time Engine")
    def run_time_engine(self, initial: str = "", timeout: str = "60s") -> None:
        self.engine.timeout = _seconds(timeout)
        start = initial or next(iter(self.engine.partitions))
        self.engine.run(start)

    # ──────────────────────────────────────────────────────────────────
    # Utilities.
    # ──────────────────────────────────────────────────────────────────

    @keyword("T Wait")
    def t_wait(self, duration: str) -> None:
        time.sleep(_seconds(duration))

    # ──────────────────────────────────────────────────────────────────
    # Private helpers.
    # ──────────────────────────────────────────────────────────────────

    def _parse_condition(self, condition: str):
        cond = condition.strip()
        if cond.startswith("after "):
            return time_guard(_seconds(cond[6:]))
        if cond.startswith("event:"):
            return event_guard(self.events, cond[6:].strip())
        for op in (">=", "<=", "!=", ">", "<", "=="):
            if op in cond:
                lhs, rhs = cond.split(op, 1)
                return signal_guard(
                    self.signals, lhs.strip(), op, float(rhs.strip())
                )
        raise ValueError(f"Cannot parse condition: {condition!r}")

    def _require_runtime(self) -> None:
        if self._ctx is None:
            raise RuntimeError(
                "rf-theia runtime not loaded — call `Load Rig` first"
            )

    def _ctx_bus(self) -> EventBus:
        self._require_runtime()
        assert self._ctx is not None
        return self._ctx.bus

    def _require_topology(self) -> Topology:
        self._require_runtime()
        if self._topology is None:
            raise RuntimeError(
                "rf-theia topology not loaded — call `Load Topology` first"
            )
        return self._topology

    def _pick_transport(self, on: str):
        """Resolve `on=<machine>` to a transport.

        Empty string or the harness machine name → LocalTransport.
        Any other name → SSHTransport (stubbed; raises on use).
        """
        assert self._local_transport is not None
        if not on:
            return self._local_transport
        # The rig knows our machines. If `on` matches one and that
        # machine looks local (no remote indicators we can use yet),
        # stay local. Real multi-machine support arrives with the
        # SSH transport.
        if self._ctx is not None and self._ctx.rig is not None:
            try:
                self._ctx.rig.machine(on)
            except KeyError:
                raise ValueError(
                    f"Run Component: `on={on!r}` not in rig "
                    f"(machines: {[m.name for m in self._ctx.rig.machines]})"
                )
        return SSHTransport(host=on)

    def _eval(self) -> ExprEvaluator:
        """Lazily build the ExprEvaluator on first Assert keyword.

        We don't construct it in Load Rig because creating it
        subscribes a permanent trace_record listener; the test may
        never use Assert keywords, in which case the listener and its
        record buffer are wasted.
        """
        self._require_runtime()
        if self._evaluator is None:
            assert self._ctx is not None
            self._evaluator = ExprEvaluator(
                bus=self._ctx.bus,
                supervisor_watcher=self._sup_watcher,
                flow_engine=self._flow_engine,
            )
        return self._evaluator


def _seconds(spec: str | float | int) -> float:
    if isinstance(spec, (int, float)):
        return float(spec)
    s = str(spec).strip().lower()
    if s.endswith("ms"):
        return float(s[:-2]) / 1000.0
    if s.endswith("s"):
        return float(s[:-1])
    return float(s)
