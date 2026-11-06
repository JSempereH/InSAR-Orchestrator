"""
Parse downloaded EGMS product files into GeoJSON points for map display, and
(for L2A/L2B) into per-point displacement time series.

EGMS products are distributed as CSV (optionally zipped) or shapefile, with
columns/fields whose exact names vary slightly between releases. We match
column names case-insensitively against known aliases rather than hardcoding
one exact schema. This has been verified against a real downloaded L2b file
(backend/downloads/egms/comillas/EGMS_L2b_001_0237_IW2_VV_2020_2024_1.zip),
whose CSV header is:

    pid,mp_type,latitude,longitude,...,mean_velocity,...,20200107,20200113,...

so `pid` and the mean velocity are present on L2A/L2B rows too (not just L3),
and every date past the fixed metadata columns is an 8-digit "YYYYMMDD" column
holding that point's cumulative line-of-sight displacement in millimeters.

A full-tile L2A/L2B CSV can be a few GB uncompressed (the file above is 3.2 GB
for one Sentinel-1 swath). Rows are read and bbox-filtered with polars
(`_points_from_csv_path`) instead of a plain-Python csv.DictReader loop -
polars' Rust CSV reader is multithreaded and filters as it reads, which cut
the real file above from ~10 minutes to under a minute on the same machine.
Since a zip entry isn't a real seekable file, and polars needs one for that
fast path, a zipped CSV is first streamed out to a temp file (still one pass,
still never holding the whole decompressed CSV in memory at once - see
`_extract_csv_from_zip`) before polars reads it.

EGMS distributes one file per whole Sentinel-1 swath tile, not clipped to the
AOI a user searched for - a small polygon can still return a file covering a
~100km strip well beyond it. Every row is filtered against the download's own
AOI bounding box (see `_geometry_bbox`) before being counted or returned, so
the map only ever shows points from the area the user actually asked about,
instead of the whole tile.

Filtering by AOI means a small polygon can no longer stop early once
_MAX_POINTS is reached (most rows get skipped, not collected), so a query
that used to return quickly by hitting the cap can now need a full pass over
a multi-million-row file to confirm there's nothing more to find. extract_points
caches its result next to the downloaded files (see `_CACHE_FILENAME`) so this
full pass only happens once per download, not on every "Show points" click.

extract_points already reads every in-AOI row once to build the map's
GeoJSON; while it's there, it also pulls out that row's full date/value
series and caches it separately (see `_TIMESERIES_CACHE_FILENAME`), so
extract_time_series - called once per point click - is a plain dict lookup
instead of a second multi-GB file scan. Without this, clicking a point would
re-scan the same file(s) from scratch every time, even right after the map's
own scan had already passed over that exact row.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
import threading
import zipfile
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np
import polars as pl

logger = logging.getLogger(__name__)

_LAT_ALIASES = {"latitude", "lat", "y"}
_LON_ALIASES = {"longitude", "lon", "long", "x"}
_VELOCITY_ALIASES_PRIORITY = ["mean_velocity", "velocity", "mean_los_velocity"]
_ID_ALIASES = {"pid", "point_id", "id"}

# (min_lon, min_lat, max_lon, max_lat)
Bbox = tuple[float, float, float, float]

# A single Sentinel-1 swath tile has been measured at 3 million EGMS points
# (see the real file cited above), and a download commonly has more than one
# such tile. Returning all of them as one GeoJSON response would be a
# multi-GB payload the browser can't render anyway, so extract_points stops
# once it has this many *in total across every file in the download* - not
# per file, since a per-file cap still has to fully open and decompress every
# file before giving up, which is most of the cost. This is a truncation, not
# a representative sample of the AOI: proper decimation (e.g. thinning by
# grid cell, or paging by map viewport) is a follow-up, not solved here.
_MAX_POINTS = 50_000

# EGMS time series columns are bare 8-digit acquisition dates, e.g. "20200107".
_DATE_COLUMN_RE = re.compile(r"^\d{8}$")

# Cache files, written next to the downloaded product files (each download
# has its own destination folder, so these can't collide with another
# download's cache). See module docstring for why both exist.
_CACHE_FILENAME = ".points_cache.json"
_TIMESERIES_CACHE_FILENAME = ".timeseries_cache.json"


def _geometry_bbox(geometry: dict) -> Optional[Bbox]:
    """(min_lon, min_lat, max_lon, max_lat) from a GeoJSON Polygon, or a
    Feature wrapping one - EGMSDownload.geometry is stored exactly as the
    frontend's draw tool produced it, which can be either shape. Mirrors how
    the EGMS adapter itself already treats the AOI as a bounding box
    (insar_core.adapters.egms._aoi_points), so this is consistent with what
    the search that produced these files actually matched against."""
    if not geometry:
        return None
    geom = geometry.get("geometry", geometry) if geometry.get("type") == "Feature" else geometry
    try:
        coords = geom["coordinates"][0]
        lons = [c[0] for c in coords]
        lats = [c[1] for c in coords]
        return min(lons), min(lats), max(lons), max(lats)
    except (KeyError, IndexError, TypeError):
        return None


def _cache_query_key(filenames: list[str], geometry: Optional[dict]) -> dict[str, Any]:
    """What a cache file is valid *for*. A destination folder is normally only
    ever queried with one fixed (filenames, geometry) pair - both come from
    the same immutable EGMSDownload row - but caching by folder alone would
    silently serve a stale/wrong result if that ever changed (or, as happened
    while building this, if something else queried the same folder with
    different parameters). Stored alongside the cached data so a mismatch is
    detected and recomputed instead of trusted blindly."""
    return {"filenames": filenames, "geometry": geometry}


def _read_cache(cache_path: Path, expected_key: dict[str, Any]) -> Optional[Any]:
    if not cache_path.exists():
        return None
    try:
        payload = json.loads(cache_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("query") != expected_key:
        return None
    return payload.get("data")


def _write_cache(cache_path: Path, key: dict[str, Any], data: Any) -> None:
    try:
        cache_path.write_text(json.dumps({"query": key, "data": data}))
    except OSError:
        pass  # non-fatal: just means the next call recomputes too


class _SeriesStore:
    """All of one download's time series, held in memory in a compact form.

    The on-disk series cache is one JSON document holding every point's series
    (tens of MB for a real EGMS tile). Parsing it took a couple of seconds, and
    it used to happen on *every* point click. The parsed store is kept in
    memory instead: values as float64 arrays, and the date list shared between
    all points that have the same dates (normally all of them), so a store of
    ~15 000 points x ~200 dates is about 25 MB instead of several hundred.
    """

    def __init__(self, stamp: tuple, data: dict[str, Any]):
        self.stamp = stamp
        shared_dates: dict[tuple[str, ...], tuple[str, ...]] = {}
        self.rows: dict[str, tuple[tuple[str, ...], np.ndarray]] = {}
        for point_id, series in data.items():
            dates = tuple(series["dates"])
            dates = shared_dates.setdefault(dates, dates)
            self.rows[point_id] = (dates, np.asarray(series["values_mm"], dtype=np.float64))

    def get(self, point_id: str) -> Optional[dict[str, Any]]:
        row = self.rows.get(point_id)
        if row is None:
            return None
        dates, values = row
        return {"dates": list(dates), "values_mm": values.tolist()}


_STORE_MAX_ENTRIES = 2
_stores: "OrderedDict[str, _SeriesStore]" = OrderedDict()
_stores_lock = threading.Lock()  # guards _stores
_load_lock = threading.Lock()  # one slow parse at a time


def _load_store(cache_path: Path, key: dict[str, Any]) -> Optional[_SeriesStore]:
    """The in-memory store for `cache_path`, loading it from disk on first use
    (or when the file changed since). None if there is no valid cache file."""
    try:
        info = cache_path.stat()
    except OSError:
        return None
    stamp = (info.st_mtime_ns, info.st_size, json.dumps(key, sort_keys=True))
    name = str(cache_path)

    with _stores_lock:
        store = _stores.get(name)
        if store is not None and store.stamp == stamp:
            _stores.move_to_end(name)
            return store

    with _load_lock:
        with _stores_lock:  # another request may have finished loading it meanwhile
            store = _stores.get(name)
            if store is not None and store.stamp == stamp:
                return store
        data = _read_cache(cache_path, key)
        if data is None:
            return None
        store = _SeriesStore(stamp, data)
        with _stores_lock:
            _stores[name] = store
            _stores.move_to_end(name)
            while len(_stores) > _STORE_MAX_ENTRIES:
                _stores.popitem(last=False)
        return store


def _remember_store(cache_path: Path, key: dict[str, Any], data: dict[str, Any]) -> None:
    """Keep freshly built series in memory, right after they were written to
    disk, so the next click does not read the file back."""
    try:
        info = cache_path.stat()
    except OSError:
        return
    store = _SeriesStore((info.st_mtime_ns, info.st_size, json.dumps(key, sort_keys=True)), data)
    with _stores_lock:
        _stores[str(cache_path)] = store
        _stores.move_to_end(str(cache_path))
        while len(_stores) > _STORE_MAX_ENTRIES:
            _stores.popitem(last=False)


def _warm_store_in_background(destination: Path, key: dict[str, Any]) -> None:
    """Load the series store right after the map's points are served, so the
    first point click does not pay the parse."""
    def _run() -> None:
        try:
            _load_store(destination / _TIMESERIES_CACHE_FILENAME, key)
        except Exception:
            logger.debug("Could not warm the time series store", exc_info=True)

    threading.Thread(target=_run, name="egms-series-warmup", daemon=True).start()


def extract_points(destination: Path, filenames: list[str], geometry: Optional[dict] = None) -> dict[str, Any]:
    """Return a GeoJSON FeatureCollection of points from the given downloaded
    files, restricted to `geometry`'s bounding box when given (EGMS ships
    whole swath-tile files well beyond the AOI that was actually searched).

    Cached to `destination / _CACHE_FILENAME` on first call: a bbox-filtered
    scan can need a full pass over a multi-million-row file (see module
    docstring), which would otherwise happen again on every page load. The
    same pass also populates the time series cache extract_time_series reads
    (see `_build_points_and_series`), since it's already looking at every row
    that matters."""
    key = _cache_query_key(filenames, geometry)
    cache_path = destination / _CACHE_FILENAME
    cached = _read_cache(cache_path, key)
    if cached is not None:
        _warm_store_in_background(destination, key)
        return cached

    result, series_by_point_id = _build_points_and_series(destination, filenames, geometry)
    _write_cache(cache_path, key, result)
    _write_cache(destination / _TIMESERIES_CACHE_FILENAME, key, series_by_point_id)
    _remember_store(destination / _TIMESERIES_CACHE_FILENAME, key, series_by_point_id)
    return result


def extract_time_series(
    destination: Path, filenames: list[str], point_id: str, geometry: Optional[dict] = None
) -> Optional[dict[str, Any]]:
    """Return `point_id`'s displacement time series, as
    `{"dates": [...], "values_mm": [...]}`, or None if it isn't found.

    Only meaningful for L2A/L2B downloads: those CSV rows carry one column per
    acquisition date, while L3 rows only carry a mean velocity.

    Normally this is a dict lookup in the cache extract_points already built
    (every point shown on the map came from that same scan, so its series is
    already sitting there - see the module docstring for why this matters).
    Only falls back to scanning the raw files if that cache doesn't exist yet
    (or doesn't match this exact query), e.g. a client calling this endpoint
    before ever loading the point map.
    """
    key = _cache_query_key(filenames, geometry)
    cache_path = destination / _TIMESERIES_CACHE_FILENAME
    store = _load_store(cache_path, key)
    if store is not None:
        return store.get(point_id)

    _, series_by_point_id = _build_points_and_series(destination, filenames, geometry)
    _write_cache(cache_path, key, series_by_point_id)
    _remember_store(cache_path, key, series_by_point_id)
    return series_by_point_id.get(point_id)


def _build_points_and_series(
    destination: Path, filenames: list[str], geometry: Optional[dict]
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """One pass over every in-AOI row, producing both the map's
    FeatureCollection and a point_id -> {"dates", "values_mm"} dict for every
    row that had date columns (L2A/L2B only; L3 rows just won't appear here).

    A download with more than one zip/csv file processes them concurrently
    (see `_process_files_in_parallel`): extraction and polars filtering for
    one file never need the others' data, and neither step holds a whole
    decompressed file in memory at once, so running them side by side is a
    real wall-clock win (measured ~1.5x on a 2-file download) without
    materially raising peak memory. Each worker gets the full `_MAX_POINTS`
    budget independently since it can't see the others' progress; the
    combined result is truncated back down to that cap afterward.

    Scope: CSV only (zipped or not). L2A/L2B are distributed as CSV in
    practice; the shapefile path below (used for some L3 downloads, which
    have no time series to extract anyway) only contributes to the points
    side of the result, and always runs sequentially (shapefiles are small).
    """
    bbox = _geometry_bbox(geometry) if geometry else None
    features: list[dict] = []
    series_by_point_id: dict[str, dict[str, Any]] = {}

    scannable: list[tuple[Path, str, str]] = []  # (path, source_label, suffix)
    for filename in filenames:
        path = destination / filename
        if not path.exists():
            continue
        suffix = path.suffix.lower()
        if suffix in (".zip", ".csv"):
            scannable.append((path, filename, suffix))
        elif suffix == ".shp":
            features.extend(_points_from_shapefile(path))

    if len(scannable) > 1:
        results = _process_files_in_parallel(scannable, bbox)
    else:
        results = [_process_one_file(path, source_label, suffix, bbox) for path, source_label, suffix in scannable]

    for file_features, file_series in results:
        features.extend(file_features)
        series_by_point_id.update(file_series)

    if len(features) > _MAX_POINTS:
        features = features[:_MAX_POINTS]
        kept_ids = {f["properties"]["point_id"] for f in features}
        series_by_point_id = {k: v for k, v in series_by_point_id.items() if k in kept_ids}

    return {"type": "FeatureCollection", "features": features}, series_by_point_id


def _process_one_file(
    path: Path, source_label: str, suffix: str, bbox: Optional[Bbox]
) -> tuple[list[dict], dict[str, dict[str, Any]]]:
    """One file's worth of work, as a single picklable call so it can run
    either inline or inside a ProcessPoolExecutor worker."""
    if suffix == ".zip":
        return _points_from_zip(path, remaining=_MAX_POINTS, bbox=bbox)
    return _points_from_csv_path(path, source_label, limit=_MAX_POINTS, bbox=bbox)


def _process_files_in_parallel(
    entries: list[tuple[Path, str, str]], bbox: Optional[Bbox]
) -> list[tuple[list[dict], dict[str, dict[str, Any]]]]:
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    # This runs inside the FastAPI process, which is multithreaded (asyncio
    # loop, background polling/credits threads) - forking a multithreaded
    # process can deadlock if another thread held a lock at fork time, so
    # this explicitly spawns clean workers instead of using Linux's fork
    # default.
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=min(len(entries), 4), mp_context=ctx) as pool:
        futures = [pool.submit(_process_one_file, path, source_label, suffix, bbox) for path, source_label, suffix in entries]
        return [future.result() for future in futures]


def _extract_csv_from_zip(zf: zipfile.ZipFile, name: str, tmp_dir: Path) -> Path:
    """Copy one zip entry out to a real file, since polars' fast CSV path
    (`_points_from_csv_path`) needs a seekable path, not an arbitrary stream.
    Still a single streamed pass - `shutil.copyfileobj` never holds more than
    one chunk of the decompressed CSV in memory at a time."""
    dest = tmp_dir / Path(name).name
    with zf.open(name) as src, dest.open("wb") as out:
        shutil.copyfileobj(src, out, length=1024 * 1024)
    return dest


def _points_from_zip(
    path: Path,
    remaining: int,
    bbox: Optional[Bbox] = None,
) -> tuple[list[dict], dict[str, dict[str, Any]]]:
    with zipfile.ZipFile(path) as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if csv_names:
            features: list[dict] = []
            series_by_point_id: dict[str, dict[str, Any]] = {}
            with tempfile.TemporaryDirectory() as tmp:
                for name in csv_names:
                    if len(features) >= remaining:
                        break
                    csv_path = _extract_csv_from_zip(zf, name, Path(tmp))
                    file_features, file_series = _points_from_csv_path(
                        csv_path, name, limit=remaining - len(features), bbox=bbox
                    )
                    features.extend(file_features)
                    series_by_point_id.update(file_series)
            return features, series_by_point_id

        shp_names = [n for n in zf.namelist() if n.lower().endswith(".shp")]
        if shp_names:
            with tempfile.TemporaryDirectory() as tmp:
                zf.extractall(tmp)
                features = []
                for name in shp_names:
                    features.extend(_points_from_shapefile(Path(tmp) / name))
                return features, {}
    return [], {}


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


def _date_columns(fieldnames: Sequence[str]) -> list[str]:
    return sorted(f for f in fieldnames if _DATE_COLUMN_RE.match(f))


def _row_point_id(id_col: Optional[str], row: dict, source_label: str, row_index: int) -> str:
    """The identifier extract_points embeds per feature and extract_time_series
    matches against. Uses the row's own `pid` (present on every real EGMS row
    we've checked) and only falls back to a synthetic id if a release ever
    ships without one, so a lookup always has something stable to compare."""
    raw_id = row.get(id_col) if id_col else None
    if raw_id:
        return str(raw_id)
    return f"{source_label}#{row_index}"


def _points_from_csv_path(
    path: Path,
    source_label: str,
    limit: int,
    bbox: Optional[Bbox] = None,
) -> tuple[list[dict], dict[str, dict[str, Any]]]:
    """Build map features for the rows that fall in `bbox`, plus - when the
    file has date columns (L2A/L2B) - each kept row's
    `{"dates", "values_mm"}`, keyed by its point_id.

    Reads and filters `path` with polars instead of a plain-Python csv.reader
    loop: a lazy scan projects down to only the columns this function needs
    (dropping ~20 metadata columns a real EGMS file carries), then filters by
    bbox and caps at `limit`, all executed by polars' multithreaded Rust CSV
    reader rather than a row-by-row Python for-loop. Only the resulting
    (already small, AOI-sized) set of matching rows is then iterated in
    Python to build the GeoJSON/series output below.

    Returns rather than mutates a shared dict, so this can run in a worker
    process (see `_process_files_in_parallel`) where a shared dict wouldn't
    be visible to the caller anyway."""
    fieldnames = pl.scan_csv(path).collect_schema().names()
    lat_col = _find_column(fieldnames, _LAT_ALIASES)
    lon_col = _find_column(fieldnames, _LON_ALIASES)
    if not lat_col or not lon_col:
        return [], {}
    vel_col = _find_velocity_column(fieldnames)
    id_col = _find_column(fieldnames, _ID_ALIASES)
    date_cols = _date_columns(fieldnames)

    keep_cols = [lat_col, lon_col]
    if vel_col:
        keep_cols.append(vel_col)
    if id_col:
        keep_cols.append(id_col)
    keep_cols.extend(date_cols)
    keep_cols = list(dict.fromkeys(keep_cols))  # de-dup, preserve order

    lf = pl.scan_csv(path, infer_schema_length=100_000).select(keep_cols).with_row_index("__row_idx")
    if bbox is not None:
        min_lon, min_lat, max_lon, max_lat = bbox
        lf = lf.filter(
            pl.col(lat_col).is_between(min_lat, max_lat) & pl.col(lon_col).is_between(min_lon, max_lon)
        )
    df = lf.head(limit).collect(engine="streaming")

    features = []
    series_by_point_id: dict[str, dict[str, Any]] = {}
    for row in df.iter_rows(named=True):
        lat = row[lat_col]
        lon = row[lon_col]
        if lat is None or lon is None:
            continue
        point_id = _row_point_id(id_col, row, source_label, row["__row_idx"])
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "velocity": row.get(vel_col) if vel_col else None,
                "point_id": point_id,
            },
        })
        if date_cols:
            dates: list[str] = []
            values_mm: list[float] = []
            for date_col in date_cols:
                value = row.get(date_col)
                if value is None:
                    continue
                dates.append(date_col)
                values_mm.append(float(value))
            series_by_point_id[point_id] = {"dates": dates, "values_mm": values_mm}
    return features, series_by_point_id


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
