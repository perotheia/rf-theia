"""Distributed components — active probes co-located with the SUT.

A :class:`Component` is a stateful object Robot can drive via keywords.
It exposes:

  - typed **ports** (send/receive channels into the SUT)
  - named **operations** (Python methods the keyword surface calls)

It is NOT a testcase. The Robot ``*** Test Cases ***`` block IS the
test; the component just gives that test a foothold on a specific
machine. One scenario can hold multiple components running on
different machines (Pair 4's reason to exist).

Transports
~~~~~~~~~~

A transport binds a Component instance to a machine. Two transports
are envisioned:

  - :class:`LocalTransport` — runs the component in-process on the
    harness host. Used for SIL and for any test where the harness
    machine IS the target.

  - :class:`SSHTransport` — pushes the component to a remote machine
    and proxies operation calls over a stdio control plane. Interface
    declared here; implementation deferred (NotImplementedError) until
    a multi-machine rig is actually running.

A transport's job is to (a) take a Component class + machine ref,
(b) own its lifecycle (start/stop), (c) marshal operation calls back
and forth. The Component subclass doesn't know which transport it's
running under.

Verdicts
~~~~~~~~

Components don't carry verdicts. Robot does (via the Verdict keyword
from Pair 1). A component operation that observes a violation raises
:class:`AssertionError` — Robot fails the keyword, the scenario
records ``fail``.
"""
from __future__ import annotations

import logging
import queue
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .event_bus import EventBus

logger = logging.getLogger("rf_theia.component")


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------


@dataclass
class PortSpec:
    """Declares a port at class definition time.

    The runtime binds ports to live channels (TIPC clients, gRPC stubs,
    in-process queues) when the component is constructed. PortSpec is
    just the declaration — the kind tag tells the transport how to
    bind it.

    Currently supported kinds:
      - ``"tipc"``   — TIPC SEQPACKET into the cluster (live theia)
      - ``"loop"``   — in-process queue (hermetic tests)
      - ``"grpc"``   — gRPC stub (reuse adapters/supervisor_grpc)

    Future:
      - ``"can"``    — CAN frames
      - ``"socket"`` — raw TCP/UDP
    """
    name: str
    kind: str
    direction: str       # "out" | "in" | "inout"
    config: dict[str, Any] = field(default_factory=dict)


class _Port:
    """Live port handle attached to a running component.

    Send/receive operations are queue-based for ``"loop"`` and stub-
    based for the rest (filled in per kind). Receive blocks up to a
    timeout; raises if no message arrives.
    """

    def __init__(self, spec: PortSpec) -> None:
        self.spec = spec
        self._inbox: queue.Queue[Any] = queue.Queue()
        # _outbound is the callable that turns a `send` into wire
        # bytes. LocalTransport wires this to a paired component's
        # _inbox; SSHTransport will wire it to its control plane.
        self._outbound: Optional[Callable[[Any], None]] = None

    def send(self, msg: Any) -> None:
        if self.spec.direction == "in":
            raise RuntimeError(f"port {self.spec.name}: send on input port")
        if self._outbound is None:
            raise RuntimeError(
                f"port {self.spec.name}: not bound to a peer "
                "(component started before LocalTransport.connect?)"
            )
        self._outbound(msg)

    def receive(self, timeout: float = 2.0) -> Any:
        if self.spec.direction == "out":
            raise RuntimeError(f"port {self.spec.name}: receive on output port")
        try:
            return self._inbox.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError(
                f"port {self.spec.name}: no message within {timeout}s"
            )

    def _deliver(self, msg: Any) -> None:
        """Internal: a peer transport has handed us a message."""
        self._inbox.put(msg)


# ---------------------------------------------------------------------------
# Component base
# ---------------------------------------------------------------------------


class Component(ABC):
    """Subclass to define an active probe.

    Subclasses declare ``ports`` as a class attribute (list of
    :class:`PortSpec`) and provide methods Robot can call. The
    runtime constructs an instance per ``Run Component`` keyword,
    binds ports, and routes ``Component Call`` to instance methods.

    Example::

        class SmProber(Component):
            ports = [
                PortSpec("sm_state",   kind="tipc", direction="out",
                         config={"type": 0x8001000D, "instance": 0}),
                PortSpec("statistics", kind="tipc", direction="in",
                         config={"type": 0x8001000E, "instance": 0}),
            ]

            def set_state(self, state: str) -> None:
                self.sm_state.send({"op": "set_state", "value": state})

            def expect_broadcast(self, msg_type: str, within: str) -> None:
                msg = self.statistics.receive(timeout=_seconds(within))
                if msg.get("type") != msg_type:
                    raise AssertionError(
                        f"expected {msg_type}, got {msg.get('type')}"
                    )
    """

    # Subclasses override.
    ports: list[PortSpec] = []

    def __init__(self, instance_name: str, bus: EventBus) -> None:
        self.instance_name = instance_name
        self.bus = bus
        # _ports maps port name → live _Port handle.
        self._ports: dict[str, _Port] = {}
        for spec in self.ports:
            p = _Port(spec)
            self._ports[spec.name] = p
            # Make the port accessible as `self.<port_name>` for
            # operation bodies.
            setattr(self, spec.name, p)

    def setup(self) -> None:
        """Override for per-instance setup. Called once after ports are
        bound to the transport."""

    def teardown(self) -> None:
        """Override for per-instance cleanup. Called by `Tear Down Rig`
        or when the transport stops the component."""


# ---------------------------------------------------------------------------
# Transport interface
# ---------------------------------------------------------------------------


class Transport(ABC):
    """Binds a Component to a machine. Owns instance lifecycle."""

    @abstractmethod
    def start(
        self, comp_cls: type[Component], instance_name: str, bus: EventBus
    ) -> Component:
        """Instantiate the component, bind ports, return the handle."""

    @abstractmethod
    def stop(self, comp: Component) -> None:
        """Stop the component, release ports."""

    @abstractmethod
    def call(self, comp: Component, method: str, **kwargs: Any) -> Any:
        """Invoke a method on the component. LocalTransport just
        ``getattr(comp, method)(**kwargs)``; SSHTransport marshals."""


class LocalTransport(Transport):
    """Run components in-process on the harness machine.

    Suitable for SIL and for any test where the harness IS the target.
    Ports of kind ``"loop"`` get connected together via a shared
    registry — useful for testing component-to-component wiring
    without going through a network.
    """

    def __init__(self) -> None:
        # Loop-port registry: name → _Port. Lets one component's
        # "out" port find a peer's "in" port by matching names.
        self._loop_registry: dict[str, _Port] = {}

    def start(
        self, comp_cls: type[Component], instance_name: str, bus: EventBus
    ) -> Component:
        comp = comp_cls(instance_name, bus)
        for port in comp._ports.values():
            self._bind_port(port)
        comp.setup()
        logger.info("LocalTransport: started %s instance=%s",
                    comp_cls.__name__, instance_name)
        return comp

    def stop(self, comp: Component) -> None:
        try:
            comp.teardown()
        finally:
            for port in comp._ports.values():
                if port.spec.kind == "loop":
                    # Drop loop bindings so a re-run gets fresh peers.
                    self._loop_registry.pop(port.spec.name, None)

    def call(self, comp: Component, method: str, **kwargs: Any) -> Any:
        fn = getattr(comp, method, None)
        if not callable(fn):
            raise AttributeError(
                f"{type(comp).__name__} has no operation {method!r}"
            )
        return fn(**kwargs)

    def _bind_port(self, port: _Port) -> None:
        kind = port.spec.kind
        if kind == "loop":
            self._bind_loop_port(port)
        elif kind == "tipc":
            self._bind_tipc_port(port)
        elif kind == "grpc":
            self._bind_grpc_port(port)
        else:
            raise NotImplementedError(
                f"LocalTransport: port kind {kind!r} not supported"
            )

    def _bind_loop_port(self, port: _Port) -> None:
        """Wire matching-name loop ports together.

        First port in: registered as peer-of-this-name. Second port in
        with the same name: gets connected to the first.
        """
        name = port.spec.name
        peer = self._loop_registry.get(name)
        if peer is None:
            self._loop_registry[name] = port
            # Configure outbound to be a no-op until a peer arrives —
            # the peer will overwrite it when it binds.
            port._outbound = lambda msg: None
            return
        # Cross-wire: peer's send → our inbox, ours → peer's inbox.
        peer._outbound = port._deliver
        port._outbound = peer._deliver

    def _bind_tipc_port(self, port: _Port) -> None:
        """TIPC ports use the SUT's TIPC. Live binding deferred to a
        helper that creates an AF_TIPC socket. Pair-4 SIL tests stay
        in the loop kind; TIPC bind only activates when a real cluster
        is reachable. For now we raise so a misconfigured test fails
        loud rather than silently no-op'ing."""
        raise NotImplementedError(
            f"port {port.spec.name!r}: TIPC binding not wired in "
            "Pair 4 v1. Use kind='loop' for hermetic tests; TIPC "
            "binding lands when live SUT integration begins."
        )

    def _bind_grpc_port(self, port: _Port) -> None:
        """gRPC ports reuse the adapters.supervisor_grpc client.
        Implementation deferred — no current scenario needs it."""
        raise NotImplementedError(
            f"port {port.spec.name!r}: gRPC binding not wired in "
            "Pair 4 v1."
        )


class SSHTransport(Transport):
    """Stubbed. Push the component to a remote machine over SSH and
    proxy method calls over a stdio control plane.

    Interface declared so the keyword layer can take ``on=<remote>``
    arguments without breaking; raises NotImplementedError on actual
    use. Lands when a multi-machine rig with a reachable remote
    target is real.
    """

    def __init__(self, host: str, port: int = 22,
                 user: Optional[str] = None) -> None:
        self.host = host
        self.port = port
        self.user = user

    def start(
        self, comp_cls: type[Component], instance_name: str, bus: EventBus
    ) -> Component:
        raise NotImplementedError(
            "SSHTransport: not implemented in Pair 4 v1. Use the local "
            "transport (omit `on=` or pass `on=<harness-machine-name>`) "
            "until a real multi-machine rig is wired up."
        )

    def stop(self, comp: Component) -> None:  # pragma: no cover
        raise NotImplementedError

    def call(self, comp: Component, method: str, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Runtime registry
# ---------------------------------------------------------------------------


class ComponentRuntime:
    """Tracks active components by instance_name. Multiplexes calls."""

    def __init__(self, bus: EventBus, local: LocalTransport) -> None:
        self.bus = bus
        self.local = local
        self._instances: dict[str, tuple[Component, Transport]] = {}

    def run(
        self,
        comp_cls: type[Component],
        instance_name: str,
        transport: Transport,
    ) -> Component:
        if instance_name in self._instances:
            raise RuntimeError(
                f"Component {instance_name!r} already running"
            )
        comp = transport.start(comp_cls, instance_name, self.bus)
        self._instances[instance_name] = (comp, transport)
        return comp

    def call(self, instance_name: str, method: str, **kwargs: Any) -> Any:
        comp, transport = self._require(instance_name)
        return transport.call(comp, method, **kwargs)

    def stop(self, instance_name: str) -> None:
        comp, transport = self._instances.pop(instance_name, (None, None))
        if comp is not None and transport is not None:
            transport.stop(comp)

    def stop_all(self) -> None:
        for name in list(self._instances):
            self.stop(name)

    def get(self, instance_name: str) -> Component:
        return self._require(instance_name)[0]

    def _require(self, instance_name: str) -> tuple[Component, Transport]:
        entry = self._instances.get(instance_name)
        if entry is None:
            raise KeyError(
                f"no component instance {instance_name!r} "
                f"(have: {sorted(self._instances)})"
            )
        return entry
