"""Write a MintPy result in formats other tools read: GeoTIFF, NetCDF, CSV and GeoPackage.

MintPy keeps its results in HDF5 files that only MintPy and a few tools read. These
functions turn the same arrays into files a GIS (QGIS, ArcGIS), xarray, a spreadsheet or a
database opens directly, with the coordinate system, units and reference stated inside
the file so it still makes sense when it leaves this app.

Units: velocity and displacement in millimetres (per year for velocity), positive towards
the satellite, as in the viewer and in EGMS. Every value is relative to the reference pixel
and reference date, which are written into each file.

Pixel convention: MintPy's X_FIRST and Y_FIRST are the upper left corner of the first
pixel (the GeoTIFF origin), so a pixel's position is its centre, half a step inside.
"""
from __future__ import annotations

import datetime as dt
import math
from pathlib import Path
from typing import Iterator, Optional

import h5py
import numpy as np
import pyproj

from insar_core.pipeline.mintpy_timeseries import (
    _read_geocoding, locate_timeseries_file, locate_velocity_file, read_reference_point,
)

FORMATS = ("geotiff", "netcdf", "csv", "gpkg")
EXTENSIONS = {"geotiff": "tif", "netcdf": "nc", "csv": "csv", "gpkg": "gpkg"}
# layer name -> (file, dataset, scale, unit, description). The velocity file is located, not named.
LAYERS = {
    "velocity": (None, "velocity", 1000.0, "mm/year", "Line-of-sight velocity, positive towards the satellite"),
    "velocity_std": (None, "velocityStd", 1000.0, "mm/year", "Standard error of the velocity from the fit"),
    # These two need the result of a validation: the shift estimated from GNSS stations, and the factor by which the
    # stated error was found too small against EGMS. They are refused, with the reason, when it has not been run.
    "velocity_calibrated": (None, "velocity", 1000.0, "mm/year",
                            "Line-of-sight velocity tied to GNSS: the velocity plus the shift estimated from the stations"),
    "velocity_std_calibrated": (None, "velocityStd", 1000.0, "mm/year",
                                "Standard error of the velocity multiplied by the factor measured against an independent source"),
    # Not a dataset of MintPy: computed from the noise of the dates (insar_core.analysis.spatial_uncertainty).
    "velocity_precision": (None, "velocity", 1000.0, "mm/year",
                           "Precision of the velocity relative to the reference point, from how the noise of the dates grows "
                           "with distance. A lower bound of the error: steady errors are not in it"),
    "temporal_coherence": ("temporalCoherence.h5", "temporalCoherence", 1.0, "1", "Temporal coherence of the inversion"),
    "coherence": ("avgSpatialCoh.h5", "coherence", 1.0, "1", "Mean spatial coherence of the interferograms"),
    "mask": ("maskTempCoh.h5", "mask", 1.0, "1", "1 where the temporal coherence passes MintPy's threshold"),
    "timeseries": (None, "timeseries", 1000.0, "mm", "Cumulative line-of-sight displacement per date"),
}
_CHUNK_ROWS = 200
_NETCDF_BLOCK_BYTES = 64 * 1024 * 1024       # dates held in memory at once while writing the cube
_NETCDF_ZLEVEL = 1                            # deflate level: 1 takes most of the saving at a fraction of the time of higher levels


class ExportError(RuntimeError):
    """A result that cannot be exported (missing files, an unknown format or layer)."""


def _open(work_dir: Path, layer: str):
    name, dataset, scale, unit, description = LAYERS[layer]
    if layer == "timeseries":
        path = locate_timeseries_file(work_dir)
    elif name is None:
        path = locate_velocity_file(work_dir)
    else:
        path = Path(work_dir) / name
    if not path.is_file():
        raise ExportError(f"{path.name} is not in {work_dir}: run MintPy first")
    return path, dataset, scale, unit, description


def _grid(work_dir: Path) -> dict:
    """Geocoding of the result, the CRS as a pyproj object, and the reference point and date."""
    velocity = locate_velocity_file(work_dir)
    with h5py.File(velocity, "r") as handle:
        geo = _read_geocoding(handle)
        attrs = {k: (v.decode() if isinstance(v, bytes) else v) for k, v in handle.attrs.items()}
    geo["crs"] = pyproj.CRS.from_epsg(geo["epsg"])
    geo["reference"] = read_reference_point(velocity)
    geo["attrs"] = attrs
    return geo


def _valid_mask(work_dir: Path, shape: tuple[int, int]) -> np.ndarray:
    path = Path(work_dir) / "maskTempCoh.h5"
    if not path.is_file():
        return np.ones(shape, bool)
    with h5py.File(path, "r") as handle:
        return handle["mask"][:].astype(bool)


def _description(geo: dict, extra: Optional[dict] = None) -> dict:
    reference = geo["reference"] or {}
    attrs = geo["attrs"]
    tags = {
        "title": "InSAR line-of-sight displacement from HyP3 interferograms and MintPy SBAS",
        "sign_convention": "positive values move towards the satellite",
        "reference_date": reference.get("date") or "",
        "reference_row": str(reference.get("row", "")),
        "reference_column": str(reference.get("column", "")),
        "reference_lat": str(reference.get("lat", "")),
        "reference_lon": str(reference.get("lon", "")),
        "orbit_direction": str(attrs.get("ORBIT_DIRECTION", "")),
        "relative_orbit": str(attrs.get("relative_orbit", "")),
        "wavelength_m": str(attrs.get("WAVELENGTH", "")),
        "heading_deg": str(attrs.get("HEADING", "")),
        "start_date": str(attrs.get("START_DATE", "")),
        "end_date": str(attrs.get("END_DATE", "")),
        "note": "every value is relative to the reference pixel and the reference date",
        "created": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    tags.update(extra or {})
    return {k: v for k, v in tags.items() if v != ""}


def _read_layer(work_dir: Path, layer: str, masked: bool, geo: dict, corrections: Optional[dict] = None) -> np.ndarray:
    path, dataset, scale, _, _ = _open(work_dir, layer)
    with h5py.File(path, "r") as handle:
        data = np.asarray(handle[dataset][:])
    if layer == "mask":
        return data.astype("uint8")
    data = data.astype("float32") * np.float32(scale)
    corrections = corrections or {}
    if layer == "velocity_precision":
        from insar_core.analysis import spatial_uncertainty

        estimate = spatial_uncertainty.read_or_estimate(work_dir)
        if not estimate or "reference" not in estimate:
            raise ExportError("The precision of the velocity could not be estimated for this result: the series is too "
                              "short, the area too small, or the result has no reference point.")
        reference = (estimate["reference"]["row"], estimate["reference"]["column"])
        data = np.where(np.isfinite(data), spatial_uncertainty.sigma_map(estimate, geo, reference), np.float32(np.nan))
    if layer == "velocity_calibrated":
        if corrections.get("offset_mm_yr") is None:
            raise ExportError("There is no GNSS calibration for this result. Validate it against GNSS first, and note that "
                              "a calibration the stations could not confirm is not applied.")
        data = data + np.float32(corrections["offset_mm_yr"])
    elif layer == "velocity_std_calibrated":
        if corrections.get("std_scale") is None:
            raise ExportError("The stated error has not been checked against EGMS for this result. Validate it against "
                              "an EGMS L2a or L2b download first.")
        data = data * np.float32(corrections["std_scale"])
    if masked and layer in ("velocity", "velocity_std", "velocity_calibrated", "velocity_std_calibrated", "velocity_precision", "timeseries"):
        valid = _valid_mask(work_dir, (geo["length"], geo["width"]))
        data[..., ~valid] = np.nan
    return data


def _dates(work_dir: Path) -> list[str]:
    with h5py.File(locate_timeseries_file(work_dir), "r") as handle:
        return [d.decode() for d in handle["date"][:]]


# ── GeoTIFF (cloud optimised) ────────────────────────────────────────────────

def export_geotiff(work_dir, dest, layer: str = "velocity", masked: bool = True, corrections: Optional[dict] = None) -> Path:
    """One layer as a cloud optimised GeoTIFF (the `timeseries` layer has one band per date).

    `corrections` is `{"offset_mm_yr": ..., "std_scale": ...}`, what the two calibrated layers need."""
    import rasterio
    import rasterio.shutil
    from rasterio.transform import Affine

    if layer not in LAYERS:
        raise ExportError(f"Unknown layer '{layer}'. Choose one of {', '.join(LAYERS)}")
    work_dir, dest = Path(work_dir), Path(dest)
    geo = _grid(work_dir)
    data = _read_layer(work_dir, layer, masked, geo, corrections)
    bands = data if data.ndim == 3 else data[None]
    _, _, _, unit, description = _open(work_dir, layer)
    applied = {}
    if layer == "velocity_calibrated":
        applied = {"calibration_shift_mm_yr": f"{corrections['offset_mm_yr']:+.3f}",
                   "calibration": "shift estimated from GNSS stations inside the area, tested on stations left out"}
    elif layer == "velocity_std_calibrated":
        applied = {"uncertainty_scale": f"{corrections['std_scale']:.3f}",
                   "calibration": "stated standard error times the factor measured against EGMS"}
    transform = Affine(geo["x_step"], 0.0, geo["x_first"], 0.0, geo["y_step"], geo["y_first"])
    nodata = None if layer == "mask" else float("nan")

    draft = dest.with_suffix(".draft.tif")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        draft, "w", driver="GTiff", height=geo["length"], width=geo["width"], count=len(bands),
        dtype=bands.dtype, crs=geo["crs"], transform=transform, nodata=nodata,
    ) as out:
        out.write(bands)
        out.update_tags(layer=layer, units=unit, description=description, **_description(geo, applied))
        if layer == "timeseries":
            for index, date in enumerate(_dates(work_dir), start=1):
                out.set_band_description(index, date)
                out.update_tags(index, date=date, units=unit)
        else:
            out.set_band_description(1, layer)
    try:
        rasterio.shutil.copy(draft, dest, driver="COG", compress="DEFLATE", overview_resampling="average")
    finally:
        draft.unlink(missing_ok=True)
    return dest


# ── points (CSV and GeoPackage) ──────────────────────────────────────────────

_SCALAR_COLUMNS = ("velocity_mm_yr", "velocity_std_mm_yr", "temporal_coherence", "mean_coherence")


def _point_blocks(work_dir: Path, masked: bool, step: int, include_series: bool, geo: dict) -> Iterator[dict]:
    """The pixels kept, a block of rows of the grid at a time, as arrays with one entry per pixel.

    Each block is `{"latitude", "longitude", "row", "column", "scalars" (pixels x 4, in the order of
    _SCALAR_COLUMNS), "series" (pixels x dates, or None), "dates"}`. Everything is computed on whole
    arrays: a result of a million pixels and two hundred dates is two hundred million numbers, and
    handling them one Python object at a time took minutes."""
    to_wgs = pyproj.Transformer.from_crs(geo["crs"], 4326, always_xy=True)
    valid_all = _valid_mask(work_dir, (geo["length"], geo["width"])) if masked else None
    series_file = locate_timeseries_file(work_dir) if include_series else None
    dates = _dates(work_dir) if include_series else []
    velocity_path, _, _, _, _ = _open(work_dir, "velocity")

    handles = {"velocity": h5py.File(velocity_path, "r")}
    for layer in ("temporal_coherence", "coherence"):
        path = Path(work_dir) / LAYERS[layer][0]
        if path.is_file():
            handles[layer] = h5py.File(path, "r")
    series = h5py.File(series_file, "r") if series_file else None
    try:
        has_std = "velocityStd" in handles["velocity"]
        for top in range(0, geo["length"], _CHUNK_ROWS):
            bottom = min(top + _CHUNK_ROWS, geo["length"])
            # the rows of this block that the step keeps, counted from the top of the grid
            offset = (-top) % step
            window = (slice(top + offset, bottom, step), slice(None, None, step))
            velocity = handles["velocity"]["velocity"][window] * 1000.0
            keep = np.isfinite(velocity)
            if valid_all is not None:
                keep &= valid_all[window]
            rows, cols = np.nonzero(keep)
            if rows.size == 0:
                continue
            scalars = np.full((rows.size, len(_SCALAR_COLUMNS)), np.nan, "float64")
            scalars[:, 0] = velocity[rows, cols]
            if has_std:
                scalars[:, 1] = handles["velocity"]["velocityStd"][window][rows, cols] * 1000.0
            if "temporal_coherence" in handles:
                scalars[:, 2] = handles["temporal_coherence"]["temporalCoherence"][window][rows, cols]
            if "coherence" in handles:
                scalars[:, 3] = handles["coherence"]["coherence"][window][rows, cols]
            cube = None
            if series is not None:
                cube = (series["timeseries"][(slice(None), *window)][:, rows, cols] * 1000.0).T      # pixels x dates
            absolute_rows = top + offset + rows * step
            absolute_cols = cols * step
            xs = geo["x_first"] + (absolute_cols + 0.5) * geo["x_step"]
            ys = geo["y_first"] + (absolute_rows + 0.5) * geo["y_step"]
            lons, lats = to_wgs.transform(xs, ys)
            yield {"latitude": np.asarray(lats), "longitude": np.asarray(lons), "row": absolute_rows, "column": absolute_cols,
                   "scalars": scalars, "series": cube, "dates": dates}
    finally:
        for handle in handles.values():
            handle.close()
        if series is not None:
            series.close()


def _point_rows(work_dir: Path, masked: bool, step: int, include_series: bool, geo: dict) -> Iterator[dict]:
    """One dict per pixel kept: position, velocity, coherence and, when asked, the series."""
    for block in _point_blocks(work_dir, masked, step, include_series, geo):
        for k in range(block["row"].size):
            row = {"latitude": float(block["latitude"][k]), "longitude": float(block["longitude"][k])}
            row.update({name: float(block["scalars"][k, i]) for i, name in enumerate(_SCALAR_COLUMNS)})
            row["row"], row["column"] = int(block["row"][k]), int(block["column"][k])
            if block["series"] is not None:
                row.update({f"d{date}": float(v) for date, v in zip(block["dates"], block["series"][k], strict=False)})
            yield row


def export_csv(work_dir, dest, masked: bool = True, include_series: bool = True, step: int = 1) -> Path:
    """A table with one row per pixel: position, velocity, quality and one column per date (mm).

    A comment block at the top says what the numbers are and what they are relative to."""
    work_dir, dest = Path(work_dir), Path(dest)
    geo = _grid(work_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tags = _description(geo, {"columns": "latitude, longitude in WGS84; velocity in mm/year; d<date> is cumulative displacement in mm"})
    wrote = False
    with open(dest, "w", newline="", encoding="utf-8") as handle:
        for key, value in tags.items():
            handle.write(f"# {key}: {value}\n")
        for block in _point_blocks(work_dir, masked, max(1, int(step)), include_series, geo):
            dates = block["dates"] if block["series"] is not None else []
            if not wrote:
                handle.write(",".join(["latitude", "longitude", *_SCALAR_COLUMNS, "row", "column", *(f"d{d}" for d in dates)]) + "\n")
                wrote = True
            columns = [block["latitude"][:, None], block["longitude"][:, None], block["scalars"],
                       block["row"][:, None], block["column"][:, None]]
            formats = ["%.6f", "%.6f", *["%.4g"] * len(_SCALAR_COLUMNS), "%d", "%d"]
            if dates:
                columns.append(block["series"])
                formats += ["%.4g"] * len(dates)
            np.savetxt(handle, np.hstack(columns), fmt=formats, delimiter=",")
    if not wrote:
        dest.unlink(missing_ok=True)
        raise ExportError("There are no pixels to export: every pixel is masked out")
    return dest


def export_geopackage(work_dir, dest, masked: bool = True, include_series: bool = False, step: int = 1) -> Path:
    """A GeoPackage point layer (WGS84) with velocity and quality, and the series if asked.

    The velocity map as a table of points is what a GIS styles and queries; for the raster
    itself use the GeoTIFF."""
    from osgeo import gdal, ogr, osr

    gdal.UseExceptions()
    work_dir, dest = Path(work_dir), Path(dest)
    geo = _grid(work_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.unlink(missing_ok=True)
    driver = ogr.GetDriverByName("GPKG")
    source = driver.CreateDataSource(str(dest))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    layer = source.CreateLayer("mintpy_points", srs, ogr.wkbPoint)
    scalar = ("velocity_mm_yr", "velocity_std_mm_yr", "temporal_coherence", "mean_coherence")
    for name in scalar:
        layer.CreateField(ogr.FieldDefn(name, ogr.OFTReal))
    for name in ("row", "column"):
        layer.CreateField(ogr.FieldDefn(name, ogr.OFTInteger))
    dates = _dates(work_dir) if include_series else []
    for date in dates:
        layer.CreateField(ogr.FieldDefn(f"d{date}", ogr.OFTReal))
    definition = layer.GetLayerDefn()

    count = 0
    layer.StartTransaction()
    try:
        for row in _point_rows(work_dir, masked, max(1, int(step)), include_series, geo):
            feature = ogr.Feature(definition)
            for name, value in row.items():
                if name in ("latitude", "longitude"):
                    continue
                if isinstance(value, float) and math.isnan(value):
                    continue
                feature.SetField(name, value)
            point = ogr.Geometry(ogr.wkbPoint)
            point.AddPoint_2D(row["longitude"], row["latitude"])
            feature.SetGeometry(point)
            layer.CreateFeature(feature)
            count += 1
        layer.CommitTransaction()
    except Exception:
        layer.RollbackTransaction()
        raise
    for key, value in _description(geo).items():
        source.SetMetadataItem(key, value)
    source.FlushCache()
    source = None
    if count == 0:
        dest.unlink(missing_ok=True)
        raise ExportError("There are no pixels to export: every pixel is masked out")
    return dest


# ── NetCDF (CF conventions) ──────────────────────────────────────────────────

def export_netcdf(work_dir, dest, masked: bool = True) -> Path:
    """The velocity, quality layers and the displacement cube in one CF-1.8 NetCDF-4 file.

    Dimensions are `time`, `y` and `x` in the result's own projected CRS (named in the
    `crs` variable), so xarray, QGIS and GDAL place it without further information."""
    from osgeo import gdal, osr

    gdal.UseExceptions()
    work_dir, dest = Path(work_dir), Path(dest)
    geo = _grid(work_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.unlink(missing_ok=True)
    length, width = geo["length"], geo["width"]

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(geo["epsg"])
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

    dataset = gdal.GetDriverByName("netCDF").CreateMultiDimensional(str(dest))
    root = dataset.GetRootGroup()
    f64 = gdal.ExtendedDataType.Create(gdal.GDT_Float64)
    f32 = gdal.ExtendedDataType.Create(gdal.GDT_Float32)
    text = gdal.ExtendedDataType.CreateString()

    def attribute(target, name, value):
        made = target.CreateAttribute(name, [], text if isinstance(value, str) else f64)
        made.Write(value)

    dim_x = root.CreateDimension("x", gdal.DIM_TYPE_HORIZONTAL_X, None, width)
    dim_y = root.CreateDimension("y", gdal.DIM_TYPE_HORIZONTAL_Y, None, length)
    x_values = geo["x_first"] + (np.arange(width) + 0.5) * geo["x_step"]
    y_values = geo["y_first"] + (np.arange(length) + 0.5) * geo["y_step"]
    for name, dim, values, standard in (("x", dim_x, x_values, "projection_x_coordinate"),
                                        ("y", dim_y, y_values, "projection_y_coordinate")):
        variable = root.CreateMDArray(name, [dim], f64)
        variable.Write(values)
        attribute(variable, "standard_name", standard)
        attribute(variable, "units", "m")
        attribute(variable, "axis", name.upper())

    dates = _dates(work_dir)
    epoch = dt.date(1970, 1, 1)
    days = np.array([(dt.datetime.strptime(d, "%Y%m%d").date() - epoch).days for d in dates], dtype="float64")
    dim_t = root.CreateDimension("time", gdal.DIM_TYPE_TEMPORAL, None, len(dates))
    time_var = root.CreateMDArray("time", [dim_t], f64)
    time_var.Write(days)
    attribute(time_var, "standard_name", "time")
    attribute(time_var, "units", "days since 1970-01-01 00:00:00")
    attribute(time_var, "calendar", "proleptic_gregorian")
    attribute(time_var, "axis", "T")

    def create(name, dims, unit, long_name, blocks):
        # Compressed, in blocks: a displacement cube is mostly smooth or masked, so it shrinks to a
        # fraction, and a reader that wants one date or one pixel does not have to load the rest.
        variable = root.CreateMDArray(name, dims, f32, ["COMPRESS=DEFLATE", f"ZLEVEL={_NETCDF_ZLEVEL}", f"BLOCKSIZE={blocks}"])
        variable.SetNoDataValueDouble(float("nan"))
        attribute(variable, "long_name", long_name)
        attribute(variable, "units", unit)
        variable.SetSpatialRef(srs)
        return variable

    plane = f"{min(length, 512)},{min(width, 512)}"
    for layer in ("velocity", "velocity_std", "temporal_coherence", "coherence"):
        try:
            data = _read_layer(work_dir, layer, masked, geo)
        except (ExportError, KeyError):
            continue
        _, _, _, unit, description = _open(work_dir, layer)
        create(layer, [dim_y, dim_x], unit, description, plane).Write(np.ascontiguousarray(data, dtype="float32"))

    # The cube is written a block of dates at a time, so memory holds a few dates and not the whole
    # series (which for a large area is several gigabytes).
    series = create("displacement", [dim_t, dim_y, dim_x], "mm", LAYERS["timeseries"][4], f"1,{plane}")
    attribute(series, "comment", "relative to the reference pixel and the reference date")
    valid = _valid_mask(work_dir, (length, width)) if masked else None
    per_block = max(1, int(_NETCDF_BLOCK_BYTES // (length * width * 4)))
    with h5py.File(locate_timeseries_file(work_dir), "r") as handle:
        source = handle["timeseries"]
        for first in range(0, len(dates), per_block):
            block = source[first:first + per_block].astype("float32") * np.float32(1000.0)
            if valid is not None:
                block[:, ~valid] = np.nan
            series.Write(np.ascontiguousarray(block), array_start_idx=[first, 0, 0])

    attribute(root, "Conventions", "CF-1.8")
    for key, value in _description(geo).items():
        attribute(root, key, value)
    dataset = None
    return dest


def export_path(dest_dir, fmt: str, layer: str = "velocity", masked: bool = True, include_series: bool = True) -> Path:
    """Where `export` writes this request, so a caller can reuse a file that is already there."""
    if fmt not in FORMATS:
        raise ExportError(f"Unknown format '{fmt}'. Choose one of {', '.join(FORMATS)}")
    if fmt == "geotiff" and layer not in LAYERS:
        raise ExportError(f"Unknown layer '{layer}'. Choose one of {', '.join(LAYERS)}")
    everything = "" if masked else "_unmasked"
    series = "_series" if include_series else ""
    stem = {"geotiff": f"mintpy_{layer}{everything}", "netcdf": f"mintpy_results{everything}",
            "csv": f"mintpy_points{series}{everything}", "gpkg": f"mintpy_points{series}{everything}"}[fmt]
    return Path(dest_dir) / f"{stem}.{EXTENSIONS[fmt]}"


def export(work_dir, dest_dir, fmt: str, layer: str = "velocity", masked: bool = True, include_series: bool = True,
           corrections: Optional[dict] = None) -> Path:
    """Write `fmt` into `dest_dir` and return the file. Raises ExportError for a bad request."""
    work_dir = Path(work_dir)
    dest = export_path(dest_dir, fmt, layer, masked, include_series)
    try:
        if fmt == "geotiff":
            return export_geotiff(work_dir, dest, layer=layer, masked=masked, corrections=corrections)
        if fmt == "netcdf":
            return export_netcdf(work_dir, dest, masked=masked)
        if fmt == "csv":
            return export_csv(work_dir, dest, masked=masked, include_series=include_series)
        return export_geopackage(work_dir, dest, masked=masked, include_series=include_series)
    except FileNotFoundError as exc:
        raise ExportError(str(exc)) from exc
