"""Tie a MintPy velocity map to GNSS, and measure how far its stated uncertainty can be trusted.

MintPy gives velocity relative to one reference pixel, which it assumes does not move, and a
standard error from a straight-line fit. Two things follow that a user of the map needs:

* Where the reference pixel really moves, every other pixel is shifted by the same amount.
  GNSS stations measure that absolute motion. `calibrate` estimates the shift from the stations
  inside the area and, only when it helps, applies it.
* The standard error of a straight-line fit treats the residual noise as independent from date
  to date. Atmospheric noise is not, so the stated error is too small. `uncertainty_scale`
  measures by how much, against a source of ground truth that has its own errors (EGMS or GNSS).

How the calibration is made, and why

1. A station velocity is estimated from its daily positions over the InSAR window, after the
   jumps of known equipment changes are taken out (`correct_equipment_steps`): an antenna change
   moves the series by millimetres without the ground moving. With two years or more of data the
   estimator is MIDAS (Blewitt et al., 2016): the median of the slopes between positions one
   year apart, which the seasons and any jump that was missed do not move. With less it is a
   straight line by least squares, whose error is widened for the autocorrelation of the daily
   positions. Over a year or so a line is not the long-term velocity of the station: the seasons
   move it by several mm/year (seen on real stations: +8 mm/year of apparent uplift over one
   year at a station that is stable over twenty). It is used all the same, on purpose: MintPy's
   velocity is a line over that same window and carries the same share of the seasons, so the
   two are still comparable, while neither is a long-term rate. Either error has a floor (`GNSS_SIGMA_FLOOR`), because daily
   GNSS positions are correlated in time and the formal error is optimistic.
2. The map is read at the station's pixel as the median of the 3 x 3 pixels around it, so that
   the noise of one pixel does not decide the calibration.
3. What a station measures depends on the quantity being calibrated. For the vertical velocity
   it is the up velocity. For the line of sight it is the three components projected with that
   pixel's own viewing geometry. For the east-west velocity it is the east velocity.
4. GNSS velocities from a global solution include the motion of the whole plate, which every
   station in a small area shares and InSAR, tied to a nearby pixel, does not see. It is removed
   before the horizontal components are used, in one of two ways (`frame`):
   * "plate": the velocity the ITRF2014 plate motion model gives at each station is subtracted
     (see `insar_core.analysis.plate_motion`). What is left is motion relative to the rigid
     plate, the same frame EGMS uses, and one station is enough.
   * "stations": the median horizontal velocity of the stations is subtracted. That makes the
     result relative to the stations' own average motion and needs at least two of them: with
     one, nothing of its horizontal motion would be left.
   The vertical component is not touched and can be calibrated with one station either way.
5. The shift is the error-weighted mean of the differences between GNSS and the map. When more
   than one station is used, the spread of the differences is compared with what the stated
   errors predict (reduced chi-square) and the uncertainty of the shift is inflated by its
   square root when it is above one.
6. With two or more stations each is left out in turn: the shift is estimated from the others
   and used to predict the left-out station. If that does not reduce the error against stations
   the shift has not seen, it is reported and not applied. With one station there is nothing to
   test against, and the result is marked as indicative.
"""
from __future__ import annotations

import math
from typing import Optional, Sequence

import numpy as np
import pyproj

from insar_core.analysis import plate_motion
from insar_core.common import day_number, robust_std, years_since_epoch
from insar_core.analysis.validation import los_unit_vector

GNSS_SIGMA_FLOOR = 1.0       # mm/year, a floor under the formal error of a GNSS velocity
MIN_GNSS_DATES = 20
PIXEL_HALF_WINDOW = 1        # a 3 x 3 window around the station
MIDAS_MIN_YEARS = 2.0        # below this there are too few pairs one year apart
MIDAS_MIN_PAIRS = 30
MIN_DATES_BESIDE_STEP = 10   # a jump is only estimated with this many positions on each side
FRAMES = ("stations", "plate")


def correct_equipment_steps(series: dict, steps: Sequence[dict], start: str, end: str) -> tuple[dict, list[dict]]:
    """The series over [start, end] with the jumps of equipment changes removed, and what was done.

    `steps` is the station's list from `insar_core.adapters.ngl.parse_steps`. For each equipment
    change inside the window the size of the jump is estimated, per component, in one fit of the
    whole window: a straight line, an annual and a semi-annual cycle when there are two years or
    more, and one step function per change. The positions after each change are then moved back
    by its jump. A change with fewer than MIN_DATES_BESIDE_STEP positions before or after it
    cannot be estimated, so the positions on its short side are dropped instead.

    Earthquakes are not corrected: NGL lists every earthquake that could have moved a station,
    and where it did the jump is real ground motion that InSAR saw as well. They are returned in
    the report so the reader knows a straight line may not describe that station.

    Returns the corrected `{date: (east, north, up)}` and a list of `{date, kind, detail, action,
    jump_mm}`, where action is "removed", "trimmed" or "kept"."""
    dates = sorted(d for d in series if start <= d <= end)
    report: list[dict] = []
    inside = [s for s in steps if start < s["date"] <= end]
    for step in inside:
        if step["kind"] == "earthquake":
            report.append({**step, "action": "kept", "jump_mm": None})
    # several changes on one day are one jump
    changes = sorted({s["date"] for s in inside if s["kind"] == "equipment"})
    details = {d: "; ".join(s["detail"] for s in inside if s["kind"] == "equipment" and s["date"] == d) for d in changes}

    usable: list[str] = []
    for change in changes:
        before = sum(1 for d in dates if d < change)
        after = len(dates) - before
        if before < MIN_DATES_BESIDE_STEP or after < MIN_DATES_BESIDE_STEP:
            dates = [d for d in dates if d >= change] if before < after else [d for d in dates if d < change]
            report.append({"date": change, "kind": "equipment", "detail": details[change], "action": "trimmed", "jump_mm": None})
        else:
            usable.append(change)
    # a trim may have left another change without positions on one side
    usable = [c for c in usable if sum(1 for d in dates if d < c) >= MIN_DATES_BESIDE_STEP
              and sum(1 for d in dates if d >= c) >= MIN_DATES_BESIDE_STEP]
    if not usable or len(dates) < 2 * MIN_DATES_BESIDE_STEP:
        return {d: tuple(series[d]) for d in dates}, report

    years = years_since_epoch(dates)
    t = years - years.mean()
    columns = [np.ones_like(t), t]
    if years[-1] - years[0] >= MIDAS_MIN_YEARS:
        for harmonic in (1.0, 2.0):
            columns += [np.sin(2 * np.pi * harmonic * years), np.cos(2 * np.pi * harmonic * years)]
    first_step = len(columns)
    date_array = np.array(dates)
    for change in usable:
        columns.append((date_array >= change).astype(float))
    design = np.column_stack(columns)
    values = np.array([series[d] for d in dates], float)
    solution, *_ = np.linalg.lstsq(design, values, rcond=None)
    jumps = solution[first_step:]                                   # changes x 3
    corrected = values - design[:, first_step:] @ jumps
    for change, jump in zip(usable, jumps, strict=False):
        report.append({"date": change, "kind": "equipment", "detail": details[change], "action": "removed",
                       "jump_mm": [float(v) for v in jump]})
    report.sort(key=lambda r: r["date"])
    return {d: tuple(float(v) for v in row) for d, row in zip(dates, corrected, strict=False)}, report


def _midas(days: np.ndarray, values: np.ndarray) -> Optional[tuple[np.ndarray, np.ndarray, int]]:
    """MIDAS velocity (Blewitt et al., 2016) of each column of `values` (dates x components).

    The slope between every position and the one a year later (within a day) is taken, and the
    velocity is the median of those slopes after dropping the ones more than two robust standard
    deviations from the first median. A year apart, the seasons cancel; as a median, a jump or a
    bad week touches few of the slopes and does not move it. The error is the robust spread of
    the slopes over the square root of the number of independent pairs (a quarter of them),
    times 1.2533 for a median and times 3, the factor the authors found against real series.
    Returns (velocity, sigma, pairs) in units per year, or None with too few pairs."""
    position = {int(day): index for index, day in enumerate(days)}
    first, second = [], []
    for index, day in enumerate(days):
        for apart in (365, 366, 364):
            partner = position.get(int(day) + apart)
            if partner is not None:
                first.append(index)
                second.append(partner)
                break
    if len(first) < MIDAS_MIN_PAIRS:
        return None
    span = (days[second] - days[first]) / 365.25
    slopes = (values[second] - values[first]) / span[:, None]
    velocity, sigma = np.empty(values.shape[1]), np.empty(values.shape[1])
    for column in range(values.shape[1]):
        v = slopes[:, column]
        median = np.median(v)
        spread = float(robust_std(v, axis=None))
        kept = v[np.abs(v - median) <= 2.0 * spread] if spread > 0 else v
        velocity[column] = np.median(kept)
        spread = float(robust_std(kept, axis=None))
        sigma[column] = 3.0 * 1.2533 * spread / math.sqrt(max(kept.size / 4.0, 1.0))
    return velocity, sigma, len(first)


def gnss_velocity(series: dict, start: str, end: str, min_dates: int = MIN_GNSS_DATES, method: str = "auto") -> Optional[dict]:
    """East, north and up velocity (mm/year) of a station over [start, end], with the error of each.

    `series` maps 'YYYYMMDD' to (east, north, up) in millimetres; pass it through
    `correct_equipment_steps` first when the station's steps are known. `method` is "midas",
    "least_squares" or "auto": MIDAS when the data cover MIDAS_MIN_YEARS and give enough pairs a
    year apart, a straight line by least squares otherwise. Returns None when the station has
    fewer than `min_dates` dates in the window, or covers less than half of it."""
    if method not in ("auto", "midas", "least_squares"):
        raise ValueError("method must be auto, midas or least_squares")
    dates = sorted(d for d in series if start <= d <= end)
    if len(dates) < min_dates:
        return None
    years = years_since_epoch(dates)
    window = (years_since_epoch([end])[0] - years_since_epoch([start])[0])
    if years[-1] - years[0] < 0.5 * window:
        return None
    values = np.array([series[d] for d in dates], float)            # dates x 3
    used, pairs = "least_squares", None
    slope = sigma = None
    if method == "midas" or (method == "auto" and years[-1] - years[0] >= MIDAS_MIN_YEARS):
        found = _midas(np.array([day_number(d) for d in dates]), values)
        if found is not None:
            slope, sigma, pairs = found
            used = "midas"
    if slope is None:
        centred = years - years.mean()
        sxx = float(centred @ centred)
        slope = (values - values.mean(axis=0)).T @ centred / sxx
        residual = values - values.mean(axis=0) - np.outer(centred, slope)
        sigma = np.sqrt((residual ** 2).sum(axis=0) / (len(dates) - 2)) / math.sqrt(sxx)
        # Daily positions are not independent: what the line leaves (the seasons above all) lasts for
        # months. With a lag-1 autocorrelation r the positions are worth n (1 - r) / (1 + r) independent
        # ones, so the error grows by the square root of the inverse. On real stations over one year a
        # line gives a vertical velocity several mm/year off with a formal error of a tenth of that.
        centred_residual = residual - residual.mean(axis=0)
        energy = (centred_residual ** 2).sum(axis=0)
        r1 = np.where(energy > 0, (centred_residual[1:] * centred_residual[:-1]).sum(axis=0) / np.where(energy > 0, energy, 1.0), 0.0)
        r1 = np.clip(r1, 0.0, 0.99)
        sigma = sigma * np.sqrt((1.0 + r1) / (1.0 - r1))
    sigma = np.maximum(sigma, GNSS_SIGMA_FLOOR)
    return {"east": float(slope[0]), "north": float(slope[1]), "up": float(slope[2]),
            "sigma_east": float(sigma[0]), "sigma_north": float(sigma[1]), "sigma_up": float(sigma[2]),
            "dates": len(dates), "window": [dates[0], dates[-1]], "method": used, "pairs": pairs}


def _pixel(geo: dict, lat: float, lon: float) -> tuple[int, int]:
    x, y = pyproj.Transformer.from_crs(4326, geo["epsg"], always_xy=True).transform(lon, lat)
    return int(np.floor((y - geo["y_first"]) / geo["y_step"])), int(np.floor((x - geo["x_first"]) / geo["x_step"]))


def _local(array: np.ndarray, row: int, column: int) -> float:
    half = PIXEL_HALF_WINDOW
    block = array[max(row - half, 0): row + half + 1, max(column - half, 0): column + half + 1]
    finite = block[np.isfinite(block)]
    return float(np.median(finite)) if finite.size >= 3 else float("nan")


def _weighted_offset(differences: np.ndarray, sigmas: np.ndarray) -> tuple[float, float, Optional[float]]:
    """Error-weighted mean of the differences, its uncertainty and the reduced chi-square (None for one value)."""
    weights = 1.0 / sigmas ** 2
    offset = float(np.sum(weights * differences) / np.sum(weights))
    sigma = float(1.0 / math.sqrt(np.sum(weights)))
    if differences.size < 2:
        return offset, sigma, None
    chi2 = float(np.sum(weights * (differences - offset) ** 2) / (differences.size - 1))
    return offset, sigma * max(1.0, math.sqrt(chi2)), chi2


QUANTITIES = ("los", "up", "east")


def calibrate(
    *, velocity: np.ndarray, std: Optional[np.ndarray], geo: dict, stations: list[dict], quantity: str,
    incidence: Optional[np.ndarray] = None, azimuth: Optional[np.ndarray] = None, frame: str = "stations",
    plate: Optional[str] = None,
) -> dict:
    """The shift that ties `velocity` (mm/year, NaN where masked) to the GNSS `stations`, and whether to apply it.

    Each station is `{"name", "lat", "lon", "velocity": gnss_velocity(...)}`. `quantity` is "los", "up" or "east".
    For "los" the viewing geometry (`incidence`, `azimuth`, in degrees, same grid) is needed.
    `frame` says how the motion of the plate is taken out of the horizontal GNSS velocity: "stations"
    (their median, needs two) or "plate" (the ITRF2014 plate motion model, one is enough). With
    "plate", `plate` names the plate (see `plate_motion.PLATES_MAS_YR`); left out, the plate that
    best matches the stations is used and named in the result.

    Returns a dict with `applied`, `offset_mm_yr` (add it to the map), `offset_sigma_mm_yr`, the stations used and
    the leave-one-out errors before and after. Raises ValueError when no station can be used."""
    if quantity not in QUANTITIES:
        raise ValueError(f"quantity must be one of {', '.join(QUANTITIES)}")
    if quantity == "los" and (incidence is None or azimuth is None):
        raise ValueError("The line-of-sight calibration needs the incidence and azimuth angles")
    if frame not in FRAMES:
        raise ValueError(f"frame must be one of {', '.join(FRAMES)}")
    length, width = velocity.shape
    usable = []
    for station in stations:
        gnss = station.get("velocity")
        if not gnss:
            continue
        row, column = _pixel(geo, station["lat"], station["lon"])
        if not (0 <= row < length and 0 <= column < width):
            continue
        insar = _local(velocity, row, column)
        if not np.isfinite(insar):
            continue
        local_std = _local(std, row, column) if std is not None else float("nan")
        usable.append({"station": station, "row": row, "column": column, "insar": insar,
                       "insar_sigma": local_std if np.isfinite(local_std) else 0.0})
    if not usable:
        raise ValueError("No GNSS station with a long enough record falls on a valid pixel of the map.")

    # Remove the motion every station shares (the plate), for the horizontal components only. With a single station
    # that leaves nothing of its horizontal motion, and a "shift" would only force the map to zero there: refuse.
    removed = {"east": 0.0, "north": 0.0}
    plate_found: Optional[dict] = None
    if quantity != "up" and frame == "plate":
        if plate is None:
            plate_found = plate_motion.best_plate([
                {"lat": u["station"]["lat"], "lon": u["station"]["lon"], "east": u["station"]["velocity"]["east"],
                 "north": u["station"]["velocity"]["north"]} for u in usable])
        else:
            plate_found = {"plate": plate, "name": plate_motion.PLATE_NAMES.get(plate, plate), "misfit_mm_yr": None, "rigid": True}
        for u in usable:
            east, north = plate_motion.plate_velocity(plate_found["plate"], u["station"]["lat"], u["station"]["lon"])
            u["plate"] = {"east": east, "north": north}
        removed = {"east": float(np.median([u["plate"]["east"] for u in usable])),
                   "north": float(np.median([u["plate"]["north"] for u in usable]))}
    elif quantity != "up":
        if len(usable) < 2:
            raise ValueError("Calibrating a horizontal or line-of-sight velocity against the stations' own average needs at "
                             "least two stations: with one, the motion of the plate cannot be told apart from the motion of "
                             "the ground. Use the plate motion model (frame='plate'), which works with one, or calibrate "
                             "the vertical component, which needs no frame.")
        removed = {"east": float(np.median([u["station"]["velocity"]["east"] for u in usable])),
                   "north": float(np.median([u["station"]["velocity"]["north"] for u in usable]))}
        for u in usable:
            u["plate"] = removed

    rows = []
    for u in usable:
        gnss = u["station"]["velocity"]
        if quantity == "up":
            truth, sigma = gnss["up"], gnss["sigma_up"]
        elif quantity == "east":
            truth, sigma = gnss["east"] - u["plate"]["east"], gnss["sigma_east"]
        else:
            assert incidence is not None and azimuth is not None      # checked at the top
            w = los_unit_vector(float(incidence[u["row"], u["column"]]), float(azimuth[u["row"], u["column"]]))
            truth = float(w @ np.array([gnss["east"] - u["plate"]["east"], gnss["north"] - u["plate"]["north"], gnss["up"]]))
            sigma = float(math.sqrt((w[0] * gnss["sigma_east"]) ** 2 + (w[1] * gnss["sigma_north"]) ** 2 + (w[2] * gnss["sigma_up"]) ** 2))
        total = math.sqrt(sigma ** 2 + u["insar_sigma"] ** 2)
        rows.append({"name": u["station"]["name"], "lat": u["station"]["lat"], "lon": u["station"]["lon"],
                     "dates": gnss["dates"], "method": gnss.get("method"), "gnss_mm_yr": float(truth), "insar_mm_yr": u["insar"],
                     "difference_mm_yr": float(truth - u["insar"]), "sigma_mm_yr": total})

    differences = np.array([r["difference_mm_yr"] for r in rows])
    sigmas = np.array([r["sigma_mm_yr"] for r in rows])
    offset, offset_sigma, chi2 = _weighted_offset(differences, sigmas)
    out = {
        "quantity": quantity, "n": len(rows), "stations": rows, "offset_mm_yr": offset, "offset_sigma_mm_yr": offset_sigma,
        "reduced_chi2": chi2, "frame_removed_mm_yr": removed if quantity != "up" else None,
        "frame": frame if quantity != "up" else None, "plate": plate_found,
        "rmse_before_mm_yr": float(np.sqrt(np.mean(differences ** 2))),
    }
    for index, row in enumerate(rows):
        row["residual_mm_yr"] = float(differences[index] - offset)

    if len(rows) >= 2:
        left_out = []
        for index in range(len(rows)):
            others = np.arange(len(rows)) != index
            shift, _, _ = _weighted_offset(differences[others], sigmas[others])
            left_out.append(differences[index] - shift)
            rows[index]["left_out_residual_mm_yr"] = float(left_out[-1])
        loo_after = float(np.sqrt(np.mean(np.square(left_out))))
        out["loo_rmse_after_mm_yr"] = loo_after
        helps = loo_after < out["rmse_before_mm_yr"]
        out["applied"] = bool(helps)
        if helps:
            out["verdict"] = {
                "status": "ok" if len(rows) >= 3 else "warn",
                "message": (f"Shift of {offset:+.1f} mm/year applied. Against stations it had not seen, the error went from "
                            f"{out['rmse_before_mm_yr']:.1f} to {loo_after:.1f} mm/year"
                            + ("." if len(rows) >= 3 else ", with only two stations to test on."))}
        else:
            out["verdict"] = {"status": "fail", "message": (
                f"A shift of {offset:+.1f} mm/year does not reduce the error against stations it had not seen "
                f"({out['rmse_before_mm_yr']:.1f} before, {loo_after:.1f} after), so it was not applied. The stations "
                "disagree with each other more than with the map.")}
    else:
        out["loo_rmse_after_mm_yr"] = None
        out["applied"] = True
        out["verdict"] = {"status": "warn", "message": (
            f"One station gives a shift of {offset:+.1f} mm/year (uncertainty {offset_sigma:.1f}). With one station there "
            "is nothing to test it against, so treat the calibrated map as indicative.")}
    if quantity != "up" and frame == "plate" and plate_found is not None:
        out["verdict"]["message"] += f" Horizontal motion is relative to the {plate_found['name']} plate (ITRF2014 plate motion model)."
        if not plate_found["rigid"]:
            out["verdict"]["message"] += (f" The stations move {plate_found['misfit_mm_yr']:.1f} mm/year relative to it, more than a "
                                          "rigid plate allows: a plate boundary zone, local horizontal motion (the flank of a "
                                          "subsidence bowl moves sideways) or a record too short for a horizontal velocity. "
                                          "That motion is in the result.")
            if out["verdict"]["status"] == "ok":
                out["verdict"]["status"] = "warn"
    elif quantity != "up":
        out["verdict"]["message"] += (" Horizontal motion is relative to the average of the stations, not to a plate.")
    return out


def uncertainty_scale(difference: np.ndarray, sigma: np.ndarray, sigma_other: Optional[np.ndarray] = None) -> dict:
    """By how much MintPy's stated error is too small, measured against another source.

    `difference` is MintPy minus the other source, per pixel, after both are re-referenced to the same zero.
    `sigma` is MintPy's stated standard error and `sigma_other` that of the other source (taken as zero when not
    given, which makes the scale an upper bound). The ratio of the observed spread to the stated one is estimated with
    the median absolute deviation, which a few badly unwrapped pixels do not move."""
    difference, sigma = np.asarray(difference, float), np.asarray(sigma, float)
    other = np.zeros_like(sigma) if sigma_other is None else np.asarray(sigma_other, float)
    keep = np.isfinite(difference) & np.isfinite(sigma) & (sigma > 0) & np.isfinite(other)
    if int(keep.sum()) < 30:
        raise ValueError("Too few pixels with a stated error to measure the uncertainty.")
    total = np.sqrt(sigma[keep] ** 2 + other[keep] ** 2)
    z = difference[keep] / total
    spread = float(robust_std(z, axis=None))
    scale = max(spread, 1e-6)

    def coverage(factor: float, width: float) -> float:
        return float(np.mean(np.abs(z / factor) < width))

    return {
        "n": int(keep.sum()), "scale": float(scale), "median_stated_sigma_mm_yr": float(np.median(sigma[keep])),
        "median_observed_spread_mm_yr": float(robust_std(difference[keep], axis=None)),
        "coverage_1sigma": coverage(1.0, 1.0), "coverage_2sigma": coverage(1.0, 2.0),
        "coverage_1sigma_scaled": coverage(scale, 1.0), "coverage_2sigma_scaled": coverage(scale, 2.0),
        "recommended_scale": float(max(scale, 1.0)),
        "message": (
            f"The stated error is about {scale:.1f} times too small: {coverage(1.0, 1.0) * 100:.0f}% of the pixels fall within "
            "one stated standard error of the other source, where 68% would be expected."
            if scale > 1.2 else
            f"The stated error is about right: {coverage(1.0, 1.0) * 100:.0f}% of the pixels fall within one stated standard error "
            "of the other source, where 68% would be expected." if scale >= 0.8 else
            f"The stated error is larger than the observed spread (scale {scale:.1f}); it was not shrunk, since a few thousand "
            "pixels of one area do not justify smaller error bars."),
    }
