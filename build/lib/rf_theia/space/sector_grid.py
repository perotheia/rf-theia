"""SectorGrid: PTZ space discretization into azimuth/elevation sectors.

Mirrors the C++ sector_grid.hpp concept from fire_director but in Python
for test-side assertions and overlap computation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ElevationBand(Enum):
    LOW = "LOW"
    MID = "MID"
    HIGH = "HIGH"


class DistanceBand(Enum):
    NEAR = "NEAR"
    MID = "MID"
    FAR = "FAR"


class MotionClass(Enum):
    STATIC = "STATIC"
    SLOW = "SLOW"
    FAST = "FAST"


class Visibility(Enum):
    VISIBLE = "VISIBLE"
    OCCLUDED = "OCCLUDED"


@dataclass
class SectorConfig:
    """Configuration for sector grid layout."""
    az_cols: int = 15        # number of azimuth sectors
    el_rows: int = 2         # number of elevation sectors
    az_min: float = -180.0
    az_max: float = 180.0
    el_min: float = -10.0
    el_max: float = 30.0


@dataclass
class Sector:
    """A single sector in the grid."""
    id: int
    center_az: float
    center_el: float
    az_min: float
    az_max: float
    el_min: float
    el_max: float


class SectorGrid:
    """PTZ space discretization.

    Divides azimuth/elevation space into a cols x rows grid.
    Default 15x2 = 30 sectors matches the C++ PatrolGrid.
    """

    def __init__(self, config: SectorConfig | None = None) -> None:
        self.config = config or SectorConfig()
        self.sectors: list[Sector] = []
        self.cols = self.config.az_cols
        self.rows = self.config.el_rows
        self._build()

    def _build(self) -> None:
        c = self.config
        az_step = (c.az_max - c.az_min) / c.az_cols
        el_step = (c.el_max - c.el_min) / c.el_rows

        self.sectors.clear()
        idx = 0
        for row in range(c.el_rows):
            for col in range(c.az_cols):
                az_lo = c.az_min + az_step * col
                az_hi = az_lo + az_step
                el_lo = c.el_min + el_step * row
                el_hi = el_lo + el_step
                self.sectors.append(Sector(
                    id=idx,
                    center_az=(az_lo + az_hi) / 2.0,
                    center_el=(el_lo + el_hi) / 2.0,
                    az_min=az_lo,
                    az_max=az_hi,
                    el_min=el_lo,
                    el_max=el_hi,
                ))
                idx += 1

    def sector_at(self, az: float, el: float) -> int:
        """Find sector containing (az, el). Returns -1 if out of range."""
        c = self.config
        if az < c.az_min or az >= c.az_max or el < c.el_min or el >= c.el_max:
            return -1
        az_step = (c.az_max - c.az_min) / c.az_cols
        el_step = (c.el_max - c.el_min) / c.el_rows
        col = min(int((az - c.az_min) / az_step), c.az_cols - 1)
        row = min(int((el - c.el_min) / el_step), c.el_rows - 1)
        return row * c.az_cols + col

    def num_sectors(self) -> int:
        return len(self.sectors)

    def get_sector(self, sector_id: int) -> Sector:
        return self.sectors[sector_id]

    def __repr__(self) -> str:
        return f"SectorGrid({self.cols}x{self.rows}={len(self.sectors)} sectors)"
