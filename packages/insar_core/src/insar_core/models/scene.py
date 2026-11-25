from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Optional


@dataclass
class AOI:
    """Area of Interest defined as a GeoJSON geometry (Polygon or Feature)."""
    geometry: dict[str, Any]

    @property
    def geometry_unwrapped(self) -> dict[str, Any]:
        """`geometry`, unwrapped from a GeoJSON Feature if it's wrapped in
        one - the drawn AOI a project stores can be either. Shared so every
        adapter that needs the raw geometry (not just this class's own wkt
        below) unwraps it the same way instead of reimplementing it."""
        geom = self.geometry
        if geom.get("type") == "Feature":
            return geom["geometry"]
        return geom

    @property
    def wkt(self) -> str:
        geom = self.geometry_unwrapped
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
    # Direct ASF data-pool URL for the raw SLC product and its filename.
    # Only needed for downloading the SLC itself (not for HyP3 submission,
    # which only needs granule_name).
    download_url: Optional[str] = None
    file_name: Optional[str] = None
    # Signed perpendicular baseline in metres relative to the first scene of the stack,
    # worked out locally from the orbit state vectors the ASF search already returns.
    # None when ASF did not return enough orbit data for the scene.
    perpendicular_baseline_m: Optional[float] = None
    # For a scene made of bursts (see insar_core.adapters.asf_burst): the identifiers of its bursts, which
    # are the same on every date of a stack. `granule_name` then holds the burst names joined with "+".
    burst_ids: Optional[tuple[str, ...]] = None


@dataclass
class TrackSummary:
    """Aggregate stats for a track/flight-direction combination within a search."""
    track_number: int
    flight_direction: str
    scene_count: int
    first_date: date
    last_date: date
