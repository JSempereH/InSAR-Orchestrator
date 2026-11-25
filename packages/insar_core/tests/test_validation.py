"""Comparing a MintPy result with EGMS-like points and with GNSS stations, on synthetic data."""
import datetime as dt

import numpy as np
import pyproj
import pytest

from insar_core.analysis import validation as v

LENGTH, WIDTH = 30, 40
GEO = {"x_first": 600000.0, "y_first": 4170000.0, "x_step": 80.0, "y_step": -80.0, "epsg": 32630}
DATES = [(dt.date(2023, 1, 3) + dt.timedelta(days=12 * k)).strftime("%Y%m%d") for k in range(12)]
YEARS = np.array([(dt.datetime.strptime(d, "%Y%m%d") - dt.datetime.strptime(DATES[0], "%Y%m%d")).days for d in DATES]) / 365.25


def _truth():
    """A bowl of subsidence in mm/year, centred in the grid."""
    rows, cols = np.mgrid[0:LENGTH, 0:WIDTH]
    return -40.0 * np.exp(-(((rows - 15) / 6.0) ** 2 + ((cols - 20) / 8.0) ** 2))


def _cube(velocity, offset=0.0, scale=1.0, noise=0.0, seed=0):
    rng = np.random.default_rng(seed)
    out = YEARS[:, None, None] * (velocity[None] * scale + offset)
    return out + rng.normal(0, noise, out.shape)


def _points(velocity, per_pixel=6, noise=2.0, seed=1, dates=DATES, area=None):
    rng = np.random.default_rng(seed)
    to_wgs = pyproj.Transformer.from_crs(32630, 4326, always_xy=True)
    lat, lon, series = [], [], []
    for r in range(LENGTH):
        for c in range(WIDTH):
            for _ in range(per_pixel):
                x = GEO["x_first"] + (c + rng.random()) * 80.0
                y = GEO["y_first"] - (r + rng.random()) * 80.0
                lo, la = to_wgs.transform(x, y)
                lat.append(la)
                lon.append(lo)
                series.append(YEARS[:len(dates)] * velocity[r, c] + rng.normal(0, noise, len(dates)))
    return np.array(lat), np.array(lon), np.array(series)


def _compare(mintpy_cube, lat, lon, series, dates=DATES, valid=None, **kw):
    return v.compare_with_points(
        geo=GEO, mintpy_dates=DATES, mintpy_series_mm=mintpy_cube,
        valid=np.ones((LENGTH, WIDTH), bool) if valid is None else valid,
        point_lat=lat, point_lon=lon, point_dates=dates, point_series_mm=series, **kw)


def test_a_result_that_recovers_the_motion_agrees_and_the_common_offset_is_separated():
    truth = _truth()
    lat, lon, series = _points(truth, noise=0.8)
    result = _compare(_cube(truth, offset=7.0, noise=0.5), lat, lon, series)
    raw, ref = result["velocity_raw"], result["velocity_rereferenced"]
    assert result["pixels"] == LENGTH * WIDTH and result["dates"] == len(DATES)
    assert raw["bias_mm_yr"] == pytest.approx(7.0, abs=0.5)           # MintPy's own zero is elsewhere
    assert abs(ref["bias_mm_yr"]) < 1.0 and ref["pearson_r"] > 0.98 and ref["slope_b_on_a"] == pytest.approx(1.0, abs=0.05)
    assert ref["rmse_mm_yr"] < 2.0
    assert result["common_offset_mm_yr"]["mintpy"] - result["common_offset_mm_yr"]["other"] == pytest.approx(7.0, abs=0.5)
    assert result["verdict"]["status"] == "ok"
    assert result["noise_mm_yr_within_pixel"] > 0 and len(result["scatter"]["other"]) == len(result["scatter"]["mintpy"])


def test_the_error_is_reported_by_how_fast_the_ground_moves():
    truth = _truth()
    lat, lon, series = _points(truth, noise=0.5)
    mintpy = truth.copy()
    mintpy[truth < -20] *= 0.6                        # the fastest motion is underestimated, as unwrapping errors would do
    bands = _compare(_cube(mintpy, noise=0.3), lat, lon, series)["by_velocity"]
    assert sum(b["n"] for b in bands) > 0 and bands == sorted(bands, key=lambda b: b["other_mm_yr"])
    fast = [b for b in bands if b["to"] is not None and b["to"] <= -20.0]
    quiet = next(b for b in bands if b["from"] == -3.0)
    assert fast and all(b["bias_mm_yr"] > 3.0 for b in fast)      # MintPy is less negative than the other source
    assert abs(quiet["bias_mm_yr"]) < 1.5


def test_a_result_with_half_the_amplitude_is_not_called_good():
    truth = _truth()
    lat, lon, series = _points(truth)
    result = _compare(_cube(truth, scale=0.5), lat, lon, series)
    assert result["velocity_rereferenced"]["slope_b_on_a"] == pytest.approx(0.5, abs=0.05)
    assert result["verdict"]["status"] == "warn" and "amplitude" in result["verdict"]["message"]


def test_a_result_unrelated_to_the_motion_fails():
    truth = _truth()
    lat, lon, series = _points(truth)
    scrambled = np.random.default_rng(3).permutation(truth.ravel()).reshape(truth.shape)
    assert _compare(_cube(scrambled), lat, lon, series)["verdict"]["status"] == "fail"


def test_a_quiet_area_is_judged_by_the_size_of_the_error_not_by_correlation():
    quiet = np.zeros((LENGTH, WIDTH))
    lat, lon, series = _points(quiet, noise=1.0)
    result = _compare(_cube(quiet, noise=0.5), lat, lon, series)
    assert result["verdict"]["status"] == "ok" and "barely deforms" in result["verdict"]["message"]


def test_only_dates_both_sources_have_are_compared_and_both_start_at_zero():
    truth = _truth()
    lat, lon, series = _points(truth, dates=DATES[2:])
    shifted = series + 99.0                               # an offset on the first date must not matter
    result = _compare(_cube(truth), lat, lon, shifted, dates=DATES[2:])
    assert result["dates"] == len(DATES) - 2 and result["window"] == [DATES[2], DATES[-1]]
    assert result["velocity_rereferenced"]["rmse_mm_yr"] < 5.0   # a 10 date, 4 month window is noisy


def test_sources_with_almost_no_dates_in_common_are_refused_with_the_reason():
    truth = _truth()
    lat, lon, series = _points(truth, dates=["20200102", "20200108"])
    with pytest.raises(ValueError, match="same track"):
        _compare(_cube(truth), lat, lon, series, dates=["20200102", "20200108"])


def test_pixels_with_too_few_points_are_left_out():
    truth = _truth()
    lat, lon, series = _points(truth, per_pixel=2)
    with pytest.raises(ValueError, match="3 or more"):
        _compare(_cube(truth), lat, lon, series)
    assert _compare(_cube(truth), lat, lon, series, min_points=2)["pixels"] == LENGTH * WIDTH


def test_pixels_mintpy_does_not_trust_are_not_compared():
    truth = _truth()
    lat, lon, series = _points(truth)
    valid = np.ones((LENGTH, WIDTH), bool)
    valid[:, :20] = False
    assert _compare(_cube(truth), lat, lon, series, valid=valid)["pixels"] == LENGTH * WIDTH // 2


def test_points_outside_the_processed_area_are_refused():
    truth = _truth()
    lat, lon, series = _points(truth)
    with pytest.raises(ValueError, match="different areas"):
        _compare(_cube(truth), lat + 5.0, lon, series)


def test_points_with_missing_dates_are_dropped_not_averaged_in():
    truth = _truth()
    lat, lon, series = _points(truth)
    series[::7, 3] = np.nan
    result = _compare(_cube(truth, noise=0.3), lat, lon, series)
    assert np.isfinite(result["velocity_rereferenced"]["rmse_mm_yr"])


# ── GNSS ─────────────────────────────────────────────────────────────────────

def test_the_los_vector_matches_the_known_geometry():
    # looking straight down: only the vertical counts
    assert v.los_unit_vector(0, 0) == pytest.approx([0, 0, 1])
    # descending Sentinel-1 (satellite to the east of the target looking west, LOS azimuth about 280 deg
    # from north anticlockwise, incidence 39): uplift and eastward motion both shorten the range
    east, north, up = v.los_unit_vector(39.0, 80.0)
    assert up == pytest.approx(np.cos(np.radians(39)))
    assert east < 0 and abs(east) > abs(north)
    # a satellite to the east (azimuth 90 deg anticlockwise from north points west->east? ground to satellite
    # towards the east): motion towards the east shortens the range, so east is positive
    assert v.los_unit_vector(30.0, 270.0)[0] > 0


def _station(name, row, col, velocity_enu, geo=GEO):
    to_wgs = pyproj.Transformer.from_crs(32630, 4326, always_xy=True)
    lon, lat = to_wgs.transform(geo["x_first"] + (col + 0.5) * 80.0, geo["y_first"] - (row + 0.5) * 80.0)
    series = {d: tuple(np.array(velocity_enu) * y) for d, y in zip(DATES, YEARS, strict=False)}
    return {"name": name, "lat": lat, "lon": lon, "series": series}


def test_gnss_stations_are_projected_onto_the_los_and_compared():
    truth = _truth()
    incidence, azimuth = np.full((LENGTH, WIDTH), 39.0), np.full((LENGTH, WIDTH), 281.0)
    weights = v.los_unit_vector(39.0, 281.0)
    stations = []
    for name, (row, col) in {"AAAA": (15, 20), "BBBB": (5, 5), "CCCC": (25, 35)}.items():
        up = truth[row, col] / weights[2]                       # purely vertical motion that gives the LOS velocity
        stations.append(_station(name, row, col, (0.0, 0.0, up)))
    result = v.compare_with_stations(
        geo=GEO, mintpy_dates=DATES, mintpy_series_mm=_cube(truth, offset=4.0), valid=np.ones((LENGTH, WIDTH), bool),
        incidence_deg=incidence, azimuth_deg=azimuth, stations=stations)
    assert result["n"] == 3 and result["common_offset_mm_yr"] == pytest.approx(4.0, abs=0.01)
    assert result["velocity_raw"]["bias_mm_yr"] == pytest.approx(4.0, abs=0.01)
    assert result["velocity_rereferenced"]["rmse_mm_yr"] < 0.01
    assert result["verdict"]["status"] == "ok"
    assert {s["name"] for s in result["stations"]} == {"AAAA", "BBBB", "CCCC"}


def test_horizontal_gnss_motion_reaches_the_los_through_the_geometry():
    truth = np.zeros((LENGTH, WIDTH))
    incidence, azimuth = np.full((LENGTH, WIDTH), 39.0), np.full((LENGTH, WIDTH), 281.0)
    east = v.los_unit_vector(39.0, 281.0)[0]
    stations = [_station("EAST", 10, 10, (10.0, 0.0, 0.0)), _station("QUIET", 20, 30, (0.0, 0.0, 0.0))]
    cube = _cube(truth)
    cube[:, 10, 10] = YEARS * 10.0 * east                        # MintPy sees exactly that LOS motion
    result = v.compare_with_stations(
        geo=GEO, mintpy_dates=DATES, mintpy_series_mm=cube, valid=np.ones((LENGTH, WIDTH), bool),
        incidence_deg=incidence, azimuth_deg=azimuth, stations=stations)
    row = next(s for s in result["stations"] if s["name"] == "EAST")
    assert row["gnss_mm_yr"] == pytest.approx(10.0 * east, abs=0.01) and abs(row["insar_mm_yr"] - row["gnss_mm_yr"]) < 0.01


def test_one_station_cannot_separate_the_offset_and_says_so():
    truth = _truth()
    result = v.compare_with_stations(
        geo=GEO, mintpy_dates=DATES, mintpy_series_mm=_cube(truth), valid=np.ones((LENGTH, WIDTH), bool),
        incidence_deg=np.full((LENGTH, WIDTH), 39.0), azimuth_deg=np.full((LENGTH, WIDTH), 281.0),
        stations=[_station("ONLY", 15, 20, (0.0, 0.0, 1.0))])
    assert "velocity_rereferenced" not in result and result["verdict"]["status"] == "warn"


def test_stations_outside_the_area_or_with_few_dates_are_skipped_and_none_left_is_an_error():
    truth = _truth()
    kwargs = dict(geo=GEO, mintpy_dates=DATES, mintpy_series_mm=_cube(truth), valid=np.ones((LENGTH, WIDTH), bool),
                  incidence_deg=np.full((LENGTH, WIDTH), 39.0), azimuth_deg=np.full((LENGTH, WIDTH), 281.0))
    outside = _station("OUT", 15, 20, (0, 0, 1))
    outside["lat"] += 3.0
    sparse = _station("SPARSE", 15, 20, (0, 0, 1))
    sparse["series"] = {d: sparse["series"][d] for d in DATES[:2]}
    with pytest.raises(ValueError, match="No GNSS station"):
        v.compare_with_stations(stations=[outside, sparse], **kwargs)


def test_the_stated_error_is_checked_against_the_points_when_given():
    truth = _truth()
    lat, lon, series = _points(truth, noise=0.8)
    cube = _cube(truth, noise=3.0)
    honest = np.full((LENGTH, WIDTH), 4.0)         # about the spread the noise produces over a year of 12 days
    too_small = np.full((LENGTH, WIDTH), 0.5)
    low = _compare(cube, lat, lon, series, mintpy_velocity_std_mm=too_small)["uncertainty"]
    fair = _compare(cube, lat, lon, series, mintpy_velocity_std_mm=honest)["uncertainty"]
    assert low["scale"] > 3 * fair["scale"] > 0
    assert "too small" in low["message"]
    assert "uncertainty" not in _compare(cube, lat, lon, series)


def test_series_error_does_not_depend_on_the_noise_of_the_first_date():
    """A bad first date in GNSS must count as one bad date, not shift every other date."""
    import numpy as np

    from insar_core.analysis.validation import compare_with_stations

    dates = [f"202401{d:02d}" for d in range(1, 21)]
    cube = np.zeros((len(dates), 3, 3), "float32")
    geo = {"x_first": 0.0, "y_first": 3.0, "x_step": 1.0, "y_step": -1.0, "width": 3, "length": 3, "epsg": 4326}
    series = {d: (0.0, 0.0, 0.0) for d in dates}
    series[dates[0]] = (0.0, 0.0, 20.0)          # one outlier of 20 mm, on the first date
    result = compare_with_stations(
        geo=geo, mintpy_dates=dates, mintpy_series_mm=cube, valid=np.ones((3, 3), bool),
        incidence_deg=np.zeros((3, 3)), azimuth_deg=np.zeros((3, 3)),
        stations=[{"name": "TEST", "lat": 1.5, "lon": 1.5, "series": series}],
    )
    # aligned on the first date the error would be about 19.5 mm; one outlier in twenty dates is about 4.4
    assert result["stations"][0]["series_rmse_mm"] < 5.0


def test_the_orthogonal_slope_is_not_pulled_down_by_noise_in_the_other_source():
    rng = np.random.default_rng(3)
    truth = rng.normal(0, 10, 5000)
    other = truth + rng.normal(0, 6, truth.size)                        # the reference is noisy too
    mintpy = truth + rng.normal(0, 6, truth.size)
    stats = v._stats(other, mintpy)
    assert stats["slope_b_on_a"] < 0.8                                  # ordinary least squares: diluted
    assert stats["slope_orthogonal"] == pytest.approx(1.0, abs=0.05)


def test_grouped_medians_equal_the_loop_they_replace():
    rng = np.random.default_rng(5)
    count = rng.integers(3, 9, 400)
    start = np.concatenate([[0], np.cumsum(count)[:-1]])
    series = rng.normal(0, 5, (count.sum(), 12))
    velocity = rng.normal(0, 2, count.sum())
    medians, noise = v._grouped(series, velocity, start, count)
    assert np.allclose(medians, [np.median(series[s:s + c], axis=0) for s, c in zip(start, count, strict=False)])
    assert np.allclose(noise, [np.std(velocity[s:s + c]) for s, c in zip(start, count, strict=False)])
