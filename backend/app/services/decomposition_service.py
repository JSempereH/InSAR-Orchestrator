"""Vertical and east-west products from two projects, and their checks against EGMS L3 and GNSS.

The mathematics lives in insar_core.analysis.decomposition and insar_core.analysis.calibration. This module
does what those must not: find the two MintPy folders, keep the products in a folder of their own, draw a preview,
read the EGMS L3 tiles and the GNSS stations, and remember the answers so they can be shown again.

A decomposition is a folder under `<downloads>/decompositions/<id>/`:

    products/vertical.tif, east.tif, vertical_std.tif, east_std.tif   the result, cloud optimised GeoTIFFs
    summary.json                                                      how it was made (reference, geometry, assumptions)
    validation_l3.json, validation_gnss.json                          what the checks found
    preview.png                                                       both components side by side
"""
from __future__ import annotations

import json
import logging
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

from insar_core.adapters import egms_l3, ngl
from insar_core.analysis import calibration, decomposition, validation
from insar_core.pipeline.mintpy_timeseries import locate_velocity_file

from app.config import settings

logger = logging.getLogger(__name__)

PRODUCTS = "products"
BASE_LAYERS = tuple(decomposition.LAYERS)                     # vertical, east, vertical_std, east_std
CALIBRATED_LAYERS = ("vertical_calibrated", "east_calibrated", "vertical_std_calibrated", "east_std_calibrated")
_RESULT_NAMES = {"l3": "validation_l3.json", "gnss": "validation_gnss.json"}

_lock = threading.Lock()
_runs: dict[str, dict] = {}


class DecompositionServiceError(RuntimeError):
    """Something the user can fix, with the reason."""


def base_dir() -> Path:
    return Path(settings.downloads_dir) / "decompositions"


def folder_for(decomposition_id: str) -> Path:
    return base_dir() / decomposition_id


def _mintpy_dir(project_dir: Path) -> Path:
    return Path(project_dir) / "mintpy"


def _velocity_mtime(project_dir: Path) -> Optional[float]:
    try:
        return locate_velocity_file(_mintpy_dir(project_dir)).stat().st_mtime
    except Exception:
        return None


# ── creating ─────────────────────────────────────────────────────────────────

def compute(folder: Path, first_dir: Path, second_dir: Path, first_label: str, second_label: str,
            reference: Optional[dict] = None) -> dict:
    """Decompose the MintPy results of two projects into `folder` and return the summary.

    Raises DecompositionServiceError when the two cannot be combined (same direction, no overlap, no results)."""
    try:
        first = decomposition.read_los_product(_mintpy_dir(first_dir), label=first_label)
        second = decomposition.read_los_product(_mintpy_dir(second_dir), label=second_label)
        result = decomposition.decompose(first, second, reference=reference)
    except decomposition.DecompositionError as exc:
        raise DecompositionServiceError(str(exc)) from exc
    folder = Path(folder)
    try:
        decomposition.write_geotiffs(result, folder / PRODUCTS)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    summary = dict(result.summary)
    summary["created_at"] = datetime.now(timezone.utc).isoformat()
    summary["sources_mtime"] = {"first": _velocity_mtime(first_dir), "second": _velocity_mtime(second_dir)}
    (folder / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    return summary


def remove(folder: Path) -> None:
    shutil.rmtree(folder, ignore_errors=True)


# ── reading back ─────────────────────────────────────────────────────────────

def _read_tif(path: Path) -> tuple[np.ndarray, dict]:
    import rasterio
    with rasterio.open(path) as src:
        data = src.read(1).astype("float64")
        transform = src.transform
        geo = {"x_first": transform.c, "y_first": transform.f, "x_step": transform.a, "y_step": transform.e,
               "width": src.width, "length": src.height, "epsg": src.crs.to_epsg()}
    return data, geo


def _saved(folder: Path, kind: str) -> Optional[dict]:
    try:
        return json.loads((Path(folder) / _RESULT_NAMES[kind]).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def status(decomposition_id: str, folder: Path) -> dict:
    """For each check: not run, running, failed, or finished with its numbers."""
    out = {}
    for kind in _RESULT_NAMES:
        with _lock:
            run = _runs.get(f"{decomposition_id}:{kind}")
        if run and run["status"] == "RUNNING":
            out[kind] = {"status": "RUNNING", "progress": run.get("progress", "")}
        elif run and run["status"] == "FAILED":
            out[kind] = {"status": "FAILED", "error": run["error"]}
        else:
            saved = _saved(folder, kind)
            out[kind] = {"status": "SUCCEEDED", "result": saved} if saved else {"status": "NOT_RUN"}
    return out


def read_summary(folder: Path) -> dict:
    return json.loads((Path(folder) / "summary.json").read_text(encoding="utf-8"))


def stale(summary: dict, first_dir: Optional[Path], second_dir: Optional[Path]) -> bool:
    """True when either project's MintPy result was written again after the products were made."""
    sources = summary.get("sources_mtime") or {}
    for key, directory in (("first", first_dir), ("second", second_dir)):
        now = _velocity_mtime(directory) if directory is not None else None
        if now is not None and sources.get(key) is not None and now > float(sources[key]) + 1.0:
            return True
    return False


# ── preview ──────────────────────────────────────────────────────────────────

_BG, _SURFACE, _TEXT, _MUTED = "#07090a", "#0e1211", "#eef2f0", "#8a9490"
_GREEN, _RED = "#3dff9a", "#ff5d6c"


def preview(folder: Path) -> Path:
    """Both components side by side on the app's dark palette, red for down or west and green for up or east."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LinearSegmentedColormap

    folder = Path(folder)
    target = folder / "preview.png"
    newest = max((folder / PRODUCTS / f"{layer}.tif").stat().st_mtime for layer in ("vertical", "east"))
    if target.exists() and target.stat().st_mtime >= newest:
        return target
    cmap = LinearSegmentedColormap.from_list("insar", [_RED, _SURFACE, _GREEN])
    cmap.set_bad(_BG)
    figure, axes = plt.subplots(1, 2, figsize=(12, 5.2), facecolor=_BG)
    for axis, layer, title in zip(axes, ("vertical", "east"), ("Vertical, mm/year (green up)", "East-west, mm/year (green east)"), strict=False):
        data, _ = _read_tif(folder / PRODUCTS / f"{layer}.tif")
        finite = data[np.isfinite(data)]
        limit = float(np.percentile(np.abs(finite), 98)) if finite.size else 1.0
        axis.set_facecolor(_BG)
        image = axis.imshow(data, cmap=cmap, vmin=-limit, vmax=limit, interpolation="nearest")
        axis.set_title(title, color=_TEXT, fontsize=11, loc="left")
        axis.set_xticks([])
        axis.set_yticks([])
        for spine in axis.spines.values():
            spine.set_edgecolor("#1c2321")
        bar = figure.colorbar(image, ax=axis, fraction=0.046, pad=0.02)
        bar.ax.yaxis.set_tick_params(color=_MUTED, labelcolor=_MUTED)
        bar.outline.set_edgecolor("#1c2321")
    figure.tight_layout()
    figure.savefig(target, dpi=110, facecolor=_BG)
    plt.close(figure)
    return target


# ── EGMS L3 ──────────────────────────────────────────────────────────────────

def _bbox(geo: dict) -> tuple[float, float, float, float]:
    from app.services import validation_service
    return validation_service._grid_bbox(geo)


def _l3_paths(download: dict) -> list[Path]:
    return [Path(download["destination_path"]) / name for name in download["filenames"]
            if (Path(download["destination_path"]) / name).exists()]


def validate_l3(folder: Path, up_download: dict, east_download: dict) -> dict:
    """Compare the vertical and east-west maps with EGMS L3 (the five year mean, calibrated to GNSS).

    The two downloads are the EGMS rows (`destination_path`, `filenames`) of the ORTHO-UP and ORTHO-EAST products."""
    folder = Path(folder)
    for download, product in ((up_download, "ORTHO-UP"), (east_download, "ORTHO-EAST")):
        if download.get("level") != "L3" or download.get("product_type") != product:
            raise DecompositionServiceError(f"Expected an EGMS L3 {product} download, got "
                                            f"{download.get('level')} {download.get('product_type')}.")
    summary = read_summary(folder)
    out: dict = {"source_name": "EGMS L3 (ortho)", "release": up_download.get("release"),
                 "window_note": ("EGMS L3 is a mean over the years of its release, and this map covers "
                                 f"{summary['ascending']['start']} to {summary['ascending']['end']}. Ground that changes speed between "
                                 "the two shows up as a difference that is not an error of either."),
                 "computed_at": datetime.now(timezone.utc).isoformat()}
    for layer, download in (("vertical", up_download), ("east", east_download)):
        velocity, geo = _read_tif(folder / PRODUCTS / f"{layer}.tif")
        std, _ = _read_tif(folder / PRODUCTS / f"{layer}_std.tif")
        try:
            grid, grid_geo = egms_l3.read_velocity(_l3_paths(download), _bbox(geo))
            out[layer] = validation.compare_with_velocity_grid(
                geo=geo, velocity_mm=velocity, std_mm=std, grid_geo=grid_geo, grid_velocity_mm=grid)
        except (egms_l3.EGMSL3Error, ValueError) as exc:
            out[layer] = {"error": str(exc)}
    if all("error" in out[layer] for layer in ("vertical", "east")):
        raise DecompositionServiceError(out["vertical"]["error"])
    (folder / _RESULT_NAMES["l3"]).write_text(json.dumps(out), encoding="utf-8")
    return out


# ── GNSS ─────────────────────────────────────────────────────────────────────

def _window(summary: dict) -> tuple[str, str]:
    def day(text: str) -> str:
        return str(text).replace("-", "")[:8]
    start = max(day(summary["ascending"]["start"]), day(summary["descending"]["start"]))
    end = min(day(summary["ascending"]["end"]), day(summary["descending"]["end"]))
    if start >= end:
        raise DecompositionServiceError("The two projects do not cover the same dates, so there is no common window to "
                                        "compare GNSS velocities over.")
    return start, end


def _run_gnss(decomposition_id: str, folder: Path) -> None:
    key = f"{decomposition_id}:gnss"

    def progress(text: str) -> None:
        with _lock:
            _runs[key]["progress"] = text

    try:
        summary = read_summary(folder)
        start, end = _window(summary)
        vertical, geo = _read_tif(folder / PRODUCTS / "vertical.tif")
        vertical_std, _ = _read_tif(folder / PRODUCTS / "vertical_std.tif")
        east, _ = _read_tif(folder / PRODUCTS / "east.tif")
        east_std, _ = _read_tif(folder / PRODUCTS / "east_std.tif")
        progress("Finding GNSS stations")
        first = datetime.strptime(start, "%Y%m%d").date()
        last = datetime.strptime(end, "%Y%m%d").date()
        listed = ngl.stations_in_area(_bbox(geo), first, last)
        if not listed:
            raise DecompositionServiceError("No GNSS station of the Nevada Geodetic Laboratory recorded over the whole window "
                                            "inside the area.")
        stations, skipped = [], []
        for number, station in enumerate(listed, start=1):
            progress(f"Downloading GNSS station {station['name']} ({number} of {len(listed)})")
            try:
                frame, series = ngl.fetch_series(station["name"])
            except ngl.NGLError:
                skipped.append(station["name"])
                continue
            stations.append({"name": station["name"], "lat": station["lat"], "lon": station["lon"], "frame": frame,
                             "velocity": calibration.gnss_velocity(series, start, end)})
        progress("Comparing")
        out: dict = {"source_name": "GNSS (Nevada Geodetic Laboratory)", "citation": ngl.CITATION, "window": [start, end],
                     "stations_found": len(listed), "stations_skipped": skipped,
                     "stations_without_enough_data": [s["name"] for s in stations if not s["velocity"]],
                     "computed_at": datetime.now(timezone.utc).isoformat()}
        for quantity, array, std in (("up", vertical, vertical_std), ("east", east, east_std)):
            try:
                out["vertical" if quantity == "up" else "east"] = calibration.calibrate(
                    velocity=array, std=std, geo=geo, stations=stations, quantity=quantity)
            except ValueError as exc:
                out["vertical" if quantity == "up" else "east"] = {"applied": False, "error": str(exc), "n": 0}
        if all(out[layer].get("error") for layer in ("vertical", "east")):
            raise DecompositionServiceError(out["vertical"]["error"])
        (folder / _RESULT_NAMES["gnss"]).write_text(json.dumps(out), encoding="utf-8")
        with _lock:
            _runs[key]["status"] = "SUCCEEDED"
    except Exception as exc:
        if not isinstance(exc, (DecompositionServiceError, ngl.NGLError, ValueError)):
            logger.exception("GNSS check of decomposition %s failed", decomposition_id)
        with _lock:
            _runs[key].update(status="FAILED", error=str(exc))


def start_gnss(decomposition_id: str, folder: Path) -> dict:
    """Check the vertical and east-west maps against GNSS stations in the area, in the background."""
    key = f"{decomposition_id}:gnss"
    with _lock:
        existing = _runs.get(key)
        if existing and existing["status"] == "RUNNING":
            raise DecompositionServiceError("A GNSS check is already running for this decomposition")
        _runs[key] = {"status": "RUNNING", "progress": ""}
    threading.Thread(target=_run_gnss, args=(decomposition_id, Path(folder)), daemon=True).start()
    return status(decomposition_id, folder)


def forget(decomposition_id: str) -> None:
    with _lock:
        for kind in _RESULT_NAMES:
            _runs.pop(f"{decomposition_id}:{kind}", None)


# ── export ───────────────────────────────────────────────────────────────────

def corrections(folder: Path) -> dict:
    """The shifts the GNSS check found for each component (only the ones the stations confirmed) and the factor by
    which the stated error was too small against EGMS L3 (never below 1)."""
    out: dict = {"vertical_offset": None, "east_offset": None, "std_scale": {"vertical": None, "east": None}}
    gnss, l3 = _saved(folder, "gnss"), _saved(folder, "l3")
    for layer in ("vertical", "east"):
        found = (gnss or {}).get(layer) or {}
        if found.get("applied"):
            out[f"{layer}_offset"] = float(found["offset_mm_yr"])
        unc = ((l3 or {}).get(layer) or {}).get("uncertainty") or {}
        if unc.get("recommended_scale"):
            out["std_scale"][layer] = float(unc["recommended_scale"])
    return out


def export(folder: Path, layer: str) -> Path:
    """The file of one layer. A calibrated layer is made from the plain one and the checks' findings, and is refused
    with the reason when the check that would justify it has not been run or did not confirm it."""
    import rasterio
    import rasterio.shutil

    folder = Path(folder)
    if layer in BASE_LAYERS:
        path = folder / PRODUCTS / f"{layer}.tif"
        if not path.exists():
            raise DecompositionServiceError("The products of this decomposition are missing")
        return path
    if layer not in CALIBRATED_LAYERS:
        raise DecompositionServiceError(f"Unknown layer '{layer}'. Choose one of {', '.join(BASE_LAYERS + CALIBRATED_LAYERS)}")
    plain = layer.replace("_calibrated", "")
    component = plain.replace("_std", "")
    found = corrections(folder)
    is_std = plain.endswith("_std")
    scale = offset = 0.0
    if is_std:
        measured = found["std_scale"][component]
        if measured is None:
            raise DecompositionServiceError("The stated error has not been checked against EGMS L3 for this decomposition. "
                                            "Run that check first.")
        scale = measured
        tags = {"uncertainty_scale": f"{scale:.3f}", "calibration": "stated standard error times the factor measured against EGMS L3"}
    else:
        shift = found[f"{component}_offset"]
        if shift is None:
            raise DecompositionServiceError(f"There is no GNSS calibration for the {component} component: run the GNSS check, "
                                            "and note that a shift the stations could not confirm is not applied.")
        offset = shift
        tags = {"calibration_shift_mm_yr": f"{offset:+.3f}", "calibration": "shift estimated from GNSS stations inside the area, "
                                                                               "tested on stations left out"}
        if component == "east":
            tags["frame"] = "relative to the average horizontal motion of the stations, not to a plate"
    out_dir = folder / "exports"
    out_dir.mkdir(exist_ok=True)
    target, draft = out_dir / f"{layer}.tif", out_dir / f"{layer}.draft.tif"
    with rasterio.open(folder / PRODUCTS / f"{plain}.tif") as src:
        data = src.read(1).astype("float32")
        data = data * np.float32(scale) if is_std else data + np.float32(offset)
        profile = src.profile
        old_tags = src.tags()
    profile.update(driver="GTiff")
    with rasterio.open(draft, "w", **profile) as out:
        out.write(data, 1)
        out.set_band_description(1, layer)
        out.update_tags(**{**old_tags, **tags, "layer": layer})
    try:
        rasterio.shutil.copy(draft, target, driver="COG", compress="DEFLATE", overview_resampling="average")
    finally:
        draft.unlink(missing_ok=True)
    return target
