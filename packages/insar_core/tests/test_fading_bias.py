"""Fading bias from closure phases, on stacks with a known answer."""
import datetime as dt

import h5py
import numpy as np
import pytest

from insar_core.analysis import fading_bias as fb

WAVELENGTH = 0.0555
SHAPE = (24, 30)


def _dates(n, step=12):
    return [(dt.date(2022, 1, 1) + dt.timedelta(days=step * k)).strftime("%Y%m%d") for k in range(n)]


def _stack(n_dates=40, neighbours=3, bias_rad_per_short=0.0, noise=0.0, offsets=False, cycles=False, biased_columns=slice(15, None), seed=0):
    """Interferograms of a network 'each date with the next N', of a ground that moves at a known rate.

    The shortest interferograms (one step) carry an extra `bias_rad_per_short` in the `biased_columns`
    (a field that fades); the rest of the area is clean. `offsets` gives each interferogram a
    constant of its own, as HyP3 does, and `cycles` adds whole cycles here and there (unwrapping errors)."""
    rng = np.random.default_rng(seed)
    dates = _dates(n_dates)
    motion = -0.05 * np.arange(n_dates)[:, None, None] * np.ones(SHAPE)            # rad per date, the same everywhere
    pairs, phases = [], []
    for i in range(n_dates):
        for j in range(i + 1, min(i + 1 + neighbours, n_dates)):
            phase = motion[j] - motion[i] + rng.normal(0, noise, SHAPE) if noise else (motion[j] - motion[i]).copy()
            if j - i == 1:
                phase[:, biased_columns] += bias_rad_per_short
            if cycles:
                phase[rng.random(SHAPE) < 0.03] += 2 * np.pi
            if offsets:
                phase += rng.uniform(-3, 3)
            pairs.append((dates[i], dates[j]))
            phases.append(phase)
    phase = np.array(phases, "float32")
    phase[phase == 0] = 1e-6                                                        # zero means no data
    return phase, pairs


def _reference():
    reference = np.zeros(SHAPE, bool)
    reference[:, :5] = True                                                         # clean, coherent ground
    return reference


def _expected_rate(bias_rad, connection, step_days=12):
    return -WAVELENGTH / (4 * np.pi) * (connection * bias_rad) / (connection * step_days / 365.25) * 1000.0


def test_loops_are_the_short_interferograms_and_the_one_that_spans_them():
    pairs = [("20220101", "20220113"), ("20220113", "20220125"), ("20220101", "20220125"), ("20220125", "20220206")]
    assert fb.closure_loops(pairs, 2) == [[0, 1, 2]]
    assert fb.closure_loops(pairs, 2, used=[True, True, False, True]) == []        # a dropped interferogram opens the loop
    with pytest.raises(fb.FadingBiasError):
        fb.closure_loops(pairs, 1)


def test_the_largest_connection_is_what_the_network_closes():
    _, pairs = _stack(n_dates=20, neighbours=3)
    assert fb.largest_connection(pairs) == 3
    _, chain = _stack(n_dates=20, neighbours=1)
    assert fb.largest_connection(chain) == 0


def test_a_known_bias_is_recovered_as_a_rate_where_it_is_and_nowhere_else():
    bias = 0.05                                                                     # rad in every 12-day interferogram
    phase, pairs = _stack(bias_rad_per_short=bias, noise=0.02)
    result = fb.estimate_from_phases(phase, pairs, WAVELENGTH, _reference())
    assert result.connection == 3 and result.loops == 37 and result.span_days == pytest.approx(36.0)
    expected = _expected_rate(bias, 3)                                              # about -6.7 mm/year
    assert np.median(result.velocity_bias_mm_yr[:, 15:]) == pytest.approx(expected, abs=0.3)
    assert np.median(result.velocity_bias_mm_yr[:, :15]) == pytest.approx(0.0, abs=0.3)
    assert result.biased[:, 15:].mean() > 0.95 and result.biased[:, :15].mean() < 0.02


def test_real_motion_is_not_bias():
    phase, pairs = _stack(bias_rad_per_short=0.0, noise=0.05)
    result = fb.estimate_from_phases(phase, pairs, WAVELENGTH, _reference())
    assert abs(result.summary["median_mm_yr"]) < 0.3
    assert result.summary["biased_fraction"] < 0.02
    assert fb.check(result)["status"] == "ok"


def test_the_constant_of_each_interferogram_is_taken_out_by_the_reference():
    """As measured on HyP3 stacks: each unwrapped interferogram has a constant of its own."""
    clean, pairs = _stack(bias_rad_per_short=0.05, noise=0.02, seed=1)
    shifted, _ = _stack(bias_rad_per_short=0.05, noise=0.02, seed=1, offsets=True)
    a = fb.estimate_from_phases(clean, pairs, WAVELENGTH, _reference())
    # the two stacks differ in their noise draws once offsets are drawn, so compare the answers, not the arrays
    b = fb.estimate_from_phases(shifted, pairs, WAVELENGTH, _reference())
    assert np.median(b.velocity_bias_mm_yr[:, 15:]) == pytest.approx(np.median(a.velocity_bias_mm_yr[:, 15:]), abs=0.4)
    assert abs(np.median(b.velocity_bias_mm_yr[:, :15])) < 0.4
    unreferenced = fb.estimate_from_phases(shifted, pairs, WAVELENGTH, None)
    assert unreferenced.consistency.mean() < 0.4                                    # without a reference nothing closes


def test_unwrapping_errors_are_not_counted_as_bias():
    phase, pairs = _stack(bias_rad_per_short=0.0, noise=0.02, cycles=True)
    result = fb.estimate_from_phases(phase, pairs, WAVELENGTH, _reference())
    assert abs(result.summary["median_mm_yr"]) < 0.3 and result.summary["biased_fraction"] < 0.02


def test_noise_alone_is_not_called_bias_although_loops_overlap():
    """Consecutive loops share interferograms; counting them as independent calls noise a bias."""
    phase, pairs = _stack(n_dates=100, bias_rad_per_short=0.0, noise=0.3, seed=4)
    result = fb.estimate_from_phases(phase, pairs, WAVELENGTH, _reference())
    assert result.summary["biased_fraction"] < 0.02
    assert result.summary["noise_floor_mm_yr"] > 1.0                                # and the summary says how blind it is


def test_the_summary_tells_fields_from_buildings():
    phase, pairs = _stack(bias_rad_per_short=0.05, noise=0.02)
    coherence = np.full(SHAPE, 0.9)
    coherence[:, 15:] = 0.4                                                         # the biased half is the less coherent one
    result = fb.estimate_from_phases(phase, pairs, WAVELENGTH, _reference(), coherence=coherence)
    summary = result.summary
    assert summary["low_coherence_median_mm_yr"] == pytest.approx(_expected_rate(0.05, 3), abs=0.4)
    assert abs(summary["high_coherence_median_mm_yr"]) < 0.4
    verdict = fb.check(result)
    assert verdict["status"] in ("warn", "fail") and "least coherent third" in verdict["message"]


def test_a_chain_without_loops_says_what_is_needed():
    phase, pairs = _stack(neighbours=1)
    with pytest.raises(fb.FadingBiasError, match="no closed loops"):
        fb.estimate_from_phases(phase, pairs, WAVELENGTH, _reference())


def test_the_reference_is_the_most_coherent_pixels():
    coherence = np.linspace(0.1, 0.99, SHAPE[0] * SHAPE[1]).reshape(SHAPE)
    chosen = fb.coherent_reference(coherence)
    assert chosen.sum() >= fb.MIN_REFERENCE_PIXELS and coherence[chosen].min() > 0.9
    few = fb.coherent_reference(coherence[:2, :5])                                  # ten pixels: all of them, not 5%
    assert few.sum() == 10


def _write_run(work, phase, pairs, coherence):
    (work / "inputs").mkdir(parents=True)
    with h5py.File(work / "inputs" / "ifgramStack.h5", "w") as handle:
        handle.create_dataset("unwrapPhase", data=phase)
        handle.create_dataset("coherence", data=np.broadcast_to(coherence, phase.shape).astype("float32"))
        handle.create_dataset("date", data=np.array([[a.encode(), b.encode()] for a, b in pairs]))
        handle.create_dataset("dropIfgram", data=np.ones(len(pairs), bool))
        handle.attrs.update({"WAVELENGTH": WAVELENGTH, "REF_Y": 2, "REF_X": 2})
    with h5py.File(work / "maskTempCoh.h5", "w") as handle:
        handle.create_dataset("mask", data=np.ones(SHAPE, bool))


def test_a_mintpy_run_is_read_and_checked(tmp_path):
    phase, pairs = _stack(bias_rad_per_short=0.05, noise=0.02, offsets=True)
    coherence = np.full(SHAPE, 0.5)
    coherence[:, :5] = 0.95
    _write_run(tmp_path, phase, pairs, coherence)
    result = fb.estimate(tmp_path)
    assert result.summary["reference_pixels"] >= fb.MIN_REFERENCE_PIXELS
    assert np.median(result.velocity_bias_mm_yr[:, 15:]) == pytest.approx(_expected_rate(0.05, 3), abs=0.5)

    from insar_core.pipeline.mintpy_qc import compute_qc
    checks = {c["id"]: c for c in compute_qc(tmp_path)["checks"]}
    assert checks["fading_bias"]["status"] in ("warn", "fail")


def test_a_folder_without_a_stack_is_refused_and_the_checks_go_on(tmp_path):
    with pytest.raises(fb.FadingBiasError, match="cannot be read"):
        fb.estimate(tmp_path)
    from insar_core.pipeline.mintpy_qc import compute_qc
    assert all(c["id"] != "fading_bias" for c in compute_qc(tmp_path)["checks"])


def test_the_bias_map_is_written_with_its_error(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    phase, pairs = _stack(bias_rad_per_short=0.05, noise=0.02)
    result = fb.estimate_from_phases(phase, pairs, WAVELENGTH, _reference())
    geo = {"x_first": 600000.0, "y_first": 4170000.0, "x_step": 80.0, "y_step": -80.0, "width": SHAPE[1], "length": SHAPE[0], "epsg": 32630}
    out = fb.write_geotiff(result, geo, tmp_path / "bias.tif")
    with rasterio.open(out) as raster:
        assert raster.count == 2 and raster.tags()["units"] == "mm/year" and raster.tags()["connection"] == "3"
        assert np.allclose(raster.read(1), result.velocity_bias_mm_yr, equal_nan=True)
