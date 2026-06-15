"""TPT Engine: Time-partition state machine with guards, signals, and stimuli."""

from .engine import EngineState, PartitionEvent, TimeEngine
from .guards import (
    Guard,
    any_guard,
    combined_guard,
    event_guard,
    signal_guard,
    time_guard,
)
from .partition import Action, Partition
from .signals import EventStore, SignalRecord, SignalStore
from .stimuli import (
    ConstantStimulus,
    RampStimulus,
    ReplayStimulus,
    SineStimulus,
    StepStimulus,
    StimulusThread,
)
from .transition import Transition

__all__ = [
    "Action",
    "ConstantStimulus",
    "EngineState",
    "EventStore",
    "Guard",
    "Partition",
    "PartitionEvent",
    "RampStimulus",
    "ReplayStimulus",
    "SignalRecord",
    "SignalStore",
    "SineStimulus",
    "StepStimulus",
    "StimulusThread",
    "TimeEngine",
    "Transition",
    "any_guard",
    "combined_guard",
    "event_guard",
    "signal_guard",
    "time_guard",
]
