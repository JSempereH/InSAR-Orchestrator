import numpy as np
import pytest

from insar_core.analysis import analyze_time_series

_START = np.datetime64("2020-01-03", "D")


def _series(fn, years=4.0, step=12, noise=1.5, seed=1):
    """Synthetic series: fn(t_years, day_of_year) plus Gaussian noise."""
    rng = np.random.default_rng(seed)
    days = np.arange(int(years * 365.25 / step)) * step
    stamps = _START + days.astype("timedelta64[D]")
    t = days / 365.25
    doy = (stamps - stamps.astype("datetime64[Y]").astype("datetime64[D]")).astype(int) + 1
    dates = [str(s).replace("-", "") for s in stamps]
    return dates, fn(t, doy) + noise * rng.standard_normal(len(days))


def test_pure_noise_is_stable():
    dates, values = _series(lambda t, d: 0 * t)
    a = analyze_time_series(dates, values)
    assert a["model"]["kind"] == "stable"
    assert a["model"]["label"] == "Stable"
    assert abs(a["velocity"]["ols_mm_yr"]) < 0.6


def test_linear_trend_recovers_velocity():
    dates, values = _series(lambda t, d: -3 * t)
    a = analyze_time_series(dates, values)
    assert a["model"]["kind"] == "linear"
    assert a["model"]["label"] == "Steady subsidence"
    assert a["velocity"]["ols_mm_yr"] == pytest.approx(-3, abs=0.5)
    assert a["velocity"]["theil_sen_mm_yr"] == pytest.approx(-3, abs=0.5)
    lo, hi = a["velocity"]["ci95"]
    assert lo < -3 < hi
    assert a["velocity"]["mann_kendall_p"] < 1e-6


def test_trend_reversal_is_located():
    dates, values = _series(lambda t, d: np.where(t < 2, -5 * t, -10 + 2 * (t - 2)))
    a = analyze_time_series(dates, values)
    m = a["model"]
    assert m["kind"] == "bilinear"
    assert m["label"] == "Trend reversal"
    assert abs((np.datetime64(m["break_date"]) - np.datetime64("2022-01-03")).astype(int)) < 90
    assert m["slope_before"] == pytest.approx(-5, abs=1.0)
    assert m["slope_after"] == pytest.approx(2, abs=1.0)
    assert a["break_index"] is not None


def test_step_is_an_abrupt_shift():
    dates, values = _series(lambda t, d: np.where(t >= 2.5, -12.0, 0.0))
    a = analyze_time_series(dates, values)
    m = a["model"]
    assert m["kind"] == "step"
    assert m["label"] == "Abrupt shift"
    assert m["step_mm"] == pytest.approx(-12, abs=2)
    assert abs((np.datetime64(m["break_date"]) - np.datetime64("2022-07-04")).astype(int)) < 45


def test_seasonal_cycle_amplitude_and_peak_month():
    # 4 mm amplitude peaking around the middle of July (fraction 0.54 of the year).
    dates, values = _series(lambda t, d: -2 * t + 4 * np.cos(2 * np.pi * (d / 365.25 - 0.54)))
    a = analyze_time_series(dates, values)
    assert a["seasonal"]["present"]
    assert a["seasonal"]["amplitude_mm"] == pytest.approx(4, abs=1)
    assert a["seasonal"]["peak_month"] in (5, 6, 7)  # June to August
    assert a["model"]["kind"] == "linear"


def test_accelerating_quadratic():
    dates, values = _series(lambda t, d: -1 * t - 1.2 * t * t)
    a = analyze_time_series(dates, values)
    assert a["model"]["kind"] == "quadratic"
    assert a["model"]["label"] == "Accelerating subsidence"
    assert a["model"]["acceleration_mm_yr2"] == pytest.approx(-2.4, abs=0.6)


def test_onset_of_motion_is_named_as_such():
    dates, values = _series(lambda t, d: np.where(t < 2, 0.0, -4 * (t - 2)))
    a = analyze_time_series(dates, values)
    assert a["model"]["kind"] == "bilinear"
    assert a["model"]["label"] == "Onset of subsidence"


def test_extremes_outliers_and_sampling():
    dates, values = _series(lambda t, d: 0 * t, noise=0.5)
    values = values.copy()
    values[40] += 15  # one clear outlier
    a = analyze_time_series(dates, values)
    assert a["max"]["value"] == pytest.approx(values.max())
    assert a["min"]["value"] == pytest.approx(values.min())
    assert a["series"]["dates"][40] in [o["date"] for o in a["noise"]["outliers"]]
    assert a["median_step_days"] == pytest.approx(12, abs=0.1)
    assert a["gaps"] == 0


def test_gap_is_reported():
    dates, values = _series(lambda t, d: 0 * t)
    keep = [i for i in range(len(dates)) if not 50 <= i < 60]  # about 4 months missing
    a = analyze_time_series([dates[i] for i in keep], values[keep])
    assert a["gaps"] == 1


def test_too_few_points_returns_none():
    dates, values = _series(lambda t, d: -t, years=0.2)
    assert len(dates) < 8
    assert analyze_time_series(dates, values) is None


def test_nan_values_are_dropped_and_dates_sorted():
    dates, values = _series(lambda t, d: -3 * t)
    values = values.copy()
    values[5] = np.nan
    order = np.random.default_rng(3).permutation(len(dates))
    a = analyze_time_series([dates[i] for i in order], values[order])
    assert a["n"] == len(dates) - 1
    assert a["series"]["dates"] == sorted(a["series"]["dates"])
    assert a["velocity"]["ols_mm_yr"] == pytest.approx(-3, abs=0.5)


def test_iso_dates_and_chart_series_are_aligned():
    dates, values = _series(lambda t, d: -3 * t)
    iso = [f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in dates]
    a = analyze_time_series(iso, values)
    s = a["series"]
    assert len(s["dates"]) == len(s["values"]) == len(s["fit"]) == len(s["trend"]) == a["n"]
    assert s["dates"][0] == iso[0]


def test_result_is_json_serialisable():
    import json
    dates, values = _series(lambda t, d: np.where(t < 2, -5 * t, -10 + 2 * (t - 2)))
    json.dumps(analyze_time_series(dates, values))


def test_short_record_notes_that_seasonality_cannot_be_separated():
    dates, values = _series(lambda t, d: -4 * t, years=0.8)
    a = analyze_time_series(dates, values)
    assert not a["seasonal"]["present"]
    assert any("shorter than 1.5 years" in line for line in a["narrative"])
