from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

import h5py
import numpy as np
import pyproj

_WGS84 = 4326

# smallbaselineApp.py writes its final time series under one of these paths,
# depending on whether a geocoding step ran. Checked in this order because a
# geocoded file is what read_point_time_series needs (querying by lat/lon
# requires the X_FIRST/Y_FIRST/X_STEP/Y_STEP attributes a geocoded HDF5 carries).
_CANDIDATE_PATHS = ("geo/geo_timeseries.h5", "geo_timeseries.h5", "timeseries.h5")

# Its default pipeline also produces a linear velocity map (timeseries2velocity
# step) under one of these, mirroring the time series file's own geo/plain split.
_VELOCITY_CANDIDATE_PATHS = ("geo/geo_velocity.h5", "geo_velocity.h5", "velocity.h5")

_REQUIRED_GEO_ATTRS = ("X_FIRST", "Y_FIRST", "X_STEP", "Y_STEP", "WIDTH", "LENGTH")

# MintPy stores displacement in meters; EGMS reports millimeters, and the
# frontend chart is shared between both sources, so this module converts to
# match rather than pushing unit handling onto the caller.
_METERS_TO_MM = 1000.0

# A real HyP3-derived grid can be several million pixels (confirmed on one
# project in this repo: 3619x2817). Unlike the EGMS CSV case, decimating an
# HDF5 array is cheap (h5py reads only the requested strided cells), so this
# cap exists only to keep the map's GeoJSON payload and marker count
# reasonable, not because reading more would be slow.
_MAX_POINTS = 50_000


def _series_behind_velocity(velocity_file: Path) -> Optional[Path]:
    """The time series file a velocity map was computed from, if it is still here.

    MintPy records it in the velocity file's `FILE_PATH` attribute. Only the file
    name is used: the folder in that attribute is wherever the run happened, which
    is not where the project is now after a move, a restore or a copy.
    """
    try:
        with h5py.File(velocity_file, "r") as f:
            recorded = f.attrs.get("FILE_PATH")
    except OSError:
        return None
    if isinstance(recorded, bytes):
        recorded = recorded.decode("utf-8", errors="replace")
    if not recorded:
        return None
    name = Path(str(recorded)).name
    if not name.startswith(("timeseries", "geo_timeseries")):
        return None
    candidate = velocity_file.parent / name
    return candidate if candidate.is_file() else None


def locate_timeseries_file(work_dir: Path) -> Path:
    """Find the smallbaselineApp.py output holding the final time series.

    "Final" means the series the velocity map was computed from: smallbaselineApp
    writes the raw inversion to `timeseries.h5` and, as each correction runs,
    further files (`timeseries_ERA5.h5`, `timeseries_ERA5_demErr.h5`, ...). Reading
    plain `timeseries.h5` would show a series without the atmospheric and DEM-error
    corrections next to a velocity map that has them (on a real Madrid run the two
    differ by up to 8 mm). So the file named in the velocity map's FILE_PATH wins,
    and the plain candidates are only the fallback when no velocity map exists yet.

    Raises FileNotFoundError if none of the candidate paths exist yet, which
    happens while a run is still in progress or if it failed before reaching
    the relevant step.
    """
    work_dir = Path(work_dir)
    for velocity_candidate in _VELOCITY_CANDIDATE_PATHS:
        velocity_file = work_dir / velocity_candidate
        if velocity_file.exists():
            behind = _series_behind_velocity(velocity_file)
            if behind is not None:
                return behind
            break
    for candidate in _CANDIDATE_PATHS:
        path = work_dir / candidate
        if path.exists():
            return path
    raise FileNotFoundError(
        f"No MintPy time series output found under {work_dir} "
        f"(looked for {', '.join(_CANDIDATE_PATHS)}). "
        "The run may still be in progress, or may have failed before this step."
    )


def locate_velocity_file(work_dir: Path) -> Path:
    """Find the smallbaselineApp.py output holding the linear velocity map.

    Same reasoning as `locate_timeseries_file`: MintPy's default pipeline
    produces this as one more step after the time series inversion."""
    work_dir = Path(work_dir)
    for candidate in _VELOCITY_CANDIDATE_PATHS:
        path = work_dir / candidate
        if path.exists():
            return path
    raise FileNotFoundError(
        f"No MintPy velocity output found under {work_dir} "
        f"(looked for {', '.join(_VELOCITY_CANDIDATE_PATHS)}). "
        "The run may still be in progress, or may have failed before this step."
    )


def _read_geocoding(f: h5py.File) -> dict:
    """Geocoding attributes shared by both the time series and velocity
    outputs. Raises if missing, same reasoning as in read_point_time_series:
    this pipeline only ever expects already-geocoded HyP3-derived stacks, so
    a radar-coordinate file here means something upstream changed and we
    should not silently map pixels to the wrong place.

    X_FIRST/Y_FIRST/X_STEP/Y_STEP are in whatever CRS the file's own EPSG
    attribute names - confirmed against a real run in this repo: HyP3/MintPy
    geocode to UTM (EPSG:32630 for the project tested), not directly to
    WGS84 lat/lon degrees, even though every other coordinate this app works
    with (project AOIs, EGMS points, the map itself) is WGS84. `epsg` is
    carried through so callers can reproject instead of treating UTM meters
    as if they were degrees.
    """
    missing = [attr for attr in _REQUIRED_GEO_ATTRS if attr not in f.attrs]
    if missing:
        raise ValueError(
            f"{f.filename} is missing geocoding attributes ({', '.join(missing)}); "
            "cannot map pixels to latitude/longitude on a radar-coordinate stack. "
            "Re-run with geocoding enabled."
        )
    return {
        "x_first": float(f.attrs["X_FIRST"]),
        "y_first": float(f.attrs["Y_FIRST"]),
        "x_step": float(f.attrs["X_STEP"]),
        "y_step": float(f.attrs["Y_STEP"]),
        "width": int(f.attrs["WIDTH"]),
        "length": int(f.attrs["LENGTH"]),
        "epsg": int(f.attrs["EPSG"]) if "EPSG" in f.attrs else _WGS84,
    }


def read_reference_point(velocity_file: Path) -> Optional[dict]:
    """Where MintPy anchored the result: the pixel every displacement is relative to.

    Every MintPy value is relative to this pixel and to a reference date, so a
    result cannot be compared with another source without knowing them. Returns
    None when the file does not record one. `lat`/`lon` are WGS84."""
    with h5py.File(velocity_file, "r") as f:
        attrs = f.attrs
        if "REF_Y" not in attrs or "REF_X" not in attrs:
            return None
        out: dict = {"row": int(attrs["REF_Y"]), "column": int(attrs["REF_X"])}
        raw_date = str(attrs.get("REF_DATE", ""))
        out["date"] = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}" if len(raw_date) == 8 else None
        try:
            geo = _read_geocoding(f)
            to_wgs = _to_wgs84(geo["epsg"])
            x = geo["x_first"] + (out["column"] + 0.5) * geo["x_step"]
            y = geo["y_first"] + (out["row"] + 0.5) * geo["y_step"]
            lon, lat = to_wgs.transform(x, y) if to_wgs else (x, y)
            out["lat"], out["lon"] = round(float(lat), 6), round(float(lon), 6)
        except (ValueError, KeyError):
            out["lat"] = out["lon"] = None
        return out


def _to_wgs84(epsg: int) -> Optional[pyproj.Transformer]:
    """A transformer from `epsg` to WGS84 lon/lat, or None if the file is
    already in WGS84 (the common case this module was first written for and
    still what every test fixture uses)."""
    if epsg == _WGS84:
        return None
    return pyproj.Transformer.from_crs(epsg, _WGS84, always_xy=True)


def _from_wgs84(epsg: int) -> Optional[pyproj.Transformer]:
    """The inverse of `_to_wgs84`, for turning a (lat, lon) query point back
    into the file's own coordinate system before computing row/col."""
    if epsg == _WGS84:
        return None
    return pyproj.Transformer.from_crs(_WGS84, epsg, always_xy=True)


def read_point_time_series(work_dir: Path, lat: float, lon: float) -> dict:
    """Read the displacement time series for the pixel nearest (lat, lon).

    Returns `{"dates": [...], "values_mm": [...]}`, matching the shape the
    EGMS time series endpoint returns, so the frontend can plot both with one
    chart component.

    The adapter that drives this pipeline (mintpy_adapter.py) does not set any
    `mintpy.geocode.*` config key. HyP3 products already arrive geocoded, so
    the resulting HDF5 is expected to carry the geocoding attributes checked
    below directly (no separate lookup-table file). If a run somehow produces
    a radar-coordinate stack instead, we raise instead of silently returning
    the wrong pixel.
    """
    path = locate_timeseries_file(work_dir)

    with h5py.File(path, "r") as f:
        geo = _read_geocoding(f)

        x, y = lon, lat
        transformer = _from_wgs84(geo["epsg"])
        if transformer is not None:
            x, y = transformer.transform(lon, lat)

        col = round((x - geo["x_first"]) / geo["x_step"])
        row = round((y - geo["y_first"]) / geo["y_step"])
        if not (0 <= row < geo["length"]) or not (0 <= col < geo["width"]):
            raise ValueError(
                f"Point ({lat}, {lon}) falls outside the processed area in {path}."
            )

        dates = [d.decode("utf-8") for d in f["date"][:]]
        values_m = f["timeseries"][:, row, col]
        values_mm = [float(v) * _METERS_TO_MM for v in values_m]

    return {"dates": dates, "values_mm": values_mm}


def read_velocity_points(work_dir: Path) -> dict:
    """Return a GeoJSON FeatureCollection of velocity points for map display.

    A real HyP3-derived grid can be several million pixels (measured on one
    project in this repo: 3619x2817). Unlike EGMS's CSV files, an HDF5
    dataset supports real strided access, so decimating down to `_MAX_POINTS`
    is a cheap, single read (`dataset[::step, ::step]`) rather than a
    row-by-row scan - h5py only reads the chunks the stride actually touches.
    """
    path = locate_velocity_file(work_dir)

    with h5py.File(path, "r") as f:
        geo = _read_geocoding(f)
        length, width = geo["length"], geo["width"]
        step = max(1, math.ceil((length * width / _MAX_POINTS) ** 0.5))
        grid = np.asarray(f["velocity"][::step, ::step])

    # Build every kept pixel's native (x, y) first - e.g. UTM meters, per the
    # file's own EPSG (see _read_geocoding) - then reproject the whole batch
    # to WGS84 lon/lat in one call instead of one pyproj call per point.
    rows, cols = np.nonzero(~np.isnan(grid))
    xs = geo["x_first"] + cols * step * geo["x_step"]
    ys = geo["y_first"] + rows * step * geo["y_step"]

    transformer = _to_wgs84(geo["epsg"])
    if transformer is not None:
        lons, lats = transformer.transform(xs, ys)
    else:
        lons, lats = xs, ys

    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [float(lon), float(lat)]},
            "properties": {"velocity": float(grid[r, c]) * _METERS_TO_MM},
        }
        for r, c, lon, lat in zip(rows, cols, lons, lats, strict=True)
    ]
    return {"type": "FeatureCollection", "features": features[:_MAX_POINTS]}
