"""Quality checks on a finished MintPy run, read from the files MintPy left in its work directory.

A time series always comes out of MintPy, whether or not it can be trusted. These checks
turn the numbers MintPy already saves (coherence, masks, residuals, the interferogram
network) into a short list of "fine" or "look at this" lines, so a bad run does not look
the same as a good one.

The thresholds are rules of thumb for Sentinel-1 C-band SBAS with 80 m pixels, taken
from what MintPy itself uses (a temporal coherence of 0.7 masks a pixel) and from runs
over a quiet urban area and a subsiding agricultural basin in southeast Spain. They flag
what deserves a look. They are not a proof that a result is right or wrong: the
validation against EGMS or GNSS is the check that says whether the numbers are correct.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import h5py
import numpy as np

from insar_core.common import read_dataset
from insar_core.pipeline.mintpy_timeseries import locate_timeseries_file, locate_velocity_file

# status order, worst last
_ORDER = {"ok": 0, "warn": 1, "fail": 2}

THRESHOLDS = {
    "coherence_warn": 0.45, "coherence_fail": 0.25,        # mean spatial coherence of the interferograms
    "valid_warn": 0.30, "valid_fail": 0.05,                 # fraction of pixels kept by the temporal coherence mask
    "temporal_coherence_warn": 0.75,                        # median over the kept pixels
    "interferograms_per_pixel_warn": 3,                     # median number used in the inversion
    "residual_rms_warn_mm": 10.0, "residual_rms_fail_mm": 25.0,
    "residual_outlier_factor": 3.0,                         # a date this many times the median RMS is an outlier
    "reference_coherence_warn": 0.7,
    "reference_edge_pixels_warn": 3,
    "unwrapping_warn": 0.20, "unwrapping_fail": 0.50,       # fraction of pixels with a triplet that does not close
    "steep_warn": 0.005, "steep_fail": 0.05,                # share of pixels whose phase jumps more than half a cycle to a neighbour
    "steep_min_days": 60,                                   # only interferograms at least this long are judged
    "ramp_warn_mm": 10.0,                                   # median over dates of the edge to edge change of a fitted plane
    "ramp_min_pixels": 500,                                 # fewer kept pixels than this cannot fix a plane
    "span_warn_days": 180,                                  # velocity from less than this is extrapolated to a year
    "ifgrams_per_date_warn": 3.0,                           # average per date; a plain chain has about 2
}


_read = read_dataset


def _check(check_id: str, label: str, value, unit: str, status: str, message: str) -> dict:
    return {"id": check_id, "label": label, "value": value, "unit": unit, "status": status, "message": message}


def _network(work_dir: Path) -> list[dict]:
    path = work_dir / "inputs" / "ifgramStack.h5"
    try:
        with h5py.File(path, "r") as handle:
            kept = handle["dropIfgram"][:].astype(bool)
            pairs = [(a.decode(), b.decode()) for a, b in handle["date"][:]]
    except (OSError, KeyError):
        return []
    used = [pair for pair, keep in zip(pairs, kept, strict=False) if keep]
    dates = sorted({d for pair in pairs for d in pair})
    parent = {d: d for d in dates}

    def find(item):
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    degree = {d: 0 for d in dates}
    for a, b in used:
        parent[find(a)] = find(b)
        degree[a] += 1
        degree[b] += 1
    groups = len({find(d) for d in dates})
    weakest = min(degree.values()) if degree else 0
    per_date = 2 * len(used) / len(dates) if dates else 0.0

    checks = []
    if groups > 1:
        checks.append(_check(
            "network_connected", "Interferogram network", groups, "groups", "fail",
            f"The network splits into {groups} separate groups of dates. Dates in different groups cannot be "
            "tied to each other, so the time series is not one consistent series."))
    elif weakest < 1:
        checks.append(_check("network_connected", "Interferogram network", groups, "groups", "fail",
                             "At least one date has no interferogram left."))
    elif weakest < 2 or per_date < THRESHOLDS["ifgrams_per_date_warn"]:
        checks.append(_check(
            "network_connected", "Interferogram network", groups, "groups", "warn",
            f"The network is connected but thin: {len(used)} interferograms for {len(dates)} dates, "
            f"and the weakest date has {weakest}. An error in one interferogram goes straight into the series."))
    else:
        checks.append(_check(
            "network_connected", "Interferogram network", groups, "groups", "ok",
            f"{len(used)} of {len(pairs)} interferograms used for {len(dates)} dates, in one connected network. "
            f"The weakest date has {weakest}."))
    return checks


def _span(work_dir: Path) -> list[dict]:
    try:
        with h5py.File(_velocity_file(work_dir), "r") as handle:
            first, last = str(handle.attrs["START_DATE"]), str(handle.attrs["END_DATE"])
        import datetime as dt
        days = (dt.datetime.strptime(last, "%Y%m%d") - dt.datetime.strptime(first, "%Y%m%d")).days
    except (OSError, KeyError, ValueError):
        return []
    if days < THRESHOLDS["span_warn_days"]:
        return [_check(
            "time_span", "Time covered", days, "days", "warn",
            f"The series covers only {days} days. A velocity in mm/year is extrapolated from that, so a few millimetres of "
            "noise become tens of mm/year. Look at the displacement itself, not the velocity, or wait for more dates.")]
    return [_check("time_span", "Time covered", days, "days", "ok", f"{days} days, enough for a velocity.")]


def _velocity_file(work_dir: Path) -> Path:
    """The velocity map of the run, wherever MintPy left it (plain, or geocoded under geo/)."""
    plain = work_dir / "velocity.h5"
    return plain if plain.exists() else locate_velocity_file(work_dir)   # raises FileNotFoundError, an OSError


def _mask(work_dir: Path) -> Optional[np.ndarray]:
    try:
        geocoded = _velocity_file(work_dir).name.startswith("geo_")
    except OSError:
        geocoded = False
    # A geocoded velocity map goes with the geocoded mask, not the radar-coordinate one (other shape).
    mask = _read(work_dir / "geo" / "geo_maskTempCoh.h5", "mask") if geocoded else _read(work_dir / "maskTempCoh.h5", "mask")
    return None if mask is None else mask.astype(bool)


def _coherence(work_dir: Path) -> list[dict]:
    coherence = _read(work_dir / "avgSpatialCoh.h5", "coherence")
    if coherence is None:
        return []
    values = coherence[np.isfinite(coherence) & (coherence > 0)]
    if values.size == 0:
        return []
    mean = float(values.mean())
    status = "fail" if mean < THRESHOLDS["coherence_fail"] else "warn" if mean < THRESHOLDS["coherence_warn"] else "ok"
    message = {
        "ok": "The interferograms keep their phase well over most of the area.",
        "warn": "Low coherence: much of the area decorrelates (vegetation, water, long time spans). "
                "Expect noisy or missing pixels there.",
        "fail": "Very low coherence: the phase is mostly noise, so the result cannot be trusted.",
    }[status]
    return [_check("coherence", "Mean spatial coherence", round(mean, 3), "", status, message)]


def _valid_pixels(work_dir: Path, mask: Optional[np.ndarray]) -> list[dict]:
    if mask is None:
        return []
    fraction = float(mask.mean())
    status = "fail" if fraction < THRESHOLDS["valid_fail"] else "warn" if fraction < THRESHOLDS["valid_warn"] else "ok"
    message = {
        "ok": f"{fraction:.0%} of the pixels pass the temporal coherence mask.",
        "warn": f"Only {fraction:.0%} of the pixels pass the temporal coherence mask, so the map has large gaps.",
        "fail": f"Only {fraction:.0%} of the pixels pass the temporal coherence mask: there is almost no usable result.",
    }[status]
    from insar_core.pipeline import mask_refinement

    refined = mask_refinement.read_summary(work_dir)
    if refined and refined["kept_by_mintpy"] > refined["kept"]:
        why = []
        if refined["dropped"].get("unreadable_closures"):
            why.append(f"{refined['dropped']['unreadable_closures']} whose triplets of interferograms do not close")
        if refined["dropped"].get("low_mean_coherence"):
            why.append(f"{refined['dropped']['low_mean_coherence']} of low mean coherence")
        message += (f" That is after the stricter mask of this app dropped {refined['fraction_dropped']:.1%} of the pixels "
                    f"MintPy's temporal coherence kept ({' and '.join(why)}).")
    out = [_check("valid_pixels", "Pixels kept", round(fraction, 3), "fraction", status, message)]

    temporal = _read(work_dir / "temporalCoherence.h5", "temporalCoherence")
    if temporal is not None and mask.any():
        median = float(np.median(temporal[mask]))
        status = "warn" if median < THRESHOLDS["temporal_coherence_warn"] else "ok"
        out.append(_check(
            "temporal_coherence", "Temporal coherence of kept pixels", round(median, 3), "median", status,
            "How well the inversion explains the phase of the kept pixels." if status == "ok"
            else "The kept pixels barely pass the threshold, so their values are less reliable."))

    used = _read(work_dir / "numInvIfgram.h5", "mask")
    if used is not None and mask.any():
        median = float(np.median(used[mask]))
        status = "warn" if median < THRESHOLDS["interferograms_per_pixel_warn"] else "ok"
        out.append(_check(
            "ifgrams_per_pixel", "Interferograms used per pixel", median, "median", status,
            "Enough redundancy to average out errors." if status == "ok"
            else "Pixels are estimated from very few interferograms."))
    return out


def _residuals(work_dir: Path) -> list[dict]:
    path = next(iter(sorted(work_dir.glob("rms_timeseriesResidual*.txt"))), None)
    if path is None:
        return []
    dates, rms = [], []
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) == 2 and not line.startswith("#"):
            try:
                dates.append(parts[0])
                rms.append(float(parts[1]) * 1000.0)
            except ValueError:
                dates.pop()
    if not rms:
        return []
    values = np.asarray(rms)
    median = float(np.median(values))
    outliers = [d for d, v in zip(dates, values, strict=False) if v > THRESHOLDS["residual_outlier_factor"] * max(median, 1.0)]
    status = ("fail" if median > THRESHOLDS["residual_rms_fail_mm"]
              else "warn" if median > THRESHOLDS["residual_rms_warn_mm"] else "ok")
    out = [_check(
        "residual_rms", "Residual after the inversion", round(median, 2), "mm (median RMS)", status,
        "The series fits the interferograms closely." if status == "ok"
        else "A large part of the interferogram phase is not explained by the series: unwrapping errors or "
             "atmosphere are likely left in it.")]
    if outliers:
        shown = ", ".join(outliers[:6]) + (" ..." if len(outliers) > 6 else "")
        out.append(_check(
            "residual_outlier_dates", "Dates with a much larger residual", len(outliers), "dates", "warn",
            f"{shown}: these dates fit the rest poorly (storm, snow, or an unwrapping error). "
            "Consider dropping them."))
    return out


def _reference(work_dir: Path, mask: Optional[np.ndarray]) -> list[dict]:
    try:
        with h5py.File(_velocity_file(work_dir), "r") as handle:
            y, x = int(handle.attrs["REF_Y"]), int(handle.attrs["REF_X"])
            length, width = int(handle.attrs["LENGTH"]), int(handle.attrs["WIDTH"])
    except (OSError, KeyError):
        return []
    coherence = _read(work_dir / "avgSpatialCoh.h5", "coherence")
    problems = []
    value = None
    if coherence is not None and 0 <= y < length and 0 <= x < width:
        value = float(coherence[y, x])
        if value < THRESHOLDS["reference_coherence_warn"]:
            problems.append(f"its coherence is {value:.2f}")
    if mask is not None and 0 <= y < length and 0 <= x < width and not mask[y, x]:
        problems.append("it is masked out")
    edge = min(y, x, length - 1 - y, width - 1 - x)
    if edge < THRESHOLDS["reference_edge_pixels_warn"]:
        problems.append(f"it is {edge} pixels from the edge")
    if problems:
        return [_check("reference_pixel", "Reference pixel", f"row {y}, column {x}", "", "warn",
                       "The reference pixel is a poor choice: " + ", ".join(problems) +
                       ". Every value in the result is relative to it.")]
    return [_check("reference_pixel", "Reference pixel", f"row {y}, column {x}", "", "ok",
                   "Coherent, inside the area and away from the edge. Every value is relative to it, "
                   "so a point that really moves here shifts the whole map.")]


def _unwrapping(work_dir: Path, mask: Optional[np.ndarray] = None) -> list[dict]:
    """Triplets of interferograms that do not close by whole cycles, over the pixels the result keeps.

    When the app's correction ran (insar_core.analysis.unwrap_correction) its own account is used:
    it tells the pixels that had a real unwrapping error, and were corrected, from the pixels whose
    closures are too noisy to read, which MintPy's count puts together."""
    from insar_core.analysis import unwrap_correction as correction

    after = _read(work_dir / correction.COUNT_FILE, "mask")
    status = _read(work_dir / correction.COUNT_FILE, "status")
    if after is not None and status is not None:
        from insar_core.pipeline import mask_refinement

        # counted over the pixels MintPy kept: the stricter mask of this app drops some of them for this very reason
        before = _read(work_dir / mask_refinement.ORIGINAL_FILE, "mask")
        dropped = bool(((mask_refinement.read_summary(work_dir) or {}).get("dropped") or {}).get("unreadable_closures"))
        if before is not None and before.shape == status.shape:
            mask = before.astype(bool)
        keep = mask if mask is not None and mask.shape == status.shape and mask.any() else np.ones(status.shape, dtype=bool)
        keep = keep & (status != correction.NO_DATA)
        if not keep.any():
            return []
        n = int(keep.sum())
        corrected = float(np.isin(status[keep], (correction.CORRECTED, correction.GROWN)).mean())
        unresolved = float(np.isin(status[keep], (correction.AMBIGUOUS, correction.UNEXPLAINED, correction.UNSOLVED)).mean())
        noisy = float((status[keep] == correction.NOISY).mean())
        fraction = float((after[keep] > 0).mean())
        level = "fail" if fraction > THRESHOLDS["unwrapping_fail"] else "warn" if fraction > THRESHOLDS["unwrapping_warn"] else "ok"
        whose = "pixels MintPy kept" if dropped else "pixels kept"
        parts = [f"Unwrapping errors of whole cycles were found and corrected in {corrected:.1%} of the {n} {whose}."
                 if corrected else f"No unwrapping error of whole cycles was found in the {whose}."]
        fate = "they are left out of the result" if dropped else "nothing was changed in them"
        if unresolved:
            parts.append(f"{unresolved:.1%} have such errors that could not be told apart; {fate}.")
        if noisy:
            parts.append(f"{noisy:.1%} have triplets that do not close because their phase is noisy, not because of an "
                         f"unwrapping error; {fate}.")
        if dropped:
            level = "ok"                           # what does not close is no longer in the map
        if level != "ok":
            parts.append("That is a large share of the map: look at the coherence and consider a higher temporal coherence threshold.")
        return [_check("unwrapping", "Pixels with triplets that do not close", round(fraction, 3), "fraction", level, " ".join(parts))]

    triplets = _read(work_dir / "numTriNonzeroIntAmbiguity.h5", "mask")
    if triplets is None:
        return []
    # Water and pixels without data have no phase, so their triplets always "close". Counting them
    # would dilute the fraction on a coast or a sparse map, so only the kept pixels are counted.
    if mask is not None and mask.shape == triplets.shape and mask.any():
        triplets = triplets[mask]
    fraction = float((triplets > 0).mean())
    status = "fail" if fraction > THRESHOLDS["unwrapping_fail"] else "warn" if fraction > THRESHOLDS["unwrapping_warn"] else "ok"
    message = {
        "ok": "Almost every triplet of interferograms closes.",
        "warn": f"{fraction:.0%} of the pixels have triplets that do not close by whole cycles: unwrapping errors or noisy pixels.",
        "fail": f"{fraction:.0%} of the pixels have triplets that do not close by whole cycles: unwrapping errors or noise are widespread.",
    }[status]
    if fraction > 0:
        message += " The unwrapping error correction was not run, so the two are not told apart."
    return [_check("unwrapping", "Pixels with triplets that do not close", round(fraction, 3), "fraction", status, message)]


def _uncertainty(work_dir: Path, mask: Optional[np.ndarray]) -> dict:
    try:
        with h5py.File(_velocity_file(work_dir), "r") as handle:
            std = handle["velocityStd"][:] * 1000.0
    except (OSError, KeyError):
        return {}
    valid = np.isfinite(std) & (mask if mask is not None else True)
    if not np.any(valid):
        return {}
    metrics: dict = {"velocity_std_mm_yr_median": round(float(np.median(std[valid])), 2)}
    # how the uncertainty changes with distance, which the per-pixel figure above cannot say
    from insar_core.analysis import spatial_uncertainty

    spatial = spatial_uncertainty.read_or_estimate(work_dir)
    if spatial:
        metrics["velocity_uncertainty"] = {
            "summary": spatial_uncertainty.describe(spatial),
            **{k: spatial[k] for k in ("distance_m", "sigma_mm_yr", "observed_mm_yr", "at", "nearest", "farthest") if k in spatial},
            **{k: spatial[k] for k in ("from_reference_mm_yr", "ratio_to_stated") if k in spatial},
        }
    return metrics


def _steep_phase(work_dir: Path, mask: Optional[np.ndarray]) -> list[dict]:
    """Where the longest interferograms change faster between neighbours than they can be followed.

    Between two adjacent pixels the unwrapped phase of a good interferogram changes by well under
    half a cycle (pi). Where the ground moves fast the change in a long interferogram reaches that,
    the phase aliases, unwrapping loses whole cycles and the motion is underestimated, whatever the
    coherence. In the Lorca test the velocity at a GNSS station in such a zone was a fifth to a half
    of the station's. The measure is the share of the kept pixels with such a step in the longest
    interferograms in use; the 12-36 day ones almost never have it, so a share that grows with
    the span says which part of the map the long ones cannot be trusted."""
    path = work_dir / "inputs" / "ifgramStack.h5"
    try:
        with h5py.File(path, "r") as handle:
            kept = handle["dropIfgram"][:].astype(bool)
            pairs = [(a.decode(), b.decode()) for a, b in handle["date"][:]]
            name = "unwrapPhase_phaseClosure" if "unwrapPhase_phaseClosure" in handle else "unwrapPhase"
            import datetime as dt
            days = np.array([(dt.datetime.strptime(b, "%Y%m%d") - dt.datetime.strptime(a, "%Y%m%d")).days for a, b in pairs])
            long_ones = [i for i in np.argsort(-days) if kept[i] and days[i] >= THRESHOLDS["steep_min_days"]][:3]
            if not long_ones:
                return []
            shares = []
            for index in long_ones:
                phase = handle[name][int(index)].astype("float64")
                valid = phase != 0
                if mask is not None and mask.shape == phase.shape:
                    valid &= mask
                if not valid.any():
                    continue
                step = np.zeros_like(phase)
                step[:-1] = np.maximum(step[:-1], np.abs(np.diff(phase, axis=0)))
                step[:, :-1] = np.maximum(step[:, :-1], np.abs(np.diff(phase, axis=1)))
                shares.append(float(((step > np.pi) & valid).sum() / valid.sum()))
    except (OSError, KeyError, ValueError):
        return []
    if not shares:
        return []
    share, span = float(np.median(shares)), int(days[long_ones[0]])
    if share >= THRESHOLDS["steep_fail"]:
        status = "fail"
    elif share >= THRESHOLDS["steep_warn"]:
        status = "warn"
    else:
        status = "ok"
    message = {
        "ok": f"The longest interferograms ({span} days) change smoothly between neighbouring pixels almost everywhere.",
        "warn": f"In the longest interferograms ({span} days), {share * 100:.1f}% of the pixels jump more than half a cycle to a neighbour. "
                "Fast motion (about a centimetre or more between neighbouring pixels in that time) aliases there, so unwrapping loses it "
                "and the velocity of those zones is underestimated. The shorter interferograms resolve it better: compare the result "
                "without the long pairs there, or with GNSS.",
        "fail": f"In the longest interferograms ({span} days), {share * 100:.1f}% of the pixels jump more than half a cycle to a neighbour. "
                "A large part of the map moves too fast for those interferograms: the velocity there is underestimated and unreliable. "
                "Leave the long pairs out of the network.",
    }[status]
    return [_check("steep_phase", "Fast motion in long interferograms", round(share * 100, 2), "% of pixels", status, message)]


def _ramps(work_dir: Path, mask: Optional[np.ndarray]) -> list[dict]:
    """How large a plane each date of the series carries, from the series MintPy finished with.

    Orbit errors and long-wavelength atmosphere or ionosphere add a different plane to every date.
    Over a year they add up to a false slope: in the Valladolid test (an area that does not move)
    the planes were 10 to 30 mm from one side of the scene to the other, they explained half of the
    velocity error against EGMS, and removing one per date took the error at a GNSS station from
    14 mm/year to 1. The measure is the median over dates of the edge to edge change (east plus north) of a plane
    fitted to the kept pixels. A plane also fits real motion that covers the whole scene, so the message
    does not say which it is: it says what to do if the area is known to be stable."""
    try:
        path = locate_timeseries_file(work_dir)
        with h5py.File(path, "r") as handle:
            cube = handle["timeseries"]
            length, width = cube.shape[1:]
            keep = mask if mask is not None and mask.shape == (length, width) else np.ones((length, width), bool)
            step = max(1, int(np.ceil(np.sqrt(length * width / 40000.0))))
            rows, columns = np.nonzero(keep[::step, ::step])
            if rows.size < THRESHOLDS["ramp_min_pixels"]:
                return []
            data = np.asarray(cube[:, ::step, ::step], "float64")[:, rows, columns] * 1000.0
    except (OSError, KeyError, FileNotFoundError, ValueError):
        return []
    x = columns / max(columns.max(), 1)
    y = rows / max(rows.max(), 1)
    design = np.c_[np.ones_like(x), x, y]
    coefficients = np.linalg.lstsq(design, data.T, rcond=None)[0]
    spans = np.abs(coefficients[1]) + np.abs(coefficients[2])
    median = float(np.median(spans))
    status = "warn" if median >= THRESHOLDS["ramp_warn_mm"] else "ok"
    message = ("Each date carries only a small plane." if status == "ok" else
               "Every date carries a plane, as orbit errors and large scale atmosphere leave them, and over a year they "
               "add up to a false velocity. If this area is known not to move over its whole extent, remove a linear "
               "ramp (the Remove a ramp option); if it does move everywhere, a plane is also what real motion looks like "
               "and removing it would delete the signal.")
    return [_check("date_ramps", "Plane carried by each date", round(median, 1), "mm (median, edge to edge)", status, message)]


def _fading_bias(work_dir: Path) -> list[dict]:
    """The bias short interferograms add to the velocity, from the closure phases (see analysis.fading_bias).

    Left out when the network has no closed loops or the stack is not there: it is one more piece
    of evidence, and its absence must not hide the other checks."""
    from insar_core.analysis import fading_bias

    try:
        return [fading_bias.check(fading_bias.estimate(work_dir))]
    except fading_bias.FadingBiasError:
        return []
    except Exception:                                   # a stack this module cannot read must not break the checks
        return []


def compute_qc(work_dir) -> dict:
    """The checks for the run in `work_dir`.

    Returns {"status": worst status or "unknown", "checks": [...], "metrics": {...}}. A check whose
    files are missing is left out, so a partial run gives a shorter list rather than an error.
    """
    work_dir = Path(work_dir)
    mask = _mask(work_dir)
    checks = [
        *_network(work_dir), *_coherence(work_dir), *_valid_pixels(work_dir, mask),
        *_span(work_dir), *_unwrapping(work_dir, mask), *_residuals(work_dir), *_reference(work_dir, mask),
        *_steep_phase(work_dir, mask),
    ]
    checks += _ramps(work_dir, mask)
    checks += _fading_bias(work_dir)
    status = max((c["status"] for c in checks), key=_ORDER.__getitem__) if checks else "unknown"
    return {"status": status, "checks": checks, "metrics": _uncertainty(work_dir, mask), "thresholds": dict(THRESHOLDS)}
