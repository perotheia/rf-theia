"""Partition: a state in the time-partition state machine."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .transition import Transition

Action = Callable[[], None]


class Partition:
    """A state in the time-partition state machine.

    Each partition represents a phase of the test (e.g., 'Init',
    'Acceleration', 'Cruise') with entry/exit actions and outbound
    transitions.
    """

    def __init__(
        self,
        name: str,
        entry_actions: list[Action] | None = None,
        exit_actions: list[Action] | None = None,
    ) -> None:
        self.name = name
        self.entry_actions: list[Action] = entry_actions or []
        self.exit_actions: list[Action] = exit_actions or []
        self.transitions: list[Transition] = []
        self.entered_at: float | None = None

    def enter(self) -> None:
        """Execute all entry actions and record entry timestamp."""
        self.entered_at = time.monotonic()
        for action in self.entry_actions:
            action()

    def exit(self) -> None:
        """Execute all exit actions."""
        for action in self.exit_actions:
            action()

    def add_transition(self, transition: Transition) -> None:
        """Register an outbound transition from this partition."""
        self.transitions.append(transition)

    def elapsed(self) -> float:
        """Seconds since this partition was entered (monotonic)."""
        if self.entered_at is None:
            return 0.0
        return time.monotonic() - self.entered_at

    def __repr__(self) -> str:
        return f"Partition({self.name!r})"
