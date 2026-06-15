"""Stimuli generators: signal waveforms for test scenarios."""

from __future__ import annotations

import csv
import math
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .signals import SignalStore


class StimulusThread(threading.Thread):
    """Base class for threaded stimulus generators."""

    def __init__(self, signal_store: SignalStore, signal_name: str) -> None:
        super().__init__(daemon=True)
        self.signal_store = signal_store
        self.signal_name = signal_name
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    @property
    def stopped(self) -> bool:
        return self._stop_event.is_set()


class ConstantStimulus(StimulusThread):
    """Set a signal to a constant value."""

    def __init__(self, signal_store: SignalStore, signal_name: str, value: float) -> None:
        super().__init__(signal_store, signal_name)
        self.value = value

    def run(self) -> None:
        self.signal_store.set(self.signal_name, self.value)


class RampStimulus(StimulusThread):
    """Ramp a signal from start to end over duration seconds."""

    def __init__(
        self,
        signal_store: SignalStore,
        signal_name: str,
        start: float,
        end: float,
        duration: float,
        steps: int = 100,
    ) -> None:
        super().__init__(signal_store, signal_name)
        self.start_val = start
        self.end_val = end
        self.duration = duration
        self.steps = steps

    def run(self) -> None:
        dt = self.duration / self.steps
        for i in range(self.steps + 1):
            if self.stopped:
                return
            t = i / self.steps
            val = self.start_val + (self.end_val - self.start_val) * t
            self.signal_store.set(self.signal_name, val)
            time.sleep(dt)


class StepStimulus(StimulusThread):
    """Step function: initial value, then target value after delay."""

    def __init__(
        self,
        signal_store: SignalStore,
        signal_name: str,
        initial: float,
        target: float,
        delay: float,
    ) -> None:
        super().__init__(signal_store, signal_name)
        self.initial = initial
        self.target = target
        self.delay = delay

    def run(self) -> None:
        self.signal_store.set(self.signal_name, self.initial)
        t0 = time.monotonic()
        while not self.stopped and (time.monotonic() - t0) < self.delay:
            time.sleep(0.01)
        if not self.stopped:
            self.signal_store.set(self.signal_name, self.target)


class SineStimulus(StimulusThread):
    """Sinusoidal signal: amplitude * sin(2*pi*freq*t) + offset."""

    def __init__(
        self,
        signal_store: SignalStore,
        signal_name: str,
        amplitude: float,
        frequency: float,
        offset: float = 0.0,
        duration: float = 10.0,
        sample_hz: float = 50.0,
    ) -> None:
        super().__init__(signal_store, signal_name)
        self.amplitude = amplitude
        self.frequency = frequency
        self.offset = offset
        self.duration = duration
        self.sample_hz = sample_hz

    def run(self) -> None:
        dt = 1.0 / self.sample_hz
        t0 = time.monotonic()
        while not self.stopped:
            t = time.monotonic() - t0
            if t >= self.duration:
                break
            val = self.amplitude * math.sin(2 * math.pi * self.frequency * t) + self.offset
            self.signal_store.set(self.signal_name, val)
            time.sleep(dt)


class ReplayStimulus(StimulusThread):
    """Replay signal values from a CSV file.

    CSV format: time,value (first row is header).
    Time is relative (seconds from start).
    """

    def __init__(
        self,
        signal_store: SignalStore,
        signal_name: str,
        csv_path: str,
    ) -> None:
        super().__init__(signal_store, signal_name)
        self.csv_path = csv_path

    def run(self) -> None:
        with open(self.csv_path) as f:
            reader = csv.DictReader(f)
            t0 = time.monotonic()
            for row in reader:
                if self.stopped:
                    return
                target_t = float(row["time"])
                value = float(row["value"])
                # Wait until target time
                while not self.stopped:
                    elapsed = time.monotonic() - t0
                    if elapsed >= target_t:
                        break
                    time.sleep(min(0.01, target_t - elapsed))
                if not self.stopped:
                    self.signal_store.set(self.signal_name, value)
