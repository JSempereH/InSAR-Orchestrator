"""MintPy time series endpoint. Run/status/cancel already exercise the
subprocess side (not covered here); this focuses on reading back a finished
run's HDF5 output, since that's the part that had no coverage at all."""
from pathlib import Path

import h5py
import numpy as np
import pytest


def _create_project(client, name="MintPy Test Project"):
    return client.post("/api/projects", json={
        "name": name,
        "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]},
        "date_start": "2023-01-01",
        "date_end": "2023-06-01",
    }).json()


def _write_geocoded_timeseries(storage_path: str):
    work_dir = Path(storage_path) / "mintpy"
    work_dir.mkdir(parents=True, exist_ok=True)
    with h5py.File(work_dir / "timeseries.h5", "w") as f:
        f.attrs["X_FIRST"] = 0.0
        f.attrs["Y_FIRST"] = 1.0
        f.attrs["X_STEP"] = 0.5
        f.attrs["Y_STEP"] = -0.5
        f.attrs["WIDTH"] = 3
        f.attrs["LENGTH"] = 3
        f.create_dataset("date", data=[b"20230101", b"20230107"])
        data = np.zeros((2, 3, 3), dtype="float32")
        data[:, 0, 0] = [0.0, 0.005]  # 0, 5 mm at (lat=1.0, lon=0.0)
        f.create_dataset("timeseries", data=data)


def test_timeseries_for_unknown_project_returns_404(client):
    resp = client.get("/api/projects/does-not-exist/mintpy/timeseries", params={"lat": 1.0, "lon": 0.0})
    assert resp.status_code == 404


def test_timeseries_without_a_run_returns_422(client):
    project = _create_project(client)

    resp = client.get(f"/api/projects/{project['id']}/mintpy/timeseries", params={"lat": 1.0, "lon": 0.0})
    assert resp.status_code == 422


def test_timeseries_returns_dates_and_values_mm(client):
    project = _create_project(client)
    _write_geocoded_timeseries(project["storage_path"])

    resp = client.get(f"/api/projects/{project['id']}/mintpy/timeseries", params={"lat": 1.0, "lon": 0.0})
    assert resp.status_code == 200
    body = resp.json()
    assert body["dates"] == ["20230101", "20230107"]
    # float32 storage in the HDF5 means the *1000 conversion isn't exact.
    assert body["values_mm"] == pytest.approx([0.0, 5.0])


def test_timeseries_outside_processed_area_returns_422(client):
    project = _create_project(client)
    _write_geocoded_timeseries(project["storage_path"])

    resp = client.get(f"/api/projects/{project['id']}/mintpy/timeseries", params={"lat": 90.0, "lon": 90.0})
    assert resp.status_code == 422


def _write_geocoded_velocity(storage_path: str):
    work_dir = Path(storage_path) / "mintpy"
    work_dir.mkdir(parents=True, exist_ok=True)
    with h5py.File(work_dir / "velocity.h5", "w") as f:
        f.attrs["X_FIRST"] = 0.0
        f.attrs["Y_FIRST"] = 1.0
        f.attrs["X_STEP"] = 0.5
        f.attrs["Y_STEP"] = -0.5
        f.attrs["WIDTH"] = 3
        f.attrs["LENGTH"] = 3
        data = np.full((3, 3), 0.002, dtype="float32")
        data[2, 2] = np.nan
        f.create_dataset("velocity", data=data)


def test_points_for_unknown_project_returns_404(client):
    resp = client.get("/api/projects/does-not-exist/mintpy/points")
    assert resp.status_code == 404


def test_points_without_a_run_returns_422(client):
    project = _create_project(client)

    resp = client.get(f"/api/projects/{project['id']}/mintpy/points")
    assert resp.status_code == 422


def test_points_returns_geojson_skipping_masked_pixels(client):
    project = _create_project(client)
    _write_geocoded_velocity(project["storage_path"])

    resp = client.get(f"/api/projects/{project['id']}/mintpy/points")
    assert resp.status_code == 200
    fc = resp.json()
    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) == 8  # 3x3 grid, one NaN pixel masked out
    assert all(f["properties"]["velocity"] == pytest.approx(2.0) for f in fc["features"])


def test_status_reports_disk_usage_before_any_run(client):
    # Distinct name: storage_path is derived from the project name, and a
    # shared name would leak other tests' leftover files into this test's
    # exact byte-count assertions.
    project = _create_project(client, name="Disk Usage Before Run")
    storage = Path(project["storage_path"])
    storage.mkdir(parents=True, exist_ok=True)
    (storage / "scene_a.zip").write_bytes(b"0" * 1000)
    (storage / "scene_b.zip").write_bytes(b"0" * 500)

    resp = client.get(f"/api/projects/{project['id']}/mintpy/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "NOT_STARTED"
    assert body["disk_usage"]["zips_bytes"] == 1500
    assert body["disk_usage"]["mintpy_output_bytes"] == 0
    assert body["disk_usage"]["total_bytes"] == 1500


def test_status_reports_disk_usage_breakdown_across_zips_extracted_and_output(client):
    project = _create_project(client, name="Disk Usage Breakdown")
    storage = Path(project["storage_path"])
    storage.mkdir(parents=True, exist_ok=True)
    (storage / "scene_a.zip").write_bytes(b"0" * 200)

    extracted = storage / "scene_a"
    extracted.mkdir()
    (extracted / "scene_a_unw_phase.tif").write_bytes(b"0" * 300)

    _write_geocoded_velocity(project["storage_path"])  # lands under storage/mintpy/velocity.h5

    resp = client.get(f"/api/projects/{project['id']}/mintpy/status")
    assert resp.status_code == 200
    disk_usage = resp.json()["disk_usage"]
    assert disk_usage["zips_bytes"] == 200
    assert disk_usage["extracted_bytes"] == 300
    assert disk_usage["mintpy_output_bytes"] > 0
    assert disk_usage["total_bytes"] == (
        disk_usage["zips_bytes"] + disk_usage["extracted_bytes"] + disk_usage["mintpy_output_bytes"]
    )


def test_get_disk_usage_is_cached_within_ttl(tmp_path):
    from app.services import mintpy_service

    downloads_dir = tmp_path / "cache-test-project"
    downloads_dir.mkdir()
    (downloads_dir / "a.zip").write_bytes(b"0" * 100)

    first = mintpy_service.get_disk_usage(downloads_dir)
    assert first["zips_bytes"] == 100

    (downloads_dir / "b.zip").write_bytes(b"0" * 900)  # would change the real total
    second = mintpy_service.get_disk_usage(downloads_dir)
    assert second["zips_bytes"] == 100  # still the cached value, new file not yet reflected

    mintpy_service._disk_usage_cache.pop(str(downloads_dir), None)  # force expiry
    third = mintpy_service.get_disk_usage(downloads_dir)
    assert third["zips_bytes"] == 1000  # recomputed for real once the cache entry is gone


def test_status_for_unknown_project_returns_404(client):
    resp = client.get("/api/projects/does-not-exist/mintpy/status")
    assert resp.status_code == 404
