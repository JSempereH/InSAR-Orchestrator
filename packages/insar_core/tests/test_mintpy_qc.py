"""Quality checks on a MintPy work directory, built from small synthetic files."""
import h5py
import numpy as np
import pytest

from insar_core.pipeline import mintpy_qc

SHAPE = (20, 30)


def _write(path, name, data, **attrs):
    path.parent.mkdir(parents=True, exist_ok=True)
    with h5py.File(path, "w") as handle:
        handle.create_dataset(name, data=data)
        handle.attrs.update(attrs)


def _stack(work, pairs, kept=None):
    path = work / "inputs" / "ifgramStack.h5"
    path.parent.mkdir(parents=True, exist_ok=True)
    with h5py.File(path, "w") as handle:
        handle.create_dataset("date", data=np.array([[a.encode(), b.encode()] for a, b in pairs]))
        handle.create_dataset("dropIfgram", data=np.array(kept if kept is not None else [True] * len(pairs)))


def _good_run(work, coherence=0.85, valid=0.95, residual_mm=2.0, reference=(10, 15), span=("20230103", "20231230")):
    rng = np.random.default_rng(1)
    dates = ["20230103", "20230115", "20230127", "20230208", "20230220"]
    pairs = [(a, b) for i, a in enumerate(dates) for b in dates[i + 1:i + 4]]
    _stack(work, pairs)
    _write(work / "avgSpatialCoh.h5", "coherence", np.full(SHAPE, coherence, "float32"))
    mask = rng.random(SHAPE) < valid
    _write(work / "maskTempCoh.h5", "mask", mask)
    _write(work / "temporalCoherence.h5", "temporalCoherence", np.full(SHAPE, 0.97, "float32"))
    _write(work / "numInvIfgram.h5", "mask", np.full(SHAPE, 9.0, "float32"))
    _write(work / "numTriNonzeroIntAmbiguity.h5", "mask", np.zeros(SHAPE, "float32"))
    with h5py.File(work / "velocity.h5", "w") as handle:
        handle.create_dataset("velocityStd", data=np.full(SHAPE, 0.003, "float32"))
        handle.attrs.update({"REF_Y": reference[0], "REF_X": reference[1], "LENGTH": SHAPE[0], "WIDTH": SHAPE[1],
                             "START_DATE": span[0], "END_DATE": span[1]})
    rows = "\n".join(f"{d}\t{residual_mm / 1000}" for d in dates)
    (work / "rms_timeseriesResidual_ramp.txt").write_text("# Date RMS (m)\n" + rows + "\n")
    return mask


def _by_id(report):
    return {c["id"]: c for c in report["checks"]}


def test_a_clean_run_has_nothing_to_flag(tmp_path):
    _good_run(tmp_path)
    report = mintpy_qc.compute_qc(tmp_path)
    assert report["status"] == "ok"
    assert {c["status"] for c in report["checks"]} == {"ok"}
    assert {"network_connected", "coherence", "valid_pixels", "residual_rms", "reference_pixel", "unwrapping"} <= set(_by_id(report))
    assert report["metrics"]["velocity_std_mm_yr_median"] == pytest.approx(3.0, abs=0.01)


def test_a_missing_directory_gives_no_checks_rather_than_an_error(tmp_path):
    report = mintpy_qc.compute_qc(tmp_path / "nothing")
    assert report["status"] == "unknown" and report["checks"] == []


def test_a_network_split_in_two_is_a_failure(tmp_path):
    _good_run(tmp_path)
    _stack(tmp_path, [("20230103", "20230115"), ("20230127", "20230208"), ("20230115", "20230103")])
    check = _by_id(mintpy_qc.compute_qc(tmp_path))["network_connected"]
    assert check["status"] == "fail" and check["value"] == 2


def test_interferograms_dropped_by_mintpy_do_not_count_toward_the_network(tmp_path):
    _good_run(tmp_path)
    pairs = [("20230103", "20230115"), ("20230115", "20230127"), ("20230127", "20230208")]
    _stack(tmp_path, pairs, kept=[True, False, True])
    assert _by_id(mintpy_qc.compute_qc(tmp_path))["network_connected"]["status"] == "fail"


def test_a_plain_chain_is_connected_but_thin(tmp_path):
    _good_run(tmp_path)
    dates = ["20230103", "20230115", "20230127", "20230208", "20230220"]
    _stack(tmp_path, list(zip(dates[:-1], dates[1:], strict=False)))
    check = _by_id(mintpy_qc.compute_qc(tmp_path))["network_connected"]
    assert check["status"] == "warn" and "thin" in check["message"]


@pytest.mark.parametrize("coherence, expected", [(0.9, "ok"), (0.4, "warn"), (0.1, "fail")])
def test_coherence_levels(tmp_path, coherence, expected):
    _good_run(tmp_path, coherence=coherence)
    assert _by_id(mintpy_qc.compute_qc(tmp_path))["coherence"]["status"] == expected


@pytest.mark.parametrize("valid, expected", [(0.95, "ok"), (0.2, "warn"), (0.01, "fail")])
def test_the_share_of_pixels_that_survive_the_mask(tmp_path, valid, expected):
    _good_run(tmp_path, valid=valid)
    assert _by_id(mintpy_qc.compute_qc(tmp_path))["valid_pixels"]["status"] == expected


@pytest.mark.parametrize("residual_mm, expected", [(2.0, "ok"), (14.0, "warn"), (40.0, "fail")])
def test_residual_levels(tmp_path, residual_mm, expected):
    _good_run(tmp_path, residual_mm=residual_mm)
    assert _by_id(mintpy_qc.compute_qc(tmp_path))["residual_rms"]["status"] == expected


def test_a_date_that_fits_much_worse_than_the_rest_is_named(tmp_path):
    _good_run(tmp_path)
    (tmp_path / "rms_timeseriesResidual_ramp.txt").write_text(
        "# Date RMS (m)\n20230103\t0.002\n20230115\t0.002\n20230127\t0.002\n20230208\t0.002\n20230220\t0.03\n")
    check = _by_id(mintpy_qc.compute_qc(tmp_path))["residual_outlier_dates"]
    assert check["status"] == "warn" and "20230220" in check["message"] and check["value"] == 1


def test_a_reference_pixel_on_the_edge_or_in_the_mask_gap_is_flagged(tmp_path):
    mask = _good_run(tmp_path, reference=(0, 15))
    check = _by_id(mintpy_qc.compute_qc(tmp_path))["reference_pixel"]
    assert check["status"] == "warn" and "edge" in check["message"]

    mask = _good_run(tmp_path, valid=0.5, reference=(10, 15))
    mask[10, 15] = False
    _write(tmp_path / "maskTempCoh.h5", "mask", mask)
    assert "masked out" in _by_id(mintpy_qc.compute_qc(tmp_path))["reference_pixel"]["message"]


def test_widespread_unwrapping_inconsistencies_are_a_failure(tmp_path):
    _good_run(tmp_path)
    _write(tmp_path / "numTriNonzeroIntAmbiguity.h5", "mask", np.ones(SHAPE, "float32"))
    assert _by_id(mintpy_qc.compute_qc(tmp_path))["unwrapping"]["status"] == "fail"


def test_the_overall_status_is_the_worst_check(tmp_path):
    _good_run(tmp_path, residual_mm=14.0)
    assert mintpy_qc.compute_qc(tmp_path)["status"] == "warn"
    _good_run(tmp_path, coherence=0.1, residual_mm=14.0)
    assert mintpy_qc.compute_qc(tmp_path)["status"] == "fail"


def test_a_series_of_a_few_weeks_is_flagged_because_its_velocity_is_extrapolated(tmp_path):
    _good_run(tmp_path, span=("20260824", "20260929"))
    check = _by_id(mintpy_qc.compute_qc(tmp_path))["time_span"]
    assert check["status"] == "warn" and check["value"] == 36 and "extrapolated" in check["message"]


def test_a_year_of_data_is_enough_for_a_velocity(tmp_path):
    _good_run(tmp_path)
    assert _by_id(mintpy_qc.compute_qc(tmp_path))["time_span"]["status"] == "ok"


def test_a_run_that_only_has_its_geocoded_velocity_still_gets_its_velocity_checks(tmp_path):
    _good_run(tmp_path)
    geo = tmp_path / "geo"
    geo.mkdir()
    (tmp_path / "velocity.h5").rename(geo / "geo_velocity.h5")
    (tmp_path / "maskTempCoh.h5").rename(geo / "geo_maskTempCoh.h5")
    report = mintpy_qc.compute_qc(tmp_path)
    ids = _by_id(report)
    assert "time_span" in ids and "reference_pixel" in ids


def _stack_with_phase(work, spans, steep_share):
    """A stack whose interferograms last `spans` days, the long ones with `steep_share` of the pixels stepping by more than pi."""
    import datetime as dt

    start = dt.date(2023, 1, 3)
    pairs = [(start.strftime("%Y%m%d"), (start + dt.timedelta(days=d)).strftime("%Y%m%d")) for d in spans]
    path = work / "inputs" / "ifgramStack.h5"
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(3)
    cube = np.zeros((len(spans), *SHAPE))
    for i, d in enumerate(spans):
        yy, xx = np.mgrid[:SHAPE[0], :SHAPE[1]]
        phase = 0.05 * (yy + xx) + rng.normal(0.0, 0.1, SHAPE) + 5.0      # smooth: well under half a cycle between neighbours
        if d >= 60:
            cols = int(round(SHAPE[1] * steep_share))
            phase[:, :cols:2] += 4.0                                      # columns that step by 4 rad (more than pi) to their neighbours
        cube[i] = phase
    with h5py.File(path, "w") as handle:
        handle.create_dataset("date", data=np.array([[a.encode(), b.encode()] for a, b in pairs]))
        handle.create_dataset("dropIfgram", data=np.ones(len(spans), bool))
        handle.create_dataset("unwrapPhase", data=cube.astype("float32"))


@pytest.mark.parametrize("steep, expected", [(0.0, "ok"), (0.03, "warn"), (0.5, "fail")])
def test_long_interferograms_that_jump_more_than_half_a_cycle_between_neighbours_are_flagged(tmp_path, steep, expected):
    _stack_with_phase(tmp_path, [12, 24, 36, 96, 96, 360], steep)
    check = _by_id(mintpy_qc.compute_qc(tmp_path))["steep_phase"]
    assert check["status"] == expected
    if expected != "ok":
        assert "underestimated" in check["message"]


def test_a_network_of_short_interferograms_has_nothing_to_say_about_steep_phase(tmp_path):
    _stack_with_phase(tmp_path, [12, 24, 36], 0.9)
    assert "steep_phase" not in _by_id(mintpy_qc.compute_qc(tmp_path))


def _series(work, planes_mm, shape=(40, 50), dates=8):
    rng = np.random.default_rng(3)
    yy, xx = np.mgrid[0:shape[0], 0:shape[1]]
    cube = np.zeros((dates, *shape))
    for i in range(1, dates):
        cube[i] = (planes_mm[0] * rng.normal() * xx / (shape[1] - 1) + planes_mm[1] * rng.normal() * yy / (shape[0] - 1)
                   + rng.normal(0, 0.5, shape)) / 1000.0
    _write(work / "timeseries.h5", "timeseries", cube)
    _write(work / "maskTempCoh.h5", "mask", np.ones(shape, bool))


def test_large_planes_on_every_date_are_flagged(tmp_path):
    _series(tmp_path, (30, 30))
    check = {c["id"]: c for c in mintpy_qc._ramps(tmp_path, np.ones((40, 50), bool))}["date_ramps"]
    assert check["status"] == "warn" and check["value"] >= 10


def test_a_flat_series_has_no_ramp_warning(tmp_path):
    _series(tmp_path, (0, 0))
    check = mintpy_qc._ramps(tmp_path, np.ones((40, 50), bool))[0]
    assert check["status"] == "ok" and check["value"] < 5


def test_missing_series_leaves_the_ramp_check_out(tmp_path):
    assert mintpy_qc._ramps(tmp_path, None) == []
