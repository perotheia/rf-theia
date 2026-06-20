"""Typed supervision tree, loaded from artheia's emitted ``executor.yaml``.

The supervisor binary on each machine reads this same YAML to decide
its restart strategy and child order. rf-theia reads it to *predict*
what the supervisor should do, then compares against what it actually
does (observed via the supervisor_watcher event stream).

This is the second stable contract with artheia after ``rig.json``:
ONLY this module knows the YAML shape. Generators / restart observers
/ keyword assertions consume the typed tree.

Tree shape (matches deploy/.staging/<machine>/ipk/executor.yaml)::

    root [one_for_all, max=3/5s]
      ar_sup [rest_for_one]
        core_sup [rest_for_one]
          exec, core, crypto, sm                 (workers, declared order)
          network_sup [one_for_one] → nm, com, …
          host_svc_sup [one_for_one] → per, rds
          pltf_sup [one_for_one] → phm, ucm, …
        app_sup [one_for_one] → app_p1, app_p2, app_p3

Strategy semantics encoded:

  one_for_one    children independent — only the crashing child restarts
  rest_for_one   restart crashing child + all children AFTER it in
                 declared order
  one_for_all    restart all children when any one crashes

Each supervisor node has its own restart-limit policy
(max_restarts / max_seconds). When limit is breached the parent
supervisor's strategy kicks in — escalation walks up the tree.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional, Union

import yaml
from pydantic import BaseModel, Field


Strategy = Literal["one_for_one", "rest_for_one", "one_for_all"]


class WorkerChild(BaseModel):
    """A leaf child — an executable process, not a sub-supervisor."""
    name: str
    type: str = "worker"
    restart: str = "permanent"  # permanent | temporary | transient
    shutdown: int = 5000        # ms grace before SIGKILL
    start_cmd: list[str] = Field(default_factory=list)
    modules: list[str] = Field(default_factory=list)

    @property
    def is_supervisor(self) -> bool:
        return False


class SupervisorNode(BaseModel):
    """An interior node — supervises children with a strategy."""
    name: str
    strategy: Strategy
    max_restarts: int = 3
    max_seconds: int = 5
    tombstone_dir: Optional[str] = None
    children: list["SupervisionChild"] = Field(default_factory=list)

    @property
    def is_supervisor(self) -> bool:
        return True

    def child_names(self) -> list[str]:
        """Declared child order (matters for rest_for_one semantics)."""
        return [c.name for c in self.children]

    def find(self, name: str) -> Optional["SupervisionChild"]:
        """Recursive search by name across the whole subtree."""
        for c in self.children:
            if c.name == name:
                return c
            if isinstance(c, SupervisorNode):
                found = c.find(name)
                if found is not None:
                    return found
        return None

    def parent_of(self, name: str) -> Optional["SupervisorNode"]:
        """Return the SupervisorNode whose ``children`` directly contains
        a child of this name, or None."""
        for c in self.children:
            if c.name == name:
                return self
            if isinstance(c, SupervisorNode):
                found = c.parent_of(name)
                if found is not None:
                    return found
        return None


# Union for children: a child is either a worker or a sub-supervisor.
# YAML discriminates by presence of `strategy` (only supervisors have it).
SupervisionChild = Union[SupervisorNode, WorkerChild]
SupervisorNode.model_rebuild()


def _coerce_child(d: dict) -> SupervisionChild:
    """Pick WorkerChild vs SupervisorNode based on YAML shape."""
    if "strategy" in d:
        return SupervisorNode(
            **{**d, "children": [_coerce_child(c) for c in d.get("children", [])]}
        )
    return WorkerChild(**d)


def load_supervision(path: str | Path) -> SupervisorNode:
    """Load + validate an executor.yaml. Returns the root SupervisorNode.

    Raises:
      FileNotFoundError if path doesn't exist.
      pydantic.ValidationError on schema mismatch.
      ValueError if the YAML root isn't a supervisor (has no strategy).
    """
    raw = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, dict) or "strategy" not in raw:
        raise ValueError(
            f"executor.yaml root must be a supervisor (has 'strategy'); "
            f"got: {type(raw).__name__}"
        )
    return SupervisorNode(
        **{**raw, "children": [_coerce_child(c) for c in raw.get("children", [])]}
    )


# ---------------------------------------------------------------------------
# Strategy semantics — what SHOULD happen when a child crashes.
# ---------------------------------------------------------------------------


def expected_restart_order(
    root: SupervisorNode, crashed_child_name: str
) -> list[str]:
    """Given a supervision tree and the name of a child that just
    crashed, return the ordered list of names that the supervisor's
    strategy says should restart (including the crashed child itself).

    Algorithm:

      1. Find the supervisor that DIRECTLY contains the crashed child
         (its parent_of() result).
      2. Apply that supervisor's strategy:
           - one_for_one: only the crashed child restarts
           - rest_for_one: crashed child + all siblings AFTER it
           - one_for_all: all siblings (in declared order, starting
             from the first)
      3. If the supervisor's restart limit is exceeded by repeated
         crashes, the next layer up applies its own strategy — but
         that's escalation, modeled separately.
    """
    parent = root.parent_of(crashed_child_name)
    if parent is None:
        raise KeyError(
            f"{crashed_child_name!r} not in supervision tree under "
            f"{root.name!r}"
        )
    names = parent.child_names()
    if parent.strategy == "one_for_one":
        return [crashed_child_name]
    if parent.strategy == "rest_for_one":
        idx = names.index(crashed_child_name)
        return names[idx:]
    if parent.strategy == "one_for_all":
        return list(names)
    raise ValueError(f"unknown strategy {parent.strategy!r}")  # pragma: no cover
