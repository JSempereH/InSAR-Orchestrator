from typing import Optional

from fastapi import APIRouter, Query

from app.schemas import SceneSearchRequest, SceneOut, TrackSummaryOut
from app.services import asf_service

router = APIRouter(prefix="/api/scenes", tags=["scenes"])


@router.post("/tracks", response_model=list[TrackSummaryOut])
def list_available_tracks(body: SceneSearchRequest):
    """Return available Sentinel-1 tracks for an AOI + date range.

    No track/direction filter; returns all tracks so the user can pick one.
    """
    summaries = asf_service.available_tracks(
        geometry=body.geometry,
        date_start=body.date_start,
        date_end=body.date_end,
    )
    return [
        TrackSummaryOut(
            track_number=s.track_number,
            flight_direction=s.flight_direction,
            scene_count=s.scene_count,
            first_date=s.first_date.isoformat(),
            last_date=s.last_date.isoformat(),
        )
        for s in summaries
    ]


@router.post("/search", response_model=list[SceneOut])
def search_scenes(body: SceneSearchRequest):
    """Return deduplicated scenes for an AOI + date range + track."""
    scenes = asf_service.search_scenes(
        geometry=body.geometry,
        date_start=body.date_start,
        date_end=body.date_end,
        track_number=body.track_number,
        flight_direction=body.flight_direction,
    )
    return [
        SceneOut(
            file_id=s.file_id,
            granule_name=s.granule_name,
            acquisition_date=s.acquisition_date.isoformat(),
            orbit=s.orbit,
            track_number=s.track_number,
            flight_direction=s.flight_direction,
            polarization=s.polarization,
            size_mb=s.size_mb,
        )
        for s in scenes
    ]
