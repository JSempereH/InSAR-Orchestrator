"""scenes.py: thin wrappers around asf_service for track/scene discovery.
Had zero endpoint-level coverage before this."""
from datetime import date

from app.services import asf_service
from insar_core.models.scene import SARScene, TrackSummary

_GEOMETRY = {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]}


def test_list_available_tracks_returns_summaries(client, monkeypatch):
    monkeypatch.setattr(asf_service, "available_tracks", lambda **kw: [
        TrackSummary(track_number=110, flight_direction="DESCENDING", scene_count=5,
                     first_date=date(2023, 1, 1), last_date=date(2023, 6, 1)),
    ])

    resp = client.post("/api/scenes/tracks", json={
        "geometry": _GEOMETRY, "date_start": "2023-01-01", "date_end": "2023-06-01",
    })

    assert resp.status_code == 200
    assert resp.json() == [{
        "track_number": 110, "flight_direction": "DESCENDING", "scene_count": 5,
        "first_date": "2023-01-01", "last_date": "2023-06-01",
    }]


def test_search_scenes_returns_deduplicated_scenes(client, monkeypatch):
    monkeypatch.setattr(asf_service, "search_scenes", lambda **kw: [
        SARScene(
            file_id="f1", granule_name="S1_01", acquisition_date=date(2023, 1, 1),
            orbit=1, track_number=110, flight_direction="DESCENDING", polarization="VV", size_mb=1200.0,
        ),
    ])

    resp = client.post("/api/scenes/search", json={
        "geometry": _GEOMETRY, "date_start": "2023-01-01", "date_end": "2023-06-01",
        "track_number": 110, "flight_direction": "DESCENDING",
    })

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["granule_name"] == "S1_01"
    assert body[0]["size_mb"] == 1200.0
