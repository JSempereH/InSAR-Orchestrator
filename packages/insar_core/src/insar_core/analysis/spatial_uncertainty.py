"""How uncertain a velocity is, as a function of distance: to the reference point, or between two points.

Why the error MintPy states is not enough. `velocityStd` is the standard error of a straight line
fitted to one pixel, relative to the reference pixel. It answers one question (how well is this
pixel known relative to the reference) and it answers it under two assumptions that do not hold:

* that the noise of one date is unrelated to the noise of the next. The troposphere has seasons,
  and so has what is left of it after a correction;
* that nothing else is asked. The noise of an InSAR date is mostly atmosphere, and atmosphere is
  smooth: two pixels 300 m apart carry nearly the same delay, two pixels 30 km apart do not. So the
  difference in velocity between two near pixels is known far better than either of them is known
  relative to a distant reference, and the per-pixel error cannot say so. Whether a bowl subsides
  faster than the field next to it is a question about a short distance, and it deserves a small
  error even when the reference is far away.

What is measured here. For each date, the displacement of every pixel minus its own straight line
is what the line did not explain: noise, and motion that is not linear. The spread of the
difference of that residual between two pixels, as a function of the distance between them, is
the structure function of the date (Hanssen, 2001, section 4.7; Emardson et al., 2003). It is
estimated from pairs of pixels drawn over the whole map, with the median absolute deviation, so
that the minority of pairs that straddle a deforming area do not set it.

A velocity is a weighted sum of the dates (the weights of a least squares line), so the variance
of a difference of velocities at distance d is the same weighted sum of the structure functions:

    var_v(d) = N / (N - 2) * sum_k w_k^2 S_k(d),      w_k = (t_k - mean t) / sum (t - mean t)^2

(Fattahi and Amelung, 2015, section 3). N / (N - 2) makes up for the two parameters each line took
out of its residuals. This holds for dates whose noise is unrelated. Where it is not, the lag-one
autocorrelation r of the differences is measured at each distance and the variance is multiplied by
(1 + r) / (1 - r), the usual correction for a first-order autoregressive noise. Both curves are
returned, so the size of that correction can be seen.

What the number is: precision, not accuracy. It is what the noise of the dates allows, and the
true error is that or more. Anything that grows steadily with time is taken by the straight line
and leaves no trace in the residuals: the fading bias of short interferograms (see
insar_core.analysis.fading_bias), a troposphere that is thicker over the valley at every date, a
slow drift of one pixel against its neighbour. So a second curve is returned beside it: the spread
of the difference of the fitted velocities themselves between pixels at distance d. That one
contains the noise, those steady errors and whatever real difference in motion there is. The
truth lies between the two, and where the ground does not deform the second is the better guide.

How it was checked.
* On simulated stacks with a known, spatially correlated noise and a deforming bowl of 20 mm/year,
  the curve is within 4% of the spread of the velocity errors over 40 realisations. With noise
  related from date to date (autoregressive, 0.5) it is 11% short, where the uncorrected curve is
  44% short: the autocorrelation is itself estimated from short residual series and comes out low.
* On a one-year Sentinel-1 stack over a subsiding basin, compared pixel by pixel with EGMS, the
  spread of the differences grows with distance as the curve does, and is about twice as large
  (2.3 to 5.0 mm/year against 0.9 to 3.3 from 1 to 12 km) after the scatter of EGMS itself is taken
  out. That stack has a measured fading bias of about 2 mm/year that varies over the area.
* On a stable city the two curves meet at 4 km (1.0 mm/year) and differ by four at 300 m (0.16
  against 0.65): neighbouring pixels differ steadily in velocity by more than their noise explains.

References
    Emardson, T. R., Simons, M., and Webb, F. H. (2003), Neutral atmospheric delay in
        interferometric synthetic aperture radar applications: Statistical description and
        mitigation, J. Geophys. Res., 108(B5), 2231.
    Fattahi, H., and Amelung, F. (2015), InSAR bias and uncertainty due to the systematic and
        stochastic tropospheric delay, J. Geophys. Res. Solid Earth, 120, 8758-8773.
    Hanssen, R. F. (2001), Radar Interferometry: Data Interpretation and Error Analysis, Kluwer.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import h5py
import numpy as np

from insar_core.common import pixel_metres, robust_std, years_since_first
from insar_core.pipeline.mintpy_timeseries import _read_geocoding, locate_timeseries_file, locate_velocity_file

CACHE_FILE = "velocity_uncertainty.json"
CITATIONS = (
    "Emardson, T. R., Simons, M., and Webb, F. H. (2003), Neutral atmospheric delay in interferometric synthetic aperture "
    "radar applications: Statistical description and mitigation, J. Geophys. Res., 108(B5), 2231",
    "Fattahi, H., and Amelung, F. (2015), InSAR bias and uncertainty due to the systematic and stochastic tropospheric "
    "delay, J. Geophys. Res. Solid Earth, 120, 8758-8773",
)
MIN_DATES = 6
MIN_PIXELS = 200
MIN_PAIRS_PER_BIN = 200
MAX_AUTOCORRELATION = 0.9


class UncertaintyError(RuntimeError):
    """The uncertainty cannot be estimated, and the message says why."""


_years = years_since_first
_spread = robust_std


def structure(series_mm: np.ndarray, years: np.ndarray, x_m: np.ndarray, y_m: np.ndarray,
              pairs: int = 300_000, bins: int = 14, seed: int = 0) -> dict:
    """The uncertainty of a difference of velocities against distance, from series at scattered pixels.

    `series_mm` is (dates, pixels), `years` the time of each date, `x_m` and `y_m` the position of
    each pixel in metres. Returns the distances (centre of each class), the standard deviation of a
    velocity difference there in mm/year (with and without the correction for noise that is related
    from date to date), the one observed in the velocities themselves, the autocorrelation and the
    number of pairs behind each."""
    series_mm = np.asarray(series_mm, dtype=np.float64)
    n_dates, n_pix = series_mm.shape
    if n_dates < MIN_DATES:
        raise UncertaintyError(f"The series has {n_dates} dates and {MIN_DATES} are needed to tell noise from motion.")
    if n_pix < MIN_PIXELS:
        raise UncertaintyError(f"Only {n_pix} pixels are kept and {MIN_PIXELS} are needed to measure how the noise changes with distance.")
    centred = years - years.mean()
    weights = centred / float((centred ** 2).sum())
    velocity = weights @ series_mm
    residual = series_mm - series_mm.mean(axis=0) - np.outer(centred, velocity)

    rng = np.random.default_rng(seed)
    first = rng.integers(0, n_pix, pairs)
    second = rng.integers(0, n_pix, pairs)
    keep = first != second
    first, second = first[keep], second[keep]
    distance = np.hypot(x_m[first] - x_m[second], y_m[first] - y_m[second])
    positive = distance[distance > 0]
    if positive.size < MIN_PAIRS_PER_BIN:
        raise UncertaintyError("The kept pixels are too few or too close together to measure how the noise changes with distance.")
    # Classes of equal width in the logarithm of the distance, from the nearest pairs to half the
    # extent: beyond it the pairs all join the same two ends of the map and are not a fair sample.
    low, high = float(np.percentile(positive, 0.5)), float(positive.max()) / 2.0
    if high <= low * 1.5:
        raise UncertaintyError("The area is too small to measure how the noise changes with distance.")
    edges = np.geomspace(low, high, bins + 1)
    which = np.digitize(distance, edges) - 1
    correction = n_dates / (n_dates - 2.0)
    out: dict[str, list] = {k: [] for k in ("distance_m", "sigma_mm_yr", "sigma_independent_mm_yr", "observed_mm_yr",
                                            "autocorrelation", "noise_per_date_mm", "pairs")}
    for b in range(bins):
        members = np.flatnonzero(which == b)
        if members.size < MIN_PAIRS_PER_BIN:
            continue
        difference = residual[:, first[members]] - residual[:, second[members]]          # (dates, pairs)
        per_date = _spread(difference, axis=1) ** 2                                       # the structure function of each date
        independent = correction * float((weights ** 2) @ per_date)
        # how alike the differences of consecutive dates are, pair by pair
        a, b_ = difference[:-1], difference[1:]
        denominator = np.sqrt((a ** 2).sum(axis=0) * (b_ ** 2).sum(axis=0))
        lag = float(np.median((a * b_).sum(axis=0)[denominator > 0] / denominator[denominator > 0]))
        r = min(max(lag, 0.0), MAX_AUTOCORRELATION)
        observed = float(_spread(velocity[first[members]] - velocity[second[members]]))
        out["distance_m"].append(float(np.exp(np.mean(np.log(distance[members])))))
        out["sigma_independent_mm_yr"].append(float(np.sqrt(independent)))
        out["sigma_mm_yr"].append(float(np.sqrt(independent * (1.0 + r) / (1.0 - r))))
        out["observed_mm_yr"].append(observed)
        out["autocorrelation"].append(round(lag, 3))
        out["noise_per_date_mm"].append(float(np.sqrt(np.median(per_date) * correction)))
        out["pairs"].append(int(members.size))
    if len(out["distance_m"]) < 3:
        raise UncertaintyError("Too few pairs of pixels at each distance to measure how the noise changes with it.")
    return out


def sigma_at(result: dict, distance_m, key: str = "sigma_mm_yr") -> np.ndarray:
    """The standard deviation of a difference of velocities between two pixels `distance_m` apart, in mm/year.

    Between the classes that were measured it is interpolated, in the logarithm of the distance.
    Nearer than the first class it is that of the first (the noise of the two pixels themselves,
    which does not go to zero), farther than the last it is that of the last, which is then a lower
    bound: `result["max_distance_m"]` says where that starts."""
    distances = np.asarray(result["distance_m"], dtype=float)
    # the curve is not allowed to come down with distance: where it does, it is the scatter of the estimate
    values = np.maximum.accumulate(np.asarray(result[key], dtype=float))
    d = np.clip(np.asarray(distance_m, dtype=float), distances[0], distances[-1])
    return np.interp(np.log(d), np.log(distances), values)


def sigma_map(result: dict, geo: dict, reference: tuple[int, int]) -> np.ndarray:
    """The uncertainty of every pixel's velocity relative to the reference pixel (row, column), in mm/year."""
    dx, dy = pixel_metres(geo)
    rows, cols = np.mgrid[:geo["length"], :geo["width"]]
    distance = np.hypot((cols - reference[1]) * dx, (rows - reference[0]) * dy)
    return sigma_at(result, distance).astype("float32")


def estimate(work_dir, samples: int = 4000, pairs: int = 300_000, seed: int = 0) -> dict:
    """Measure the uncertainty of the velocities of the MintPy run in `work_dir` against distance.

    Reads the final displacement series at `samples` of the pixels the result keeps. Returns the
    curves of `structure` plus what a person needs to read them: the uncertainty at round
    distances, how it compares with the error MintPy states, and the reference pixel."""
    work_dir = Path(work_dir)
    try:
        series_file = locate_timeseries_file(work_dir)
        velocity_file = locate_velocity_file(work_dir)
    except FileNotFoundError as exc:
        raise UncertaintyError(str(exc)) from exc
    with h5py.File(velocity_file, "r") as handle:
        geo = _read_geocoding(handle)
        stated = handle["velocityStd"][:].astype("float32") * 1000.0 if "velocityStd" in handle else None
        kept = np.isfinite(handle["velocity"][:])
        reference = (int(handle.attrs["REF_Y"]), int(handle.attrs["REF_X"])) if "REF_Y" in handle.attrs else None
    mask_file = work_dir / "maskTempCoh.h5"
    if mask_file.is_file():
        with h5py.File(mask_file, "r") as handle:
            mask = handle["mask"][:].astype(bool)
        if mask.shape == kept.shape:
            kept &= mask
    rows, cols = np.nonzero(kept)
    if rows.size < MIN_PIXELS:
        raise UncertaintyError(f"Only {rows.size} pixels are kept and {MIN_PIXELS} are needed to measure how the noise changes with distance.")
    rng = np.random.default_rng(seed)
    chosen = np.sort(rng.choice(rows.size, min(samples, rows.size), replace=False))
    rows, cols = rows[chosen], cols[chosen]
    with h5py.File(series_file, "r") as handle:
        dates = [d.decode() for d in handle["date"][:]]
        cube = handle["timeseries"]
        series = np.empty((len(dates), rows.size), dtype=np.float64)
        for k in range(len(dates)):                      # one date at a time: the cube of a whole scene does not fit in memory
            series[k] = cube[k][rows, cols]
    series *= 1000.0
    good = np.all(np.isfinite(series), axis=0)
    rows, cols, series = rows[good], cols[good], series[:, good]
    years = _years(dates)
    dx, dy = pixel_metres(geo)
    result = structure(series, years, cols * dx, rows * dy, pairs=pairs, seed=seed)

    result.update({
        "dates": len(dates), "span_years": round(float(years[-1] - years[0]), 2), "pixels_sampled": int(rows.size),
        "pixel_m": [round(dx, 1), round(dy, 1)], "max_distance_m": result["distance_m"][-1],
        "series_file": series_file.name,
    })
    marks = [d for d in (500.0, 1000.0, 2000.0, 5000.0, 10_000.0, 20_000.0, 50_000.0, 100_000.0)
             if result["distance_m"][0] <= d <= result["max_distance_m"]]
    result["at"] = [{"distance_km": d / 1000.0, "sigma_mm_yr": round(float(sigma_at(result, d)), 2),
                     "observed_mm_yr": round(float(sigma_at(result, d, "observed_mm_yr")), 2)} for d in marks]
    result["nearest"] = {"distance_km": round(result["distance_m"][0] / 1000.0, 2), "sigma_mm_yr": round(result["sigma_mm_yr"][0], 2)}
    result["farthest"] = {"distance_km": round(result["max_distance_m"] / 1000.0, 1), "sigma_mm_yr": round(float(sigma_at(result, result["max_distance_m"])), 2)}
    if reference is not None:
        distance = np.hypot((cols - reference[1]) * dx, (rows - reference[0]) * dy)
        relative = sigma_at(result, distance)
        result["reference"] = {"row": reference[0], "column": reference[1]}
        result["from_reference_mm_yr"] = {"median": round(float(np.median(relative)), 2), "p90": round(float(np.percentile(relative, 90)), 2)}
        if stated is not None:
            mine = stated[rows, cols]
            usable = np.isfinite(mine) & (mine > 0) & (distance > 0)
            if usable.sum() >= 30:
                result["stated_mm_yr_median"] = round(float(np.median(mine[usable])), 2)
                result["ratio_to_stated"] = round(float(np.median(relative[usable] / mine[usable])), 2)
    for key in ("distance_m", "sigma_mm_yr", "sigma_independent_mm_yr", "observed_mm_yr", "noise_per_date_mm"):
        result[key] = [round(v, 3) for v in result[key]]
    result["results_mtime"] = max(velocity_file.stat().st_mtime, mask_file.stat().st_mtime if mask_file.is_file() else 0.0)
    result["computed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    return result


def read_or_estimate(work_dir) -> Optional[dict]:
    """The estimate for the run in `work_dir`, kept beside the results and made again when they change.

    None when it cannot be made (a short series, a small area): it is one more piece of evidence,
    and a result without it is still a result."""
    work_dir = Path(work_dir)
    cache = work_dir / CACHE_FILE
    try:
        current = locate_velocity_file(work_dir).stat().st_mtime
    except FileNotFoundError:
        return None
    mask_file = work_dir / "maskTempCoh.h5"
    if mask_file.is_file():                                  # the mask decides which pixels are measured
        current = max(current, mask_file.stat().st_mtime)
    try:
        stored = json.loads(cache.read_text(encoding="utf-8"))
        if stored.get("results_mtime") == current:
            return stored
    except (OSError, ValueError):
        pass
    try:
        result = estimate(work_dir)
    except (UncertaintyError, OSError, KeyError, ValueError):
        return None
    try:
        cache.write_text(json.dumps(result), encoding="utf-8")
    except OSError:
        pass
    return result


def describe(result: dict) -> str:
    """One paragraph that says what the numbers mean, for the quality panel and the report."""
    near, far = result["nearest"], result["farthest"]
    seen_near = round(float(sigma_at(result, result["distance_m"][0], "observed_mm_yr")), 2)
    text = (f"From the noise of the dates, the difference in velocity between two points {near['distance_km'] * 1000:.0f} m "
            f"apart is precise to {near['sigma_mm_yr']} mm/year (one standard deviation), and between two points "
            f"{far['distance_km']:g} km apart to {far['sigma_mm_yr']} mm/year.")
    relative = result.get("from_reference_mm_yr")
    if relative:
        text += f" Relative to the reference point the typical pixel is precise to {relative['median']} mm/year."
    ratio = result.get("ratio_to_stated")
    if ratio is not None and (ratio >= 1.3 or ratio <= 0.77):
        text += f" That is {ratio:g} times the standard error MintPy states for it."
    text += (" This is precision, not accuracy: errors that grow steadily with time (the fading bias, a troposphere that "
             f"is the same at every date) are not in it. The velocities of pixels {near['distance_km'] * 1000:.0f} m apart "
             f"actually differ by {seen_near} mm/year, which is that noise plus those errors plus real motion.")
    return text
