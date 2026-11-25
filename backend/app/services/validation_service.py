"""Validate a project's MintPy result against an independent source (EGMS, GNSS).

The comparison itself lives in insar_core.analysis.validation. This module does what that
one must not: find the files on disk, read the EGMS tiles without holding millions of rows in
memory, run for a minute or two in the background, and keep the answer next to the results it
describes so it can be shown again after a restart (and called stale once MintPy runs again).
"""
from __future__ import annotations

import json
import logging
import tempfile
import threading
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import h5py
import numpy as np
import polars as pl
import pyproj

from insar_core.adapters import ngl
from insar_core.common import PixelCube
from insar_core.analysis import calibration, plate_motion, validation
from insar_core.pipeline.mintpy_timeseries import (
    _read_geocoding, locate_timeseries_file, locate_velocity_file, read_reference_point,
)

from app.services import egms_points

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_runs: dict[str, "ValidationState"] = {}
_RESULT_NAME = "validation_{kind}.json"
_BBOX_MARGIN_DEG = 0.002


@dataclass
class ValidationState:
    project_id: str
    kind: str
    status: str = "RUNNING"          # RUNNING | SUCCEEDED | FAILED
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: Optional[str] = None
    error: Optional[str] = None
    progress: str = ""
    result: Optional[dict] = None


def _result_path(downloads_dir: Path, kind: str) -> Path:
    return Path(downloads_dir) / "mintpy" / _RESULT_NAME.format(kind=kind)


def _results_mtime(downloads_dir: Path) -> Optional[float]:
    try:
        return (Path(downloads_dir) / "mintpy" / "velocity.h5").stat().st_mtime
    except OSError:
        return None


def _saved(downloads_dir: Path, kind: str) -> Optional[dict]:
    try:
        return json.loads(_result_path(downloads_dir, kind).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def corrections(downloads_dir: Path) -> dict:
    """What the last validations of this result found that the calibrated export layers apply.

    `offset_mm_yr` is the shift that ties the velocity to GNSS, only when the stations confirmed it, and `std_scale` the
    factor by which the stated error was too small against EGMS (never below 1). A validation of an older MintPy result
    is not used: it describes something that is gone."""
    newest = _results_mtime(downloads_dir)
    out: dict = {"offset_mm_yr": None, "std_scale": None}
    for kind, key in (("gnss", "calibration"), ("egms", "uncertainty")):
        result = _saved(downloads_dir, kind)
        if not result or (newest is not None and newest > float(result.get("mintpy_results_mtime") or 0) + 1.0):
            continue
        found = result.get(key) or {}
        if kind == "gnss" and found.get("applied"):
            out["offset_mm_yr"] = float(found["offset_mm_yr"])
        if kind == "egms" and found.get("recommended_scale"):
            out["std_scale"] = float(found["recommended_scale"])
    return out


def get_status(project_id: str, downloads_dir: Path) -> dict:
    """What every kind of validation of this project has done: running, finished or never run."""
    out: dict = {}
    for kind in ("egms", "gnss"):
        with _lock:
            state = _runs.get(f"{project_id}:{kind}")
        if state is not None and state.status == "RUNNING":
            out[kind] = {"status": "RUNNING", "started_at": state.started_at, "progress": state.progress}
            continue
        if state is not None and state.status == "FAILED":
            out[kind] = {"status": "FAILED", "started_at": state.started_at, "error": state.error}
            continue
        result = state.result if state is not None and state.result else _saved(downloads_dir, kind)
        if result is None:
            out[kind] = {"status": "NOT_RUN"}
            continue
        newest = _results_mtime(downloads_dir)
        # The numbers describe the MintPy result of a given moment; once MintPy has written new
        # results they describe something that is gone.
        stale = newest is not None and newest > float(result.get("mintpy_results_mtime") or 0) + 1.0
        out[kind] = {"status": "SUCCEEDED", "result": result, "stale": stale}
    return out


# ── reading MintPy ───────────────────────────────────────────────────────────

def _read_mintpy(work_dir: Path) -> dict:
    velocity_file = locate_velocity_file(work_dir)
    series_file = locate_timeseries_file(work_dir)
    with h5py.File(velocity_file, "r") as handle:
        geo = _read_geocoding(handle)
        direction = str(handle.attrs.get("ORBIT_DIRECTION", "")).upper()
        track = str(handle.attrs.get("relative_orbit", ""))
        velocity_mm = handle["velocity"][:].astype("float64") * 1000.0
        velocity_std_mm = (handle["velocityStd"][:].astype("float64") * 1000.0) if "velocityStd" in handle else None
    with h5py.File(series_file, "r") as handle:
        dates = [d.decode() for d in handle["date"][:]]
    series_mm = PixelCube(series_file, "timeseries", scale=1000.0)   # read at the pixels compared, never whole
    mask_file = work_dir / "maskTempCoh.h5"
    if mask_file.is_file():
        with h5py.File(mask_file, "r") as handle:
            valid = handle["mask"][:].astype(bool)
    else:
        valid = np.ones(series_mm.shape[1:], bool)
    velocity_mm[~valid] = np.nan
    if velocity_std_mm is not None:
        velocity_std_mm[~valid] = np.nan
    return {"geo": geo, "dates": dates, "series_mm": series_mm, "valid": valid,
            "velocity_mm": velocity_mm, "velocity_std_mm": velocity_std_mm,
            "direction": direction, "track": track, "reference": read_reference_point(velocity_file)}


def _grid_bbox(geo: dict) -> tuple[float, float, float, float]:
    """(min_lon, min_lat, max_lon, max_lat) of the MintPy grid, with a small margin."""
    to_wgs = pyproj.Transformer.from_crs(geo["epsg"], 4326, always_xy=True)
    xs = [geo["x_first"], geo["x_first"] + geo["width"] * geo["x_step"]]
    ys = [geo["y_first"], geo["y_first"] + geo["length"] * geo["y_step"]]
    corners = [to_wgs.transform(x, y) for x in xs for y in ys]
    lons, lats = [c[0] for c in corners], [c[1] for c in corners]
    return (min(lons) - _BBOX_MARGIN_DEG, min(lats) - _BBOX_MARGIN_DEG,
            max(lons) + _BBOX_MARGIN_DEG, max(lats) + _BBOX_MARGIN_DEG)


# ── reading EGMS ─────────────────────────────────────────────────────────────

def read_egms_points(destination: Path, filenames: list[str], bbox: tuple, wanted_dates: set[str], progress=None) -> dict:
    """Latitude, longitude and the series on `wanted_dates` of every EGMS point inside `bbox`.

    Unlike the map's point extraction this does not stop at a number of points (a 20 km square
    holds a couple of hundred thousand) and it keeps only the columns needed, so memory stays
    at a few numbers per point."""
    min_lon, min_lat, max_lon, max_lat = bbox
    lat_parts, lon_parts, value_parts = [], [], []
    for number, name in enumerate(filenames, start=1):
        path = Path(destination) / name
        if not path.exists() or path.suffix.lower() != ".zip":
            continue
        if progress:
            progress(f"Reading EGMS file {number} of {len(filenames)}")
        with zipfile.ZipFile(path) as archive, tempfile.TemporaryDirectory() as tmp:
            for member in (m for m in archive.namelist() if m.lower().endswith(".csv")):
                csv_path = egms_points._extract_csv_from_zip(archive, member, Path(tmp))
                names = pl.scan_csv(csv_path).collect_schema().names()
                lat_col = egms_points._find_column(names, egms_points._LAT_ALIASES)
                lon_col = egms_points._find_column(names, egms_points._LON_ALIASES)
                columns = [c for c in egms_points._date_columns(names) if c in wanted_dates]
                if not (lat_col and lon_col and columns):
                    continue
                frame = (
                    pl.scan_csv(csv_path, infer_schema_length=100_000)
                    .select([lat_col, lon_col, *columns])
                    .filter(pl.col(lat_col).is_between(min_lat, max_lat) & pl.col(lon_col).is_between(min_lon, max_lon))
                    .collect(engine="streaming")
                )
                if frame.height == 0:
                    continue
                lat_parts.append(frame[lat_col].to_numpy())
                lon_parts.append(frame[lon_col].to_numpy())
                value_parts.append((columns, frame.select(columns).to_numpy().astype("float32")))
    if not lat_parts:
        raise ValueError(
            "No EGMS point with a time series lies inside the MintPy area on the dates MintPy has. "
            "Use an L2a or L2b download that covers the area.")
    # Tiles of one release share their dates; if they ever differ, keep the dates all of them have.
    dates = [d for d in value_parts[0][0] if all(d in columns for columns, _ in value_parts)]
    series = np.concatenate([
        values[:, [columns.index(d) for d in dates]] for columns, values in value_parts
    ])
    return {"lat": np.concatenate(lat_parts), "lon": np.concatenate(lon_parts), "series_mm": series, "dates": dates}


def _check_comparable(mintpy: dict, download: dict) -> list[str]:
    """Refuse an EGMS download that cannot be the same ground as the MintPy result; return warnings."""
    if download.get("level") not in ("L2A", "L2B"):
        raise ValueError("Only EGMS L2a and L2b downloads carry the time series needed to validate against.")
    warnings = []
    egms_direction = str(download.get("direction") or "").upper()
    if egms_direction and mintpy["direction"] and not (
        egms_direction.startswith(mintpy["direction"][:3]) or mintpy["direction"].startswith(egms_direction[:3])
    ):
        raise ValueError(
            f"The EGMS download is {egms_direction.lower()} and the MintPy result is {mintpy['direction'].lower()}: "
            "they look at the ground from different sides and cannot be compared point by point.")
    if not egms_direction:
        warnings.append("The EGMS download does not say its direction, so it could not be checked against MintPy's.")
    return warnings


def _run_egms(project_id: str, downloads_dir: Path, download: dict) -> None:
    key = f"{project_id}:egms"

    def progress(text: str) -> None:
        with _lock:
            _runs[key].progress = text

    try:
        work_dir = Path(downloads_dir) / "mintpy"
        progress("Reading the MintPy result")
        mintpy = _read_mintpy(work_dir)
        warnings = _check_comparable(mintpy, download)
        points = read_egms_points(
            Path(download["destination_path"]), list(download["filenames"]), _grid_bbox(mintpy["geo"]),
            set(mintpy["dates"]), progress=progress,
        )
        progress("Comparing")
        result = validation.compare_with_points(
            geo=mintpy["geo"], mintpy_dates=mintpy["dates"], mintpy_series_mm=mintpy["series_mm"], valid=mintpy["valid"],
            point_lat=points["lat"], point_lon=points["lon"], point_dates=points["dates"],
            point_series_mm=points["series_mm"], mintpy_velocity_std_mm=mintpy["velocity_std_mm"],
        )
        result.update({
            "source_name": "EGMS " + str(download.get("level")),
            "download_id": download.get("id"), "release": download.get("release"),
            "files": list(download["filenames"]), "warnings": warnings,
            "reference": mintpy["reference"], "orbit_direction": mintpy["direction"],
            "relative_orbit": mintpy["track"],
            "computed_at": datetime.now(timezone.utc).isoformat(),
            "mintpy_results_mtime": _results_mtime(downloads_dir),
        })
        _result_path(downloads_dir, "egms").write_text(json.dumps(result), encoding="utf-8")
        with _lock:
            state = _runs[key]
            state.result, state.status = result, "SUCCEEDED"
            state.finished_at = datetime.now(timezone.utc).isoformat()
    except Exception as exc:
        logger.exception("Validation against EGMS failed for project %s", project_id)
        with _lock:
            state = _runs[key]
            state.status, state.error = "FAILED", str(exc)
            state.finished_at = datetime.now(timezone.utc).isoformat()


def start_egms(project_id: str, downloads_dir: Path, download: dict) -> dict:
    """Validate the project's MintPy result against an EGMS download, in the background.

    `download` is the EGMS download row as a dict (id, level, release, direction,
    destination_path, filenames). Raises RuntimeError when it cannot start."""
    if not (Path(downloads_dir) / "mintpy" / "velocity.h5").exists():
        raise RuntimeError("There are no MintPy results to validate yet")
    if download.get("level") not in ("L2A", "L2B"):
        raise RuntimeError("Only EGMS L2a and L2b downloads carry the time series needed to validate against.")
    key = f"{project_id}:egms"
    with _lock:
        existing = _runs.get(key)
        if existing is not None and existing.status == "RUNNING":
            raise RuntimeError("A validation against EGMS is already running for this project")
        _runs[key] = ValidationState(project_id=project_id, kind="egms")
    threading.Thread(target=_run_egms, args=(project_id, Path(downloads_dir), download), daemon=True).start()
    return get_status(project_id, downloads_dir)


def _read_angles(work_dir: Path, shape: tuple[int, int]) -> tuple[np.ndarray, np.ndarray]:
    """Incidence angle and LOS azimuth (degrees) of every pixel, needed to project GNSS onto the LOS."""
    path = Path(work_dir) / "inputs" / "geometryGeo.h5"
    try:
        with h5py.File(path, "r") as handle:
            incidence, azimuth = handle["incidenceAngle"][:], handle["azimuthAngle"][:]
    except (OSError, KeyError):
        raise ValueError(
            "The viewing geometry (incidence and azimuth angles) is not in the MintPy folder, so GNSS "
            "motion cannot be projected onto the line of sight.") from None
    if incidence.shape != shape:
        raise ValueError("The viewing geometry does not match the size of the result.")
    return incidence, azimuth


def _calibration(mintpy: dict, stations: list[dict], incidence: np.ndarray, azimuth: np.ndarray) -> dict:
    """The shift that ties the line-of-sight velocity map to the stations, or why there is none.

    The motion of the plate is taken out of each station with the ITRF2014 plate motion model, so the
    calibrated map is relative to the rigid plate (the frame EGMS uses) and one station is enough."""
    start, end = mintpy["dates"][0], mintpy["dates"][-1]
    with_velocity = [
        {"name": s["name"], "lat": s["lat"], "lon": s["lon"], "velocity": calibration.gnss_velocity(s["series"], start, end)}
        for s in stations
    ]
    try:
        return calibration.calibrate(
            velocity=mintpy["velocity_mm"], std=mintpy["velocity_std_mm"], geo=mintpy["geo"], stations=with_velocity,
            quantity="los", incidence=incidence, azimuth=azimuth, frame="plate",
        )
    except ValueError as exc:
        return {"applied": False, "error": str(exc), "n": 0}


def _station_quality(names: set, progress) -> tuple[dict, dict, Optional[str], list[str]]:
    """The known steps and NGL's own long-term velocity of each station, and what could not be fetched.

    Both are extra evidence, not requirements: when a table cannot be read the validation goes on
    without it and says so, since a comparison without step correction is still worth having."""
    warnings: list[str] = []
    steps: dict = {}
    midas: dict = {}
    midas_frame: Optional[str] = None
    progress("Reading the known steps of the GNSS stations")
    try:
        steps = ngl.fetch_steps(names)
    except ngl.NGLError:
        warnings.append("The table of known steps (antenna changes, earthquakes) could not be read, so the GNSS series "
                        "were used as they are.")
    progress("Reading the long-term velocities of the GNSS stations")
    try:
        midas_frame, midas = ngl.fetch_midas(names)
    except ngl.NGLError:
        warnings.append("The Nevada Geodetic Laboratory's own velocities could not be read, so the station velocities "
                        "could not be cross-checked against them.")
    return steps, midas, midas_frame, warnings


def _run_gnss(project_id: str, downloads_dir: Path) -> None:
    key = f"{project_id}:gnss"

    def progress(text: str) -> None:
        with _lock:
            _runs[key].progress = text

    try:
        work_dir = Path(downloads_dir) / "mintpy"
        progress("Reading the MintPy result")
        mintpy = _read_mintpy(work_dir)
        incidence, azimuth = _read_angles(work_dir, mintpy["series_mm"].shape[1:])
        first = datetime.strptime(mintpy["dates"][0], "%Y%m%d").date()
        last = datetime.strptime(mintpy["dates"][-1], "%Y%m%d").date()
        progress("Finding GNSS stations")
        listed = ngl.stations_in_area(_grid_bbox(mintpy["geo"]), first, last)
        if not listed:
            raise ValueError("No GNSS station of the Nevada Geodetic Laboratory recorded over the whole period inside the area.")
        stations, frames, skipped = [], {}, []
        for number, station in enumerate(listed, start=1):
            progress(f"Downloading GNSS station {station['name']} ({number} of {len(listed)})")
            try:
                frame, series = ngl.fetch_series(station["name"])
            except ngl.NGLError:
                skipped.append(station["name"])
                continue
            frames[station["name"]] = frame
            stations.append({"name": station["name"], "lat": station["lat"], "lon": station["lon"], "series": series})
        # Take the jumps of equipment changes out of each series before anything is compared with it.
        steps, midas, midas_frame, quality_warnings = _station_quality({s["name"] for s in stations}, progress)
        step_reports: dict = {}
        for station in stations:
            station["series"], step_reports[station["name"]] = calibration.correct_equipment_steps(
                station["series"], steps.get(station["name"], []), mintpy["dates"][0], mintpy["dates"][-1])
        progress("Comparing")
        result = validation.compare_with_stations(
            geo=mintpy["geo"], mintpy_dates=mintpy["dates"], mintpy_series_mm=mintpy["series_mm"], valid=mintpy["valid"],
            incidence_deg=incidence, azimuth_deg=azimuth, stations=stations, frame="plate",
        )
        for row in result["stations"]:
            row["frame"] = frames.get(row["name"])
            row["steps"] = step_reports.get(row["name"], [])
            # NGL's velocity over the station's whole record: a second opinion, over a different period.
            row["ngl_midas"] = midas.get(row["name"])
        earthquakes = sorted({row["name"] for row in result["stations"] if any(s["kind"] == "earthquake" for s in row["steps"])})
        if earthquakes:
            quality_warnings.append(
                f"An earthquake that could have moved {', '.join(earthquakes)} happened inside the period. If it did, "
                "the motion is real and is in both GNSS and InSAR, but a straight line describes neither well.")
        partial = sorted(row["name"] for row in result["stations"] if row.get("coverage", 1.0) < 0.6)
        if partial:
            quality_warnings.append(
                f"{', '.join(partial)} recorded on fewer than 60% of the dates (a gap in the station's record). The comparison "
                "covers only the dates it has, and its velocities are those of that part of the period.")
        progress("Tying the velocity map to the stations")
        result["calibration"] = _calibration(mintpy, stations, incidence, azimuth)
        result.update({
            "warnings": quality_warnings, "ngl_midas_frame": midas_frame,
            "citations": [ngl.CITATION, ngl.MIDAS_CITATION, plate_motion.CITATION],
            "source_name": "GNSS (Nevada Geodetic Laboratory)", "citation": ngl.CITATION,
            "stations_found": len(listed), "stations_skipped": skipped,
            "reference": mintpy["reference"], "orbit_direction": mintpy["direction"], "relative_orbit": mintpy["track"],
            "computed_at": datetime.now(timezone.utc).isoformat(),
            "mintpy_results_mtime": _results_mtime(downloads_dir),
        })
        _result_path(downloads_dir, "gnss").write_text(json.dumps(result), encoding="utf-8")
        with _lock:
            state = _runs[key]
            state.result, state.status = result, "SUCCEEDED"
            state.finished_at = datetime.now(timezone.utc).isoformat()
    except Exception as exc:
        if not isinstance(exc, (ValueError, ngl.NGLError)):
            logger.exception("Validation against GNSS failed for project %s", project_id)
        with _lock:
            state = _runs[key]
            state.status, state.error = "FAILED", str(exc)
            state.finished_at = datetime.now(timezone.utc).isoformat()


def start_gnss(project_id: str, downloads_dir: Path) -> dict:
    """Validate the project's MintPy result against nearby GNSS stations, in the background."""
    if not (Path(downloads_dir) / "mintpy" / "velocity.h5").exists():
        raise RuntimeError("There are no MintPy results to validate yet")
    key = f"{project_id}:gnss"
    with _lock:
        existing = _runs.get(key)
        if existing is not None and existing.status == "RUNNING":
            raise RuntimeError("A validation against GNSS is already running for this project")
        _runs[key] = ValidationState(project_id=project_id, kind="gnss")
    threading.Thread(target=_run_gnss, args=(project_id, Path(downloads_dir)), daemon=True).start()
    return get_status(project_id, downloads_dir)


def forget_project(project_id: str) -> None:
    with _lock:
        for kind in ("egms", "gnss"):
            _runs.pop(f"{project_id}:{kind}", None)
