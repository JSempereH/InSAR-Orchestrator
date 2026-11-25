"""GNSS steps, the MIDAS estimator and the plate motion model, on cases with a known answer.

The two files in tests/data are lines copied from the tables the Nevada Geodetic Laboratory
publishes (steps.txt and midas.IGS14.txt), so the parsers are tested on the real layout."""
import datetime as dt
import math
from pathlib import Path

import numpy as np
import pytest

from insar_core.adapters import ngl
from insar_core.analysis import calibration as cal
from insar_core.analysis import plate_motion
from insar_core.analysis.validation import gnss_on_dates

DATA = Path(__file__).parent / "data"


def _series(start, days, velocity=(0.0, 0.0, 0.0), noise=0.0, seasonal=0.0, jumps=(), seed=0):
    """Daily east, north, up in mm: a line, an annual cycle of amplitude `seasonal` on all three,
    and `jumps` as (date, (east, north, up))."""
    rng = np.random.default_rng(seed)
    first = dt.date.fromisoformat(start)
    out = {}
    for i in range(days):
        day = first + dt.timedelta(days=i)
        key = day.strftime("%Y%m%d")
        t = i / 365.25
        value = np.array(velocity) * t + seasonal * math.sin(2 * math.pi * t) + rng.normal(0, noise, 3)
        for when, jump in jumps:
            if key >= when:
                value = value + np.array(jump)
        out[key] = tuple(float(v) for v in value)
    return out


# ── parsers, on real lines ───────────────────────────────────────────────────

def test_steps_are_read_from_the_real_table():
    steps = ngl.parse_steps((DATA / "ngl_steps_excerpt.txt").read_text())
    assert set(steps) == {"ALAC", "GOLD", "YEBE"}
    quake = steps["ALAC"][0]
    assert quake == {"date": "20030521", "kind": "earthquake", "detail": "magnitude 6.8, 393 km away"}
    assert steps["GOLD"][0]["date"] == "19940117"                       # a two-digit year in the 1900s
    assert any(s["kind"] == "equipment" and "Antenna" in s["detail"] for s in steps["YEBE"])
    assert all(a["date"] <= b["date"] for a, b in zip(steps["YEBE"], steps["YEBE"][1:], strict=False))
    assert set(ngl.parse_steps((DATA / "ngl_steps_excerpt.txt").read_text(), {"YEBE"})) == {"YEBE"}


def test_midas_table_is_read_in_mm_per_year():
    table = ngl.parse_midas((DATA / "ngl_midas_excerpt.txt").read_text())
    assert table["YEBE"]["east"] == pytest.approx(18.857)
    assert table["YEBE"]["north"] == pytest.approx(16.430)
    assert table["YEBE"]["up"] == pytest.approx(0.074)
    assert table["YEBE"]["sigma_up"] == pytest.approx(0.368)
    assert table["YEBE"]["steps"] == 4


def test_midas_falls_back_to_the_older_frame():
    def get(url):
        if "IGS20" in url:
            raise ngl.NGLError("Not found")
        return (DATA / "ngl_midas_excerpt.txt").read_text()

    frame, table = ngl.fetch_midas({"WTZR"}, get=get)
    assert frame == "IGS14" and set(table) == {"WTZR"}


# ── plate motion, against the real MIDAS velocities ──────────────────────────

@pytest.mark.parametrize("name, lat, lon, plate", [
    ("YEBE", 40.5249, -3.0886, "EURA"), ("MADR", 40.4292, -4.2497, "EURA"), ("WTZR", 49.1442, 12.8789, "EURA"),
    ("ALAC", 38.3389, -0.4812, "EURA"), ("MKEA", 19.8014, -155.4563, "PCFC"),
])
def test_plate_model_matches_real_station_velocities(name, lat, lon, plate):
    """Stations on the rigid part of a plate move with the model to within about 1 mm/year."""
    observed = ngl.parse_midas((DATA / "ngl_midas_excerpt.txt").read_text())[name]
    east, north = plate_motion.plate_velocity(plate, lat, lon)
    assert math.hypot(observed["east"] - east, observed["north"] - north) < 1.0
    found = plate_motion.best_plate([{"lat": lat, "lon": lon, "east": observed["east"], "north": observed["north"]}])
    assert found["plate"] == plate and found["rigid"]


def test_a_station_that_fits_no_plate_is_reported_as_not_rigid():
    east, north = plate_motion.plate_velocity("EURA", 37.0, -2.0)
    found = plate_motion.best_plate([{"lat": 37.0, "lon": -2.0, "east": east + 8.0, "north": north}])
    assert found["rigid"] is False and found["misfit_mm_yr"] > plate_motion.RIGID_PLATE_LIMIT_MM_YR


def test_unknown_plate_is_refused():
    with pytest.raises(ValueError, match="Unknown plate"):
        plate_motion.plate_velocity("ATLANTIS", 0.0, 0.0)


# ── steps ────────────────────────────────────────────────────────────────────

def test_an_antenna_change_is_taken_out_and_the_velocity_recovered():
    truth = (3.0, -2.0, -5.0)
    series = _series("2020-01-01", 1100, truth, noise=1.0, seasonal=2.0, jumps=[("20210601", (4.0, -3.0, 12.0))])
    steps = [{"date": "20210601", "kind": "equipment", "detail": "Antenna Type Changed"}]
    biased = cal.gnss_velocity(series, "20200101", "20230101", method="least_squares")
    fixed, report = cal.correct_equipment_steps(series, steps, "20200101", "20230101")
    clean = cal.gnss_velocity(fixed, "20200101", "20230101", method="least_squares")
    assert report[0]["action"] == "removed"
    assert report[0]["jump_mm"] == pytest.approx([4.0, -3.0, 12.0], abs=0.5)
    assert abs(biased["up"] - truth[2]) > 3.0                           # the jump biases a plain line
    assert clean["up"] == pytest.approx(truth[2], abs=0.5)
    assert clean["east"] == pytest.approx(truth[0], abs=0.5)


def test_an_earthquake_is_reported_and_left_in():
    series = _series("2020-01-01", 800, (0.0, 0.0, 0.0), jumps=[("20210101", (0.0, 0.0, 10.0))])
    steps = [{"date": "20210101", "kind": "earthquake", "detail": "magnitude 6.0, 20 km away"}]
    fixed, report = cal.correct_equipment_steps(series, steps, "20200101", "20220301")
    assert report == [{**steps[0], "action": "kept", "jump_mm": None}]
    assert fixed["20211231"][2] == pytest.approx(10.0)


def test_a_change_at_the_edge_of_the_window_trims_the_short_side():
    series = _series("2020-01-01", 400, (0.0, 0.0, 0.0), jumps=[("20200105", (0.0, 0.0, 30.0))])
    steps = [{"date": "20200105", "kind": "equipment", "detail": "Receiver Changed"}]
    fixed, report = cal.correct_equipment_steps(series, steps, "20200101", "20210101")
    assert report[0]["action"] == "trimmed" and min(fixed) == "20200105"


def test_a_change_outside_the_window_changes_nothing():
    series = _series("2020-01-01", 400, (1.0, 1.0, 1.0))
    steps = [{"date": "20190101", "kind": "equipment", "detail": "Antenna"}, {"date": "20230101", "kind": "equipment", "detail": "Antenna"}]
    fixed, report = cal.correct_equipment_steps(series, steps, "20200101", "20210101")
    assert report == [] and fixed == {d: v for d, v in series.items() if d <= "20210101"}


# ── MIDAS ────────────────────────────────────────────────────────────────────

def test_midas_recovers_the_velocity_under_a_strong_seasonal_cycle():
    truth = (0.0, 0.0, -4.0)
    series = _series("2020-01-01", 913, truth, noise=1.5, seasonal=6.0)
    midas = cal.gnss_velocity(series, "20200101", "20220701")
    assert midas["method"] == "midas" and midas["pairs"] > 500
    assert midas["up"] == pytest.approx(truth[2], abs=0.4)
    assert midas["sigma_up"] >= cal.GNSS_SIGMA_FLOOR


def test_midas_survives_a_jump_nobody_listed():
    truth = (2.0, 0.0, 0.0)
    series = _series("2019-01-01", 1500, truth, noise=1.0, jumps=[("20210101", (10.0, 0.0, 0.0))])
    line = cal.gnss_velocity(series, "20190101", "20230201", method="least_squares")
    midas = cal.gnss_velocity(series, "20190101", "20230201")
    assert abs(line["east"] - truth[0]) > 2.0
    assert midas["east"] == pytest.approx(truth[0], abs=0.5)


def test_a_short_record_uses_a_line_and_says_so():
    series = _series("2022-01-01", 400, (1.0, 2.0, 3.0), noise=0.5)
    found = cal.gnss_velocity(series, "20220101", "20230201")
    assert found["method"] == "least_squares" and found["pairs"] is None


def test_the_error_of_a_line_grows_when_the_positions_are_correlated():
    """White noise against the same noise plus a slow cycle: the second leaves correlated residuals,
    so its positions are worth fewer independent ones and the error must be several times larger."""
    cal_floor = cal.GNSS_SIGMA_FLOOR
    try:
        cal.GNSS_SIGMA_FLOOR = 0.0
        white = cal.gnss_velocity(_series("2022-01-01", 366, noise=1.0), "20220101", "20230101")
        slow = cal.gnss_velocity(_series("2022-01-01", 366, noise=1.0, seasonal=5.0), "20220101", "20230101")
    finally:
        cal.GNSS_SIGMA_FLOOR = cal_floor
    assert slow["sigma_up"] > 3.0 * white["sigma_up"]


# ── the plate frame in the calibration ───────────────────────────────────────

def _one_station_case(extra_east=0.0):
    size = 20
    geo = {"x_first": 600000.0, "y_first": 4180000.0, "x_step": 80.0, "y_step": -80.0, "width": size, "length": size, "epsg": 32630}
    import pyproj
    lon, lat = pyproj.Transformer.from_crs(32630, 4326, always_xy=True).transform(600000.0 + 10.5 * 80, 4180000.0 - 10.5 * 80)
    east, north = plate_motion.plate_velocity("EURA", lat, lon)
    velocity = {"east": east + extra_east, "north": north, "up": -6.0, "sigma_east": 1.0, "sigma_north": 1.0, "sigma_up": 1.0,
                "dates": 900, "method": "midas"}
    station = {"name": "TEST", "lat": lat, "lon": lon, "velocity": velocity}
    return geo, station, size


def test_one_station_calibrates_the_line_of_sight_in_the_plate_frame():
    geo, station, size = _one_station_case()
    insar = np.full((size, size), -1.0)                                 # the map reads -1 where GNSS, without the plate, reads -6 up
    incidence, azimuth = np.full((size, size), 39.0), np.full((size, size), -100.0)
    with pytest.raises(ValueError, match="two stations"):
        cal.calibrate(velocity=insar, std=None, geo=geo, stations=[station], quantity="los", incidence=incidence, azimuth=azimuth)
    out = cal.calibrate(velocity=insar, std=None, geo=geo, stations=[station], quantity="los", incidence=incidence,
                        azimuth=azimuth, frame="plate")
    expected = -6.0 * math.cos(math.radians(39.0))                      # only the vertical motion is left of the station
    assert out["plate"]["plate"] == "EURA" and out["applied"]
    assert out["offset_mm_yr"] == pytest.approx(expected - (-1.0), abs=1e-6)
    assert "Eurasia plate" in out["verdict"]["message"]


def test_motion_relative_to_the_plate_stays_in_the_result_and_is_flagged():
    geo, station, size = _one_station_case(extra_east=7.0)
    out = cal.calibrate(velocity=np.zeros((size, size)), std=None, geo=geo, stations=[station], quantity="east", frame="plate")
    assert out["stations"][0]["gnss_mm_yr"] == pytest.approx(7.0, abs=1e-6)
    assert out["plate"]["rigid"] is False and "rigid plate allows" in out["verdict"]["message"]


# ── GNSS on the acquisition date ─────────────────────────────────────────────

def test_the_position_on_a_date_is_the_median_of_the_week_around_it():
    series = {f"202401{d:02d}": (0.0, 0.0, 0.0) for d in range(1, 15)}
    series["20240108"] = (0.0, 0.0, 40.0)                               # one bad day, on the acquisition date itself
    del series["20240112"]
    found = gnss_on_dates(series, ["20240108", "20240112", "20240201"])
    assert found["20240108"] == (0.0, 0.0, 0.0)
    assert "20240112" in found                                          # the day is missing but its neighbours are there
    assert "20240201" not in found                                      # nothing within three days
    assert gnss_on_dates(series, ["20240108"], half_window_days=0)["20240108"][2] == 40.0


def test_the_common_offset_against_stations_is_free_of_the_plate():
    """A station that only moves with its plate, under a map that reads zero: in the plate frame
    the two agree, in the global frame the plate shows up as an offset of centimetres per year."""
    from insar_core.analysis.validation import compare_with_stations

    dates = [(dt.date(2021, 1, 1) + dt.timedelta(days=12 * k)).strftime("%Y%m%d") for k in range(60)]
    lat, lon = 40.0, -3.0
    east, north = plate_motion.plate_velocity("EURA", lat, lon)
    series = {}
    for i in range(800):
        day = dt.date(2020, 12, 20) + dt.timedelta(days=i)
        series[day.strftime("%Y%m%d")] = (east * i / 365.25, north * i / 365.25, 0.0)
    geo = {"x_first": -3.5, "y_first": 40.5, "x_step": 0.1, "y_step": -0.1, "width": 10, "length": 10, "epsg": 4326}
    common = dict(geo=geo, mintpy_dates=dates, mintpy_series_mm=np.zeros((len(dates), 10, 10), "float32"), valid=np.ones((10, 10), bool),
                  incidence_deg=np.full((10, 10), 39.0), azimuth_deg=np.full((10, 10), -100.0),
                  stations=[{"name": "TEST", "lat": lat, "lon": lon, "series": series}])
    in_plate = compare_with_stations(**common, frame="plate")
    in_global = compare_with_stations(**common)
    assert in_plate["plate"]["plate"] == "EURA"
    assert in_plate["common_offset_mm_yr"] == pytest.approx(0.0, abs=0.05)
    assert abs(in_global["common_offset_mm_yr"]) > 5.0


def test_a_station_with_a_gap_says_how_much_of_the_period_it_covers():
    from insar_core.analysis.validation import compare_with_stations

    dates = [(dt.date(2023, 1, 3) + dt.timedelta(days=12 * k)).strftime("%Y%m%d") for k in range(30)]
    series = {(dt.date(2023, 8, 1) + dt.timedelta(days=i)).strftime("%Y%m%d"): (0.0, 0.0, 0.0) for i in range(150)}
    geo = {"x_first": -3.5, "y_first": 40.5, "x_step": 0.1, "y_step": -0.1, "width": 10, "length": 10, "epsg": 4326}
    out = compare_with_stations(geo=geo, mintpy_dates=dates, mintpy_series_mm=np.zeros((30, 10, 10), "float32"), valid=np.ones((10, 10), bool),
                                incidence_deg=np.full((10, 10), 39.0), azimuth_deg=np.full((10, 10), -100.0),
                                stations=[{"name": "GAPS", "lat": 40.0, "lon": -3.0, "series": series}])
    row = out["stations"][0]
    assert row["dates"] == 12 and row["coverage"] == pytest.approx(12 / 30) and row["window"][0] == "20230807"
