"""Cross-checks between the topology graph (netgraph.json) and the
deployment (rig.json).

Catches the bugs that hide in the gap between "what we said we'd wire"
and "what we said we'd deploy":

  - rig component points at a composition that doesn't exist in the
    netgraph
  - a node receives messages that no deployed sender emits
  - a destination references a node not deployed anywhere
  - an entire node has no signals declared (likely a placeholder)

Each check returns a list of :class:`Issue`. Severity classifies the
finding:

  - ``"error"``: definitely broken — keyword should fail the test
  - ``"warning"``: suspicious — keyword surfaces but doesn't fail by
                    default (test can opt in with ``severity="warning"``)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .rig import Rig
from .topology import Topology


Severity = Literal["error", "warning"]


@dataclass
class Issue:
    severity: Severity
    code: str
    message: str

    def __str__(self) -> str:
        return f"[{self.severity}] {self.code}: {self.message}"


def _composition_from_art_node(art_node: str) -> str:
    """Extract the composition name from a Component.art_node string.

    artheia emits ``art_node`` in the form ``"<package>/<TypeName>"``
    (e.g. ``"system.app/MyAppP1"``). The TypeName is the
    composition. Robust to no-slash form: returns the whole string
    if there's no separator.
    """
    if "/" in art_node:
        return art_node.split("/", 1)[1]
    return art_node


def deployed_compositions(rig: Rig) -> set[str]:
    """Set of composition names referenced by ANY rig component."""
    return {
        _composition_from_art_node(c.art_node)
        for c in rig.all_components()
        if c.art_node
    }


def deployed_node_types(rig: Rig, topo: Topology) -> set[str]:
    """Set of node-type names that are deployed somewhere.

    Walks: rig component → composition → prototypes → node-types.
    A node-type counts as deployed iff at least one prototype of it
    is part of a composition that some rig component points at.
    """
    deployed_comps = deployed_compositions(rig)
    nodes: set[str] = set()
    for comp in topo.compositions:
        if comp.name not in deployed_comps:
            continue
        for proto in comp.prototypes:
            nodes.add(proto.node)
    return nodes


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_rig_compositions_resolve(rig: Rig, topo: Topology) -> list[Issue]:
    """Every composition referenced by a rig component must exist in
    the topology. Catches typos in service.py's art_node fields and
    out-of-date netgraph re-runs."""
    out: list[Issue] = []
    topo_comp_names = {c.name for c in topo.compositions}
    for comp in rig.all_components():
        if not comp.art_node:
            continue
        wanted = _composition_from_art_node(comp.art_node)
        if wanted not in topo_comp_names:
            out.append(Issue(
                severity="error",
                code="rig_composition_missing",
                message=(
                    f"rig component {comp.name!r} references composition "
                    f"{wanted!r} not in topology "
                    f"(art_node={comp.art_node!r})"
                ),
            ))
    return out


def check_destinations_deployed(rig: Rig, topo: Topology) -> list[Issue]:
    """Every node that appears as a destination in netgraph routing
    must actually be deployed (i.e. its node-type is part of some
    deployed composition).

    This catches the "we wired a message to a node that nobody
    deploys" class of bug — particularly common after a refactor
    where someone removed a prototype but left the connections.
    """
    out: list[Issue] = []
    deployed = deployed_node_types(rig, topo)
    seen: set[tuple[str, str, str]] = set()
    for node in topo.nodes:
        for msg, sig in node.signals.items():
            if sig.direction == "in":
                continue
            for dest in sig.destinations:
                if dest.node in deployed:
                    continue
                key = (node.name, msg, dest.node)
                if key in seen:
                    continue
                seen.add(key)
                out.append(Issue(
                    severity="error",
                    code="destination_not_deployed",
                    message=(
                        f"{node.name}.signals[{msg!r}] routes to "
                        f"{dest.node!r}, which is not deployed by any "
                        f"rig composition"
                    ),
                ))
    return out


def check_orphan_node_types(rig: Rig, topo: Topology) -> list[Issue]:
    """Node types declared in netgraph but not deployed anywhere.

    Warnings (not errors): orphans can be intentional — e.g. an
    abstract node prototype kept around as a base for `extends`. The
    test author opts into stricter checking if appropriate.
    """
    out: list[Issue] = []
    deployed = deployed_node_types(rig, topo)
    for node in topo.nodes:
        if node.name in deployed:
            continue
        out.append(Issue(
            severity="warning",
            code="orphan_node_type",
            message=(
                f"node type {node.name!r} is declared in topology but "
                f"not deployed by any rig composition"
            ),
        ))
    return out


def check_silent_nodes(_rig: Rig, topo: Topology) -> list[Issue]:
    """Nodes with no signals at all — possibly an unfinished port
    declaration. Warning only."""
    out: list[Issue] = []
    for node in topo.nodes:
        if not node.signals:
            out.append(Issue(
                severity="warning",
                code="silent_node",
                message=(
                    f"node {node.name!r} has no signals declared (no "
                    f"messages emitted or received)"
                ),
            ))
    return out


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


_ALL_CHECKS = [
    check_rig_compositions_resolve,
    check_destinations_deployed,
    check_orphan_node_types,
    check_silent_nodes,
]


def validate_against_rig(rig: Rig, topo: Topology) -> list[Issue]:
    """Run every cross-check, return the combined issue list.

    Order is stable so tests can compare against golden output if
    that becomes useful. Severity ordering: errors first, warnings
    second — but otherwise within-check order is preserved.
    """
    issues: list[Issue] = []
    for check in _ALL_CHECKS:
        issues.extend(check(rig, topo))
    # Stable sort: errors first.
    issues.sort(key=lambda i: 0 if i.severity == "error" else 1)
    return issues
