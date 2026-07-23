from __future__ import annotations

from datetime import date
from typing import List, Optional

from insar_core.adapters.asf import ASFAdapter
from insar_core.models.scene import AOI, SearchParams, SARScene, TrackSummary

_adapter = ASFAdapter()


def search_scenes(
    geometry: dict,
    date_start: str,
    date_end: str,
    track_number: Optional[int] = None,
    flight_direction: Optional[str] = None,
) -> List[SARScene]:
    params = SearchParams(
        aoi=AOI(geometry=geometry),
        date_start=date.fromisoformat(date_start),
        date_end=date.fromisoformat(date_end),
        track_number=track_number,
        flight_direction=flight_direction,
    )
    return _adapter.search(params)


def available_tracks(
    geometry: dict,
    date_start: str,
    date_end: str,
) -> List[TrackSummary]:
    params = SearchParams(
        aoi=AOI(geometry=geometry),
        date_start=date.fromisoformat(date_start),
        date_end=date.fromisoformat(date_end),
    )
    return _adapter.available_tracks(params)
