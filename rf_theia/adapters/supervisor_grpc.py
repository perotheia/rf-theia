"""Supervisor adapter for the T Sup keyword family — PROBE-backed.

Drives the supervisor over the Theia transport using ``artheia.probe`` +
``tdb.art`` (the Theia Debug Bridge client model), NOT a hand-rolled gRPC/TIPC
client and NOT any ``tools.tdb`` / ``tools.rtdb`` Python import. The probe
materializes a client node (``TdbSup``) straight from the .art: it resolves the
supervisor's TIPC address + each op's service_id from the imported
``SupervisorControlIf`` interface, so when the Theia transport changes we swap
the runtime + probe and this adapter keeps working unchanged.

Why the probe and not a Python client module:
  * NO cross-repo Python dependency. rf-theia ships with artheia (its own dep);
    it must NOT ``import tools.tdb`` from a sibling checkout — that path doesn't
    exist in a deb-installed consuming workspace. The .art interface is the
    contract; the probe is the only code needed.
  * The .art IS the single source of truth for the wire (addresses + service
    ids + message shapes). The adapter states intent ("RestartChild foo"); the
    probe does the framing.

The probe needs the tdb.art + the proto tree to resolve types. Both ship with
the framework: ``$THEIA_ROOT/system/tools/tdb/tdb.art`` and
``$THEIA_ROOT/platform/proto``. We locate them via $THEIA_ROOT (the installed
deb / sourced source checkout) with a repo-root fallback.

Keyword-friendly surface (unchanged so the .robot scenarios don't move):
  - lifecycle: connect / close
  - mutators:  start_child / restart_child / terminate_child
  - polled assertions: expect_child_state / expect_restart_count
  - topology:  get_topology (a plain nested dict)

Methods raise AssertionError (not RuntimeError) on timeout so Robot reports a
test failure, not a library error.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Optional


def _theia_root() -> Path:
    """Where tdb.art + the proto tree live. $THEIA_ROOT (installed deb / sourced
    source checkout) wins; else walk up from this file to a repo that has
    system/tools/tdb/tdb.art."""
    env = os.environ.get("THEIA_ROOT")
    if env and (Path(env) / "system" / "tools" / "tdb" / "tdb.art").is_file():
        return Path(env)
    here = Path(__file__).resolve()
    for cand in [here, *here.parents]:
        if (cand / "system" / "tools" / "tdb" / "tdb.art").is_file():
            return cand
    # Last resort: assume the framework repo is three levels up (the dev tree).
    return here.parents[3]


# ChildState.state enum (platform/supervisor's .art). Kept local so the adapter
# doesn't reach into a generated pb2 module.
_STATE_NAMES = {
    0: "STOPPED", 1: "STARTING", 2: "RUNNING", 3: "TERMINATING", 4: "RESTARTING",
}


class SupervisorClient:
    """Probe-backed supervisor client for the T Sup keywords. One TdbSup probe
    per instance, bound on connect()."""

    # The tdb client node that `requires SupervisorControlIf` + receives the
    # SupervisorEventIf firehose, and the supervisor control node it targets.
    _CLIENT_NODE = "TdbSup"
    _SUP_NODE = "SupervisorCtl"

    def __init__(self, endpoint: str = "", poll_interval: float = 0.1) -> None:
        # endpoint kept for signature compat; the probe resolves the TIPC
        # address from the .art, so no host:port is needed for the TIPC path.
        self.endpoint = endpoint
        self.poll_interval = poll_interval
        self._ctx: Any = None      # artheia.gen_server.probe.ArtheiaContext
        self._sup: Any = None      # the bound TdbSup NodeProbe

    # ----- lifecycle --------------------------------------------------

    def connect(self, timeout: float = 5.0) -> None:
        """Materialize the TdbSup probe from tdb.art and bind its TIPC address.
        A quick GetTree confirms the supervisor answers; raises AssertionError
        if it doesn't within ``timeout``."""
        # Lazy import so a unit test that never touches the supervisor doesn't
        # pay the probe (TIPC socket) import cost.
        from artheia.gen_server.probe import ArtheiaContext

        root = _theia_root()
        art = root / "system" / "tools" / "tdb" / "tdb.art"
        proto = root / "platform" / "proto"
        self._ctx = ArtheiaContext(str(art), proto_root=str(proto))
        self._sup = self._ctx.probe(self._CLIENT_NODE).start()

        deadline = time.monotonic() + timeout
        last_err: Optional[Exception] = None
        while time.monotonic() < deadline:
            try:
                self._sup.call(self._SUP_NODE, "GetTree",
                               timeout=min(2.0, timeout))
                return
            except Exception as e:        # not bound yet / not up yet
                last_err = e
                time.sleep(self.poll_interval)
        self.close()
        raise AssertionError(
            f"supervisor (tdb.art {self._SUP_NODE}) not reachable within "
            f"{timeout}s: {last_err}")

    def close(self) -> None:
        if self._sup is not None:
            try:
                self._sup.stop()
            except Exception:
                pass
            self._sup = None
        self._ctx = None

    # ----- mutators ---------------------------------------------------

    def restart_child(self, name: str) -> None:
        reply = self._call("RestartChild", name=name)
        self._raise_if_failed(reply, "RestartChild", name)

    def terminate_child(self, name: str) -> None:
        reply = self._call("TerminateChild", name=name)
        self._raise_if_failed(reply, "TerminateChild", name)

    def start_child(self, name: str) -> None:
        """StartChild by name. The supervisor owns the spec — it re-launches the
        already-registered child; an unknown name comes back as a non-zero
        ControlReply.status."""
        reply = self._call("StartChild", spec={"name": name})
        self._raise_if_failed(reply, "StartChild", name)

    # ----- assertions -------------------------------------------------

    def expect_child_state(self, name: str, state: str,
                           timeout: float = 5.0) -> None:
        """Poll GetTree until child ``name`` reports the named state, else raise.
        ``state`` matches case-insensitively against the ChildState.state enum
        name (or its number)."""
        target = state.strip().upper()
        deadline = time.monotonic() + timeout
        last_seen = "<no snapshot>"
        while time.monotonic() < deadline:
            child = self._find_child(name)
            if child is not None:
                seen = self._state_name(getattr(child, "state", -1))
                last_seen = seen
                if seen.upper() == target or str(getattr(child, "state")) == target:
                    return
            time.sleep(self.poll_interval)
        raise AssertionError(
            f"child {name!r}: expected state {target}, last seen "
            f"{last_seen!r} within {timeout}s")

    def expect_restart_count(self, name: str, count: int,
                             timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        last_seen = -1
        while time.monotonic() < deadline:
            child = self._find_child(name)
            if child is not None:
                last_seen = int(getattr(child, "restart_count", 0))
                if last_seen >= count:
                    return
            time.sleep(self.poll_interval)
        raise AssertionError(
            f"child {name!r}: expected restart_count >= {count}, last seen "
            f"{last_seen} within {timeout}s")

    def get_topology(self) -> dict:
        """The latest GetTree snapshot as a plain nested dict (generation,
        timestamp_ms, children[] — each child a {name, state, pid,
        restart_count, parent_name, kind} dict). The probe decodes the top
        message to a dict but leaves nested ChildState as proto objects, so we
        flatten them here for keyword convenience."""
        snap = self._call("GetTree")
        children = []
        for ch in (snap.get("children", []) or []):
            children.append({
                "name": getattr(ch, "name", ""),
                "parent_name": getattr(ch, "parent_name", ""),
                "kind": getattr(ch, "kind", 0),
                "pid": getattr(ch, "pid", -1),
                "state": getattr(ch, "state", 0),
                "state_name": self._state_name(getattr(ch, "state", 0)),
                "restart_count": getattr(ch, "restart_count", 0),
            })
        return {
            "generation": snap.get("generation", 0),
            "timestamp_ms": snap.get("timestamp_ms", 0),
            "children": children,
        }

    # ----- internals --------------------------------------------------

    def _require(self):
        if self._sup is None:
            raise RuntimeError("SupervisorClient.connect() not called")
        return self._sup

    def _call(self, op_name: str, op: str = "", **fields) -> dict:
        """Probe-call a SupervisorControlIf op; return the reply dict."""
        try:
            return self._require().call(self._SUP_NODE, op or op_name, **fields)
        except TimeoutError as e:
            raise AssertionError(str(e)) from e

    def _find_child(self, name: str):
        """The raw ChildState proto object for ``name`` (attribute access:
        .state / .restart_count / .pid), or None. The probe decodes nested
        repeated messages as proto objects, not dicts."""
        snap = self._call("GetTree")
        for child in (snap.get("children", []) or []):
            if getattr(child, "name", None) == name:
                return child
        return None

    @staticmethod
    def _state_name(state_value: int) -> str:
        return _STATE_NAMES.get(int(state_value), f"STATE_{state_value}")

    @staticmethod
    def _raise_if_failed(reply: dict, op: str, name: str) -> None:
        status = int(reply.get("status", 0))
        if status != 0:
            msg = reply.get("message", "")
            raise AssertionError(
                f"{op}({name!r}) failed: status={status} message={msg!r}")
