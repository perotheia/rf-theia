"""TraceAnalyzer: offline analysis of JSONL traces from trace_collector.

Wraps the existing analyze_trace.py functionality and exposes it as
RF keywords for post-test validation.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd


class TraceAnalyzer:
    """Offline analysis of JSONL traces."""

    def __init__(self, trace_path: str | Path) -> None:
        self.trace_path = Path(trace_path)
        self.events: list[dict] = []
        self.df: pd.DataFrame | None = None
        self._load()

    def _load(self) -> None:
        """Load JSONL trace file."""
        self.events = []
        with open(self.trace_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    self.events.append(json.loads(line))
        self.df = pd.DataFrame(self.events)

    def filter(self, src: str) -> pd.DataFrame:
        """Filter events by source topic."""
        if self.df is None or self.df.empty:
            return pd.DataFrame()
        return self.df[self.df["src"] == src].copy()

    def state_transitions(self) -> list[dict]:
        """Extract system_state transition sequence."""
        states = self.filter("system_state")
        if states.empty:
            return []
        return states[["t", "data"]].to_dict("records")

    def bt_transitions(self) -> pd.DataFrame:
        """Extract BT transition events."""
        return self.filter("bt_transition")

    def ground_truth_drones(self) -> pd.DataFrame:
        """Extract ground truth drone positions."""
        return self.filter("ground_truth")

    def ptz_positions(self) -> pd.DataFrame:
        """Extract PTZ state history."""
        return self.filter("ptz_state")

    def engagement_events(self) -> list[dict]:
        """Extract engagement journal entries."""
        engagements = self.filter("engagement")
        if engagements.empty:
            return []
        return engagements.to_dict("records")

    def duration(self) -> float:
        """Total trace duration in seconds."""
        if self.df is None or self.df.empty:
            return 0.0
        return float(self.df["t"].max() - self.df["t"].min())

    def patrol_coverage(self, total_sectors: int = 30) -> float:
        """Fraction of sectors visited during trace."""
        heatmap = self.filter("sector_heatmap")
        if heatmap.empty:
            return 0.0
        visited = set()
        for _, row in heatmap.iterrows():
            sectors = row.get("sectors", [])
            if isinstance(sectors, list):
                for s in sectors:
                    if isinstance(s, dict) and s.get("age", 999) < 30:
                        visited.add(s.get("id"))
        return len(visited) / total_sectors if total_sectors > 0 else 0.0

    def tracking_lock_fraction(self) -> float:
        """Fraction of time spent in engagement states."""
        states = self.state_transitions()
        if len(states) < 2:
            return 0.0
        total = 0.0
        engaged = 0.0
        for i in range(len(states) - 1):
            dt = states[i + 1]["t"] - states[i]["t"]
            total += dt
            if "FireDirection" in str(states[i].get("data", "")):
                engaged += dt
        return engaged / total if total > 0 else 0.0

    def summary(self) -> dict:
        """Generate summary metrics dict."""
        return {
            "duration_sec": self.duration(),
            "total_events": len(self.events),
            "state_transitions": len(self.state_transitions()),
            "patrol_coverage": self.patrol_coverage(),
            "tracking_lock": self.tracking_lock_fraction(),
            "engagement_count": len(self.engagement_events()),
        }
