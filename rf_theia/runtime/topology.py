"""Typed topology graph, loaded from artheia's emitted ``netgraph.json``.

artheia emits this via:

    artheia gen-netgraph <art_file> --out netgraph.json

The schema is the third stable contract with artheia after ``rig.json``
(Pair 1) and ``executor.yaml`` (Pair 3). All three describe the same
SUT from different angles:

  - rig.json       — what's deployed where         (machines, components)
  - executor.yaml  — how things restart            (supervision strategy)
  - netgraph.json  — how things wire to each other (message topology)

This module reads only netgraph.json. Cross-checks against rig.json
live in :mod:`rf_theia.runtime.topology_check` (Pair 5.2).

Schema highlights:

  Topology
    nodes[]                — every node in the system
      ports[]              — ports the node exposes
      signals{msg → spec}  — per-message routing: direction + destinations
    compositions[]         — wiring of node prototypes into processes
      prototypes[]         — node instances under composition-local names
      connections[]        — port-to-port wires inside the composition

Static graph queries:

  destinations_of(node, msg)  → list of destination nodes
  reachable_from(node)        → transitive set of nodes node can talk to
  routes_to(msg)              → list of nodes that receive msg
  emits(node, msg)            → bool: does node send msg?
  receives(node, msg)         → bool: does node receive msg?
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field


Direction = Literal["in", "out", "inout"]
Family = Literal["senderReceiver", "clientServer"]


class TipcAddr(BaseModel):
    """TIPC type + instance pair, stored as hex/decimal strings per
    artheia's JSON shape. ``int_type`` converts on demand."""
    type: str       # e.g. "0xd0010001"
    instance: str   # e.g. "0"

    @property
    def int_type(self) -> int:
        return int(self.type, 0)

    @property
    def int_instance(self) -> int:
        return int(self.instance, 0)


class Port(BaseModel):
    """One port on a node — direction-tagged + interface-typed."""
    name: str
    direction: Direction
    family: Family
    interface: str
    messages: list[str] = Field(default_factory=list)


class Destination(BaseModel):
    """Where a signal lands. ``node`` is the destination node name;
    tipc_type / tipc_instance let live code look it up by address."""
    node: str
    tipc_type: str
    tipc_instance: str


class Signal(BaseModel):
    """Per-message routing entry. ``direction`` is from THIS node's
    POV — ``out`` means this node sends it; ``in`` means it receives.
    ``destinations`` is the set of peers on the other end."""
    direction: Direction
    destinations: list[Destination] = Field(default_factory=list)


class Node(BaseModel):
    name: str
    tipc: TipcAddr
    ports: list[Port] = Field(default_factory=list)
    signals: dict[str, Signal] = Field(default_factory=dict)


class Prototype(BaseModel):
    """A node instance inside a composition. ``name`` is the local
    binding; ``node`` is the node-type it refers to."""
    name: str
    node: str
    tipc: TipcAddr


class Endpoint(BaseModel):
    prototype: str  # name in the composition's prototypes
    port: str


class Connection(BaseModel):
    source: Endpoint
    target: Endpoint
    interface: str
    messages: list[str] = Field(default_factory=list)


class Composition(BaseModel):
    name: str
    prototypes: list[Prototype] = Field(default_factory=list)
    connections: list[Connection] = Field(default_factory=list)


class Topology(BaseModel):
    package: str
    nodes: list[Node] = Field(default_factory=list)
    compositions: list[Composition] = Field(default_factory=list)

    # ----- lookups ----------------------------------------------------

    def node(self, name: str) -> Node:
        for n in self.nodes:
            if n.name == name:
                return n
        raise KeyError(
            f"node {name!r} not in topology "
            f"(have: {[n.name for n in self.nodes]})"
        )

    def node_names(self) -> list[str]:
        return [n.name for n in self.nodes]

    def composition(self, name: str) -> Composition:
        for c in self.compositions:
            if c.name == name:
                return c
        raise KeyError(
            f"composition {name!r} not in topology "
            f"(have: {[c.name for c in self.compositions]})"
        )

    # ----- graph queries ---------------------------------------------

    def destinations_of(self, node_name: str, msg: str) -> list[str]:
        """Which nodes does ``node_name`` send ``msg`` to?

        Empty list if the node doesn't emit msg OR if msg has no
        declared destinations. Distinguishes: a node may declare an
        "out" signal whose destinations[] is empty (e.g. a broadcast
        that nobody subscribes to yet) — that's still a valid result.
        """
        n = self.node(node_name)
        sig = n.signals.get(msg)
        if sig is None or sig.direction == "in":
            return []
        return [d.node for d in sig.destinations]

    def sources_of(self, node_name: str, msg: str) -> list[str]:
        """Which nodes send ``msg`` to ``node_name``?

        Inferred by scanning every node's outbound destinations —
        netgraph stores routing from the sender's perspective.
        """
        sources: list[str] = []
        for n in self.nodes:
            sig = n.signals.get(msg)
            if sig is None or sig.direction == "in":
                continue
            if any(d.node == node_name for d in sig.destinations):
                sources.append(n.name)
        return sources

    def emits(self, node_name: str, msg: str) -> bool:
        sig = self.node(node_name).signals.get(msg)
        return sig is not None and sig.direction in ("out", "inout")

    def receives(self, node_name: str, msg: str) -> bool:
        sig = self.node(node_name).signals.get(msg)
        return sig is not None and sig.direction in ("in", "inout")

    def reachable_from(self, node_name: str) -> set[str]:
        """Transitive closure: set of nodes ``node_name`` can talk to
        directly OR through forwarding chains.

        Closure walks outbound destinations only — a path A→B→C means
        A.signals[X].destinations contains B AND B.signals[Y].destinations
        contains C (for any X, Y). Useful for "is gateway reachable
        from sm?".
        """
        seen: set[str] = set()
        frontier: list[str] = [node_name]
        while frontier:
            cur = frontier.pop()
            if cur in seen:
                continue
            seen.add(cur)
            try:
                node = self.node(cur)
            except KeyError:
                continue
            for sig in node.signals.values():
                if sig.direction == "in":
                    continue
                for dest in sig.destinations:
                    if dest.node not in seen:
                        frontier.append(dest.node)
        seen.discard(node_name)  # don't count self in "reachable"
        return seen

    def routes_to(self, msg: str) -> list[str]:
        """Every node that RECEIVES ``msg`` (inferred from senders'
        destinations[])."""
        receivers: set[str] = set()
        for n in self.nodes:
            sig = n.signals.get(msg)
            if sig is None or sig.direction == "in":
                continue
            for d in sig.destinations:
                receivers.add(d.node)
        return sorted(receivers)


def load_topology(path: str | Path) -> Topology:
    """Load + validate artheia's gen-netgraph JSON output."""
    raw = json.loads(Path(path).read_text())
    return Topology.model_validate(raw)
