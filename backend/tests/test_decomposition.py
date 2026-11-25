"""Vertical and east-west decomposition through the API: products, refusals, checks against EGMS L3 and GNSS."""
import datetime as dt
import time
from pathlib import Path

import h5py
import numpy as np
import pyproj
import pytest

from app.database import SessionLocal
from app.models import EGMSDownload
from insar_core.analysis.validation import los_unit_vector

LENGTH, WIDTH = 40, 50
X0, Y0 = 600000.0, 4170000.0
INC = {"ASCENDING": (38.0, 100.0), "DESCENDING": (38.4, -100.0)}
DATES = [(dt.date(2023, 1, 3) + dt.timedelta(days=12 * k)).strftime("%Y%m%d") for k in range(10)]


def _truth():
    rows, cols = np.mgrid[0:LENGTH, 0:WIDTH].astype(float)
    vertical = -30.0 * np.exp(-(((rows - 20) / 8.0) ** 2 + ((cols - 25) / 10.0) ** 2))
    east = 3.0 * (cols - 25) / 25.0
    return vertical, east


def _project(client, name, direction):
    return client.post("/api/projects", json={
        "name": name, "geometry": {"type": "Polygon", "coordinates": [[[-1.9, 37.6], [-1.9, 37.7], [-1.8, 37.7], [-1.8, 37.6], [-1.9, 37.6]]]},
        "flight_direction": direction, "date_start": "2023-01-01", "date_end": "2023-12-31",
    }).json()


def _write_mintpy(storage_path, direction, vertical, east, offset=0.0, with_geometry=True):
    work = Path(storage_path) / "mintpy"
    (work / "inputs").mkdir(parents=True, exist_ok=True)
    incidence, azimuth = INC[direction]
    w = los_unit_vector(incidence, azimuth)
    los_mm = w[0] * east + w[2] * vertical + offset
    attrs = {"X_FIRST": X0, "Y_FIRST": Y0, "X_STEP": 80.0, "Y_STEP": -80.0, "WIDTH": WIDTH, "LENGTH": LENGTH, "EPSG": "32630",
             "REF_Y": 4, "REF_X": 4, "REF_DATE": DATES[0], "ORBIT_DIRECTION": direction, "START_DATE": DATES[0], "END_DATE": DATES[-1],
             "HEADING": "-12.0" if direction == "ASCENDING" else "-168.0"}
    with h5py.File(work / "velocity.h5", "w") as handle:
        handle.create_dataset("velocity", data=(los_mm / 1000.0).astype("float32"))
        handle.create_dataset("velocityStd", data=np.full((LENGTH, WIDTH), 0.0008, "float32"))
        handle.attrs.update({**attrs, "FILE_PATH": str(work / "timeseries.h5")})
    with h5py.File(work / "maskTempCoh.h5", "w") as handle:
        handle.create_dataset("mask", data=np.ones((LENGTH, WIDTH), bool))
    if with_geometry:
        with h5py.File(work / "inputs" / "geometryGeo.h5", "w") as handle:
            handle.create_dataset("incidenceAngle", data=np.full((LENGTH, WIDTH), incidence, "float32"))
            handle.create_dataset("azimuthAngle", data=np.full((LENGTH, WIDTH), azimuth, "float32"))
    return work


@pytest.fixture()
def pair(client):
    vertical, east = _truth()
    asc, desc = _project(client, "Asc", "ASCENDING"), _project(client, "Desc", "DESCENDING")
    _write_mintpy(asc["storage_path"], "ASCENDING", vertical, east, offset=2.0)
    _write_mintpy(desc["storage_path"], "DESCENDING", vertical, east, offset=-1.0)
    return asc, desc, vertical, east


def _create(client, first, second, **extra):
    return client.post("/api/decompositions", json={"first_project_id": first["id"], "second_project_id": second["id"], **extra})


def test_two_projects_of_opposite_directions_give_vertical_and_east(client, pair, tmp_path):
    asc, desc, vertical, east = pair
    response = _create(client, asc, desc)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["ascending"]["name"] == "Asc" and body["descending"]["name"] == "Desc"
    assert body["name"] == "Asc + Desc"
    assert body["summary"]["pixels"] > 0.9 * LENGTH * WIDTH
    assert "north motion is zero" in body["summary"]["assumptions"]
    assert body["stale"] is False and body["products_available"] is True
    assert body["checks"]["l3"]["status"] == "NOT_RUN" and body["checks"]["gnss"]["status"] == "NOT_RUN"

    import rasterio
    tif = client.get(f"/api/decompositions/{body['id']}/export?layer=vertical")
    assert tif.status_code == 200
    (tmp_path / "vertical.tif").write_bytes(tif.content)
    with rasterio.open(tmp_path / "vertical.tif") as src:
        data = src.read(1)
    reference = vertical[2:7, 2:7].mean()
    assert np.nanmax(np.abs(data - (vertical - reference))) < 0.2


def test_the_order_of_the_two_projects_does_not_matter(client, pair):
    asc, desc, _, _ = pair
    one, other = _create(client, asc, desc).json(), _create(client, desc, asc).json()
    assert one["ascending"]["id"] == other["ascending"]["id"] == asc["id"]
    assert one["descending"]["id"] == other["descending"]["id"] == desc["id"]


def test_two_projects_of_the_same_direction_are_refused_and_nothing_is_left(client):
    vertical, east = _truth()
    a, b = _project(client, "A1", "ASCENDING"), _project(client, "A2", "ASCENDING")
    for p in (a, b):
        _write_mintpy(p["storage_path"], "ASCENDING", vertical, east)
    before = client.get("/api/decompositions").json()
    response = _create(client, a, b)
    assert response.status_code == 422 and "one ascending and one descending" in response.json()["detail"]
    assert client.get("/api/decompositions").json() == before


def test_a_project_without_mintpy_results_is_refused_with_the_reason(client, pair):
    asc, _, _, _ = pair
    empty = _project(client, "Empty", "DESCENDING")
    response = _create(client, asc, empty)
    assert response.status_code == 422 and "run MintPy" in response.json()["detail"]


def test_the_same_project_twice_and_unknown_projects_are_refused(client, pair):
    asc, desc, _, _ = pair
    assert _create(client, asc, asc).status_code == 422
    assert client.post("/api/decompositions", json={"first_project_id": asc["id"], "second_project_id": "nope"}).status_code == 404


def test_a_reference_point_is_used_and_recorded(client, pair):
    asc, desc, _, _ = pair
    to_wgs = pyproj.Transformer.from_crs(32630, 4326, always_xy=True)
    lon, lat = to_wgs.transform(X0 + 30.5 * 80.0, Y0 - 30.5 * 80.0)
    body = _create(client, asc, desc, reference={"lat": lat, "lon": lon}).json()
    assert body["reference"]["lat"] == pytest.approx(lat)
    assert body["summary"]["reference"]["row"] == 30 and body["summary"]["reference"]["column"] == 30


def test_listing_getting_and_deleting(client, pair):
    asc, desc, _, _ = pair
    created = _create(client, asc, desc).json()
    assert created["id"] in [d["id"] for d in client.get("/api/decompositions").json()]
    assert client.get(f"/api/decompositions/{created['id']}").json()["name"] == created["name"]
    assert client.delete(f"/api/decompositions/{created['id']}").status_code == 204
    assert client.get(f"/api/decompositions/{created['id']}").status_code == 404
    assert created["id"] not in [d["id"] for d in client.get("/api/decompositions").json()]


def test_the_preview_is_a_png(client, pair):
    asc, desc, _, _ = pair
    created = _create(client, asc, desc).json()
    response = client.get(f"/api/decompositions/{created['id']}/preview.png")
    assert response.status_code == 200 and response.headers["content-type"] == "image/png"
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_the_result_is_stale_once_a_project_is_processed_again(client, pair):
    asc, desc, vertical, east = pair
    created = _create(client, asc, desc).json()
    time.sleep(1.1)
    _write_mintpy(asc["storage_path"], "ASCENDING", vertical, east)
    assert client.get(f"/api/decompositions/{created['id']}").json()["stale"] is True


def test_calibrated_layers_are_refused_until_the_checks_justify_them(client, pair):
    asc, desc, _, _ = pair
    created = _create(client, asc, desc).json()
    for layer, word in (("vertical_calibrated", "GNSS"), ("east_std_calibrated", "EGMS L3")):
        response = client.get(f"/api/decompositions/{created['id']}/export?layer={layer}")
        assert response.status_code == 422 and word in response.json()["detail"]
    assert client.get(f"/api/decompositions/{created['id']}/export?layer=nope").status_code == 422


# ── EGMS L3 ──────────────────────────────────────────────────────────────────

def _write_l3(folder, truth, product_type, name):
    """An EGMS L3 tile (zip with a GeoTIFF) over the test area, holding `truth` (a function of UTM position)."""
    import rasterio
    import zipfile
    from rasterio.transform import from_origin
    to_laea = pyproj.Transformer.from_crs(32630, 3035, always_xy=True)
    to_utm = pyproj.Transformer.from_crs(3035, 32630, always_xy=True)
    x, y = to_laea.transform(X0 + 25 * 80.0, Y0 - 20 * 80.0)
    left, top = np.floor(x / 100000.0) * 100000.0, np.ceil(y / 100000.0) * 100000.0
    columns, rows = np.meshgrid(np.arange(1000), np.arange(1000))
    ux, uy = to_utm.transform(left + (columns + 0.5) * 100.0, top - (rows + 0.5) * 100.0)
    values = truth(ux, uy).astype("float32")
    folder.mkdir(parents=True, exist_ok=True)
    tiff = folder / f"{name}.tiff"
    with rasterio.open(tiff, "w", driver="GTiff", height=1000, width=1000, count=1, dtype="float32", crs="EPSG:3035",
                       transform=from_origin(left, top, 100.0, 100.0), nodata=-9999.0) as out:
        out.write(values, 1)
    with zipfile.ZipFile(folder / f"{name}.zip", "w") as z:
        z.write(tiff, f"{name}.tiff")
    tiff.unlink()
    db = SessionLocal()
    try:
        row = EGMSDownload(name=name, geometry={}, level="L3", release="2020-2024", product_type=product_type,
                           destination_path=str(folder), filenames=[f"{name}.zip"])
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _field(kind):
    vertical, east = _truth()

    def at(x, y):
        columns, rows = (x - X0) / 80.0 - 0.5, (Y0 - y) / 80.0 - 0.5
        if kind == "up":
            return -30.0 * np.exp(-(((rows - 20) / 8.0) ** 2 + ((columns - 25) / 10.0) ** 2))
        return 3.0 * (columns - 25) / 25.0
    return at


def test_the_maps_are_checked_against_egms_l3_and_the_error_is_judged(client, pair, tmp_path):
    asc, desc, _, _ = pair
    created = _create(client, asc, desc).json()
    up = _write_l3(tmp_path / "up", _field("up"), "ORTHO-UP", "EGMS_L3_E32N16_100km_U_2020_2024_1")
    east = _write_l3(tmp_path / "east", _field("east"), "ORTHO-EAST", "EGMS_L3_E32N16_100km_E_2020_2024_1")
    response = client.post(f"/api/decompositions/{created['id']}/validation/l3", json={"up_download_id": up, "east_download_id": east})
    assert response.status_code == 200, response.text
    result = response.json()["checks"]["l3"]["result"]
    assert result["vertical"]["velocity_rereferenced"]["pearson_r"] > 0.98
    assert result["vertical"]["velocity_rereferenced"]["slope_b_on_a"] == pytest.approx(1.0, abs=0.08)
    assert "uncertainty" in result["vertical"] and "window_note" in result
    # with a measured factor the calibrated error layer can now be written
    layer = client.get(f"/api/decompositions/{created['id']}/export?layer=vertical_std_calibrated")
    assert layer.status_code == 200 and layer.content[:4] in (b"II*\x00", b"MM\x00*")


def test_l3_downloads_of_the_wrong_kind_are_refused(client, pair, tmp_path):
    asc, desc, _, _ = pair
    created = _create(client, asc, desc).json()
    up = _write_l3(tmp_path / "up", _field("up"), "ORTHO-UP", "EGMS_L3_E32N16_100km_U_2020_2024_1")
    response = client.post(f"/api/decompositions/{created['id']}/validation/l3", json={"up_download_id": up, "east_download_id": up})
    assert response.status_code == 422 and "ORTHO-EAST" in response.json()["detail"]


# ── GNSS ─────────────────────────────────────────────────────────────────────

def _fake_ngl(monkeypatch, vertical_velocities):
    """Stations at known places on the map with daily series whose up velocity is given."""
    from insar_core.adapters import ngl
    to_wgs = pyproj.Transformer.from_crs(32630, 4326, always_xy=True)
    places = [(10, 10), (20, 25), (30, 40)]
    listed, series = [], {}
    for index, ((row, column), up) in enumerate(zip(places, vertical_velocities, strict=False)):
        lon, lat = to_wgs.transform(X0 + (column + 0.5) * 80.0, Y0 - (row + 0.5) * 80.0)
        name = f"ST{index}"
        listed.append({"name": name, "lat": lat, "lon": lon})
        first = dt.date(2023, 1, 1)
        rng = np.random.default_rng(index)
        series[name] = {(first + dt.timedelta(days=i)).strftime("%Y%m%d"): (20.0 * i / 365.25, 15.0 * i / 365.25,
                                                                          up * i / 365.25 + rng.normal(0, 1.0)) for i in range(365)}
    monkeypatch.setattr(ngl, "stations_in_area", lambda bbox, first, last: listed)
    monkeypatch.setattr(ngl, "fetch_series", lambda name: ("IGS20", series[name]))


def _wait(client, decomposition_id, kind="gnss", seconds=30):
    for _ in range(seconds * 10):
        check = client.get(f"/api/decompositions/{decomposition_id}").json()["checks"][kind]
        if check["status"] != "RUNNING":
            return check
        time.sleep(0.1)
    raise AssertionError("the check did not finish")


def test_gnss_stations_calibrate_the_vertical_map_and_the_shift_is_tested(client, pair, monkeypatch):
    asc, desc, vertical, _ = pair
    created = _create(client, asc, desc).json()
    # the map is relative to its reference, which sits at the truth of pixels (2..6, 2..6): about -0.0
    reference = vertical[2:7, 2:7].mean()
    stations = [vertical[10, 10], vertical[20, 25], vertical[30, 40]]
    _fake_ngl(monkeypatch, [v - reference + 3.0 for v in stations])        # the true motion is 3 mm/year above the map
    assert client.post(f"/api/decompositions/{created['id']}/validation/gnss").status_code == 200
    check = _wait(client, created["id"])
    assert check["status"] == "SUCCEEDED", check
    up = check["result"]["vertical"]
    assert up["n"] == 3 and up["applied"] is True
    assert up["offset_mm_yr"] == pytest.approx(3.0, abs=1.2)
    assert up["loo_rmse_after_mm_yr"] < up["rmse_before_mm_yr"]
    assert check["result"]["east"]["frame_removed_mm_yr"]["east"] == pytest.approx(20.0, abs=1.0)
    layer = client.get(f"/api/decompositions/{created['id']}/export?layer=vertical_calibrated")
    assert layer.status_code == 200


def test_a_gnss_check_with_no_station_fails_with_the_reason(client, pair, monkeypatch):
    from insar_core.adapters import ngl
    asc, desc, _, _ = pair
    created = _create(client, asc, desc).json()
    monkeypatch.setattr(ngl, "stations_in_area", lambda bbox, first, last: [])
    client.post(f"/api/decompositions/{created['id']}/validation/gnss")
    check = _wait(client, created["id"])
    assert check["status"] == "FAILED" and "No GNSS station" in check["error"]
