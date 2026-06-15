"""Typed Rig context, loaded from artheia's ``rig-deps`` JSON output.

artheia emits this via:

    artheia rig-deps apps.manifest.rig --out demo_rig.json

The schema is the STABLE contract between artheia (the SUT description
language) and rf-theia (the test framework). When artheia changes its
output, only this module updates — no other rf-theia code knows the
field shape.

The Rig object is what ``Load Rig`` keyword binds in Robot's Suite
Setup. Keywords reach into it via ``runtime.rig.machine_for(name)``,
``runtime.rig.find_component(name)`` etc.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


class Vehicle(BaseModel):
    """Vehicle identity — name + make/model pinning."""
    name: str
    make: str
    model: str


class Component(BaseModel):
    """One executable component on a machine.

    Maps to a single FC daemon or app process. ``name`` is what the
    supervisor uses as ``ChildSpec.name``; that's the handle for
    restart/terminate RPCs and the identifier that appears in trace
    records as the ``<node>`` field.
    """
    name: str
    bazel_target: str
    owner: str
    art_node: str
    bazel_buildable: bool = False


class Application(BaseModel):
    """Logical grouping of components on a machine (matches the
    artheia ``Application`` concept — usually ``platform_app`` plus an
    app-specific bundle)."""
    name: str
    components: list[Component] = Field(default_factory=list)


class Machine(BaseModel):
    """One target the rig deploys to. ``kind`` is ``host`` (dev/admin
    box, no payload) or ``target`` (carries components)."""
    name: str
    kind: str  # "host" | "target"
    arch: str  # "amd64" | "arm64" | ...
    applications: list[Application] = Field(default_factory=list)

    def components(self) -> list[Component]:
        """Flat component list across this machine's applications."""
        return [c for app in self.applications for c in app.components]


class FlatComponent(BaseModel):
    """Same components as ``Machine.applications``, but flattened with
    a back-reference to the machine. Convenience for keyword paths
    that want ``component.machine`` directly."""
    name: str
    bazel_target: str
    machine: str
    owner: Optional[str] = None
    art_node: Optional[str] = None


class Rig(BaseModel):
    """Typed view over artheia's rig-deps JSON output.

    Don't construct directly — call :func:`load_rig`. The class is
    intentionally a passive data holder; the active orchestration
    (gRPC channels, trace tails) lives in
    :mod:`rf_theia.runtime.context`, which holds a Rig plus the
    adapter handles.
    """
    vehicle: Vehicle
    machines: list[Machine] = Field(default_factory=list)
    flat_components: list[FlatComponent] = Field(default_factory=list)

    # ----- convenience lookups ---------------------------------------

    def machine(self, name: str) -> Machine:
        """Return the machine with this name, or raise KeyError."""
        for m in self.machines:
            if m.name == name:
                return m
        raise KeyError(f"machine {name!r} not in rig "
                       f"(have: {[m.name for m in self.machines]})")

    def find_component(self, name: str) -> Component:
        """Locate a component by name across all machines + apps.

        Returns the Component; the machine binding is available via
        :meth:`machine_for`. Raises KeyError if not found.
        """
        for m in self.machines:
            for c in m.components():
                if c.name == name:
                    return c
        raise KeyError(f"component {name!r} not in rig "
                       f"(have: {[c.name for c in self.all_components()]})")

    def machine_for(self, component_name: str) -> Machine:
        """Which machine hosts this component? Raises KeyError."""
        for m in self.machines:
            for c in m.components():
                if c.name == component_name:
                    return m
        raise KeyError(f"component {component_name!r} not deployed "
                       "on any machine")

    def all_components(self) -> list[Component]:
        """Flat component list across the whole rig."""
        return [c for m in self.machines for c in m.components()]


def load_rig(path: str | Path) -> Rig:
    """Load + validate artheia's ``rig-deps`` JSON output.

    The path is typically:

      - ``dist/manifest/<rig>/rig.json``  (Bazel-built rig artifact)
      - ``testing/rf_theia/scenarios/fixtures/<rig>.json``  (test
        fixture captured by `artheia rig-deps --out`)

    Raises:
      FileNotFoundError if path doesn't exist.
      pydantic.ValidationError if the JSON doesn't match the schema.
    """
    p = Path(path)
    raw = json.loads(p.read_text())
    return Rig.model_validate(raw)
