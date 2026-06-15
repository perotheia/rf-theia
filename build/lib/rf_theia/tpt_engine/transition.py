"""Transition: a guarded edge between partitions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .partition import Partition

Guard = Callable[[], bool]


class Transition:
    """A guarded directed edge between two partitions.

    When the guard evaluates to True, the engine exits the source
    partition and enters the target. Guards are evaluated in
    registration order; first to fire wins.
    """

    def __init__(
        self,
        target: Partition,
        guard: Guard,
        name: str | None = None,
    ) -> None:
        self.target = target
        self.guard = guard
        self.name = name

    def evaluate(self) -> bool:
        """Evaluate the guard. Returns True if transition should fire."""
        return self.guard()

    def __repr__(self) -> str:
        label = self.name or "unnamed"
        return f"Transition({label} -> {self.target.name})"
