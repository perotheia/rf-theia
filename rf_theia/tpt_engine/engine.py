"""TimeEngine: main TPT execution loop."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from .guards import Guard
from .partition import Partition
from .stimuli import StimulusThread
from .transition import Transition

logger = logging.getLogger(__name__)


class EngineState(Enum):
    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    TIMEOUT = "timeout"
    STOPPED = "stopped"


@dataclass
class PartitionEvent:
    """Record of a partition transition."""
    timestamp: float
    from_partition: str
    to_partition: str
    guard_name: str | None
    elapsed_in_source: float


class TimeEngine:
    """Main TPT execution loop.

    Runs partitions in sequence as guards fire. The evaluation loop
    runs at approximately 10ms resolution using time.monotonic().
    """

    def __init__(
        self,
        evaluation_interval: float = 0.010,
        timeout: float = 60.0,
    ) -> None:
        self.evaluation_interval = evaluation_interval
        self.timeout = timeout
        self.partitions: dict[str, Partition] = {}
        self.current_partition: Partition | None = None
        self.start_time: float = 0.0
        self.state = EngineState.IDLE
        self.history: list[PartitionEvent] = []
        self._stop_event = threading.Event()
        self._stimuli: list[StimulusThread] = []

    def add_partition(self, partition: Partition) -> None:
        """Register a partition in the engine."""
        self.partitions[partition.name] = partition

    def add_transition(
        self,
        source_name: str,
        target_name: str,
        guard: Guard,
        name: str | None = None,
    ) -> None:
        """Add a guarded transition between named partitions."""
        source = self.partitions[source_name]
        target = self.partitions[target_name]
        transition = Transition(target, guard, name)
        source.add_transition(transition)

    def add_stimulus(self, stimulus: StimulusThread) -> None:
        """Register a stimulus thread (started when engine runs)."""
        self._stimuli.append(stimulus)

    def run(self, initial_partition: str) -> None:
        """Execute the scenario starting from the named partition.

        Blocks until a terminal partition is reached, timeout, or stop().
        """
        self.state = EngineState.RUNNING
        self.start_time = time.monotonic()
        self._stop_event.clear()
        self.history.clear()

        # Start all stimuli threads
        for stim in self._stimuli:
            stim.start()

        partition = self.partitions[initial_partition]
        self._enter_partition(partition, from_name="")

        try:
            while self.state == EngineState.RUNNING:
                if self._stop_event.is_set():
                    self.state = EngineState.STOPPED
                    break

                if self.elapsed() > self.timeout:
                    logger.warning("Scenario timeout (%.1fs)", self.timeout)
                    self.state = EngineState.TIMEOUT
                    break

                next_part = self._evaluate_transitions(partition)
                if next_part is not None:
                    elapsed_in = partition.elapsed()
                    guard_name = self._last_guard_name
                    partition.exit()

                    self.history.append(PartitionEvent(
                        timestamp=time.monotonic() - self.start_time,
                        from_partition=partition.name,
                        to_partition=next_part.name,
                        guard_name=guard_name,
                        elapsed_in_source=elapsed_in,
                    ))
                    logger.info(
                        "Transition: %s -> %s (guard=%s, after %.3fs)",
                        partition.name, next_part.name, guard_name, elapsed_in,
                    )

                    self._enter_partition(next_part, from_name=partition.name)
                    partition = next_part
                else:
                    time.sleep(self.evaluation_interval)

        finally:
            # Stop all stimuli
            for stim in self._stimuli:
                stim.stop()
            if partition:
                partition.exit()
            if self.state == EngineState.RUNNING:
                self.state = EngineState.COMPLETED

    def stop(self) -> None:
        """Gracefully stop the engine from another thread."""
        self._stop_event.set()

    def elapsed(self) -> float:
        """Total elapsed time since run() was called."""
        return time.monotonic() - self.start_time

    def get_history(self) -> list[PartitionEvent]:
        """Return the ordered list of partition transitions."""
        return list(self.history)

    def _enter_partition(self, partition: Partition, from_name: str) -> None:
        """Enter a partition and log the initial event."""
        self.current_partition = partition
        partition.enter()
        logger.info("Entered partition: %s", partition.name)
        if not self.history and from_name == "":
            self.history.append(PartitionEvent(
                timestamp=0.0,
                from_partition="",
                to_partition=partition.name,
                guard_name=None,
                elapsed_in_source=0.0,
            ))

    def _evaluate_transitions(self, partition: Partition) -> Partition | None:
        """Evaluate all outbound transitions. Returns next partition or None."""
        self._last_guard_name: str | None = None
        for transition in partition.transitions:
            if transition.evaluate():
                self._last_guard_name = transition.name or getattr(
                    transition.guard, "__name__", None
                )
                return transition.target
        # No transitions and no outbound edges = terminal
        if not partition.transitions:
            self.state = EngineState.COMPLETED
        return None
