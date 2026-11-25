"""Validating a project's MintPy result against EGMS: the files read, the answer kept, the refusals."""
import datetime as dt
import json
import os
import zipfile
from pathlib import Path

import h5py
import numpy as np
import pyproj
import pytest

from app.database import SessionLocal
from app.models import EGMSDownload
from app.services import validation_service

LENGTH, WIDTH = 24, 30
GEO = {"X_FIRST": 600000.0, "Y_FIRST": 4170000.0, "X_STEP": 80.0, "Y_STEP": -80.0}
DATES = [(dt.date(2023, 1, 3) + dt.timedelta(days=12 * k)).strftime("%Y%m%d") for k in range(14)]
YEARS = np.array([(dt.datetime.strptime(d, "%Y%m%d") - dt.datetime.strptime(DATES[0], "%Y%m%d")).days for d in DATES]) / 365.25


def _truth():
    rows, cols = np.mgrid[0:LENGTH, 0:WIDTH]
    return -40.0 * np.exp(-(((rows - 12) / 5.0) ** 2 + ((cols - 15) / 7.0) ** 2))


def _create_project(client, name):
    return client.post("/api/projects", json={
        "name": name, "geometry": {"type": "Polygon", "coordinates": [[[-1.8, 37.6], [-1.8, 37.7], [-1.6, 37.7], [-1.6, 37.6], [-1.8, 37.6]]]},
        "date_start": "2023-01-01", "date_end": "2023-12-31",
    }).json()


def _write_mintpy(storage_path, velocity, offset=3.0, direction="DESCENDING"):
    work = Path(storage_path) / "mintpy"
    work.mkdir(parents=True, exist_ok=True)
    attrs = {**GEO, "WIDTH": WIDTH, "LENGTH": LENGTH, "EPSG": 32630, "REF_Y": 2, "REF_X": 3, "REF_DATE": DATES[0],
             "ORBIT_DIRECTION": direction, "relative_orbit": "8"}
    series_mm = YEARS[:, None, None] * (velocity[None] + offset)
    with h5py.File(work / "timeseries.h5", "w") as handle:
        handle.create_dataset("date", data=np.array([d.encode() for d in DATES]))
        handle.create_dataset("timeseries", data=(series_mm / 1000.0).astype("float32"))
        handle.attrs.update(attrs)
    with h5py.File(work / "velocity.h5", "w") as handle:
        handle.create_dataset("velocity", data=((velocity + offset) / 1000.0).astype("float32"))
        handle.create_dataset("velocityStd", data=np.full((LENGTH, WIDTH), 0.001, "float32"))
        handle.attrs.update({**attrs, "FILE_PATH": str(work / "timeseries.h5")})
    with h5py.File(work / "maskTempCoh.h5", "w") as handle:
        handle.create_dataset("mask", data=np.ones((LENGTH, WIDTH), bool))
    return work


def _write_egms_zip(folder, velocity, per_pixel=5, name="EGMS_L2b_008_0001_IW2_VV_2020_2024_1.zip", extra_far_points=200):
    rng = np.random.default_rng(4)
    to_wgs = pyproj.Transformer.from_crs(32630, 4326, always_xy=True)
    lines = ["pid,mp_type,latitude,longitude,height,mean_velocity," + ",".join(DATES)]
    n = 0
    for r in range(LENGTH):
        for c in range(WIDTH):
            for _ in range(per_pixel):
                x = GEO["X_FIRST"] + (c + rng.random()) * 80.0
                y = GEO["Y_FIRST"] - (r + rng.random()) * 80.0
                lon, lat = to_wgs.transform(x, y)
                values = YEARS * velocity[r, c] + rng.normal(0, 0.8, len(DATES))
                lines.append(f"P{n},PS,{lat:.6f},{lon:.6f},100.0,{velocity[r, c]:.2f}," + ",".join(f"{v:.3f}" for v in values))
                n += 1
    for k in range(extra_far_points):                     # EGMS tiles hold the whole swath, far from the area too
        lines.append(f"F{k},PS,{38.9 + k * 1e-4:.6f},-1.0,100.0,0.0," + ",".join("0.0" for _ in DATES))
    folder.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(folder / name, "w") as archive:
        archive.writestr(name.replace(".zip", ".csv"), "\n".join(lines) + "\n")
    return name


def _download(client, folder, level="L2B", direction="Descending", filenames=None):
    db = SessionLocal()
    row = EGMSDownload(name="Lorca", geometry={}, level=level, release="2020_2024_1", direction=direction,
                       destination_path=str(folder), filenames=filenames or [])
    db.add(row)
    db.commit()
    out = {"id": row.id, "level": level, "release": "2020_2024_1", "direction": direction,
           "destination_path": str(folder), "filenames": filenames or []}
    db.close()
    return out


def _run(project_id, storage_path, download):
    validation_service._runs[f"{project_id}:egms"] = validation_service.ValidationState(project_id=project_id, kind="egms")
    validation_service._run_egms(project_id, Path(storage_path), download)
    return validation_service._runs[f"{project_id}:egms"]


def test_a_result_that_matches_egms_is_validated_and_the_answer_is_kept(client, tmp_path):
    project = _create_project(client, "Validation ok")
    truth = _truth()
    _write_mintpy(project["storage_path"], truth)
    name = _write_egms_zip(tmp_path / "egms", truth)
    state = _run(project["id"], project["storage_path"], _download(client, tmp_path / "egms", filenames=[name]))

    assert state.status == "SUCCEEDED", state.error
    result = state.result
    assert result["verdict"]["status"] == "ok" and result["pixels"] == LENGTH * WIDTH
    assert result["points"] == LENGTH * WIDTH * 5                       # the far points were never loaded
    assert result["velocity_raw"]["bias_mm_yr"] == pytest.approx(3.0, abs=0.7)    # MintPy's offset, found
    assert abs(result["velocity_rereferenced"]["bias_mm_yr"]) < 1.0 and result["velocity_rereferenced"]["pearson_r"] > 0.97
    assert result["reference"]["row"] == 2 and result["orbit_direction"] == "DESCENDING"
    assert result["source_name"] == "EGMS L2B" and result["files"] == [name]

    saved = json.loads((Path(project["storage_path"]) / "mintpy" / "validation_egms.json").read_text())
    assert saved["pixels"] == result["pixels"]


def test_the_status_endpoint_shows_not_run_then_the_saved_result_and_calls_it_stale_after_a_new_run(client, tmp_path):
    project = _create_project(client, "Validation status")
    truth = _truth()
    work = _write_mintpy(project["storage_path"], truth)
    url = f"/api/projects/{project['id']}/validation"
    assert client.get(url).json()["egms"] == {"status": "NOT_RUN"}

    name = _write_egms_zip(tmp_path / "egms", truth)
    _run(project["id"], project["storage_path"], _download(client, tmp_path / "egms", filenames=[name]))
    validation_service._runs.clear()                                       # as after a restart
    body = client.get(url).json()["egms"]
    assert body["status"] == "SUCCEEDED" and body["stale"] is False and body["result"]["verdict"]["status"] == "ok"

    later = (work / "velocity.h5").stat().st_mtime + 60
    os.utime(work / "velocity.h5", (later, later))                         # MintPy ran again
    assert client.get(url).json()["egms"]["stale"] is True


def test_an_egms_download_from_the_other_side_is_refused_with_the_reason(client, tmp_path):
    project = _create_project(client, "Validation direction")
    truth = _truth()
    _write_mintpy(project["storage_path"], truth, direction="DESCENDING")
    name = _write_egms_zip(tmp_path / "egms", truth)
    state = _run(project["id"], project["storage_path"], _download(client, tmp_path / "egms", direction="Ascending", filenames=[name]))
    assert state.status == "FAILED" and "different sides" in state.error


def test_a_download_that_does_not_cover_the_area_fails_clearly(client, tmp_path):
    project = _create_project(client, "Validation elsewhere")
    truth = _truth()
    _write_mintpy(project["storage_path"], truth)
    name = _write_egms_zip(tmp_path / "egms", truth, per_pixel=0, extra_far_points=50)
    state = _run(project["id"], project["storage_path"], _download(client, tmp_path / "egms", filenames=[name]))
    assert state.status == "FAILED" and "No EGMS point" in state.error


def test_two_tiles_are_read_and_their_points_joined(client, tmp_path):
    project = _create_project(client, "Validation tiles")
    truth = _truth()
    _write_mintpy(project["storage_path"], truth)
    first = _write_egms_zip(tmp_path / "egms", truth, per_pixel=3, name="EGMS_L2b_008_0001_IW2_VV_2020_2024_1.zip")
    second = _write_egms_zip(tmp_path / "egms", truth, per_pixel=3, name="EGMS_L2b_008_0002_IW2_VV_2020_2024_1.zip")
    state = _run(project["id"], project["storage_path"], _download(client, tmp_path / "egms", filenames=[first, second]))
    assert state.status == "SUCCEEDED" and state.result["points"] == LENGTH * WIDTH * 6


def test_the_endpoint_starts_a_run_and_refuses_what_cannot_work(client, tmp_path, monkeypatch):
    project = _create_project(client, "Validation endpoint")
    url = f"/api/projects/{project['id']}/validation/egms"
    assert client.post("/api/projects/nope/validation/egms", json={"download_id": "x"}).status_code == 404
    assert client.post(url, json={"download_id": "missing"}).status_code == 404

    download = _download(client, tmp_path / "egms")
    reply = client.post(url, json={"download_id": download["id"]})
    assert reply.status_code == 422 and "no MintPy results" in reply.json()["detail"]

    _write_mintpy(project["storage_path"], _truth())
    started = []
    monkeypatch.setattr(validation_service.threading, "Thread", lambda target, args, daemon: type(
        "T", (), {"start": lambda self: started.append(args)})())
    assert client.post(url, json={"download_id": download["id"]}).status_code == 200
    assert started and started[0][2]["id"] == download["id"]
    assert client.post(url, json={"download_id": download["id"]}).status_code == 422      # already running

    l3 = _download(client, tmp_path / "egms", level="L3")
    validation_service._runs.clear()
    reply = client.post(url, json={"download_id": l3["id"]})
    assert reply.status_code == 422 and "L2a and L2b" in reply.json()["detail"]


# ── GNSS ─────────────────────────────────────────────────────────────────────

def _write_geometry(storage_path, incidence=39.0, azimuth=281.0):
    folder = Path(storage_path) / "mintpy" / "inputs"
    folder.mkdir(parents=True, exist_ok=True)
    with h5py.File(folder / "geometryGeo.h5", "w") as handle:
        handle.create_dataset("incidenceAngle", data=np.full((LENGTH, WIDTH), incidence, "float32"))
        handle.create_dataset("azimuthAngle", data=np.full((LENGTH, WIDTH), azimuth, "float32"))


def _fake_ngl(monkeypatch, truth, rows_cols, fail=()):
    from insar_core.analysis import plate_motion
    from insar_core.analysis.validation import los_unit_vector
    from app.services import validation_service as service

    to_wgs = pyproj.Transformer.from_crs(32630, 4326, always_xy=True)
    weights = los_unit_vector(39.0, 281.0)
    stations = {}
    for name, (r, c) in rows_cols.items():
        lon, lat = to_wgs.transform(GEO["X_FIRST"] + (c + 0.5) * 80.0, GEO["Y_FIRST"] - (r + 0.5) * 80.0)
        up = truth[r, c] / weights[2]                  # MintPy carries a +3 mm/year offset the stations do not
        # In a global frame a station moves with its plate; the calibration takes that out with the plate model.
        plate_east, plate_north = plate_motion.plate_velocity("EURA", lat, lon)
        stations[name] = {"name": name, "lat": lat, "lon": lon,
                          "series": {d: (plate_east * y, plate_north * y, up * y) for d, y in zip(DATES, YEARS, strict=False)}}

    monkeypatch.setattr(service.ngl, "stations_in_area", lambda bbox, start, end: [
        {"name": n, "lat": s["lat"], "lon": s["lon"], "first": start, "last": end} for n, s in stations.items()])

    def fetch(name):
        if name in fail:
            raise service.ngl.NGLError("Not found")
        return "IGS20", stations[name]["series"]

    monkeypatch.setattr(service.ngl, "fetch_series", fetch)
    # the step and velocity tables are not reached either: no steps, and no table of velocities
    monkeypatch.setattr(service.ngl, "fetch_steps", lambda names: {})

    def no_midas(names):
        raise service.ngl.NGLError("Not found")

    monkeypatch.setattr(service.ngl, "fetch_midas", no_midas)


def _run_gnss(project_id, storage_path):
    validation_service._runs[f"{project_id}:gnss"] = validation_service.ValidationState(project_id=project_id, kind="gnss")
    validation_service._run_gnss(project_id, Path(storage_path))
    return validation_service._runs[f"{project_id}:gnss"]


def test_gnss_stations_validate_the_result_and_the_answer_is_kept(client, monkeypatch):
    project = _create_project(client, "Validation gnss")
    truth = _truth()
    _write_mintpy(project["storage_path"], truth)
    _write_geometry(project["storage_path"])
    _fake_ngl(monkeypatch, truth, {"AAAA": (12, 15), "BBBB": (4, 4), "CCCC": (20, 25)})
    state = _run_gnss(project["id"], project["storage_path"])

    assert state.status == "SUCCEEDED", state.error
    result = state.result
    assert result["n"] == 3 and result["verdict"]["status"] == "ok" and "Blewitt" in result["citation"]
    assert result["common_offset_mm_yr"] == pytest.approx(3.0, abs=0.05)
    assert {row["frame"] for row in result["stations"]} == {"IGS20"}
    body = client.get(f"/api/projects/{project['id']}/validation").json()
    validation_service._runs.clear()
    assert client.get(f"/api/projects/{project['id']}/validation").json()["gnss"]["status"] == "SUCCEEDED"
    assert body["egms"] == {"status": "NOT_RUN"}


def test_a_station_that_cannot_be_downloaded_is_skipped_and_named(client, monkeypatch):
    project = _create_project(client, "Validation gnss skip")
    truth = _truth()
    _write_mintpy(project["storage_path"], truth)
    _write_geometry(project["storage_path"])
    _fake_ngl(monkeypatch, truth, {"AAAA": (12, 15), "BBBB": (4, 4), "CCCC": (20, 25)}, fail=("CCCC",))
    result = _run_gnss(project["id"], project["storage_path"]).result
    assert result["n"] == 2 and result["stations_skipped"] == ["CCCC"] and result["stations_found"] == 3


def test_no_station_in_the_area_is_a_clear_failure(client, monkeypatch):
    from app.services import validation_service as service

    project = _create_project(client, "Validation gnss none")
    _write_mintpy(project["storage_path"], _truth())
    _write_geometry(project["storage_path"])
    monkeypatch.setattr(service.ngl, "stations_in_area", lambda bbox, start, end: [])
    state = _run_gnss(project["id"], project["storage_path"])
    assert state.status == "FAILED" and "No GNSS station" in state.error


def test_an_unreachable_laboratory_is_reported_not_crashed(client, monkeypatch):
    from app.services import validation_service as service

    def down(bbox, start, end):
        raise service.ngl.NGLError("Could not reach the Nevada Geodetic Laboratory (ConnectionError).")

    project = _create_project(client, "Validation gnss down")
    _write_mintpy(project["storage_path"], _truth())
    _write_geometry(project["storage_path"])
    monkeypatch.setattr(service.ngl, "stations_in_area", down)
    state = _run_gnss(project["id"], project["storage_path"])
    assert state.status == "FAILED" and "Could not reach" in state.error


def test_the_viewing_geometry_is_needed_and_its_absence_is_explained(client, monkeypatch):
    project = _create_project(client, "Validation gnss geometry")
    _write_mintpy(project["storage_path"], _truth())
    state = _run_gnss(project["id"], project["storage_path"])
    assert state.status == "FAILED" and "viewing geometry" in state.error


def test_the_gnss_endpoint_needs_results_and_does_not_start_twice(client, monkeypatch):
    project = _create_project(client, "Validation gnss endpoint")
    url = f"/api/projects/{project['id']}/validation/gnss"
    assert client.post("/api/projects/nope/validation/gnss").status_code == 404
    assert client.post(url).status_code == 422
    _write_mintpy(project["storage_path"], _truth())
    started = []
    monkeypatch.setattr(validation_service.threading, "Thread", lambda target, args, daemon: type(
        "T", (), {"start": lambda self: started.append(args)})())
    assert client.post(url).status_code == 200 and started
    assert client.post(url).status_code == 422
