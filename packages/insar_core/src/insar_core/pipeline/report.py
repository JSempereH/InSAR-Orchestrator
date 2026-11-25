"""One page that says what a result is, how it was made and how far to trust it.

A velocity map on its own cannot be published or handed to someone else: the reader needs the
period, the track, the pairs, the corrections, the reference point, the quality of the run, how
it compares with independent data and what to cite. All of that is already on disk or in the
project's records. `build_report` gathers it into a single HTML file with no outside
dependencies (the figures are inside it), which opens in any browser, prints to PDF and can go
into the supplementary material of a paper as it is.

What the report does not do is judge. It states the numbers and the checks with their
thresholds, and the limits of what the method can say, and leaves the conclusion to the reader.

The inputs are a MintPy work directory and, optionally, two dictionaries the caller already has:
the provenance record (project, pairs, software, options) and the validation results. Every
section is left out when its input is missing, so a run with no validation still gets a report.
"""
from __future__ import annotations

import base64
import datetime as dt
import html
import io
import json
from pathlib import Path
from typing import Any, Optional

import h5py
import numpy as np

from insar_core.common import pixel_metres
from insar_core.pipeline.mintpy_qc import compute_qc
from insar_core.pipeline.mintpy_timeseries import (
    _read_geocoding, _series_behind_velocity, locate_timeseries_file, locate_velocity_file, read_reference_point,
)

# The app's palette: black and green, red for motion away from the satellite and for failures, amber for warnings.
BG, PANEL, BORDER = "#07090a", "#0e1413", "#1f2927"
TEXT, MUTED, GREEN, RED, AMBER = "#eef2f0", "#8a9490", "#3dff9a", "#ff5d6c", "#ffb020"
_STATUS_COLOR = {"ok": GREEN, "warn": AMBER, "fail": RED, "unknown": MUTED}
_STATUS_WORD = {"ok": "fine", "warn": "look at this", "fail": "problem", "unknown": "not checked"}

# What each suffix of MintPy's final time series file name means: the corrections that were applied to it.
_CORRECTIONS = (
    ("ERA5", "Tropospheric delay removed with ERA5 weather data (PyAPS)"),
    ("tropHgt", "Tropospheric delay removed by its correlation with height"),
    ("SET", "Solid Earth tides removed"),
    ("ion", "Ionospheric delay removed"),
    ("ramp", "A ramp removed from every date"),
    ("demErr", "DEM error estimated and removed"),
)

CITATIONS = {
    "sentinel1": "Contains modified Copernicus Sentinel data, processed by ESA.",
    "sbas": "Berardino, P., Fornaro, G., Lanari, R., and Sansosti, E. (2002), A new algorithm for surface deformation monitoring "
            "based on small baseline differential SAR interferograms, IEEE Trans. Geosci. Remote Sens., 40(11), 2375-2383",
    "mintpy": "Yunjun, Z., Fattahi, H., and Amelung, F. (2019), Small baseline InSAR time series analysis: Unwrapping error "
              "correction and noise reduction, Computers and Geosciences, 133, 104331",
    "hyp3": "Hogenson, K., et al. (2020), Hybrid Pluggable Processing Pipeline (HyP3): A cloud-native infrastructure for "
            "generic processing of SAR data, Zenodo. ASF DAAC HyP3, using the GAMMA or ISCE2 "
            "software named below.",
    "gamma": "Werner, C., Wegmuller, U., Strozzi, T., and Wiesmann, A. (2000), GAMMA SAR and interferometric processing "
             "software, Proceedings of the ERS-ENVISAT Symposium, Gothenburg.",
    "isce2": "Rosen, P. A., Gurrola, E., Sacco, G. F., and Zebker, H. (2012), The InSAR Scientific Computing Environment, "
             "Proceedings of EUSAR 2012, 730-733.",
    "nisar": "NASA-ISRO SAR (NISAR) L2 GUNW products, distributed by the Alaska Satellite Facility DAAC.",
    "era5": "Hersbach, H., et al. (2020), The ERA5 global reanalysis, Q. J. R. Meteorol. Soc., 146, 1999-2049",
    "pyaps": "Jolivet, R., Grandin, R., Lasserre, C., Doin, M.-P., and Peltzer, G. (2011), Systematic InSAR tropospheric phase "
             "delay corrections from global meteorological reanalysis data, Geophys. Res. Lett., 38, L17311",
    "egms": "European Ground Motion Service (EGMS), Copernicus Land Monitoring Service. Crosetto, M., et al. (2020), The "
            "evolution of wide-area DInSAR: from regional and national services to the European Ground Motion Service, "
            "Remote Sensing, 12(12), 2043",
}

LIMITS = (
    "Every value is along the satellite's line of sight, which mixes vertical and horizontal motion. A negative value is "
    "motion away from the satellite. It is subsidence only where the ground does not move horizontally.",
    "Every value is relative to the reference point and the reference date stated above. If the reference point itself "
    "moves, every other value is shifted by the same amount.",
    "The standard error MintPy states for a pixel comes from the fit of a straight line, is relative to the reference "
    "point, and treats the noise of each date as independent. It cannot say how well the difference between two nearby "
    "pixels is known, which is far better than either is known relative to a distant reference. The section on precision "
    "gives that, and what it leaves out. Where a validation against an independent source measured the real error, it "
    "is given in this report.",
    "A velocity describes the period of the result. It is a long-term rate only where the ground moves steadily. Over a "
    "year or less a seasonal cycle alone changes it by millimetres per year.",
    "Pixels that lost coherence (vegetation, water, snow, fast change) are masked and say nothing. Motion faster than "
    "about a quarter of the wavelength between two acquisitions of neighbouring pixels can be lost to unwrapping errors.",
)


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _number(value: Any, digits: int = 1, unit: str = "") -> str:
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return "n/a"
    try:
        text = f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return _esc(value)
    return f"{text} {unit}".strip()


def _date(text: Any) -> str:
    text = str(text or "")
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}" if len(text) == 8 and text.isdigit() else text


# ── what is on disk ──────────────────────────────────────────────────────────

def read_result(work_dir) -> dict:
    """The facts of a finished MintPy run, read from its files: period, geometry, corrections, numbers."""
    work_dir = Path(work_dir)
    velocity_file = locate_velocity_file(work_dir)
    with h5py.File(velocity_file, "r") as handle:
        attrs = {k: (v.decode() if isinstance(v, bytes) else v) for k, v in handle.attrs.items()}
        geo = _read_geocoding(handle)
        velocity = handle["velocity"][:].astype("float32") * np.float32(1000.0)
        std = handle["velocityStd"][:].astype("float32") * np.float32(1000.0) if "velocityStd" in handle else None
    mask = None
    mask_file = work_dir / "maskTempCoh.h5"
    if mask_file.is_file():
        with h5py.File(mask_file, "r") as handle:
            mask = handle["mask"][:].astype(bool)
    kept = np.isfinite(velocity) & (mask if mask is not None else True)
    dates: list[str] = []
    series_name = ""
    try:
        series_file = locate_timeseries_file(work_dir)
        series_name = series_file.name
        with h5py.File(series_file, "r") as handle:
            dates = [d.decode() for d in handle["date"][:]]
    except (FileNotFoundError, OSError, KeyError):
        pass
    behind = _series_behind_velocity(velocity_file)
    stem = (behind.name if behind is not None else series_name).replace(".h5", "")
    parts = stem.split("_")[1:]
    corrections = [text for key, text in _CORRECTIONS if key in parts]
    from insar_core.analysis import unwrap_correction

    unwrapping = unwrap_correction.read_summary(work_dir)
    if unwrapping:
        fixed = unwrapping["pixels"].get("corrected", 0) + unwrapping["pixels"].get("corrected_with_neighbours", 0)
        corrections.insert(0, f"Unwrapping errors of whole cycles corrected from the closure of interferogram triplets, in {fixed} "
                              f"of {unwrapping['pixels_examined']} pixels (per pixel, after Yunjun et al., 2019)")
    values = velocity[kept]
    return {
        "attrs": attrs, "geo": geo, "velocity": velocity, "std": std, "mask": mask, "kept": kept, "dates": dates,
        "series_file": stem + ".h5" if stem else "", "corrections": corrections,
        "reference": read_reference_point(velocity_file),
        "stats": {
            "pixels": int(velocity.size), "kept": int(kept.sum()),
            "median": float(np.median(values)) if values.size else None,
            "p02": float(np.percentile(values, 2)) if values.size else None,
            "p98": float(np.percentile(values, 98)) if values.size else None,
            "std_median": float(np.median(std[kept & np.isfinite(std)])) if std is not None and (kept & np.isfinite(std)).any() else None,
        },
    }


def read_network(work_dir) -> Optional[dict]:
    """The interferogram network of the run: the pairs, which were used, and a baseline per date."""
    from insar_core.common import read_stack_network

    try:
        pairs, used, bperp = read_stack_network(Path(work_dir) / "inputs" / "ifgramStack.h5")
    except (OSError, KeyError):
        return None
    dates = sorted({d for pair in pairs for d in pair})
    position = {d: i for i, d in enumerate(dates)}
    baseline = None
    if bperp is not None and len(dates) > 1 and np.isfinite(bperp).all():
        # each pair gives the difference of two dates' baselines; solve for one per date, the first at zero
        design = np.zeros((len(pairs) + 1, len(dates)))
        for row, (a, b) in enumerate(pairs):
            design[row, position[a]], design[row, position[b]] = -1.0, 1.0
        design[-1, 0] = 1.0
        baseline = np.linalg.lstsq(design, np.append(bperp, 0.0), rcond=None)[0]
    days = [(dt.datetime.strptime(b, "%Y%m%d") - dt.datetime.strptime(a, "%Y%m%d")).days for a, b in pairs]
    return {"pairs": pairs, "used": used, "dates": dates, "baseline": baseline,
            "temporal_days": {"median": float(np.median(days)), "max": int(max(days))} if days else None}


# ── figures ──────────────────────────────────────────────────────────────────

def _figure_to_uri(figure) -> str:
    import matplotlib.pyplot as plt

    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", dpi=130, facecolor=BG, bbox_inches="tight")
    plt.close(figure)
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _style(axis) -> None:
    axis.set_facecolor(BG)
    axis.tick_params(colors=MUTED, labelsize=8)
    for spine in axis.spines.values():
        spine.set_color(BORDER)
    axis.xaxis.label.set_color(MUTED)
    axis.yaxis.label.set_color(MUTED)
    axis.title.set_color(TEXT)


def _decimated(array: np.ndarray, limit: int = 1200) -> np.ndarray:
    step = max(1, int(np.ceil(max(array.shape) / limit)))
    return array[::step, ::step]


def _extent(geo: dict) -> tuple:
    """Map extent in kilometres from the upper left corner, so the axes read as distances."""
    dx, dy = pixel_metres(geo)
    return (0.0, dx * geo["width"] / 1000.0, dy * geo["length"] / 1000.0, 0.0)


def figure_velocity(result: dict) -> Optional[str]:
    """The masked velocity map. Red is motion away from the satellite, green towards it, in the app's palette."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.colors import LinearSegmentedColormap
    except ImportError:
        return None
    data = np.where(result["kept"], result["velocity"], np.nan)
    if not np.isfinite(data).any():
        return None
    limit = float(np.nanpercentile(np.abs(data), 98)) or 1.0
    colors = LinearSegmentedColormap.from_list("los", [RED, "#3a2b2e", "#2a3331", "#24463a", GREEN])
    colors = colors.with_extremes(bad=BG)
    figure, axis = plt.subplots(figsize=(6.2, 5.0))
    image = axis.imshow(_decimated(data), cmap=colors, vmin=-limit, vmax=limit, extent=_extent(result["geo"]), interpolation="nearest")
    reference = result["reference"]
    if reference and reference.get("row") is not None:
        extent = _extent(result["geo"])
        x = (reference["column"] + 0.5) / result["geo"]["width"] * extent[1]
        y = (reference["row"] + 0.5) / result["geo"]["length"] * extent[2]
        axis.plot(x, y, marker="s", markersize=7, markerfacecolor="none", markeredgecolor=TEXT, markeredgewidth=1.4)
        axis.annotate("reference", (x, y), textcoords="offset points", xytext=(7, 5), color=TEXT, fontsize=8)
    axis.set_xlabel("km east of the upper left corner")
    axis.set_ylabel("km south of it")
    axis.set_title("Line-of-sight velocity (mm/year)", fontsize=10)
    _style(axis)
    bar = figure.colorbar(image, ax=axis, fraction=0.046, pad=0.03)
    bar.ax.tick_params(colors=MUTED, labelsize=8)
    bar.outline.set_edgecolor(BORDER)
    bar.set_label("away from the satellite          towards it", color=MUTED, fontsize=8, labelpad=8)
    return _figure_to_uri(figure)


def figure_histogram(result: dict) -> Optional[str]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None
    values = result["velocity"][result["kept"]]
    if values.size < 10:
        return None
    low, high = np.percentile(values, [0.5, 99.5])
    figure, axis = plt.subplots(figsize=(4.6, 3.0))
    axis.hist(values, bins=60, range=(float(low), float(high)), color=GREEN, alpha=0.85)
    axis.axvline(0.0, color=MUTED, linewidth=0.8)
    axis.set_xlabel("velocity (mm/year)")
    axis.set_ylabel("pixels")
    axis.set_title("Distribution of the velocity of the kept pixels", fontsize=10)
    _style(axis)
    return _figure_to_uri(figure)


def figure_uncertainty(uncertainty: Optional[dict]) -> Optional[str]:
    """The precision of a difference of velocities against the distance between the two points."""
    if not uncertainty:
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None
    distance = np.asarray(uncertainty["distance_m"]) / 1000.0
    figure, axis = plt.subplots(figsize=(7.4, 3.2))
    axis.plot(distance, uncertainty["sigma_mm_yr"], color=GREEN, linewidth=1.6, marker="o", markersize=3,
              label="from the noise of the dates (precision)")
    axis.plot(distance, uncertainty["observed_mm_yr"], color=TEXT, linewidth=1.2, linestyle="--", marker="o", markersize=3,
              label="spread of the velocities themselves (noise, steady errors and real motion)")
    axis.set_xscale("log")
    axis.set_yscale("log")
    axis.set_xlabel("distance between two points (km)")
    axis.set_ylabel("difference of velocity, 1 sigma (mm/year)")
    axis.set_title("How well a difference of velocity is known, against distance", fontsize=10)
    from matplotlib.ticker import FuncFormatter, LogLocator, NullFormatter

    for side in (axis.xaxis, axis.yaxis):
        side.set_major_locator(LogLocator(base=10, subs=(1.0, 2.0, 5.0)))
        side.set_major_formatter(FuncFormatter(lambda value, _: f"{value:g}"))
        side.set_minor_formatter(NullFormatter())
    legend = axis.legend(fontsize=8, frameon=False, loc="lower right")
    for text in legend.get_texts():
        text.set_color(MUTED)
    axis.grid(True, which="major", color=BORDER, linewidth=0.4)
    _style(axis)
    return _figure_to_uri(figure)


def figure_network(network: Optional[dict]) -> Optional[str]:
    """Each acquisition as a point (date, perpendicular baseline) and each interferogram as a line."""
    if not network or network["baseline"] is None:
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except ImportError:
        return None
    when = {d: dt.datetime.strptime(d, "%Y%m%d") for d in network["dates"]}
    height = dict(zip(network["dates"], network["baseline"], strict=False))
    figure, axis = plt.subplots(figsize=(7.4, 3.2))
    for (a, b), used in zip(network["pairs"], network["used"], strict=False):
        axis.plot([when[a], when[b]], [height[a], height[b]], color=GREEN if used else RED, linewidth=0.7,
                  alpha=0.55 if used else 0.9, linestyle="-" if used else "--")
    axis.scatter([when[d] for d in network["dates"]], [height[d] for d in network["dates"]], s=14, color=TEXT, zorder=3)
    axis.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    axis.set_ylabel("perpendicular baseline (m)")
    dropped = int((~network["used"]).sum())
    axis.set_title(f"Interferogram network: {int(network['used'].sum())} used" + (f", {dropped} dropped (red)" if dropped else ""), fontsize=10)
    _style(axis)
    figure.autofmt_xdate()
    return _figure_to_uri(figure)


# ── HTML ─────────────────────────────────────────────────────────────────────

_CSS = f"""
:root {{ color-scheme: dark; }}
* {{ box-sizing: border-box; }}
html {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
body {{ margin: 0; background: {BG}; color: {TEXT}; font: 14px/1.55 Figtree, "Segoe UI", system-ui, sans-serif; }}
main {{ max-width: 960px; margin: 0 auto; padding: 36px 20px 60px; }}
h1, h2 {{ font-family: Sora, Figtree, system-ui, sans-serif; letter-spacing: -0.01em; }}
h1 {{ font-size: 26px; margin: 0 0 4px; }}
h2 {{ font-size: 16px; margin: 34px 0 10px; padding-bottom: 6px; border-bottom: 1px solid {BORDER}; }}
h2 span {{ color: {GREEN}; margin-right: 8px; font-family: "JetBrains Mono", ui-monospace, monospace; font-size: 12px; }}
p {{ margin: 6px 0; }}
.muted {{ color: {MUTED}; }}
.small {{ font-size: 12px; }}
.mono {{ font-family: "JetBrains Mono", ui-monospace, monospace; font-size: 12px; }}
.tiles {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-top: 18px; }}
.tile {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 12px; padding: 10px 12px; }}
.tile .label {{ color: {MUTED}; font-size: 11px; }}
.tile .value {{ font-family: Sora, Figtree, system-ui, sans-serif; font-size: 19px; font-weight: 600; }}
.tile .note {{ color: {MUTED}; font-size: 11px; }}
table {{ border-collapse: collapse; width: 100%; font-size: 12.5px; }}
th {{ text-align: left; color: {MUTED}; font-weight: 600; padding: 5px 8px 5px 0; border-bottom: 1px solid {BORDER}; }}
td {{ padding: 5px 8px 5px 0; border-bottom: 1px solid {BORDER}; vertical-align: top; }}
td.key {{ color: {MUTED}; white-space: nowrap; width: 1%; padding-right: 18px; }}
.pill {{ display: inline-block; padding: 1px 9px; border-radius: 999px; font-size: 11px; font-weight: 600; border: 1px solid currentColor; white-space: nowrap; }}
.figures {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 14px; align-items: start; }}
figure {{ margin: 0; background: {PANEL}; border: 1px solid {BORDER}; border-radius: 12px; padding: 10px; }}
figure img {{ width: 100%; height: auto; display: block; border-radius: 6px; }}
figcaption {{ color: {MUTED}; font-size: 11.5px; margin-top: 6px; }}
ul, ol {{ margin: 6px 0; padding-left: 20px; }}
li {{ margin: 4px 0; }}
details {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 12px; padding: 8px 12px; margin-top: 10px; }}
summary {{ cursor: pointer; color: {GREEN}; font-size: 12.5px; }}
pre {{ white-space: pre-wrap; word-break: break-word; font: 11.5px/1.5 "JetBrains Mono", ui-monospace, monospace; color: {MUTED}; margin: 8px 0 2px; }}
.callout {{ border-left: 3px solid {AMBER}; padding: 2px 0 2px 12px; margin: 8px 0; color: {TEXT}; }}
@media print {{ main {{ padding: 0; }} details {{ break-inside: avoid; }} figure, .tile, tr {{ break-inside: avoid; }} }}
"""


def _pill(status: str) -> str:
    color = _STATUS_COLOR.get(status, MUTED)
    return f'<span class="pill" style="color:{color}">{_esc(_STATUS_WORD.get(status, status))}</span>'


def _rows(pairs: list[tuple[str, Any]]) -> str:
    body = "".join(f'<tr><td class="key">{_esc(key)}</td><td>{value}</td></tr>' for key, value in pairs if value not in (None, ""))
    return f"<table>{body}</table>" if body else ""


def _tile(label: str, value: str, note: str = "") -> str:
    return (f'<div class="tile"><div class="label">{_esc(label)}</div><div class="value">{value}</div>'
            + (f'<div class="note">{_esc(note)}</div>' if note else "") + "</div>")


def _section(number: int, title: str, body: str) -> str:
    return f"<h2><span>{number:02d}</span>{_esc(title)}</h2>{body}" if body else ""


def _figure(uri: Optional[str], caption: str) -> str:
    return f'<figure><img alt="{_esc(caption)}" src="{uri}"><figcaption>{_esc(caption)}</figcaption></figure>' if uri else ""


def _stats_rows(stats: Optional[dict]) -> list[tuple[str, Any]]:
    if not stats:
        return []
    slope = stats.get("slope_orthogonal") if stats.get("slope_orthogonal") is not None else stats.get("slope_b_on_a")
    return [
        ("Compared", f"{stats.get('n')} values"),
        ("RMSE", _number(stats.get("rmse_mm_yr"), 2, "mm/year")),
        ("Correlation", _number(stats.get("pearson_r"), 2)),
        ("Slope", _number(slope, 2) + ' <span class="muted">(1.0 is the same amplitude; orthogonal regression)</span>' if slope is not None else None),
        ("Bias", _number(stats.get("bias_mm_yr"), 2, "mm/year")),
    ]


def _validation_block(kind: str, result: dict) -> str:
    verdict = result.get("verdict") or {}
    parts = [f"<p><strong>{_esc(result.get('source_name') or kind)}</strong> {_pill(verdict.get('status', 'unknown'))}</p>",
             f"<p>{_esc(verdict.get('message', ''))}</p>"]
    rows = _stats_rows(result.get("velocity_rereferenced"))
    if result.get("window"):
        rows.insert(0, ("Period compared", f"{_date(result['window'][0])} to {_date(result['window'][1])}, {result.get('dates')} dates"))
    offset = result.get("common_offset_mm_yr")
    if isinstance(offset, dict):
        rows.append(("Offset removed", f"{_number(offset.get('mintpy'), 2)} (this result) and {_number(offset.get('other'), 2)} (the other) mm/year"))
    elif offset is not None:
        rows.append(("Offset removed", _number(offset, 2, "mm/year")))
    if result.get("timeseries_rmse_mm"):
        rows.append(("Error of the displacement series", f"{_number(result['timeseries_rmse_mm'].get('median'), 1)} mm (median over pixels)"))
    if result.get("noise_mm_yr_within_pixel") is not None:
        rows.append(("Noise of the other source inside a pixel", _number(result["noise_mm_yr_within_pixel"], 2, "mm/year")))
    if result.get("plate"):
        plate = result["plate"]
        rows.append(("Frame of the GNSS velocities", f"relative to the {_esc(plate.get('name'))} plate (ITRF2014 plate motion model)"))
    rows.append(("Computed", _esc(str(result.get("computed_at", ""))[:19].replace("T", " ") + " UTC")))
    parts.append(_rows(rows))
    uncertainty = result.get("uncertainty")
    if uncertainty:
        parts.append(f'<p class="callout">{_esc(uncertainty.get("message"))} Factor to apply to the stated error: '
                     f'<strong>{_number(uncertainty.get("recommended_scale"), 2)}</strong>.</p>')
    stations = result.get("stations")
    if stations:
        head = "<tr><th>Station</th><th>Dates</th><th>GNSS (mm/yr)</th><th>InSAR (mm/yr)</th><th>Difference</th><th>Series RMSE (mm)</th><th>Steps in the period</th></tr>"
        body = ""
        for s in stations:
            steps = "; ".join(f"{_date(x.get('date'))} {x.get('kind')}, {x.get('action')}" for x in s.get("steps") or []) or "none"
            share = f' ({100.0 * s["coverage"]:.0f}%)' if s.get("coverage") is not None else ""
            body += (f'<tr><td class="mono">{_esc(s.get("name"))}</td><td>{_esc(s.get("dates"))}{share}</td><td>{_number(s.get("gnss_mm_yr"), 2)}</td>'
                     f'<td>{_number(s.get("insar_mm_yr"), 2)}</td><td>{_number(s.get("difference_rereferenced_mm_yr"), 2)}</td>'
                     f'<td>{_number(s.get("series_rmse_mm"), 1)}</td><td>{_esc(steps)}</td></tr>')
        parts.append(f"<table>{head}{body}</table>")
    calibration = result.get("calibration")
    if calibration and not calibration.get("error"):
        cv = calibration.get("verdict") or {}
        parts.append(f"<p><strong>Tie to GNSS</strong> {_pill(cv.get('status', 'unknown'))} "
                     f"{'applied to the calibrated layer' if calibration.get('applied') else 'not applied'}</p><p>{_esc(cv.get('message', ''))}</p>")
        parts.append(_rows([
            ("Shift", _number(calibration.get("offset_mm_yr"), 2, "mm/year") + " &plusmn; " + _number(calibration.get("offset_sigma_mm_yr"), 2)),
            ("Error against the stations before", _number(calibration.get("rmse_before_mm_yr"), 2, "mm/year")),
            ("Error against stations left out, after", _number(calibration.get("loo_rmse_after_mm_yr"), 2, "mm/year")),
        ]))
    for warning in result.get("warnings") or []:
        parts.append(f'<p class="callout small">{_esc(warning)}</p>')
    return "".join(parts)


def build_report(work_dir, provenance: Optional[dict] = None, validation: Optional[dict] = None,
                 title: Optional[str] = None, generated_at: Optional[dt.datetime] = None) -> str:
    """The report of the MintPy run in `work_dir`, as one self-contained HTML document.

    `provenance` is the project's record (project, engine, batches, pairs, last_mintpy_run or
    mintpy and software at the top level), `validation` is `{"egms": result, "gnss": result}`
    with the results of insar_core.analysis.validation. Raises FileNotFoundError when the run has
    no velocity map yet."""
    work_dir = Path(work_dir)
    provenance = provenance or {}
    validation = {k: v for k, v in (validation or {}).items() if v}
    generated_at = generated_at or dt.datetime.now(dt.timezone.utc)
    result = read_result(work_dir)
    network = read_network(work_dir)
    qc = compute_qc(work_dir)
    project = provenance.get("project") or {}
    engine = provenance.get("engine") or {}
    run = provenance.get("last_mintpy_run") or provenance
    software = run.get("software") or provenance.get("software") or {}
    options = (run.get("mintpy") or {}).get("options") or {}
    batches = provenance.get("batches") or []
    attrs, stats, reference = result["attrs"], result["stats"], result["reference"] or {}
    name = title or project.get("name") or work_dir.parent.name
    dates = result["dates"]
    first, last = _date(attrs.get("START_DATE") or (dates[0] if dates else "")), _date(attrs.get("END_DATE") or (dates[-1] if dates else ""))
    try:
        span_days = (dt.date.fromisoformat(last) - dt.date.fromisoformat(first)).days
    except ValueError:
        span_days = None
    direction = str(attrs.get("ORBIT_DIRECTION") or project.get("flight_direction") or "").lower()
    track = attrs.get("relative_orbit") or project.get("track_number")

    # 0. the numbers a reader looks for first
    tiles = [
        _tile("Period", f"{span_days} days" if span_days is not None else "n/a", f"{first} to {last}"),
        _tile("Acquisitions", str(len(dates)) if dates else "n/a",
              f"{int(network['used'].sum())} interferograms used" if network else ""),
        _tile("Track", _esc(track if track not in (None, "") else "n/a"), direction),
        _tile("Pixels kept", f"{100.0 * stats['kept'] / stats['pixels']:.0f}%" if stats["pixels"] else "n/a",
              f"{stats['kept']} of {stats['pixels']}"),
        _tile("Velocity, 2% to 98%", f"{_number(stats['p02'])} to {_number(stats['p98'])}", "mm/year, line of sight"),
        _tile("Stated error", _number(stats["std_median"], 2), "mm/year, median; see the limits below"),
        _tile("Quality checks", _pill(qc["status"]), f"{len(qc['checks'])} checks"),
    ]
    for kind, entry in validation.items():
        tiles.append(_tile(f"Against {'EGMS' if kind == 'egms' else 'GNSS' if kind == 'gnss' else kind}",
                           _pill((entry.get("verdict") or {}).get("status", "unknown")),
                           f"RMSE {_number((entry.get('velocity_rereferenced') or {}).get('rmse_mm_yr'), 1)} mm/year"
                           if entry.get("velocity_rereferenced") else ""))

    # 1. data and processing
    hyp3_parameters = next((b.get("parameters") for b in batches if b.get("parameters")), None) or {}
    pairing = hyp3_parameters.get("pairing") or {}
    geo = result["geo"]
    data_rows = [
        ("Source of interferograms", _esc(engine.get("label") or engine.get("id") or hyp3_parameters.get("processor"))),
        ("Sensor and band", _esc(" ".join(str(v) for v in (engine.get("sensor"), f"{engine.get('band')}-band" if engine.get("band") else "") if v))),
        ("Track and direction", _esc(f"{track} {direction}".strip())),
        ("Wavelength", _number(float(attrs["WAVELENGTH"]) * 100.0, 2, "cm") if attrs.get("WAVELENGTH") else None),
        ("Period", f"{first} to {last}" + (f" ({span_days} days, {len(dates)} acquisitions)" if span_days is not None else "")),
        ("Interferograms", (f"{int(network['used'].sum())} used of {len(network['pairs'])}, temporal baseline median "
                            f"{network['temporal_days']['median']:.0f} days, longest {network['temporal_days']['max']} days") if network else None),
        ("How the pairs were chosen", _esc(", ".join(f"{k}: {v}" for k, v in pairing.items() if v is not None)) if pairing else None),
        ("Processing parameters", _esc(", ".join(f"{k}: {v}" for k, v in (hyp3_parameters.get("hyp3") or {}).items())) or None),
        ("Grid", f"{geo['width']} x {geo['length']} pixels of {abs(geo['x_step']):g} x {abs(geo['y_step']):g}, EPSG:{geo['epsg']}"),
        ("Area processed", _esc((run.get("crop") or {}).get("subset_lalo")) if (run.get("crop") or {}).get("applied") else None),
        ("Time series analysed", f'<span class="mono">{_esc(result["series_file"])}</span>' if result["series_file"] else None),
        ("Corrections applied", "<br>".join(_esc(c) for c in result["corrections"]) or "None beyond the network inversion"),
    ]

    from insar_core.pipeline import mask_refinement

    refined = mask_refinement.read_summary(work_dir)
    if refined:
        why = []
        if refined["criteria"].get("unreadable_closures"):
            why.append(f"{refined['dropped'].get('unreadable_closures', 0)} pixels whose triplets of interferograms do not close "
                       "and could not be corrected")
        if refined["criteria"].get("min_mean_coherence") is not None:
            why.append(f"{refined['dropped'].get('low_mean_coherence', 0)} pixels of mean spatial coherence under "
                       f"{refined['criteria']['min_mean_coherence']:g}")
        data_rows.append(("Mask", f"MintPy's temporal coherence mask, less {' and '.join(why)}: {refined['kept']} of the "
                                  f"{refined['kept_by_mintpy']} pixels it kept remain"))

    # 2. reference
    reference_rows = [
        ("Reference point", f"row {reference.get('row')}, column {reference.get('column')}" if reference.get("row") is not None else None),
        ("Position", f"{reference.get('lat')}, {reference.get('lon')} (latitude, longitude, WGS84)" if reference.get("lat") is not None else None),
        ("Reference date", _esc(reference.get("date") or _date(attrs.get("REF_DATE")))),
        ("Sign", "Positive values move towards the satellite, negative away from it"),
    ]

    # 3. quality checks
    quality = ""
    if qc["checks"]:
        body = "".join(
            f'<tr><td>{_esc(c["label"])}</td><td class="mono">{_esc(c["value"])} {_esc(c["unit"])}</td><td>{_pill(c["status"])}</td>'
            f'<td>{_esc(c["message"])}</td></tr>' for c in qc["checks"])
        quality = (f"<table><tr><th>Check</th><th>Value</th><th></th><th>What it means</th></tr>{body}</table>"
                   '<p class="small muted">The thresholds are rules of thumb for C-band SBAS at 80 m and are listed with the full '
                   "record at the end. They flag what deserves a look. Agreement with independent data is the test of correctness.</p>")

    # 4. figures
    figures = "".join([
        _figure(figure_velocity(result), "Velocity along the line of sight, masked by temporal coherence. The square is the reference point, "
                                         "where the velocity is zero by construction."),
        _figure(figure_histogram(result), "How the velocities of the kept pixels are distributed."),
        _figure(figure_network(network), "Each point is an acquisition and each line an interferogram. A date tied by few lines "
                                         "depends on each of them."),
    ])

    # 4b. precision against distance
    from insar_core.analysis import spatial_uncertainty

    uncertainty = spatial_uncertainty.read_or_estimate(work_dir)
    precision = ""
    if uncertainty:
        marks = "".join(f"<tr><td>{m['distance_km']:g} km</td><td class=\"mono\">{m['sigma_mm_yr']}</td>"
                        f"<td class=\"mono\">{m['observed_mm_yr']}</td></tr>" for m in uncertainty.get("at") or [])
        precision = (
            f"<p>{_esc(spatial_uncertainty.describe(uncertainty))}</p>"
            + _figure(figure_uncertainty(uncertainty),
                      "The green curve is what the noise of the dates allows: the least the error can be. The dashed curve is "
                      "what the velocity map shows, which also holds real differences in motion. Over ground that does not "
                      "deform the truth is near the dashed curve; over deforming ground it is between the two.")
            + (f"<table><tr><th>Distance between two points</th><th>Precision (mm/year)</th><th>Seen in the velocities (mm/year)</th></tr>{marks}</table>" if marks else "")
            + f'<p class="small muted">Measured on {uncertainty["pixels_sampled"]} of the kept pixels and {uncertainty["dates"]} dates. '
              "To judge whether two places move differently, compare the difference of their velocities with the value at the "
              "distance between them, not with the error of each pixel.</p>")
        cite_uncertainty = list(spatial_uncertainty.CITATIONS)
    else:
        cite_uncertainty = []

    # 5. validation
    validated = "".join(_validation_block(kind, entry) for kind, entry in validation.items())
    if not validated:
        validated = ('<p class="callout">This result has not been compared with independent data (EGMS or GNSS). The quality '
                     "checks above say the run is internally consistent, not that the velocities are correct.</p>")

    # 6. citations: only what this result used
    cite = [CITATIONS["sentinel1"]] if engine.get("sensor", "sentinel-1") == "sentinel-1" else [CITATIONS["nisar"]]
    engine_id = engine.get("id") or ""
    if engine_id.startswith("hyp3") or "hyp3" in str(hyp3_parameters.get("processor", "")).lower():
        cite += [CITATIONS["hyp3"], CITATIONS["isce2"] if engine_id == "hyp3_burst" else CITATIONS["gamma"]]
    cite += [CITATIONS["sbas"], CITATIONS["mintpy"]]
    if any(c["id"] == "fading_bias" for c in qc["checks"]):
        from insar_core.analysis import fading_bias

        cite += list(fading_bias.CITATIONS)
    cite += cite_uncertainty
    if any("ERA5" in c for c in result["corrections"]):
        cite += [CITATIONS["era5"], CITATIONS["pyaps"]]
    if "egms" in validation:
        cite.append(CITATIONS["egms"])
    for entry in validation.values():
        cite += [c for c in entry.get("citations") or ([entry["citation"]] if entry.get("citation") else []) if c not in cite]

    # 7. software and the full record
    versions = _rows([(k, f'<span class="mono">{_esc(v)}</span>') for k, v in software.items() if v])
    set_options = {k: v for k, v in options.items() if v not in ("auto", "no", "") and not k.startswith(("mintpy.load.", "mintpy.compute."))}
    record = {
        "generated_at": generated_at.isoformat(), "work_dir": work_dir.name, "project": project, "engine": engine,
        "period": [first, last], "acquisition_dates": dates, "reference": reference, "corrections": result["corrections"],
        "statistics": stats, "quality": {"status": qc["status"], "checks": qc["checks"], "thresholds": qc.get("thresholds")},
        "interferograms": [{"reference": a, "secondary": b, "used": bool(u)} for (a, b), u in zip(network["pairs"], network["used"], strict=False)] if network else None,
        "batches": batches, "mintpy_options": options, "software": software,
        "validation": {k: {kk: vv for kk, vv in v.items() if kk != "scatter"} for k, v in validation.items()},
    }
    reproduce = (
        _rows([("MintPy options that are not left to MintPy",
                "<br>".join(f'<span class="mono">{_esc(k)} = {_esc(v)}</span>' for k, v in sorted(set_options.items())) or None)])
        + f"<details><summary>Software versions</summary>{versions}</details>" * bool(versions)
        + "<details><summary>The full record of this result, as JSON</summary>"
          '<p class="small muted">Every date, pair, option, check and number of this report, for a script to read. The pairs are '
          "named by their acquisition dates, which with the track and the area identify the Sentinel-1 data used.</p>"
          f"<pre>{_esc(json.dumps(record, indent=1, default=str))}</pre></details>"
    )

    sections = [
        ("Data and processing", _rows(data_rows)),
        ("What the values are relative to", _rows(reference_rows)),
        ("Result", f'<div class="figures">{figures}</div>' if figures else ""),
        ("Quality of the run", quality),
        ("Precision of the velocity", precision),
        ("Comparison with independent data", validated),
        ("Limits of what this result can say", "<ul>" + "".join(f"<li>{_esc(text)}</li>" for text in LIMITS) + "</ul>"),
        ("What to cite", "<ol>" + "".join(f"<li>{_esc(text)}</li>" for text in cite) + "</ol>"),
        ("To reproduce it", reproduce),
    ]
    body = "".join(_section(number, heading, content) for number, (heading, content) in enumerate(sections, start=1))
    description = f'<p class="muted">{_esc(project.get("description"))}</p>' if project.get("description") else ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_esc(name)}: InSAR result report</title><style>{_CSS}</style></head><body><main>"
        f'<p class="small muted">InSAR result report</p><h1>{_esc(name)}</h1>{description}'
        f'<p class="small muted">Generated {generated_at:%Y-%m-%d %H:%M} UTC'
        + (f" with insar-core {_esc(software.get('insar-core'))} and MintPy {_esc(software.get('mintpy'))}" if software.get("mintpy") else "")
        + f'</p><div class="tiles">{"".join(tiles)}</div>{body}</main></body></html>'
    )


def write_report(work_dir, dest, **kwargs) -> Path:
    """`build_report` written to `dest`. Returns the file."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    draft = dest.with_suffix(".draft.html")
    draft.write_text(build_report(work_dir, **kwargs), encoding="utf-8")
    draft.replace(dest)
    return dest
