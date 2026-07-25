"""
Parse downloaded EGMS L3 product files into GeoJSON points for map display.

EGMS L3 products are distributed as CSV (optionally zipped) or shapefile, with
columns/fields whose exact names vary slightly between releases. We match
column names case-insensitively against known aliases rather than hardcoding
one exact schema, since this hasn't been verified against a live download.
"""
from __future__ import annotations

import csv
import io
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Optional, Sequence

_LAT_ALIASES = {"latitude", "lat", "y"}
_LON_ALIASES = {"longitude", "lon", "long", "x"}
_VELOCITY_ALIASES_PRIORITY = ["mean_velocity", "velocity", "mean_los_velocity"]


def extract_points(destination: Path, filenames: list[str]) -> dict[str, Any]:
    """Return a GeoJSON FeatureCollection of points from the given downloaded files."""
    features: list[dict] = []
    for filename in filenames:
        path = destination / filename
        if not path.exists():
            continue
        suffix = path.suffix.lower()
        if suffix == ".zip":
            features.extend(_points_from_zip(path))
        elif suffix == ".csv":
            features.extend(_points_from_csv_bytes(path.read_bytes()))
        elif suffix == ".shp":
            features.extend(_points_from_shapefile(path))
    return {"type": "FeatureCollection", "features": features}


def _points_from_zip(path: Path) -> list[dict]:
    with zipfile.ZipFile(path) as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if csv_names:
            features: list[dict] = []
            for name in csv_names:
                features.extend(_points_from_csv_bytes(zf.read(name)))
            return features

        shp_names = [n for n in zf.namelist() if n.lower().endswith(".shp")]
        if shp_names:
            with tempfile.TemporaryDirectory() as tmp:
                zf.extractall(tmp)
                features = []
                for name in shp_names:
                    features.extend(_points_from_shapefile(Path(tmp) / name))
                return features
    return []


def _find_column(fieldnames: Sequence[str], aliases: set[str]) -> Optional[str]:
    lower_map = {f.lower(): f for f in fieldnames}
    for alias in aliases:
        if alias in lower_map:
            return lower_map[alias]
    return None


def _find_velocity_column(fieldnames: Sequence[str]) -> Optional[str]:
    lower_map = {f.lower(): f for f in fieldnames}
    for alias in _VELOCITY_ALIASES_PRIORITY:
        if alias in lower_map:
            return lower_map[alias]
    for f in fieldnames:
        fl = f.lower()
        if "velocity" in fl and "std" not in fl:
            return f
    return None


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _points_from_csv_bytes(data: bytes) -> list[dict]:
    text = data.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames or []
    lat_col = _find_column(fieldnames, _LAT_ALIASES)
    lon_col = _find_column(fieldnames, _LON_ALIASES)
    if not lat_col or not lon_col:
        return []
    vel_col = _find_velocity_column(fieldnames)
    id_col = _find_column(fieldnames, {"pid", "point_id", "id"})

    features = []
    for row in reader:
        lat = _to_float(row.get(lat_col))
        lon = _to_float(row.get(lon_col))
        if lat is None or lon is None:
            continue
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "velocity": _to_float(row.get(vel_col)) if vel_col else None,
                "pid": row.get(id_col) if id_col else None,
            },
        })
    return features


def _points_from_shapefile(shp_path: Path) -> list[dict]:
    import shapefile  # pyshp

    features = []
    with shapefile.Reader(str(shp_path)) as sf:
        field_names = [f[0] for f in sf.fields[1:]]  # skip the deletion flag
        vel_col = _find_velocity_column(field_names)
        for shape_rec in sf.shapeRecords():
            geom = shape_rec.shape.__geo_interface__
            if geom.get("type") != "Point":
                continue
            record = shape_rec.record.as_dict()
            velocity = _to_float(record.get(vel_col)) if vel_col else None
            features.append({
                "type": "Feature",
                "geometry": geom,
                "properties": {"velocity": velocity},
            })
    return features
