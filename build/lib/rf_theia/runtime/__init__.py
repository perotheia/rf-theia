"""rf_theia.runtime — the semantics layer.

Pairs with rf_theia.TheiaTestLibrary (the Robot keyword surface). The
runtime owns:

  - typed Rig context (loaded from artheia's rig-deps JSON)
  - flow engine for hybrid automata
  - event bus + assertion monitors (later pairs)
  - adapter lifecycle (supervisor gRPC, trace tails)

The library is routing — every keyword forwards to a runtime method.
"""

from .rig import Component, Machine, Rig, Vehicle, load_rig

__all__ = [
    "Component", "Machine", "Rig", "Vehicle", "load_rig",
]
