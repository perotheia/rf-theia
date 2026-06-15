"""Supervisor gRPC adapter for the T Sup keyword family.

Wraps ``tools.supdbg.client.Client`` with a small surface tuned to the
keyword library's needs:

  - keyword-friendly methods (string args, polled assertions, dict
    topology snapshot)
  - polling timeout helpers (``expect_child_state``, ``expect_restart_count``)
  - lazy import of the supdbg module so unit tests that don't touch
    the supervisor don't pay the grpcio import cost.

The supdbg ``_gen/`` proto stubs use flat-style imports
(``import ChildState_pb2`` instead of ``from . import ChildState_pb2``);
supdbg already handles that by inserting ``_gen/`` into ``sys.path`` on
import, so the adapter just needs to make ``supdbg`` itself importable.
"""
from __future__ import annotations

import os
import sys
import time
from typing import Any

# Make tools/supdbg/ importable. We expect the repo root to be three
# levels up from this file: testing/rf_theia/adapters/supervisor_grpc.py
_REPO_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)
_TOOLS_DIR = os.path.join(_REPO_ROOT, "tools")
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)


class SupervisorClient:
    """Adapter exposing the subset of supdbg.client that keyword-level
    scenarios need. Holds one channel per instance.

    Methods raise :class:`AssertionError` (not RuntimeError) on timeout
    so that Robot Framework reports them as test failures rather than
    library errors.
    """

    def __init__(self, endpoint: str = "localhost:5051",
                 poll_interval: float = 0.1) -> None:
        self.endpoint = endpoint
        self.poll_interval = poll_interval
        self._client: Any = None  # supdbg.client.Client, lazy

    # ----- lifecycle --------------------------------------------------

    def connect(self, timeout: float = 5.0) -> None:
        """Open the gRPC channel and wait for READY. Raises
        :class:`AssertionError` if the supervisor doesn't answer."""
        from supdbg.client import Client
        self._client = Client(self.endpoint)
        if not self._client.wait_ready(timeout=timeout):
            self._client.close()
            self._client = None
            raise AssertionError(
                f"supervisor at {self.endpoint!r} not reachable within "
                f"{timeout}s"
            )

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # ----- mutators ---------------------------------------------------

    def start_child(self, name: str) -> None:
        """Start a child by name. Spec lookup is the supervisor's job —
        if the child isn't already registered, this fails with a
        non-zero ControlReply.status."""
        # supdbg's start_child takes a full ChildSpec; we route through
        # the more usable name-only path by re-resolving against the
        # current tree snapshot.
        spec = self._spec_for(name)
        reply = self._require().start_child(spec)
        self._raise_if_failed(reply, op="StartChild", name=name)

    def restart_child(self, name: str) -> None:
        reply = self._require().restart_child(name)
        self._raise_if_failed(reply, op="RestartChild", name=name)

    def terminate_child(self, name: str) -> None:
        reply = self._require().terminate_child(name)
        self._raise_if_failed(reply, op="TerminateChild", name=name)

    # ----- assertions -------------------------------------------------

    def expect_child_state(self, name: str, state: str,
                           timeout: float = 5.0) -> None:
        """Poll until child ``name`` reports the named state, or raise.

        ``state`` is matched case-insensitively against the
        ChildState.state enum's name. Numbers and known names both
        accepted (e.g. ``"RUNNING"``, ``"2"``).
        """
        target = state.strip().upper()
        deadline = time.monotonic() + timeout
        last_seen: str = "<no snapshot>"
        while time.monotonic() < deadline:
            child = self._find_child(name)
            if child is not None:
                seen = self._state_name(child.state)
                last_seen = seen
                if seen.upper() == target or str(child.state) == target:
                    return
            time.sleep(self.poll_interval)
        raise AssertionError(
            f"child {name!r}: expected state {target}, last seen "
            f"{last_seen!r} within {timeout}s"
        )

    def expect_restart_count(self, name: str, count: int,
                             timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        last_seen: int = -1
        while time.monotonic() < deadline:
            child = self._find_child(name)
            if child is not None:
                last_seen = child.restart_count
                if last_seen >= count:
                    return
            time.sleep(self.poll_interval)
        raise AssertionError(
            f"child {name!r}: expected restart_count >= {count}, last "
            f"seen {last_seen} within {timeout}s"
        )

    def get_topology(self) -> dict:
        """Return the latest TreeSnapshot as a plain nested dict.

        Useful when the scenario wants to assert on supervisor
        hierarchy without writing a custom polling loop.
        """
        snap = self._require().tree(timeout=3.0)
        if snap is None:
            raise AssertionError("no TreeSnapshot received within 3s")
        return _snapshot_to_dict(snap)

    # ----- robot node: signal inject + service call (#387) ------------

    def inject_signal(self, tipc_type: int, tipc_instance: int,
                      msg_type: str, payload: bytes,
                      src: str = "RobotTest") -> None:
        """Cast a signal AT a component over the com robot node,
        impersonating ``src``. Raises if com couldn't send the frame."""
        ack = self._require().inject_signal(
            int(tipc_type), int(tipc_instance), msg_type, payload, src)
        if not ack.sent:
            raise AssertionError(
                f"InjectSignal({msg_type} → 0x{int(tipc_type):x}/"
                f"{int(tipc_instance)}) not sent: {ack.message}")

    def call_service(self, tipc_type: int, tipc_instance: int,
                     req_msg_type: str, payload: bytes,
                     src: str = "RobotTest",
                     timeout_ms: int = 0) -> bytes:
        """Call a service ON a component and return the reply payload
        bytes (the caller decodes with the matching _pb2 type). Raises
        on timeout / send failure."""
        rep = self._require().call_service(
            int(tipc_type), int(tipc_instance), req_msg_type, payload,
            src, int(timeout_ms))
        if not rep.ok:
            raise AssertionError(
                f"CallService({req_msg_type} → 0x{int(tipc_type):x}/"
                f"{int(tipc_instance)}) failed: {rep.message}")
        return rep.payload

    # ----- internals --------------------------------------------------

    def _require(self):
        if self._client is None:
            raise RuntimeError("SupervisorClient.connect() not called")
        return self._client

    def _find_child(self, name: str):
        snap = self._require().tree(timeout=2.0)
        if snap is None:
            return None
        for child in snap.children:
            if child.name == name:
                return child
        return None

    def _spec_for(self, name: str):
        """Look up the existing ChildSpec for ``name`` on the supervisor.

        StartChild on the supdbg client requires a fully-formed spec.
        For test purposes we synthesize one by reading the supervisor's
        snapshot; if the child has been deleted, the snapshot won't
        contain it and StartChild becomes an explicit error.
        """
        snap = self._require().tree(timeout=3.0)
        if snap is None:
            raise AssertionError("StartChild: no TreeSnapshot available")
        for child in snap.children:
            if child.name == name and child.HasField("spec"):
                return child.spec
        raise AssertionError(
            f"StartChild: child {name!r} not found in snapshot — "
            f"supervisor has no spec for it"
        )

    @staticmethod
    def _state_name(state_value: int) -> str:
        # Mirror ChildState.State enum from platform/supervisor's .art.
        # Keep the mapping local so the adapter doesn't reach into the
        # generated pb2 module for enum lookup.
        return {
            0: "UNKNOWN",
            1: "STARTING",
            2: "RUNNING",
            3: "STOPPING",
            4: "STOPPED",
            5: "CRASHED",
        }.get(int(state_value), f"STATE_{state_value}")

    @staticmethod
    def _raise_if_failed(reply, op: str, name: str) -> None:
        if reply.status != 0:
            raise AssertionError(
                f"{op}({name!r}) failed: status={reply.status} "
                f"reason={reply.reason!r}"
            )


def _snapshot_to_dict(snap) -> dict:
    """Convert a TreeSnapshot proto to a plain dict for keyword use."""
    return {
        "machine": getattr(snap, "machine", ""),
        "supervisors": [
            {"name": s.name, "strategy": s.strategy}
            for s in getattr(snap, "supervisors", [])
        ],
        "children": [
            {
                "name": c.name,
                "parent": c.parent_name,
                "pid": c.pid,
                "state": SupervisorClient._state_name(c.state),
                "restart_count": c.restart_count,
                "uptime_ms": c.uptime_ms,
                "flags": getattr(c, "flags", 0),
            }
            for c in getattr(snap, "children", [])
        ],
    }
