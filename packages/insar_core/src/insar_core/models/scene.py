from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional


@dataclass
class AOI:
    """Area of Interest defined as a GeoJSON geometry (Polygon or Feature)."""
    geometry: dict[str, Any]

    @property
    def wkt(self) -> str:
        geom = self.geometry
        if geom.get("type") == "Feature":
            geom = geom["geometry"]
        if geom["type"] != "Polygon":
            raise ValueError(f"Only Polygon geometry is supported, got {geom['type']}")
        coords = geom["coordinates"][0]
        coord_str = ", ".join(f"{lon} {lat}" for lon, lat in coords)
        return f"POLYGON(({coord_str}))"

    @classmethod
    def from_bbox(cls, lon_min: float, lat_min: float, lon_max: float, lat_max: float) -> AOI:
        return cls(geometry={
            "type": "Polygon",
            "coordinates": [[
                [lon_min, lat_min], [lon_max, lat_min],
                [lon_max, lat_max], [lon_min, lat_max],
                [lon_min, lat_min],
            ]],
        })


@dataclass
class SearchParams:
    """Parameters for a Sentinel-1 scene search."""
    aoi: AOI
    date_start: date
    date_end: date
    track_number: Optional[int] = None
    flight_direction: Optional[str] = None  # "ASCENDING" or "DESCENDING"


@dataclass
class SARScene:
    """A single Sentinel-1 SLC acquisition."""
    file_id: str
    granule_name: str       # 67-char name required by HyP3 (strips '-SLC' suffix)
    acquisition_date: date
    orbit: int              # absolute orbit number
    track_number: int       # relative orbit / path number (key for SBAS)
    flight_direction: str   # ASCENDING or DESCENDING
    polarization: str
    size_mb: Optional[float] = None


@dataclass
class TrackSummary:
    """Aggregate stats for a track/flight-direction combination within a search."""
    track_number: int
    flight_direction: str
    scene_count: int
    first_date: date
    last_date: date
