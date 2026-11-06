"""EGMS search/download endpoints. All network calls to the real EGMS API
are mocked out via egms_service.get_egms_adapter."""
import time
from pathlib import Path

import pytest

from app.services import egms_download_queue, egms_points, egms_service

_GEOMETRY = {"type": "Polygon", "coordinates": [[[3.0, 49.0], [3.6, 49.0], [3.6, 49.5], [3.0, 49.0]]]}

# A real EGMS L2b download the project owner already has on disk. backend/downloads
# is gitignored (personal data), so this file only exists on machines that ran a
# real download; the test below skips itself where it doesn't, but running it once
# is what confirmed the CSV schema documented at the top of egms_points.py.
_REAL_L2B_ZIP = (
    Path(__file__).resolve().parents[1]
    / "downloads" / "egms" / "comillas" / "EGMS_L2b_001_0237_IW2_VV_2020_2024_1.zip"
)


class _FakeAdapter:
    def __init__(self, hits=None, options=None, fail=False):
        self._hits = hits if hits is not None else [
            {"filename": "EGMS_L3_E40N30_U.zip", "filesize": 1_000_000},
        ]
        self._options = options or ["2019-2023"]
        self.fail = fail
        self.downloaded = []

    def list_options(self, kind):
        return self._options

    def search(self, params):
        from insar_core.models.egms import EGMSProduct
        return [
            EGMSProduct(query_id="q1", filename=h["filename"], level=params.level,
                        size_mb=h.get("filesize", 0) / 1e6)
            for h in self._hits
        ]

    def download(self, product, destination, progress_cb=None):
        if self.fail:
            raise RuntimeError("boom")
        self.downloaded.append(product.filename)
        if progress_cb:
            progress_cb(total_bytes=100, downloaded_bytes=100)


@pytest.fixture(autouse=True)
def _reset_queue():
    """The download queue module holds process-global state; drain it between tests."""
    yield
    egms_download_queue.cancel()
    for _ in range(50):
        if not egms_download_queue.get_state()["active"]:
            break
        time.sleep(0.02)


def test_search_without_credentials_returns_422(client):
    resp = client.post("/api/egms/search", json={
        "geometry": _GEOMETRY, "level": "L3", "release": "2019-2023",
    })
    assert resp.status_code == 422


def test_search_returns_products(client, monkeypatch):
    monkeypatch.setattr(egms_service, "get_egms_adapter", lambda db: _FakeAdapter())

    resp = client.post("/api/egms/search", json={
        "geometry": _GEOMETRY, "level": "L3", "release": "2019-2023", "product_type": "ORTHO-UP",
    })
    assert resp.status_code == 200
    products = resp.json()
    assert len(products) == 1
    assert products[0]["filename"] == "EGMS_L3_E40N30_U.zip"
    assert products[0]["query_id"] == "q1"
    assert products[0]["size_mb"] == 1.0


def test_list_options_proxies_adapter(client, monkeypatch):
    monkeypatch.setattr(egms_service, "get_egms_adapter", lambda db: _FakeAdapter(options=["L2A", "L2B", "L3"]))

    resp = client.get("/api/egms/options/levels")
    assert resp.status_code == 200
    assert resp.json() == ["L2A", "L2B", "L3"]


def test_download_queue_lifecycle(client, monkeypatch):
    fake = _FakeAdapter()
    monkeypatch.setattr(egms_service, "get_egms_adapter", lambda db: fake)

    resp = client.post("/api/egms/downloads/queue", json={
        "products": [{"query_id": "q1", "filename": "product.zip", "level": "L3", "size_mb": 1.0}],
        "destination_name": "Test AOI",
        "geometry": _GEOMETRY, "level": "L3", "release": "2019-2023", "product_type": "ORTHO-UP",
    })
    assert resp.status_code == 200

    state = client.get("/api/egms/downloads/queue").json()
    for _ in range(50):
        if not state["active"]:
            break
        time.sleep(0.02)
        state = client.get("/api/egms/downloads/queue").json()

    assert not state["active"]
    assert state["done"] == 1
    assert fake.downloaded == ["product.zip"]
    assert state["destination"] and state["destination"].endswith("test-aoi")


def test_download_queue_cancel_clears_pending(client, monkeypatch):
    monkeypatch.setattr(egms_service, "get_egms_adapter", lambda db: _FakeAdapter())

    client.post("/api/egms/downloads/queue", json={
        "products": [
            {"query_id": "q1", "filename": "a.zip", "level": "L3", "size_mb": 1.0},
            {"query_id": "q1", "filename": "b.zip", "level": "L3", "size_mb": 1.0},
        ],
        "destination_name": "Test AOI",
        "geometry": _GEOMETRY, "level": "L3", "release": "2019-2023", "product_type": "ORTHO-UP",
    })
    resp = client.delete("/api/egms/downloads/queue")
    assert resp.status_code == 200
    assert resp.json() == {"cancelled": True}


def test_download_creates_inventory_record_and_is_listed(client, monkeypatch):
    monkeypatch.setattr(egms_service, "get_egms_adapter", lambda db: _FakeAdapter())

    resp = client.post("/api/egms/downloads/queue", json={
        "products": [{"query_id": "q1", "filename": "listed-product.zip", "level": "L3", "size_mb": 1.0}],
        "destination_name": "Listed AOI",
        "geometry": _GEOMETRY, "level": "L3", "release": "2019-2023", "product_type": "ORTHO-UP",
    })
    assert resp.status_code == 200

    listed = client.get("/api/egms/downloads").json()
    record = next(d for d in listed if d["filenames"] == ["listed-product.zip"])
    assert record["name"] == "Listed AOI"
    assert record["level"] == "L3"
    assert record["release"] == "2019-2023"
    assert record["geometry"] == _GEOMETRY


def test_delete_download_record(client, monkeypatch):
    monkeypatch.setattr(egms_service, "get_egms_adapter", lambda db: _FakeAdapter())

    client.post("/api/egms/downloads/queue", json={
        "products": [{"query_id": "q1", "filename": "to-delete.zip", "level": "L3", "size_mb": 1.0}],
        "destination_name": "Doomed AOI",
        "geometry": _GEOMETRY, "level": "L3", "release": "2019-2023", "product_type": "ORTHO-UP",
    })
    download_id = next(
        d for d in client.get("/api/egms/downloads").json() if d["filenames"] == ["to-delete.zip"]
    )["id"]

    resp = client.delete(f"/api/egms/downloads/{download_id}")
    assert resp.status_code == 200
    assert all(d["id"] != download_id for d in client.get("/api/egms/downloads").json())


def test_points_accepts_l2b_downloads(client, monkeypatch, tmp_path):
    """L2A/L2B CSVs carry latitude/longitude/mean_velocity too, so the point
    map (the entry point for picking a point to request a time series for)
    must work for them, not just L3."""
    monkeypatch.setattr(egms_service, "get_egms_adapter", lambda db: _FakeAdapter())

    dest_dir = tmp_path / "egms-l2b-points-test"
    dest_dir.mkdir()
    (dest_dir / "points.csv").write_text(
        "pid,latitude,longitude,mean_velocity,20200107,20200113\n"
        "P1,49.1,3.2,-4.5,-0.1,0.3\n"
    )
    monkeypatch.setattr(
        "app.services.egms_download_queue.resolve_destination",
        lambda mountpoint, name: dest_dir,
    )

    client.post("/api/egms/downloads/queue", json={
        "products": [{"query_id": "q1", "filename": "points.csv", "level": "L2B", "size_mb": 0.001}],
        "destination_name": "Test AOI",
        "geometry": _GEOMETRY, "level": "L2B", "release": "2019-2023", "direction": "descending",
    })
    download_id = next(
        d for d in client.get("/api/egms/downloads").json() if d["filenames"] == ["points.csv"]
    )["id"]

    resp = client.get(f"/api/egms/downloads/{download_id}/points")
    assert resp.status_code == 200
    features = resp.json()["features"]
    assert len(features) == 1
    assert features[0]["properties"]["point_id"] == "P1"


def test_points_rejects_unsupported_level(client, monkeypatch):
    monkeypatch.setattr(egms_service, "get_egms_adapter", lambda db: _FakeAdapter())

    client.post("/api/egms/downloads/queue", json={
        "products": [{"query_id": "q1", "filename": "weird-product.zip", "level": "L4", "size_mb": 1.0}],
        "destination_name": "Test AOI",
        "geometry": _GEOMETRY, "level": "L4", "release": "2019-2023",
    })
    download_id = next(
        d for d in client.get("/api/egms/downloads").json() if d["filenames"] == ["weird-product.zip"]
    )["id"]

    resp = client.get(f"/api/egms/downloads/{download_id}/points")
    assert resp.status_code == 400


def test_points_parses_csv_file(client, monkeypatch, tmp_path):
    monkeypatch.setattr(egms_service, "get_egms_adapter", lambda db: _FakeAdapter())

    dest_dir = tmp_path / "egms-points-test"
    dest_dir.mkdir()
    (dest_dir / "points.csv").write_text(
        "pid,latitude,longitude,mean_velocity,mean_velocity_std\n"
        "1,49.1,3.2,-4.5,0.3\n"
        "2,49.2,3.3,2.1,0.4\n"
    )
    monkeypatch.setattr(
        "app.services.egms_download_queue.resolve_destination",
        lambda mountpoint, name: dest_dir,
    )

    client.post("/api/egms/downloads/queue", json={
        "products": [{"query_id": "q1", "filename": "points.csv", "level": "L3", "size_mb": 0.001}],
        "destination_name": "Test AOI",
        "geometry": _GEOMETRY, "level": "L3", "release": "2019-2023", "product_type": "ORTHO-UP",
    })
    download_id = next(
        d for d in client.get("/api/egms/downloads").json() if d["filenames"] == ["points.csv"]
    )["id"]

    for _ in range(50):
        if not client.get("/api/egms/downloads/queue").json()["active"]:
            break
        time.sleep(0.02)

    resp = client.get(f"/api/egms/downloads/{download_id}/points")
    assert resp.status_code == 200
    fc = resp.json()
    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) == 2
    velocities = sorted(f["properties"]["velocity"] for f in fc["features"])
    assert velocities == [-4.5, 2.1]


def test_points_filters_to_the_downloads_own_aoi_bbox(client, monkeypatch, tmp_path):
    """EGMS ships one file per whole swath tile, not clipped to the AOI that
    was searched - a row far outside the AOI the download was made for
    (like a different Sentinel-1 swath entirely) must not show up on the map."""
    monkeypatch.setattr(egms_service, "get_egms_adapter", lambda db: _FakeAdapter())

    dest_dir = tmp_path / "egms-bbox-test"
    dest_dir.mkdir()
    (dest_dir / "points.csv").write_text(
        "pid,latitude,longitude,mean_velocity\n"
        "inside,49.2,3.3,1.0\n"
        "far_away,10.0,-70.0,2.0\n"
    )
    monkeypatch.setattr(
        "app.services.egms_download_queue.resolve_destination",
        lambda mountpoint, name: dest_dir,
    )

    client.post("/api/egms/downloads/queue", json={
        "products": [{"query_id": "q1", "filename": "points.csv", "level": "L3", "size_mb": 0.001}],
        "destination_name": "Bbox test",
        "geometry": _GEOMETRY, "level": "L3", "release": "2019-2023", "product_type": "ORTHO-UP",
    })
    download_id = next(
        d for d in client.get("/api/egms/downloads").json() if d["filenames"] == ["points.csv"]
    )["id"]

    resp = client.get(f"/api/egms/downloads/{download_id}/points")
    assert resp.status_code == 200
    features = resp.json()["features"]
    assert [f["properties"]["point_id"] for f in features] == ["inside"]


def test_extract_points_is_cached_after_first_call(tmp_path):
    """A bbox-filtered scan of a real multi-GB EGMS file can take minutes
    (confirmed against a real download); this is what keeps a second "Show
    points" click for the same download from repeating that scan."""
    dest_dir = tmp_path / "egms-cache-test"
    dest_dir.mkdir()
    (dest_dir / "points.csv").write_text(
        "pid,latitude,longitude,mean_velocity\n"
        "1,49.1,3.2,-4.5\n"
    )

    fc1 = egms_points.extract_points(dest_dir, ["points.csv"])
    assert len(fc1["features"]) == 1
    assert (dest_dir / ".points_cache.json").exists()

    # Add a second row directly on disk - if extract_points reads the cache
    # instead of re-parsing, this must not show up in the next call.
    (dest_dir / "points.csv").write_text(
        "pid,latitude,longitude,mean_velocity\n"
        "1,49.1,3.2,-4.5\n"
        "2,49.2,3.3,2.1\n"
    )
    fc2 = egms_points.extract_points(dest_dir, ["points.csv"])
    assert len(fc2["features"]) == 1


def test_extract_time_series_is_populated_by_extract_points(tmp_path):
    """extract_points does one pass over each row for both the map and the
    time series cache, so a point click never has to re-scan the file."""
    dest_dir = tmp_path / "egms-series-cache-test"
    dest_dir.mkdir()
    (dest_dir / "points.csv").write_text(
        "pid,latitude,longitude,mean_velocity,20200107,20200113\n"
        "P1,49.1,3.2,-4.5,-0.1,0.3\n"
    )

    fc = egms_points.extract_points(dest_dir, ["points.csv"])
    point_id = fc["features"][0]["properties"]["point_id"]
    assert (dest_dir / ".timeseries_cache.json").exists()

    # Corrupt the source file - if extract_time_series were reading the raw
    # CSV instead of the cache extract_points already wrote, this would blow
    # up or return nothing instead of the still-cached series.
    (dest_dir / "points.csv").write_text("not,a,valid,csv,at,all")
    series = egms_points.extract_time_series(dest_dir, ["points.csv"], point_id)
    assert series == {"dates": ["20200107", "20200113"], "values_mm": [-0.1, 0.3]}


def test_extract_points_merges_results_from_multiple_files(tmp_path):
    """A download with more than one zip file processes them concurrently
    (see egms_points._process_files_in_parallel - measured ~1.5x faster on a
    real two-file download). This checks the merge itself: results from every
    file must show up together, regardless of how they were computed."""
    import zipfile

    dest_dir = tmp_path / "egms-multi-file-test"
    dest_dir.mkdir()

    def _make_zip(name: str, pid: str, lon: float, lat: float) -> None:
        csv_name = f"{name}.csv"
        with zipfile.ZipFile(dest_dir / f"{name}.zip", "w") as zf:
            zf.writestr(csv_name, (
                "pid,latitude,longitude,mean_velocity,20200107,20200113\n"
                f"{pid},{lat},{lon},1.0,0.1,0.2\n"
            ))

    _make_zip("file_a", "A1", 3.2, 49.1)
    _make_zip("file_b", "B1", 3.3, 49.2)

    fc = egms_points.extract_points(dest_dir, ["file_a.zip", "file_b.zip"])
    point_ids = sorted(f["properties"]["point_id"] for f in fc["features"])
    assert point_ids == ["A1", "B1"]

    series_a = egms_points.extract_time_series(dest_dir, ["file_a.zip", "file_b.zip"], "A1")
    series_b = egms_points.extract_time_series(dest_dir, ["file_a.zip", "file_b.zip"], "B1")
    assert series_a == {"dates": ["20200107", "20200113"], "values_mm": [0.1, 0.2]}
    assert series_b == {"dates": ["20200107", "20200113"], "values_mm": [0.1, 0.2]}


def test_cache_is_ignored_when_query_parameters_change(tmp_path):
    """A cache written for one (filenames, geometry) pair must never be
    served to a call with different ones - the destination folder is the
    only cache key, so without checking the query itself a stale result from
    an earlier query would be returned silently instead of recomputed."""
    dest_dir = tmp_path / "egms-cache-invalidation-test"
    dest_dir.mkdir()
    (dest_dir / "a.csv").write_text(
        "pid,latitude,longitude,mean_velocity\n1,49.1,3.2,-4.5\n"
    )
    (dest_dir / "b.csv").write_text(
        "pid,latitude,longitude,mean_velocity\n2,49.2,3.3,2.1\n"
    )

    fc_a = egms_points.extract_points(dest_dir, ["a.csv"])
    assert [f["properties"]["point_id"] for f in fc_a["features"]] == ["1"]

    # Same destination folder, different filenames - must recompute, not
    # reuse the cache written for "a.csv" above.
    fc_b = egms_points.extract_points(dest_dir, ["b.csv"])
    assert [f["properties"]["point_id"] for f in fc_b["features"]] == ["2"]


def test_timeseries_rejects_l3_downloads(client, monkeypatch):
    monkeypatch.setattr(egms_service, "get_egms_adapter", lambda db: _FakeAdapter())

    client.post("/api/egms/downloads/queue", json={
        "products": [{"query_id": "q1", "filename": "l3-product.zip", "level": "L3", "size_mb": 1.0}],
        "destination_name": "Test AOI",
        "geometry": _GEOMETRY, "level": "L3", "release": "2019-2023", "product_type": "ORTHO-UP",
    })
    download_id = next(
        d for d in client.get("/api/egms/downloads").json() if d["filenames"] == ["l3-product.zip"]
    )["id"]

    resp = client.get(f"/api/egms/downloads/{download_id}/points/whatever/timeseries")
    assert resp.status_code == 400


def test_timeseries_returns_dates_and_values(client, monkeypatch, tmp_path):
    monkeypatch.setattr(egms_service, "get_egms_adapter", lambda db: _FakeAdapter())

    dest_dir = tmp_path / "egms-timeseries-test"
    dest_dir.mkdir()
    (dest_dir / "points.csv").write_text(
        "pid,latitude,longitude,mean_velocity,20200107,20200113\n"
        "P1,49.1,3.2,-4.5,-0.1,0.3\n"
        "P2,49.2,3.3,2.1,0.2,0.4\n"
    )
    monkeypatch.setattr(
        "app.services.egms_download_queue.resolve_destination",
        lambda mountpoint, name: dest_dir,
    )

    client.post("/api/egms/downloads/queue", json={
        "products": [{"query_id": "q1", "filename": "points.csv", "level": "L2B", "size_mb": 0.001}],
        "destination_name": "Test AOI",
        "geometry": _GEOMETRY, "level": "L2B", "release": "2019-2023", "direction": "descending",
    })
    download_id = next(
        d for d in client.get("/api/egms/downloads").json() if d["filenames"] == ["points.csv"]
    )["id"]

    resp = client.get(f"/api/egms/downloads/{download_id}/points/P1/timeseries")
    assert resp.status_code == 200
    # Two dates are too few for a statistical summary, so it is null.
    assert resp.json() == {"dates": ["20200107", "20200113"], "values_mm": [-0.1, 0.3], "analysis": None}

    resp = client.get(f"/api/egms/downloads/{download_id}/points/does-not-exist/timeseries")
    assert resp.status_code == 404


@pytest.mark.skipif(
    not _REAL_L2B_ZIP.exists(),
    reason="Real EGMS L2b sample only present on machines that ran a real download",
)
def test_extract_functions_against_real_l2b_download():
    """Smoke test against an actual downloaded EGMS L2b product, not a
    hand-written fixture, so a schema change on EGMS's side would be caught
    here instead of only in production.

    This runs against the user's real downloads folder (not a tmp_path),
    since the whole point is testing the real file. It uses only one of the
    (possibly several) real files in that folder, which is a different cache
    key than the app's own real usage (which passes every file) - so it must
    restore whatever cache was already there afterward, not just delete it:
    deleting would just as easily wipe out a real, already-computed cache
    from actual use of the app as it would a stray file left by a previous
    test run."""
    destination = _REAL_L2B_ZIP.parent
    filenames = [_REAL_L2B_ZIP.name]
    cache_paths = [destination / ".points_cache.json", destination / ".timeseries_cache.json"]
    backups = {path: (path.read_bytes() if path.exists() else None) for path in cache_paths}

    try:
        fc = egms_points.extract_points(destination, filenames)
        assert fc["type"] == "FeatureCollection"
        assert len(fc["features"]) > 0
        first = fc["features"][0]
        assert first["geometry"]["type"] == "Point"
        point_id = first["properties"]["point_id"]
        assert point_id

        series = egms_points.extract_time_series(destination, filenames, point_id)
        assert series is not None
        assert len(series["dates"]) == len(series["values_mm"])
        assert len(series["dates"]) > 100  # multi-year, roughly weekly cadence
        assert all(len(d) == 8 and d.isdigit() for d in series["dates"])
        assert series["dates"] == sorted(series["dates"])
    finally:
        for path, original in backups.items():
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(original)


def _make_long_download(client, monkeypatch, tmp_path, rows: int = 30):
    """A queued L2B download whose CSV has `rows` dates with a clear linear trend."""
    monkeypatch.setattr(egms_service, "get_egms_adapter", lambda db: _FakeAdapter())
    dest_dir = tmp_path / "egms-long-series"
    dest_dir.mkdir()
    import datetime as dt
    dates = [(dt.date(2020, 1, 7) + dt.timedelta(days=12 * i)).strftime("%Y%m%d") for i in range(rows)]
    values = [f"{-0.5 * i:.2f}" for i in range(rows)]  # -0.5 mm per 12 days
    (dest_dir / "points.csv").write_text(
        "pid,latitude,longitude,mean_velocity," + ",".join(dates) + "\n"
        "P1,49.1,3.2,-15.0," + ",".join(values) + "\n"
    )
    monkeypatch.setattr("app.services.egms_download_queue.resolve_destination", lambda mountpoint, name: dest_dir)
    client.post("/api/egms/downloads/queue", json={
        "products": [{"query_id": "q1", "filename": "points.csv", "level": "L2B", "size_mb": 0.001}],
        "destination_name": "Long series",
        "geometry": _GEOMETRY, "level": "L2B", "release": "2019-2023", "direction": "descending",
    })
    return next(d for d in client.get("/api/egms/downloads").json() if d["filenames"] == ["points.csv"])["id"], dest_dir


def test_timeseries_includes_statistical_analysis(client, monkeypatch, tmp_path):
    download_id, _ = _make_long_download(client, monkeypatch, tmp_path)

    body = client.get(f"/api/egms/downloads/{download_id}/points/P1/timeseries").json()

    analysis = body["analysis"]
    assert analysis is not None
    assert analysis["n"] == 30 == len(body["dates"])
    assert analysis["model"]["kind"] == "linear"
    assert analysis["velocity"]["ols_mm_yr"] == pytest.approx(-0.5 / (12 / 365.25), rel=0.01)  # about -15 mm/yr
    assert len(analysis["series"]["fit"]) == len(analysis["series"]["dates"]) == 30


def test_series_store_is_served_from_memory(client, monkeypatch, tmp_path):
    """After the first request the parsed series stay in memory: later clicks
    must not read (and parse) the on-disk JSON again."""
    download_id, dest_dir = _make_long_download(client, monkeypatch, tmp_path)
    egms_points._stores.clear()
    assert client.get(f"/api/egms/downloads/{download_id}/points/P1/timeseries").status_code == 200

    def boom(*args, **kwargs):
        raise AssertionError("the series cache file was parsed again")

    monkeypatch.setattr(egms_points, "_read_cache", boom)
    assert client.get(f"/api/egms/downloads/{download_id}/points/P1/timeseries").status_code == 200


def test_series_store_reloads_when_the_cache_file_changes(tmp_path):
    key = egms_points._cache_query_key(["points.csv"], None)
    cache = tmp_path / ".timeseries_cache.json"
    egms_points._write_cache(cache, key, {"A": {"dates": ["20200101"], "values_mm": [1.0]}})
    egms_points._stores.clear()
    assert egms_points._load_store(cache, key).get("A")["values_mm"] == [1.0]

    egms_points._write_cache(cache, key, {"A": {"dates": ["20200101", "20200113"], "values_mm": [1.0, 2.0]}})
    assert egms_points._load_store(cache, key).get("A")["values_mm"] == [1.0, 2.0]
