"""DroneModel: discrete drone state for test scenarios."""

from __future__ import annotations

from dataclasses import dataclass, field

from .sector_grid import DistanceBand, MotionClass, Visibility


@dataclass
class DroneState:
    """Discrete representation of a drone's state.

    Uses sector-based position instead of continuous coordinates,
    per the RF-TPT-LS Events > Geometry design principle.
    """
    id: str
    sector_az: int  # azimuth sector index
    sector_el: str  # elevation band: LOW/MID/HIGH
    distance: DistanceBand = DistanceBand.FAR
    motion: MotionClass = MotionClass.STATIC
    visibility: Visibility = Visibility.VISIBLE
    health: float = 1.0
    alive: bool = True

    # Continuous positions (from ground truth) for overlap computation
    az_deg: float = 0.0
    el_deg: float = 0.0
    range_m: float = 0.0


@dataclass
class PTZState:
    """Current PTZ camera state."""
    pan_deg: float = 0.0
    tilt_deg: float = 0.0
    zoom: float = 1.0
    sector_id: int = -1  # resolved sector from grid


class WorldModel:
    """Centralized test-side world state.

    Holds drones, PTZ state, and provides the state needed for
    the OverlapEngine to compute discrete events.
    """

    def __init__(self) -> None:
        self.drones: dict[str, DroneState] = {}
        self.ptz = PTZState()

    def add_drone(self, drone: DroneState) -> None:
        self.drones[drone.id] = drone

    def remove_drone(self, drone_id: str) -> None:
        self.drones.pop(drone_id, None)

    def get_drone(self, drone_id: str) -> DroneState | None:
        return self.drones.get(drone_id)

    def set_ptz(self, pan: float, tilt: float, zoom: float = 1.0) -> None:
        self.ptz.pan_deg = pan
        self.ptz.tilt_deg = tilt
        self.ptz.zoom = zoom

    def __repr__(self) -> str:
        return f"WorldModel(drones={len(self.drones)}, ptz=({self.ptz.pan_deg:.1f}, {self.ptz.tilt_deg:.1f}))"
