"""Calibration against GNSS and the check of the stated uncertainty, on cases with a known answer."""
import datetime as dt

import numpy as np
import pyproj
import pytest

from insar_core.analysis import calibration as cal
from insar_core.analysis.validation import los_unit_vector

SIZE = 40
GEO = {"x_first": 600000.0, "y_first": 4180000.0, "x_step": 80.0, "y_step": -80.0, "width": SIZE, "length": SIZE, "epsg": 32630}


def _latlon(row, column):
    x, y = GEO["x_first"] + (column + 0.5) * 80.0, GEO["y_first"] - (row + 0.5) * 80.0
    lon, lat = pyproj.Transformer.from_crs(32630, 4326, always_xy=True).transform(x, y)
    return lat, lon


def _daily(start, days, velocity, noise=0.0, seed=0):
    """A GNSS series: east, north, up in mm for `days` consecutive days, with the given velocities in mm/year."""
    rng = np.random.default_rng(seed)
    first = dt.date.fromisoformat(start)
    out = {}
    for i in range(days):
        t = i / 365.25
        out[(first + dt.timedelta(days=i)).strftime("%Y%m%d")] = tuple(float(v * t + rng.normal(0, noise)) for v in velocity)
    return out


def _station(name, row, column, east, north, up, days=365):
    series = _daily("2023-01-01", days, (east, north, up), noise=1.0, seed=sum(map(ord, name)))
    velocity = cal.gnss_velocity(series, "20230101", "20231231")
    lat, lon = _latlon(row, column)
    return {"name": name, "lat": lat, "lon": lon, "velocity": velocity}


def test_a_gnss_velocity_is_recovered_with_an_error():
    series = _daily("2023-01-01", 365, (20.0, 15.0, -6.0), noise=1.5)
    v = cal.gnss_velocity(series, "20230101", "20231231")
    assert v["east"] == pytest.approx(20.0, abs=1.0)
    assert v["north"] == pytest.approx(15.0, abs=1.0)
    assert v["up"] == pytest.approx(-6.0, abs=1.0)
    assert v["sigma_up"] >= cal.GNSS_SIGMA_FLOOR
    assert v["dates"] == 365


def test_a_station_with_too_little_record_has_no_velocity():
    assert cal.gnss_velocity(_daily("2023-01-01", 10, (1, 1, 1)), "20230101", "20231231") is None
    # enough dates but only the first quarter of the window
    assert cal.gnss_velocity(_daily("2023-01-01", 90, (1, 1, 1)), "20230101", "20231231") is None


def test_the_vertical_shift_is_the_gnss_minus_the_map():
    # the map reads the truth minus 4 mm/year everywhere (its reference pixel really rises by 4)
    truth = {"A": -10.0, "B": -2.0, "C": 3.0}
    velocity = np.zeros((SIZE, SIZE))
    stations = []
    for index, (name, up) in enumerate(truth.items()):
        row, column = 8 + index * 10, 10 + index * 8
        velocity[row - 1: row + 2, column - 1: column + 2] = up - 4.0
        stations.append(_station(name, row, column, 20.0, 15.0, up))
    result = cal.calibrate(velocity=velocity, std=np.full((SIZE, SIZE), 0.5), geo=GEO, stations=stations, quantity="up")
    assert result["offset_mm_yr"] == pytest.approx(4.0, abs=1.0)
    assert result["applied"] is True
    assert result["loo_rmse_after_mm_yr"] < result["rmse_before_mm_yr"]
    assert result["frame_removed_mm_yr"] is None
    assert result["verdict"]["status"] == "ok"


def test_stations_that_disagree_do_not_calibrate():
    velocity = np.zeros((SIZE, SIZE))
    stations = [_station("A", 10, 10, 0, 0, 0.0), _station("B", 20, 20, 0, 0, 12.0), _station("C", 30, 30, 0, 0, -12.0)]
    result = cal.calibrate(velocity=velocity, std=None, geo=GEO, stations=stations, quantity="up")
    assert result["applied"] is False
    assert result["verdict"]["status"] == "fail"
    assert "not applied" in result["verdict"]["message"]


def test_one_station_is_applied_but_marked_indicative():
    velocity = np.full((SIZE, SIZE), -3.0)
    result = cal.calibrate(velocity=velocity, std=None, geo=GEO, stations=[_station("A", 20, 20, 0, 0, 2.0)], quantity="up")
    assert result["applied"] is True and result["n"] == 1
    assert result["loo_rmse_after_mm_yr"] is None
    assert result["verdict"]["status"] == "warn"
    assert result["offset_mm_yr"] == pytest.approx(5.0, abs=1.0)


def test_plate_motion_is_removed_before_the_line_of_sight_is_projected():
    incidence, azimuth = np.full((SIZE, SIZE), 38.0), np.full((SIZE, SIZE), -100.0)
    # two stations on a plate moving 20 mm/yr east and 15 north; one also sinks 8 mm/yr
    stations = [_station("A", 10, 10, 20.0, 15.0, 0.0), _station("B", 25, 25, 20.0, 15.0, -8.0)]
    w = los_unit_vector(38.0, -100.0)
    velocity = np.zeros((SIZE, SIZE))
    velocity[9:12, 9:12] = 0.0
    velocity[24:27, 24:27] = w[2] * -8.0                                  # InSAR sees the sinking only, relative to the plate
    result = cal.calibrate(velocity=velocity, std=None, geo=GEO, stations=stations, quantity="los", incidence=incidence, azimuth=azimuth)
    assert result["frame_removed_mm_yr"]["east"] == pytest.approx(20.0, abs=1.0)
    # without the frame removed the difference would be near the plate's LOS share (about 15 mm/yr)
    assert abs(result["offset_mm_yr"]) < 3.0
    assert "relative to the average of the stations" in result["verdict"]["message"]


def test_line_of_sight_needs_the_geometry():
    with pytest.raises(ValueError, match="incidence and azimuth"):
        cal.calibrate(velocity=np.zeros((SIZE, SIZE)), std=None, geo=GEO, stations=[], quantity="los")


def test_no_usable_station_is_an_error():
    outside = {"name": "X", "lat": 10.0, "lon": 10.0, "velocity": cal.gnss_velocity(_daily("2023-01-01", 365, (1, 1, 1)), "20230101", "20231231")}
    with pytest.raises(ValueError, match="No GNSS station"):
        cal.calibrate(velocity=np.zeros((SIZE, SIZE)), std=None, geo=GEO, stations=[outside, {"name": "Y", "lat": 0, "lon": 0}], quantity="up")


def test_a_masked_pixel_is_not_used():
    velocity = np.full((SIZE, SIZE), np.nan)
    with pytest.raises(ValueError, match="No GNSS station"):
        cal.calibrate(velocity=velocity, std=None, geo=GEO, stations=[_station("A", 20, 20, 0, 0, 0.0)], quantity="up")


def test_stated_error_that_is_too_small_is_measured():
    rng = np.random.default_rng(1)
    sigma = np.full(5000, 1.0)
    difference = rng.normal(0.0, 3.0, 5000)          # the real spread is three times the stated one
    result = cal.uncertainty_scale(difference, sigma)
    assert result["scale"] == pytest.approx(3.0, rel=0.1)
    assert result["coverage_1sigma"] < 0.45
    assert result["coverage_1sigma_scaled"] == pytest.approx(0.68, abs=0.05)
    assert "too small" in result["message"]
    assert result["recommended_scale"] == result["scale"]


def test_the_other_sources_own_error_is_taken_out():
    rng = np.random.default_rng(2)
    sigma, other = np.full(5000, 1.0), np.full(5000, 2.0)
    difference = rng.normal(0.0, np.sqrt(1.0 + 4.0), 5000)          # exactly what both stated errors predict
    result = cal.uncertainty_scale(difference, sigma, other)
    assert result["scale"] == pytest.approx(1.0, rel=0.08)
    assert "about right" in result["message"]


def test_an_overstated_error_is_not_shrunk():
    rng = np.random.default_rng(3)
    result = cal.uncertainty_scale(rng.normal(0.0, 0.5, 5000), np.full(5000, 2.0))
    assert result["scale"] < 0.8
    assert result["recommended_scale"] == 1.0
    assert "not shrunk" in result["message"]


def test_too_few_pixels_cannot_measure_the_uncertainty():
    with pytest.raises(ValueError, match="Too few"):
        cal.uncertainty_scale(np.zeros(10), np.ones(10))


def test_a_few_bad_pixels_do_not_move_the_scale():
    rng = np.random.default_rng(4)
    difference = rng.normal(0.0, 1.0, 5000)
    difference[:100] += 80.0                                          # badly unwrapped pixels
    assert cal.uncertainty_scale(difference, np.ones(5000))["scale"] == pytest.approx(1.0, rel=0.1)


def test_a_horizontal_or_line_of_sight_calibration_needs_two_stations():
    velocity = np.zeros((SIZE, SIZE))
    one = [_station("A", 20, 20, 20.0, 15.0, 2.0)]
    with pytest.raises(ValueError, match="at least two stations"):
        cal.calibrate(velocity=velocity, std=None, geo=GEO, stations=one, quantity="east")
    with pytest.raises(ValueError, match="at least two stations"):
        cal.calibrate(velocity=velocity, std=None, geo=GEO, stations=one, quantity="los",
                      incidence=np.full((SIZE, SIZE), 38.0), azimuth=np.full((SIZE, SIZE), -100.0))
    assert cal.calibrate(velocity=velocity, std=None, geo=GEO, stations=one, quantity="up")["applied"] is True
