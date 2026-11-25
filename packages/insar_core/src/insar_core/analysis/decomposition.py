"""Vertical and east-west motion from an ascending and a descending MintPy result.

A line of sight (LOS) velocity mixes vertical and horizontal motion, and one track cannot
tell them apart. Two tracks that look at the same ground from opposite sides can: the LOS
of an ascending pass looks east-ish and up, a descending one west-ish and up, so the same
vertical motion shows the same in both and the east-west motion shows with opposite signs.

How the separation is made, and what it assumes

1. Both velocity maps are put on the grid of the first one (bilinear). Pixels that either
   run has masked out stay masked.
2. Each MintPy result is relative to its own reference pixel, and the two reference pixels
   are not the same place. Both maps are shifted so that the median of a small window around
   one common point is zero (by default a reference point MintPy chose, see `_common_reference`). That assumes the ground does not move, vertically or
   east-west, at that point. It is the same assumption MintPy makes for one track, now shared.
3. For every pixel the two LOS values are written as `v = w_up * up + w_east * east`, with
   the weights from that pixel's own incidence and azimuth, and the 2x2 system is solved.
4. The north component is taken as zero. Sentinel-1 flies nearly north-south and looks
   nearly east-west, so its LOS is almost blind to north motion, but not entirely: the part
   of the LOS that sees north is a share of a few percent to about fifteen percent of the
   motion. A north motion that is the same everywhere cancels with the common reference;
   what leaks is north motion that differs across the area. `north_leakage` states how much
   of a difference of 1 mm/year in north motion shows up in the vertical and the east-west
   results, so a reader can judge it.
5. The uncertainty of each component comes from the standard error MintPy reports for each
   LOS velocity, carried through the same 2x2 system. Where the system is poorly conditioned
   (the two lines of sight too alike) the result is masked: there the two tracks do not
   carry two independent pieces of information.

MintPy's velocity standard error comes from the fit of a straight line to a time series
with atmospheric noise that is correlated in time, so it is smaller than the real error.
`std_scale` lets the caller inflate it by a factor measured against an independent source
(see `insar_core.analysis.calibration.uncertainty_scale`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import h5py
import numpy as np
import pyproj

from insar_core.analysis.validation import los_unit_vector
from insar_core.pipeline.mintpy_timeseries import _read_geocoding, locate_velocity_file, read_reference_point

MAX_CONDITION = 8.0          # beyond this the two lines of sight are too alike to separate two components
REFERENCE_HALF_WINDOW = 2    # a 5 x 5 window around the common reference point
MIN_COMMON_PIXELS = 100


class DecompositionError(RuntimeError):
    """The two results cannot be combined, and the message says why."""


@dataclass
class LosProduct:
    """One MintPy result, read for decomposition. Velocities are in mm/year, positive towards the satellite."""
    velocity: np.ndarray
    std: np.ndarray
    incidence: np.ndarray
    azimuth: np.ndarray
    geo: dict
    direction: str                       # "ascending" or "descending"
    reference: Optional[dict] = None
    start_date: str = ""
    end_date: str = ""
    label: str = ""


@dataclass
class Decomposition:
    vertical: np.ndarray
    east: np.ndarray
    vertical_std: np.ndarray
    east_std: np.ndarray
    condition: np.ndarray
    geo: dict
    summary: dict = field(default_factory=dict)


def _geometry_file(work_dir: Path) -> Path:
    for candidate in (work_dir / "inputs" / "geometryGeo.h5", work_dir / "geometryGeo.h5"):
        if candidate.is_file():
            return candidate
    raise DecompositionError(f"{work_dir} has no geometryGeo.h5: run MintPy on it first")


def read_los_product(work_dir, label: str = "") -> LosProduct:
    """Velocity, its standard error and the viewing geometry of a finished MintPy run."""
    work_dir = Path(work_dir)
    try:
        velocity_file = locate_velocity_file(work_dir)
    except Exception as exc:
        raise DecompositionError(f"{work_dir} has no velocity file: run MintPy on it first") from exc
    with h5py.File(velocity_file, "r") as handle:
        geo = _read_geocoding(handle)
        attrs = {k: (v.decode() if isinstance(v, bytes) else v) for k, v in handle.attrs.items()}
        # float32 throughout: MintPy stores float32, and a velocity in mm/year needs no more. Four
        # layers per track and the reprojected copies make this half the memory of float64.
        velocity = handle["velocity"][:].astype("float32") * np.float32(1000.0)
        std = (handle["velocityStd"][:].astype("float32") * np.float32(1000.0)) if "velocityStd" in handle else np.full_like(velocity, np.nan)
    mask_file = work_dir / "maskTempCoh.h5"
    if mask_file.is_file():
        with h5py.File(mask_file, "r") as handle:
            keep = handle["mask"][:].astype(bool)
        velocity[~keep] = np.nan
        std[~keep] = np.nan
    with h5py.File(_geometry_file(work_dir), "r") as handle:
        incidence = handle["incidenceAngle"][:].astype("float32")
        azimuth = handle["azimuthAngle"][:].astype("float32")
    direction = str(attrs.get("ORBIT_DIRECTION", "")).lower()
    if direction not in ("ascending", "descending"):
        # Heading is about -10 degrees for an ascending pass and about -170 for a descending one.
        heading = float(attrs.get("HEADING", "nan"))
        if not np.isfinite(heading):
            raise DecompositionError(f"{work_dir.name}: cannot tell whether the track is ascending or descending")
        direction = "descending" if abs(heading) > 90 else "ascending"
    return LosProduct(
        velocity=velocity, std=std, incidence=incidence, azimuth=azimuth, geo=geo, direction=direction,
        reference=read_reference_point(velocity_file), start_date=str(attrs.get("START_DATE", "")),
        end_date=str(attrs.get("END_DATE", "")), label=label or work_dir.parent.name,
    )


def _transform(geo: dict):
    from rasterio.transform import Affine
    return Affine(geo["x_step"], 0.0, geo["x_first"], 0.0, geo["y_step"], geo["y_first"])


def resample(product: LosProduct, geo: dict) -> LosProduct:
    """The product on another grid, with bilinear interpolation that skips masked pixels."""
    from rasterio.crs import CRS
    from rasterio.warp import Resampling, reproject

    if (product.geo["epsg"], product.geo["x_first"], product.geo["y_first"], product.geo["x_step"], product.geo["y_step"],
            product.geo["width"], product.geo["length"]) == (geo["epsg"], geo["x_first"], geo["y_first"], geo["x_step"],
                                                              geo["y_step"], geo["width"], geo["length"]):
        return product
    out = {}
    for name in ("velocity", "std", "incidence", "azimuth"):
        destination = np.full((geo["length"], geo["width"]), np.nan, "float32")
        reproject(
            np.asarray(getattr(product, name), "float32"), destination, src_transform=_transform(product.geo), src_crs=CRS.from_epsg(product.geo["epsg"]),
            dst_transform=_transform(geo), dst_crs=CRS.from_epsg(geo["epsg"]), src_nodata=np.nan, dst_nodata=np.nan,
            resampling=Resampling.bilinear, init_dest_nodata=True,
        )
        out[name] = destination
    return LosProduct(geo=dict(geo), direction=product.direction, reference=product.reference, start_date=product.start_date,
                      end_date=product.end_date, label=product.label, **out)


def _pixel(geo: dict, lat: float, lon: float) -> tuple[int, int]:
    x, y = pyproj.Transformer.from_crs(4326, geo["epsg"], always_xy=True).transform(lon, lat)
    return int(np.floor((y - geo["y_first"]) / geo["y_step"])), int(np.floor((x - geo["x_first"]) / geo["x_step"]))


def _window_median(array: np.ndarray, row: int, column: int, half: int) -> float:
    block = array[max(row - half, 0): row + half + 1, max(column - half, 0): column + half + 1]
    finite = block[np.isfinite(block)]
    return float(np.median(finite)) if finite.size >= max(3, block.size // 3) else float("nan")


def _common_reference(ascending: LosProduct, descending: LosProduct, reference: Optional[dict]) -> dict:
    """Where both maps are made to read zero: the given point, or else a MintPy reference point.

    Without a given point the ascending result's reference point is tried first and the descending one's second, and the
    first that has valid pixels in both maps is used. The order of the two results handed to `decompose` does not matter."""
    if reference:
        candidates = [reference]
    else:
        candidates = [p.reference for p in (ascending, descending) if p.reference and "lat" in p.reference]
    if not candidates:
        raise DecompositionError("There is no reference point to line the two results up on: give a latitude and a longitude")
    problems: list[str] = []
    for point in candidates:
        row, column = _pixel(ascending.geo, float(point["lat"]), float(point["lon"]))
        length, width = ascending.velocity.shape
        if not (0 <= row < length and 0 <= column < width):
            problems.append("The reference point is outside the area both results cover")
            continue
        offsets = [_window_median(p.velocity, row, column, REFERENCE_HALF_WINDOW) for p in (ascending, descending)]
        if not all(np.isfinite(offsets)):
            problems.append("The reference point has no valid pixels in one of the two results: pick a point in the "
                            "area both cover with coherent pixels")
            continue
        return {"lat": float(point["lat"]), "lon": float(point["lon"]), "row": row, "column": column, "offsets_mm_yr": offsets}
    raise DecompositionError(" / ".join(dict.fromkeys(problems)))


def period_overlap(first: LosProduct, second: LosProduct) -> Optional[dict]:
    """How much of the two results' periods is common, or None when a result does not state its dates.

    A velocity is the mean rate over the period of its own result. Two velocities over different
    periods are only the same quantity where the ground moves at a constant rate; where it speeds
    up, slows down or reverses, combining them mixes two different rates and the vertical and
    east-west results are wrong by the difference. `share` is the common time over the longer of
    the two periods (1 when they are the same, 0 when they do not touch)."""
    import datetime as dt
    try:
        spans = [(dt.datetime.strptime(p.start_date, "%Y%m%d"), dt.datetime.strptime(p.end_date, "%Y%m%d")) for p in (first, second)]
    except ValueError:
        return None
    common = (min(spans[0][1], spans[1][1]) - max(spans[0][0], spans[1][0])).days
    longest = max((end - start).days for start, end in spans)
    return {"common_days": max(common, 0), "share": max(common, 0) / longest if longest > 0 else 0.0}


MIN_PERIOD_SHARE = 0.8       # below this the two velocities are warned about as covering different periods


def north_leakage(first: LosProduct, second: LosProduct, valid: np.ndarray) -> dict:
    """What a difference of 1 mm/year in north motion between a pixel and the reference appears as in the vertical
    and east results, given the assumption that there is none. Median of the absolute value over the valid pixels."""
    wa = los_unit_vector(first.incidence, first.azimuth)
    wd = los_unit_vector(second.incidence, second.azimuth)
    # Weights are (east, north, up). With A = [[up_a, east_a], [up_d, east_d]], [up, east] = A^-1 [v_a, v_d],
    # and a north motion n adds north_a * n to v_a and north_d * n to v_d.
    a11, a12, a21, a22 = wa[2], wa[0], wd[2], wd[0]
    det = a11 * a22 - a12 * a21
    with np.errstate(divide="ignore", invalid="ignore"):
        to_up = (a22 * wa[1] - a12 * wd[1]) / det
        to_east = (-a21 * wa[1] + a11 * wd[1]) / det
    return {"into_vertical": float(np.nanmedian(np.abs(to_up[valid]))), "into_east": float(np.nanmedian(np.abs(to_east[valid])))}


def decompose(first: LosProduct, second: LosProduct, reference: Optional[dict] = None, std_scale: float = 1.0,
              max_condition: float = MAX_CONDITION) -> Decomposition:
    """Vertical and east-west velocity (mm/year, up and east positive) from two tracks of opposite direction.

    `first` and `second` can be given in either order but must look from different sides. `reference` is
    `{"lat": ..., "lon": ...}`, the point both are made to read zero at."""
    if first.direction == second.direction:
        raise DecompositionError(f"Both results are {first.direction}: a vertical and east-west split needs one ascending and "
                                 "one descending result")
    if std_scale <= 0:
        raise DecompositionError("std_scale must be above 0")
    ascending, descending = (first, second) if first.direction == "ascending" else (second, first)
    descending = resample(descending, ascending.geo)
    point = _common_reference(ascending, descending, reference)
    # `offsets_mm_yr` follows the order the two were handed to `_common_reference`: ascending, then descending.
    v_a = ascending.velocity - point["offsets_mm_yr"][0]
    v_d = descending.velocity - point["offsets_mm_yr"][1]
    s_a, s_d = ascending.std * std_scale, descending.std * std_scale

    valid = np.isfinite(v_a) & np.isfinite(v_d) & np.isfinite(ascending.incidence) & np.isfinite(descending.incidence)
    if int(valid.sum()) < MIN_COMMON_PIXELS:
        raise DecompositionError(f"Only {int(valid.sum())} pixels are valid in both results: the two areas hardly overlap or "
                                 "most pixels were masked")
    wa = los_unit_vector(ascending.incidence, ascending.azimuth)
    wd = los_unit_vector(descending.incidence, descending.azimuth)
    a11, a12, a21, a22 = wa[2], wa[0], wd[2], wd[0]          # rows: ascending, descending; columns: up, east
    det = a11 * a22 - a12 * a21
    with np.errstate(divide="ignore", invalid="ignore"):
        up = (a22 * v_a - a12 * v_d) / det
        east = (-a21 * v_a + a11 * v_d) / det
        up_std = np.sqrt((a22 * s_a) ** 2 + (a12 * s_d) ** 2) / np.abs(det)
        east_std = np.sqrt((a21 * s_a) ** 2 + (a11 * s_d) ** 2) / np.abs(det)
        # Condition number of the 2 x 2 matrix, from its singular values.
        trace = a11 ** 2 + a12 ** 2 + a21 ** 2 + a22 ** 2
        discriminant = np.sqrt(np.maximum(trace ** 2 - 4 * det ** 2, 0.0))
        condition = np.sqrt((trace + discriminant) / np.maximum(trace - discriminant, 1e-12))
    keep = valid & np.isfinite(condition) & (condition <= max_condition)
    for array in (up, east, up_std, east_std):
        array[~keep] = np.nan
    condition = np.where(valid, condition, np.nan)
    if int(keep.sum()) < MIN_COMMON_PIXELS:
        raise DecompositionError(
            "The two lines of sight are too alike to separate vertical from east-west motion (condition number above "
            f"{max_condition:g} on almost every pixel). Check that the two results are from opposite directions of the same area.")

    leakage = north_leakage(ascending, descending, keep)
    overlap = period_overlap(ascending, descending)
    warnings: list[str] = []
    if overlap is None:
        warnings.append("A result does not state its dates, so it could not be checked that both cover the same period.")
    elif overlap["common_days"] == 0:
        warnings.append("The two results cover periods that do not overlap. Their velocities are the same quantity only "
                        "where the ground moves at a constant rate.")
    elif overlap["share"] < MIN_PERIOD_SHARE:
        warnings.append(f"The two results share only {overlap['share']:.0%} of their period. Where the ground does not move at "
                        "a constant rate, the vertical and east-west velocities mix two different periods.")
    summary = {
        "period_overlap": overlap, "warnings": warnings,
        "ascending": {"label": ascending.label, "start": ascending.start_date, "end": ascending.end_date},
        "descending": {"label": descending.label, "start": descending.start_date, "end": descending.end_date},
        "reference": point, "std_scale": std_scale, "pixels": int(keep.sum()), "masked_for_geometry": int((valid & ~keep).sum()),
        "median_condition_number": float(np.nanmedian(condition[keep])), "north_leakage": leakage,
        "vertical_std_median_mm_yr": float(np.nanmedian(up_std[keep])) if np.isfinite(up_std[keep]).any() else None,
        "east_std_median_mm_yr": float(np.nanmedian(east_std[keep])) if np.isfinite(east_std[keep]).any() else None,
        "assumptions": ["north motion is zero", "the ground does not move at the reference point"],
    }
    return Decomposition(vertical=up, east=east, vertical_std=up_std, east_std=east_std, condition=condition,
                         geo=dict(ascending.geo), summary=summary)


LAYERS = {
    "vertical": ("Vertical velocity, positive up", "mm/year"),
    "east": ("East-west velocity, positive towards the east", "mm/year"),
    "vertical_std": ("Standard error of the vertical velocity", "mm/year"),
    "east_std": ("Standard error of the east-west velocity", "mm/year"),
}


def write_geotiffs(result: Decomposition, dest_dir) -> dict[str, Path]:
    """Each layer as a cloud optimised GeoTIFF in `dest_dir`; returns {layer: path}."""
    import rasterio
    import rasterio.shutil

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    geo = result.geo
    written = {}
    for layer, (description, unit) in LAYERS.items():
        data = getattr(result, layer).astype("float32")
        path = dest_dir / f"{layer}.tif"
        draft = path.with_suffix(".draft.tif")
        with rasterio.open(draft, "w", driver="GTiff", height=geo["length"], width=geo["width"], count=1, dtype="float32",
                           crs=pyproj.CRS.from_epsg(geo["epsg"]), transform=_transform(geo), nodata=float("nan")) as out:
            out.write(data, 1)
            out.set_band_description(1, layer)
            out.update_tags(layer=layer, units=unit, description=description, sign_convention="vertical up positive, east positive",
                            reference_lat=str(result.summary["reference"]["lat"]), reference_lon=str(result.summary["reference"]["lon"]),
                            assumptions="; ".join(result.summary["assumptions"]),
                            note="values are relative to the reference point, where the ground is assumed not to move")
        try:
            rasterio.shutil.copy(draft, path, driver="COG", compress="DEFLATE", overview_resampling="average")
        finally:
            draft.unlink(missing_ok=True)
        written[layer] = path
    return written
