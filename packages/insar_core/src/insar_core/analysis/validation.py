"""Compare a MintPy result with an independent source of ground motion: how far to trust it.

Two sources are supported, both in the line of sight (LOS) of the same Sentinel-1 track:

* `compare_with_points`: a cloud of measurement points with their own time series, such as
  the European Ground Motion Service (EGMS L2a and L2b products).
* `compare_with_stations`: GNSS stations, whose east, north and up motion is projected onto
  the LOS first.

Both give the same kind of answer: velocity and time series differences, with the part that
no comparison can settle (the common offset) removed and stated.

How the comparison is made, and why

1. Only dates both sources have are used. MintPy is relative to a reference pixel and a
   reference date, EGMS to GNSS, so a constant between two series means nothing: when the
   series themselves are compared, the mean difference of each pixel is removed first. (Lining
   both up on the first common date instead would put the noise of that one date into every
   other date.) A date that only one source has cannot be compared.
2. EGMS has hundreds of points per square kilometre and MintPy one value per 80 m pixel, so
   the points are put into the MintPy pixel they fall in and their median is compared (a
   median, because a single point on a roof or a car park does not describe the pixel).
   Pixels with fewer than `min_points` points are left out.
3. Each source is fitted with a straight line over the window (mm/year).
4. The raw difference mixes two things: how well the two agree, and which point each one
   calls zero. The common offset is estimated as the median over the compared pixels and
   removed from each side ("re-referenced"), and both figures are reported.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional, Sequence

import numpy as np
import pyproj

from insar_core.common import years_since_first

MIN_DATES = 5
MAX_SCATTER_POINTS = 4000


_years = years_since_first


def _slopes(series: np.ndarray, years: np.ndarray) -> np.ndarray:
    """Straight line fit of every row of `series` (pixels x dates), in units per year."""
    centred = years - years.mean()
    return (series - series.mean(axis=1, keepdims=True)) @ centred / float(centred @ centred)


def _orthogonal_slope(a: np.ndarray, b: np.ndarray) -> Optional[float]:
    """Slope of the line that fits `b` against `a` when both carry noise (Deming regression with
    equal error variances, also called orthogonal or major axis regression).

    Ordinary least squares of b on a assumes a is exact. When a is noisy too, as EGMS and GNSS
    are, its slope is pulled towards zero (regression dilution), which would read as MintPy
    recovering less motion than there is. This slope does not have that bias."""
    if a.size <= 2:
        return None
    da, db = a - a.mean(), b - b.mean()
    saa, sbb, sab = float(da @ da), float(db @ db), float(da @ db)
    if saa <= 0 or sab == 0:
        return None
    return float((sbb - saa + np.sqrt((sbb - saa) ** 2 + 4 * sab ** 2)) / (2 * sab))


def _stats(a: np.ndarray, b: np.ndarray) -> dict:
    """How `b` (MintPy) differs from `a` (the other source)."""
    difference = b - a
    slope = float(np.polyfit(a, b, 1)[0]) if a.size > 2 and np.std(a) > 0 else None
    correlation = float(np.corrcoef(a, b)[0, 1]) if a.size > 2 and np.std(a) > 0 and np.std(b) > 0 else None
    return {
        "n": int(a.size),
        "bias_mm_yr": float(np.mean(difference)),
        "rmse_mm_yr": float(np.sqrt(np.mean(difference ** 2))),
        "mad_mm_yr": float(np.median(np.abs(difference - np.median(difference)))),
        "pearson_r": correlation,
        "slope_b_on_a": slope,
        "slope_orthogonal": _orthogonal_slope(a, b),
        "std_a": float(np.std(a)),
        "std_b": float(np.std(b)),
    }


def _verdict(rereferenced: dict, noise: Optional[float]) -> dict:
    """A plain reading of the numbers. Rules of thumb, not a proof: see the module docstring."""
    r, rmse, spread = rereferenced["pearson_r"], rereferenced["rmse_mm_yr"], rereferenced["std_a"]
    # the slope that allows for noise in both sources; see _orthogonal_slope
    slope = rereferenced.get("slope_orthogonal") or rereferenced["slope_b_on_a"]
    if rereferenced["n"] < 30:
        return {"status": "warn", "message": f"Only {rereferenced['n']} pixels could be compared, too few to say much."}
    if spread < 4.0:
        # Almost nothing moves: the correlation of noise with noise says nothing, so the size of the error does.
        status = "ok" if rmse <= 3.0 else "warn" if rmse <= 6.0 else "fail"
        return {"status": status, "message": (
            f"The area barely deforms (spread {spread:.1f} mm/year), so agreement is judged by the error: "
            f"{rmse:.1f} mm/year RMSE." + (" This is a check that the result is quiet where the ground is quiet, "
                                           "not a check that it recovers real motion." if status == "ok" else ""))}
    if r is None or slope is None:
        return {"status": "warn", "message": "The comparison could not be summarised."}
    if r >= 0.9 and 0.85 <= slope <= 1.15:
        return {"status": "ok", "message": (
            f"MintPy recovers the same pattern of motion as the other source: correlation {r:.2f}, "
            f"slope {slope:.2f}, RMSE {rmse:.1f} mm/year" + (f" (the other source's own noise within a pixel is {noise:.1f})." if noise else "."))}
    if r >= 0.7:
        return {"status": "warn", "message": (
            f"The pattern is similar (correlation {r:.2f}) but the amplitude differs (slope {slope:.2f}) "
            "or the scatter is large. Check the troposphere correction and the reference point.")}
    return {"status": "fail", "message": (
        f"Weak agreement (correlation {r:.2f}, slope {slope:.2f}). Check that both are the same track and "
        "direction, and look at the quality checks of the run.")}


_BIN_EDGES = (-np.inf, -30.0, -20.0, -10.0, -3.0, 3.0, 10.0, 20.0, 30.0, np.inf)


def _by_velocity(a: np.ndarray, b: np.ndarray) -> list[dict]:
    """Agreement within bands of the other source's velocity, to show whether the error depends on
    how fast the ground moves (InSAR can lose the fastest motion to unwrapping errors)."""
    rows = []
    for low, high in zip(_BIN_EDGES[:-1], _BIN_EDGES[1:], strict=False):
        inside = (a >= low) & (a < high)
        if inside.sum() < 5:
            continue
        rows.append({
            "from": None if np.isinf(low) else low, "to": None if np.isinf(high) else high, "n": int(inside.sum()),
            "other_mm_yr": float(a[inside].mean()), "mintpy_mm_yr": float(b[inside].mean()),
            "bias_mm_yr": float(np.mean(b[inside] - a[inside])),
            "rmse_mm_yr": float(np.sqrt(np.mean((b[inside] - a[inside]) ** 2))),
        })
    return rows


def _thin(*arrays: np.ndarray, limit: int = MAX_SCATTER_POINTS):
    size = arrays[0].size
    if size <= limit:
        return [a.tolist() for a in arrays]
    keep = np.linspace(0, size - 1, limit).astype(int)
    return [a[keep].tolist() for a in arrays]


def _grouped(series: np.ndarray, velocity: np.ndarray, start: np.ndarray, count: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Median series and spread of the velocities of each group of consecutive rows.

    Group g is rows start[g] to start[g] + count[g]. Groups of the same size are stacked and
    reduced in one numpy call, so the cost follows the number of distinct sizes (tens), not the
    number of pixels (which can be hundreds of thousands)."""
    medians = np.empty((start.size, series.shape[1]), "float64")
    noise = np.empty(start.size, "float64")
    for size in np.unique(count):
        groups = np.flatnonzero(count == size)
        rows = start[groups][:, None] + np.arange(size)[None, :]          # groups x size
        medians[groups] = np.median(series[rows], axis=1)
        noise[groups] = np.std(velocity[rows], axis=1)
    return medians, noise


def gnss_on_dates(series: dict, dates: Sequence[str], half_window_days: int = 3) -> dict:
    """The station position on each of `dates`: the median of its daily positions within
    `half_window_days` of the date, per component. Dates with no position in the window are left out.

    A daily GNSS position has a noise of 2 to 3 mm horizontally and 5 to 8 mm vertically, while
    the ground moves far less than that in a week. The median of a week of positions cuts that
    noise by about half and is not moved by one bad day, so a comparison with InSAR reflects the
    ground and not the GNSS noise of the acquisition day."""
    out = {}
    for date in dates:
        day = dt.datetime.strptime(date, "%Y%m%d")
        near = [series[key] for offset in range(-half_window_days, half_window_days + 1)
                if (key := (day + dt.timedelta(days=offset)).strftime("%Y%m%d")) in series]
        if near:
            out[date] = tuple(float(v) for v in np.median(np.asarray(near, float), axis=0))
    return out


def _pixel_of(lon: np.ndarray, lat: np.ndarray, geo: dict) -> tuple[np.ndarray, np.ndarray]:
    x, y = pyproj.Transformer.from_crs(4326, geo["epsg"], always_xy=True).transform(lon, lat)
    columns = np.floor((np.asarray(x) - geo["x_first"]) / geo["x_step"]).astype(int)
    rows = np.floor((np.asarray(y) - geo["y_first"]) / geo["y_step"]).astype(int)
    return rows, columns


def compare_with_points(
    *, geo: dict, mintpy_dates: Sequence[str], mintpy_series_mm: np.ndarray, valid: np.ndarray,
    point_lat: np.ndarray, point_lon: np.ndarray, point_dates: Sequence[str], point_series_mm: np.ndarray,
    min_points: int = 3, mintpy_velocity_std_mm: Optional[np.ndarray] = None,
) -> dict:
    """MintPy against a cloud of points with time series (EGMS).

    `geo` has x_first, y_first, x_step, y_step and epsg of the MintPy grid. `mintpy_series_mm` is
    dates x rows x columns, `valid` rows x columns (True where MintPy trusts the pixel),
    `point_series_mm` points x dates. Raises ValueError when the two cannot be compared.
    `mintpy_velocity_std_mm` (rows x columns, mm/year), when given, is used to check how realistic MintPy's stated
    error is: see `insar_core.analysis.calibration.uncertainty_scale`.
    """
    common = sorted(set(mintpy_dates) & set(point_dates))
    if len(common) < MIN_DATES:
        raise ValueError(
            f"The two series share only {len(common)} dates ({MIN_DATES} are needed). EGMS and the HyP3 pairs "
            "must come from the same track, so that the acquisition dates match.")
    m_position = {d: i for i, d in enumerate(mintpy_dates)}
    p_position = {d: i for i, d in enumerate(point_dates)}
    m_index = [m_position[d] for d in common]
    p_index = [p_position[d] for d in common]
    years = _years(common)

    # The cube is only read at the pixels that hold points, further down: converting all of it
    # here would cost several times the size of the time series file in memory.
    cube = mintpy_series_mm
    series = np.asarray(point_series_mm, "float64")[:, p_index]
    finite = np.isfinite(series).all(axis=1)
    lat, lon, series = np.asarray(point_lat)[finite], np.asarray(point_lon)[finite], series[finite]

    rows, columns = _pixel_of(lon, lat, geo)
    length, width = cube.shape[1:]
    inside = (rows >= 0) & (rows < length) & (columns >= 0) & (columns < width)
    inside[inside] &= np.asarray(valid, bool)[rows[inside], columns[inside]]
    rows, columns, series = rows[inside], columns[inside], series[inside]
    if rows.size == 0:
        raise ValueError("No EGMS point falls on a valid MintPy pixel: the two cover different areas.")

    keys = rows * width + columns
    order = np.argsort(keys, kind="stable")
    keys, series = keys[order], series[order]
    unique, start, count = np.unique(keys, return_index=True, return_counts=True)
    enough = count >= min_points
    pixels = unique[enough]
    if pixels.size == 0:
        raise ValueError(f"No MintPy pixel holds {min_points} or more EGMS points.")
    point_velocity = _slopes(series, years)
    medians, noise = _grouped(series, point_velocity, start[enough], count[enough])

    pixel_rows, pixel_columns = pixels // width, pixels % width
    mintpy = np.asarray(cube[:, pixel_rows, pixel_columns], "float64")[m_index].T
    v_other, v_mintpy = _slopes(medians, years), _slopes(mintpy, years)
    offset_other, offset_mintpy = float(np.median(v_other)), float(np.median(v_mintpy))
    raw = _stats(v_other, v_mintpy)
    rereferenced = _stats(v_other - offset_other, v_mintpy - offset_mintpy)

    # per date, remove each side's own median over the pixels, then each pixel's mean difference
    # (a constant between two series is a matter of reference, not of agreement), and compare
    difference = (mintpy - np.median(mintpy, axis=0)) - (medians - np.median(medians, axis=0))
    difference -= difference.mean(axis=1, keepdims=True)
    rmse = np.sqrt(np.mean(difference ** 2, axis=1))
    egms_noise = float(np.median(noise))
    scatter_other, scatter_mintpy = _thin(v_other - offset_other, v_mintpy - offset_mintpy)

    uncertainty = None
    if mintpy_velocity_std_mm is not None:
        from insar_core.analysis.calibration import uncertainty_scale
        stated = np.asarray(mintpy_velocity_std_mm, "float64")[pixel_rows, pixel_columns]
        # the other source's own error in a pixel: the standard error of the median of its points' velocities
        other_error = 1.2533 * noise / np.sqrt(count[enough])
        try:
            uncertainty = uncertainty_scale((v_mintpy - offset_mintpy) - (v_other - offset_other), stated, other_error)
        except ValueError:
            uncertainty = None

    return {
        "source": "points",
        "window": [common[0], common[-1]],
        "dates": len(common),
        "pixels": int(pixels.size),
        "points": int(rows.size),
        "points_per_pixel_median": float(np.median(count[enough])),
        "common_offset_mm_yr": {"other": offset_other, "mintpy": offset_mintpy},
        "velocity_raw": raw,
        "velocity_rereferenced": rereferenced,
        "timeseries_rmse_mm": {"median": float(np.median(rmse)), "p90": float(np.percentile(rmse, 90))},
        "noise_mm_yr_within_pixel": egms_noise,
        "by_velocity": _by_velocity(v_other - offset_other, v_mintpy - offset_mintpy),
        "scatter": {"other": scatter_other, "mintpy": scatter_mintpy},
        "verdict": _verdict(rereferenced, egms_noise),
        **({"uncertainty": uncertainty} if uncertainty else {}),
    }


def los_unit_vector(incidence_deg: "float | np.ndarray", azimuth_deg: "float | np.ndarray") -> np.ndarray:
    """East, north and up weights that turn ground motion into LOS motion, positive towards the satellite.
    Given arrays of angles it returns an array of shape (3, ...), one weight per component.

    `incidence_deg` is the incidence angle and `azimuth_deg` the azimuth of the LOS vector
    (ground to satellite) measured anticlockwise from north, the convention of MintPy's
    azimuthAngle layer."""
    inc, az = np.radians(incidence_deg), np.radians(azimuth_deg)
    return np.array([-np.sin(inc) * np.sin(az), np.sin(inc) * np.cos(az), np.cos(inc)])


def compare_with_stations(
    *, geo: dict, mintpy_dates: Sequence[str], mintpy_series_mm: np.ndarray, valid: np.ndarray,
    incidence_deg: np.ndarray, azimuth_deg: np.ndarray, stations: list[dict], min_dates: int = MIN_DATES,
    half_window_days: int = 3, frame: str = "global", plate: Optional[str] = None,
) -> dict:
    """MintPy against GNSS stations.

    Each station is a dict with `name`, `lat`, `lon` and `series`: {date: (east_mm, north_mm, up_mm)}
    relative to nothing in particular (any constant offset is removed). A station is compared at the
    MintPy pixel it falls in, over the dates both have, once its motion is projected onto the LOS of
    that pixel. The GNSS position on an acquisition date is the median of the daily positions
    within `half_window_days` of it (see `gnss_on_dates`); 0 uses that day alone.

    `frame` is "global" (the series as they are, which in a global frame include the motion of the
    plate, so the common offset reported is mostly the plate seen along the line of sight) or
    "plate": the motion the ITRF2014 plate motion model gives at each station is taken out first,
    and the common offset is then what it should be, the motion of MintPy's reference pixel.
    `plate` names the plate; left out, the one that best matches the stations is used."""
    if frame not in ("global", "plate"):
        raise ValueError("frame must be global or plate")
    plate_found = None
    if frame == "plate":
        from insar_core.analysis import plate_motion

        if plate is None:
            observed = []
            for station in stations:
                days = sorted(d for d in station["series"] if mintpy_dates[0] <= d <= mintpy_dates[-1])
                if len(days) >= min_dates:
                    slope = _slopes(np.array([station["series"][d][:2] for d in days], float).T, _years(days))
                    observed.append({"lat": station["lat"], "lon": station["lon"], "east": float(slope[0]), "north": float(slope[1])})
            plate_found = plate_motion.best_plate(observed)
        else:
            plate_found = {"plate": plate, "name": plate_motion.PLATE_NAMES.get(plate, plate), "misfit_mm_yr": None, "rigid": True}
    rows_out, offsets = [], []
    date_position = {d: i for i, d in enumerate(mintpy_dates)}
    for station in stations:
        positions = gnss_on_dates(station["series"], mintpy_dates, half_window_days)
        common = sorted(positions)
        if len(common) < min_dates:
            continue
        rows, columns = _pixel_of(np.array([station["lon"]]), np.array([station["lat"]]), geo)
        row, column = int(rows[0]), int(columns[0])
        length, width = mintpy_series_mm.shape[1:]
        if not (0 <= row < length and 0 <= column < width) or not valid[row, column]:
            continue
        weights = los_unit_vector(float(incidence_deg[row, column]), float(azimuth_deg[row, column]))
        m_index = [date_position[d] for d in common]
        insar = np.asarray(mintpy_series_mm[:, row, column], "float64")[m_index]
        gnss = np.array([np.dot(weights, positions[d]) for d in common], float)
        years = _years(common)
        if plate_found is not None:
            from insar_core.analysis import plate_motion

            east, north = plate_motion.plate_velocity(plate_found["plate"], station["lat"], station["lon"])
            gnss = gnss - (weights[0] * east + weights[1] * north) * years
        # A constant between the two is a matter of reference: remove the mean of each, so the
        # noise of the first date does not go into the error of all the others.
        insar, gnss = insar - insar.mean(), gnss - gnss.mean()
        v_gnss, v_insar = _slopes(gnss[None], years)[0], _slopes(insar[None], years)[0]
        rows_out.append({
            "name": station["name"], "lat": station["lat"], "lon": station["lon"], "dates": len(common),
            "window": [common[0], common[-1]], "gnss_mm_yr": float(v_gnss), "insar_mm_yr": float(v_insar),
            # the share of MintPy's dates the station has a position for: a station with a long gap is
            # compared over the part it covers only, and its velocities are those of that part
            "coverage": len(common) / len(mintpy_dates),
            "series_rmse_mm": float(np.sqrt(np.mean((insar - gnss) ** 2))),
            "horizontal_share": float(np.hypot(weights[0], weights[1])),
        })
        offsets.append(v_insar - v_gnss)
    if not rows_out:
        raise ValueError("No GNSS station falls on a valid MintPy pixel with enough dates in common.")

    gnss_v = np.array([r["gnss_mm_yr"] for r in rows_out])
    insar_v = np.array([r["insar_mm_yr"] for r in rows_out])
    offset = float(np.median(offsets))
    out = {
        "source": "gnss",
        "frame": frame, "plate": plate_found,
        "stations": rows_out,
        "n": len(rows_out),
        "velocity_raw": _stats(gnss_v, insar_v),
        "common_offset_mm_yr": offset,
    }
    if len(rows_out) >= 2:
        out["velocity_rereferenced"] = _stats(gnss_v + offset, insar_v)
        for row in rows_out:
            row["difference_rereferenced_mm_yr"] = float(row["insar_mm_yr"] - row["gnss_mm_yr"] - offset)
        spread = out["velocity_rereferenced"]["rmse_mm_yr"]
        status = "ok" if spread <= 3.0 else "warn" if spread <= 6.0 else "fail"
        out["verdict"] = {"status": status if len(rows_out) >= 3 else "warn", "message": (
            f"{len(rows_out)} stations, RMSE {spread:.1f} mm/year after removing the common offset"
            + ("." if len(rows_out) >= 3 else ". With fewer than three stations this is only indicative."))}
    else:
        out["verdict"] = {"status": "warn", "message": (
            "Only one station is in the area. The offset between GNSS and MintPy cannot be told apart from "
            "disagreement with one station, so only the raw difference is given.")}
    return out


def compare_with_velocity_grid(
    *, geo: dict, velocity_mm: np.ndarray, grid_geo: dict, grid_velocity_mm: np.ndarray,
    std_mm: Optional[np.ndarray] = None, min_pixels: int = 30,
) -> dict:
    """A velocity map against a gridded velocity of the same quantity (the EGMS L3 vertical and east-west products).

    Unlike `compare_with_points` this compares one mean velocity per pixel and no time series, so it says nothing
    about dates, and the other source's window is whatever it was produced for (EGMS L3 is a five year mean). The map
    is averaged onto the coarser grid of the other source, so each compared value covers the same ground. Both are
    then re-referenced to their own median over the compared pixels, as in the other comparisons, and the offset is
    reported.

    `geo` and `grid_geo` hold x_first, y_first, x_step, y_step, width, length and epsg. Both velocities are in mm/year
    with NaN where there is no value. `std_mm`, when given, is used to measure how realistic the stated error is."""
    from rasterio.crs import CRS
    from rasterio.transform import Affine
    from rasterio.warp import Resampling, reproject

    def transform(g: dict) -> Affine:
        return Affine(g["x_step"], 0.0, g["x_first"], 0.0, g["y_step"], g["y_first"])

    def onto_grid(array: np.ndarray) -> np.ndarray:
        out = np.full((grid_geo["length"], grid_geo["width"]), np.nan, "float64")
        reproject(np.asarray(array, "float64"), out, src_transform=transform(geo), src_crs=CRS.from_epsg(geo["epsg"]),
                  dst_transform=transform(grid_geo), dst_crs=CRS.from_epsg(grid_geo["epsg"]), src_nodata=np.nan,
                  dst_nodata=np.nan, resampling=Resampling.average, init_dest_nodata=True)
        return out

    mine = onto_grid(velocity_mm)
    other = np.asarray(grid_velocity_mm, "float64")
    keep = np.isfinite(mine) & np.isfinite(other)
    if int(keep.sum()) < min_pixels:
        raise ValueError(f"Only {int(keep.sum())} pixels have a value in both (at least {min_pixels} are needed): the two cover "
                         "different areas or most of the map is masked.")
    a, b = other[keep], mine[keep]
    offset_other, offset_mine = float(np.median(a)), float(np.median(b))
    raw = _stats(a, b)
    rereferenced = _stats(a - offset_other, b - offset_mine)
    scatter_other, scatter_mine = _thin(a - offset_other, b - offset_mine)
    result = {
        "source": "grid", "pixels": int(keep.sum()), "grid_step_m": float(abs(grid_geo["x_step"])),
        "common_offset_mm_yr": {"other": offset_other, "mintpy": offset_mine},
        "velocity_raw": raw, "velocity_rereferenced": rereferenced,
        "by_velocity": _by_velocity(a - offset_other, b - offset_mine),
        "scatter": {"other": scatter_other, "mintpy": scatter_mine},
        "verdict": _verdict(rereferenced, None),
    }
    if std_mm is not None:
        from insar_core.analysis.calibration import uncertainty_scale
        sigma = onto_grid(std_mm)[keep]
        try:
            result["uncertainty"] = uncertainty_scale((b - offset_mine) - (a - offset_other), sigma)
        except ValueError:
            pass
    return result
