"""The uncertainty of a velocity against distance, on stacks whose noise is known."""
import datetime as dt
import json

import h5py
import numpy as np
import pytest

from insar_core.analysis import spatial_uncertainty as su
from insar_core.pipeline import mintpy_export, mintpy_qc, report

SHAPE, PIXEL = (90, 120), 80.0


def _fields(rng, n, corr_m=2500.0, sigma=6.0, white=1.5):
    """`n` fields of noise that is alike over about `corr_m` metres, plus noise of each pixel alone. In mm."""
    ky = np.fft.fftfreq(SHAPE[0], PIXEL)[:, None]
    kx = np.fft.fftfreq(SHAPE[1], PIXEL)[None, :]
    spectrum = (1 + (2 * np.pi * np.hypot(ky, kx) * corr_m) ** 2) ** -1.5
    out = np.fft.ifft2(np.fft.fft2(rng.normal(size=(n, *SHAPE))) * np.sqrt(spectrum)).real
    return out * (sigma / out.std()) + rng.normal(0, white, out.shape)


def _years(n=40, span=3.0):
    years = np.sort(np.random.default_rng(1).uniform(0, span, n))
    years[0] = 0.0
    return years


def _bowl(depth=-20.0):
    yy, xx = np.mgrid[:SHAPE[0], :SHAPE[1]]
    return depth * np.exp(-(((yy - 45) * PIXEL) ** 2 + ((xx - 60) * PIXEL) ** 2) / (2 * 1200.0 ** 2))


def _sample(n=2500, seed=2):
    rng = np.random.default_rng(seed)
    return rng.integers(0, SHAPE[0], n), rng.integers(0, SHAPE[1], n)


def _truth(errors, rows, cols, distances):
    """The spread of the difference of velocity errors (trials x pixels) in the classes around `distances`."""
    rng = np.random.default_rng(9)
    i, j = rng.integers(0, len(rows), 150_000), rng.integers(0, len(rows), 150_000)
    separation = np.hypot((cols[i] - cols[j]) * PIXEL, (rows[i] - rows[j]) * PIXEL)
    d = np.asarray(distances)
    which = np.digitize(separation, np.sqrt(d[:-1] * d[1:]))
    return np.array([np.sqrt(np.mean((errors[:, i[which == b]] - errors[:, j[which == b]]) ** 2)) for b in range(len(d))])


def _trials(autocorrelation=0.0, trials=12, motion=None):
    years = _years()
    rows, cols = _sample()
    centred = years - years.mean()
    motion = _bowl() if motion is None else motion
    errors, results = [], []
    for trial in range(trials):
        noise = _fields(np.random.default_rng(100 + trial), len(years))
        for k in range(1, len(years)):
            noise[k] = autocorrelation * noise[k - 1] + np.sqrt(1 - autocorrelation ** 2) * noise[k]
        series = (noise + years[:, None, None] * motion)[:, rows, cols]
        results.append(su.structure(series, years, cols * PIXEL, rows * PIXEL, pairs=120_000, seed=trial))
        errors.append((centred / (centred ** 2).sum()) @ series - motion[rows, cols])
    return np.array(errors), results, rows, cols


def test_the_predicted_spread_is_the_one_the_velocity_errors_have_even_over_a_deforming_bowl():
    errors, results, rows, cols = _trials()
    truth = _truth(errors, rows, cols, results[0]["distance_m"])
    predicted = np.mean([r["sigma_mm_yr"] for r in results], axis=0)
    assert truth[-1] > 1.5 * truth[0]                                    # the error does grow with distance
    # the last class is left out: on a map this small its pairs all join the same two ends
    assert np.all(np.abs(predicted[1:-1] / truth[1:-1] - 1) < 0.10)
    # what the velocity map shows is far larger here, because the bowl is real motion
    observed = np.mean([r["observed_mm_yr"] for r in results], axis=0)
    assert observed.max() > 3 * predicted.max()


def test_over_ground_that_does_not_move_the_velocities_show_what_was_predicted():
    _, results, _, _ = _trials(trials=4, motion=np.zeros(SHAPE))
    for result in results:
        ratio = np.array(result["observed_mm_yr"]) / np.array(result["sigma_mm_yr"])
        # up to a third of the map; farther apart one map has too few independent pairs to say
        assert np.all(np.abs(ratio[:9] - 1) < 0.15)


def test_noise_that_lasts_from_date_to_date_widens_the_error_and_is_mostly_accounted_for():
    errors, results, rows, cols = _trials(autocorrelation=0.5)
    truth = _truth(errors, rows, cols, results[0]["distance_m"])
    corrected = np.mean([r["sigma_mm_yr"] for r in results], axis=0)
    independent = np.mean([r["sigma_independent_mm_yr"] for r in results], axis=0)
    assert np.all(independent[1:] / truth[1:] < 0.70)                    # taking the dates as independent is far too small
    assert np.all((corrected[1:] / truth[1:] > 0.78) & (corrected[1:] / truth[1:] < 1.1))
    assert np.median(results[0]["autocorrelation"]) > 0.25


def test_the_curve_is_read_between_its_points_and_held_beyond_them():
    result = {"distance_m": [100.0, 1000.0, 10000.0], "sigma_mm_yr": [0.2, 1.0, 0.9], "observed_mm_yr": [0.5, 1.0, 2.0]}
    assert su.sigma_at(result, 50.0) == pytest.approx(0.2)               # nearer than measured: the noise of the two pixels
    assert su.sigma_at(result, 10 ** 2.5) == pytest.approx(0.6)          # half way in the logarithm of the distance
    assert su.sigma_at(result, 10000.0) == pytest.approx(1.0)            # the curve is not let come down with distance
    assert su.sigma_at(result, 1e6) == pytest.approx(1.0)
    assert su.sigma_at(result, 10000.0, "observed_mm_yr") == pytest.approx(2.0)
    geo = {"x_step": 80.0, "y_step": -80.0, "length": 4, "width": 200, "epsg": 32630, "y_first": 0.0}
    relative = su.sigma_map(result, geo, (0, 0))
    assert relative.shape == (4, 200) and relative[0, 0] == pytest.approx(0.2)
    assert relative[0, 199] == pytest.approx(1.0) and relative[0, 199] > relative[0, 5] > relative[0, 2]


def test_too_little_to_measure_is_said_plainly():
    rows, cols = _sample(300)
    with pytest.raises(su.UncertaintyError, match="dates"):
        su.structure(np.zeros((4, 300)), np.arange(4.0), cols * PIXEL, rows * PIXEL)
    with pytest.raises(su.UncertaintyError, match="pixels"):
        su.structure(np.zeros((20, 50)), np.arange(20.0), cols[:50] * PIXEL, rows[:50] * PIXEL)


@pytest.fixture
def run(tmp_path):
    """A finished run on disk: 30 dates of correlated noise over a bowl, in MintPy's layout."""
    rng = np.random.default_rng(4)
    years = _years(30, 2.0)
    start = dt.date(2023, 1, 3)
    dates = [(start + dt.timedelta(days=int(round(y * 365.25)))).strftime("%Y%m%d") for y in years]
    years = np.array([(dt.datetime.strptime(d, "%Y%m%d").date() - start).days for d in dates]) / 365.25
    cube = (_fields(rng, len(dates)) + years[:, None, None] * _bowl()) / 1000.0
    cube -= cube[:, 5:6, 7:8]                                            # everything is relative to the reference pixel
    centred = years - years.mean()
    velocity = np.tensordot(centred / (centred ** 2).sum(), cube, axes=(0, 0))
    geo = {"X_FIRST": 600000.0, "Y_FIRST": 4170000.0, "X_STEP": PIXEL, "Y_STEP": -PIXEL, "WIDTH": SHAPE[1], "LENGTH": SHAPE[0],
           "EPSG": 32630, "REF_Y": 5, "REF_X": 7, "REF_DATE": dates[0], "START_DATE": dates[0], "END_DATE": dates[-1]}
    mask = np.ones(SHAPE, dtype=bool)
    mask[:, :10] = False
    with h5py.File(tmp_path / "timeseries.h5", "w") as handle:
        handle.create_dataset("timeseries", data=cube.astype("float32"))
        handle.create_dataset("date", data=np.array([d.encode() for d in dates]))
        handle.attrs.update(geo)
    with h5py.File(tmp_path / "velocity.h5", "w") as handle:
        handle.create_dataset("velocity", data=velocity.astype("float32"))
        handle.create_dataset("velocityStd", data=np.full(SHAPE, 0.0004, "float32"))
        handle.attrs.update(geo)
    with h5py.File(tmp_path / "maskTempCoh.h5", "w") as handle:
        handle.create_dataset("mask", data=mask)
    return tmp_path


def test_a_run_is_measured_from_its_files_and_the_answer_is_kept_until_the_results_change(run):
    result = su.read_or_estimate(run)
    assert result["dates"] == 30 and result["pixels_sampled"] == 4000 and result["reference"] == {"row": 5, "column": 7}
    assert result["sigma_mm_yr"][-1] > 1.5 * result["sigma_mm_yr"][0]
    assert result["from_reference_mm_yr"]["median"] > result["nearest"]["sigma_mm_yr"]
    assert result["ratio_to_stated"] > 2                                 # the stated error of this run is far too small
    assert all(m["sigma_mm_yr"] > 0 for m in result["at"]) and [m["distance_km"] for m in result["at"]] == [0.5, 1.0, 2.0, 5.0]
    stored = json.loads((run / su.CACHE_FILE).read_text())
    assert stored == result and su.read_or_estimate(run)["computed_at"] == result["computed_at"]
    text = su.describe(result)
    assert "precision, not accuracy" in text and "reference point" in text and "times the standard error MintPy states" in text

    with h5py.File(run / "velocity.h5", "a") as handle:                  # the run was made again
        handle.attrs["note"] = "again"
    import os
    os.utime(run / "velocity.h5", (result["results_mtime"] + 10, result["results_mtime"] + 10))
    assert su.read_or_estimate(run)["results_mtime"] == result["results_mtime"] + 10


def test_a_run_too_short_to_measure_gives_nothing_rather_than_an_error(run):
    with h5py.File(run / "timeseries.h5", "a") as handle:
        cube, dates = handle["timeseries"][:4], handle["date"][:4]
        del handle["timeseries"], handle["date"]
        handle.create_dataset("timeseries", data=cube)
        handle.create_dataset("date", data=dates)
    assert su.read_or_estimate(run) is None
    assert su.read_or_estimate(run / "nowhere") is None
    with pytest.raises(su.UncertaintyError, match="dates"):
        su.estimate(run)


def test_the_quality_checks_the_report_and_the_export_carry_it(run, tmp_path_factory):
    metrics = mintpy_qc.compute_qc(run)["metrics"]
    assert "precision, not accuracy" in metrics["velocity_uncertainty"]["summary"]
    assert len(metrics["velocity_uncertainty"]["at"]) == 4

    page = report.build_report(run)
    assert "Precision of the velocity" in page and "Fattahi" in page and "Emardson" in page
    assert "not with the error of each pixel" in page

    rasterio = pytest.importorskip("rasterio")
    path = mintpy_export.export(run, tmp_path_factory.mktemp("out"), "geotiff", layer="velocity_precision")
    with rasterio.open(path) as source:
        data = source.read(1)
        assert "Precision of the velocity" in source.tags()["description"] if "description" in source.tags() else True
    assert np.isnan(data[:, :10]).all()                                  # masked pixels stay empty
    assert data[5, 12] < data[5, 60] < data[80, 119]                     # it grows away from the reference pixel
    assert np.nanmin(data) == pytest.approx(su.read_or_estimate(run)["sigma_mm_yr"][0], abs=1e-3)
