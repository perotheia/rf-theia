"""OverlapEngine: converts PTZ + Drone state into discrete events.

The key abstraction: replaces continuous geometry with discrete events
(PTZ_FOV_OVERLAP, PTZ_LOST, LASER_ALIGNMENT_OK, LASER_DAMAGE_APPLIED).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .drone_model import DroneState, WorldModel
from .sector_grid import SectorGrid, Visibility

if TYPE_CHECKING:
    from ..tpt_engine.signals import EventStore

logger = logging.getLogger(__name__)

# FOV half-width at different zoom levels (approximate degrees)
_FOV_HALF_WIDTH = {1.0: 45.0, 2.0: 22.5, 4.0: 11.25, 8.0: 5.6}


def _fov_half_width(zoom: float) -> float:
    """Interpolate FOV half-width for zoom level."""
    if zoom <= 1.0:
        return 45.0
    if zoom >= 8.0:
        return 5.6
    # Linear interpolation in log space
    return 90.0 / (2.0 * zoom)


class OverlapEngine:
    """Compute discrete overlap events from continuous state.

    On each update() call, compares PTZ pointing direction against
    drone positions and emits events when overlap state changes.
    """

    def __init__(
        self,
        world: WorldModel,
        events: EventStore,
        grid: SectorGrid,
        alignment_threshold_deg: float = 2.0,
    ) -> None:
        self.world = world
        self.events = events
        self.grid = grid
        self.alignment_threshold = alignment_threshold_deg
        self._prev_overlap: dict[str, bool] = {}
        self._prev_aligned: dict[str, bool] = {}

    def update(self) -> None:
        """Evaluate overlap state for all drones and emit events."""
        ptz = self.world.ptz
        ptz.sector_id = self.grid.sector_at(ptz.pan_deg, ptz.tilt_deg)

        for drone in self.world.drones.values():
            if not drone.alive:
                continue

            in_fov = self._in_fov(ptz.pan_deg, ptz.tilt_deg, ptz.zoom, drone)
            was_in_fov = self._prev_overlap.get(drone.id, False)

            # FOV overlap transitions
            if in_fov and not was_in_fov:
                self.events.emit("PTZ_FOV_OVERLAP", {"target": drone.id})
                logger.debug("PTZ_FOV_OVERLAP: %s", drone.id)

            if not in_fov and was_in_fov:
                self.events.emit("PTZ_LOST", {"target": drone.id})
                logger.debug("PTZ_LOST: %s", drone.id)

            self._prev_overlap[drone.id] = in_fov

            # Laser alignment (tighter threshold)
            if in_fov:
                aligned = self._laser_aligned(ptz.pan_deg, ptz.tilt_deg, drone)
                was_aligned = self._prev_aligned.get(drone.id, False)

                if aligned and not was_aligned:
                    self.events.emit("LASER_ALIGNMENT_OK", {"target": drone.id})
                    logger.debug("LASER_ALIGNMENT_OK: %s", drone.id)

                self._prev_aligned[drone.id] = aligned
            else:
                self._prev_aligned[drone.id] = False

    def _in_fov(self, ptz_az: float, ptz_el: float, zoom: float, drone: DroneState) -> bool:
        """Check if drone is within PTZ field of view."""
        if drone.visibility != Visibility.VISIBLE:
            return False
        half = _fov_half_width(zoom)
        daz = abs(ptz_az - drone.az_deg)
        if daz > 180:
            daz = 360 - daz  # wrap-around
        del_ = abs(ptz_el - drone.el_deg)
        return daz <= half and del_ <= half

    def _laser_aligned(self, ptz_az: float, ptz_el: float, drone: DroneState) -> bool:
        """Check if laser is aligned within threshold."""
        daz = abs(ptz_az - drone.az_deg)
        if daz > 180:
            daz = 360 - daz
        del_ = abs(ptz_el - drone.el_deg)
        return daz <= self.alignment_threshold and del_ <= self.alignment_threshold

    def reset(self) -> None:
        """Clear overlap tracking state."""
        self._prev_overlap.clear()
        self._prev_aligned.clear()
