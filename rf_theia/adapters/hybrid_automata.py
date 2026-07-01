"""HybridAutomata — drive + observe ANY gen_statem FC standalone.

A gen_statem FC (MyFsm, SmDaemon, …) is a hybrid automaton: discrete states
with event- and timeout-driven transitions, each carrying FSM `data` (OTP
`{State, Data}`). This adapter tests one such FC end-to-end, parameterized by
the FC's node names + `.art`, so the SAME machinery drives my_fsm AND sm:

  - INJECT events at the gate via artheia.probe (ordered casts over one
    connection) — the statem node takes no wire messages; its gate (a receiver)
    post_event()s each event into the FSM in-process.
  - OBSERVE the STATEM trace via artheia.observer (TIPC firehose) — each
    committed transition lands on an EventBus as a `statem_transition` event
    carrying from/to state + the decoded FSM data.

The TheiaTestLibrary's `* Statem *` keywords are thin wrappers over this; the
adapter holds all the logic + state so the keyword surface stays declarative.

Boundary: this is the SINGLE rf-theia module that imports artheia (probe +
observer, both stable contracts) for statem testing — it composes the already-
quarantined runtime/probe_adapter + runtime/statem_observer. artheia.model /
.generators stay banned.
"""
from __future__ import annotations

import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# adapters[0] rf_theia[1] testing[2] theia[3].
_WS = Path(__file__).resolve().parents[3]
for _p in (str(_WS / "testing"), str(_WS / "artheia"), str(_WS / "tools" / "tdb")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from rf_theia.runtime.event_bus import EventBus              # noqa: E402
from rf_theia.runtime.statem_observer import StatemObserver  # noqa: E402
from rf_theia.runtime.probe_adapter import ProbeAdapter      # noqa: E402

CENTRAL_DIR = _WS / "install" / "central"
TK_STATEM = 5


def _seconds(spec) -> float:
    s = str(spec).strip().lower()
    if s.endswith("ms"):
        return float(s[:-2]) / 1000.0
    if s.endswith("s"):
        return float(s[:-1])
    return float(s)


class HybridAutomata:
    """Drives + observes one gen_statem FC. One instance per FC under test.

    Construction parameters identify the FC; nothing is hardcoded so the same
    adapter serves my_fsm, sm, and any future statem FC:

      node    — the statem node's kNodeName (trace src + push target), e.g.
                "my_fsm" / "sm_daemon".
      gate    — the receiver node events are cast at, e.g. "MyFsmGate" /
                "SmGate".
      tester  — a sender node (in the FC's .art, not deployed) the probe binds
                as the cast SOURCE, e.g. "MyFsmTester" / "SmTester".
      art     — the FC `.art` (canonical system/ path) carrying gate + tester +
                the event messages.
      ready   — OPTIONAL supervisor-log substring (a node's on_enter line) used
                only as an extra readiness signal / diagnostic. The PRIMARY
                ready-check is transport-based: poll the gate's TIPC binding
                (uniform across FCs — a node's log may go to its own file sink,
                not the supervisor stdout, so a log grep isn't reliable).
    """

    def __init__(self, *, node: str, gate: str, tester: str,
                 art: str | Path, ready: str = "") -> None:
        self.node = node
        self.gate = gate
        self.tester = tester
        self.art = str(_WS / art) if not str(art).startswith("/") else str(art)
        self.ready = ready
        self._bus = EventBus()
        self._sup: Optional[subprocess.Popen] = None
        self._sup_log: Optional[Path] = None
        self._obs: Optional[StatemObserver] = None
        self._probe: Optional[ProbeAdapter] = None
        self._wait_anchor = 0.0
        self._last: Optional[dict] = None

    # ----- lifecycle --------------------------------------------------

    def start(self, timeout: float = 12.0) -> None:
        """Stage install/central, launch the supervisor (THEIA_TRACE=1 so the
        FSM tracer emits), enable STATEM trace on the node, then attach the
        observer + probe. After this the FSM is at its initial state +
        observable."""
        try:
            socket.socket(socket.AF_TIPC, socket.SOCK_SEQPACKET).close()
        except OSError as e:
            raise AssertionError(f"AF_TIPC unavailable ({e}); modprobe tipc")

        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        r = subprocess.run(["bash", "apps/stage_local.sh"], cwd=str(_WS),
                           env=env, capture_output=True, text=True)
        if r.returncode != 0:
            raise AssertionError(
                f"stage_local.sh failed:\n{r.stdout}\n{r.stderr}")

        self._sup_log = Path(f"/tmp/rf_statem_{self.node}_{os.getpid()}.log")
        senv = dict(env, THEIA_TRACE="1", THEIA_LOG_LEVEL="info",
                    THEIA_SUPERVISOR_MANIFEST="executor.json",
                    THEIA_INSTALL_DIR=str(CENTRAL_DIR / "current"),
                    THEIA_SUPERVISOR_INSTANCE="0")
        self._sup = subprocess.Popen(
            ["./supervisor"], cwd=str(CENTRAL_DIR),
            stdout=open(self._sup_log, "w"), stderr=subprocess.STDOUT,
            env=senv, preexec_fn=os.setsid)
        # Primary readiness: the gate's TIPC binding is up (uniform across FCs;
        # a node's on_enter log may route to its own file, not here). Resolve
        # the gate address from the .art, then poll a probe connect.
        self._wait_gate_bound(timeout)

        self._enable_statem_trace(timeout=4.0)

        self._obs = StatemObserver(self._bus, node=self.node).start(timeout=4.0)
        self._probe = ProbeAdapter(
            art=Path(self.art), tester=self.tester, gate=self.gate).start()
        time.sleep(0.5)   # let the observer settle its subscribe

    def stop(self) -> None:
        if self._probe is not None:
            self._probe.stop(); self._probe = None
        if self._obs is not None:
            self._obs.stop(); self._obs = None
        if self._sup is not None and self._sup.poll() is None:
            try:
                os.killpg(os.getpgid(self._sup.pid), signal.SIGTERM)
                self._sup.wait(timeout=6)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(os.getpgid(self._sup.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self._sup = None

    # ----- drive / observe / assert -----------------------------------

    def emit(self, event: str, **fields) -> None:
        """Cast an event (a gate-interface data element name) at the gate.
        Anchors the wait-window so a following wait only matches transitions
        caused by THIS event."""
        if self._probe is None:
            raise AssertionError(f"emit before start() for {self.node}")
        self._wait_anchor = time.monotonic()
        self._probe.emit(event, **fields)

    def wait_for_state(self, state: str, within: str = "2s") -> dict:
        """Block until the FSM enters ``state`` (reactive, via the STATEM
        trace). Returns the transition payload (incl. decoded ``data``)."""
        timeout = _seconds(within)
        ev = self._bus.wait_for(
            "statem_transition",
            match=lambda e: e.payload.get("to_state") == state,
            timeout=timeout, since=self._wait_anchor or None)
        if ev is None:
            raise AssertionError(
                f"{self.node} did not enter {state!r} within {within} "
                f"(see {self._sup_log})")
        self._last = ev.payload
        return ev.payload

    def assert_data(self, **expected) -> None:
        """Assert fields of the LAST observed transition's FSM data (OTP Data
        term). Values compared as strings (Robot args are strings)."""
        if self._last is None:
            raise AssertionError(
                f"assert_data before any wait_for_state for {self.node}")
        data = self._last.get("data") or {}
        for key, want in expected.items():
            got = data.get(key)
            if str(got) != str(want):
                raise AssertionError(
                    f"{self.node} data[{key!r}]={got!r}, expected {want!r} "
                    f"(full data: {data!r})")

    # ----- internals --------------------------------------------------

    def _wait_gate_bound(self, timeout: float) -> None:
        """Poll until the gate's TIPC name is connectable, or fail. Proves the
        FC process is up + the gate node bound — independent of how the FC
        logs. Resolves the gate address from the .art via the probe context."""
        from artheia.gen_server.probe import ArtheiaContext
        from artheia.gen_server.probe.transport import TipcClient
        ctx = ArtheiaContext(self.art, proto_root=str(_WS / "platform" / "proto"))
        g = ctx.ref(self.gate)
        deadline = time.time() + timeout
        last_err = ""
        while time.time() < deadline:
            if self._sup and self._sup.poll() is not None:
                raise AssertionError(
                    f"supervisor exited before {self.gate} bound:\n"
                    f"{self._sup_log.read_text() if self._sup_log else ''}")
            c = TipcClient(g.tipc_type, g.tipc_instance)
            try:
                if c.connect(total_timeout_ms=300):
                    c.close()
                    return
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
            finally:
                try:
                    c.close()
                except Exception:
                    pass
            time.sleep(0.2)
        raise AssertionError(
            f"gate {self.gate} (0x{g.tipc_type:08x}) not bound within "
            f"{timeout}s — {self.node} FC didn't come up. {last_err}")

    def _enable_statem_trace(self, timeout: float) -> None:
        from tdb_client import SupervisorClient
        sup = SupervisorClient.from_workspace(_WS)
        try:
            sup.configure_trace(target_node=self.node, enabled=True,
                                kind=TK_STATEM)
        finally:
            try:
                sup.probe.stop()
            except Exception:
                pass
        line = self._grep(
            self._sup_log,
            re.compile(rf"trace config ENABLE kind {TK_STATEM} for {self.node}"),
            timeout)
        if line is None:
            raise AssertionError(
                f"supervisor never logged STATEM trace enable for {self.node} "
                f"(see {self._sup_log})")

    def _wait_log(self, path: Path, needle: str, timeout: float,
                  what: str) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if path.exists() and needle in path.read_text():
                return
            if self._sup and self._sup.poll() is not None:
                raise AssertionError(
                    f"supervisor exited before {what}:\n{path.read_text()}")
            time.sleep(0.1)
        raise AssertionError(
            f"timed out waiting for {what}:\n"
            f"{path.read_text() if path.exists() else '(no log)'}")

    def _grep(self, path: Optional[Path], pat: "re.Pattern", timeout: float):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if path and path.exists():
                m = pat.search(path.read_text())
                if m:
                    return m.group(0)
            time.sleep(0.1)
        return None
