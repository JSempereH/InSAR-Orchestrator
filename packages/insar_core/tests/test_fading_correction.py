"""Correcting the fading bias of short interferograms, on stacks whose bias is known."""
import datetime as dt

import numpy as np
import pytest

from insar_core.analysis import fading_correction as fc

SHAPE = (40, 50)
A1, A2 = 0.47, 0.31


def _network(n_dates=31, long_pair=True):
    start = dt.date(2023, 1, 4)
    dates = [(start + dt.timedelta(days=12 * k)).strftime("%Y%m%d") for k in range(n_dates)]
    pairs = [(dates[i], dates[i + s]) for i in range(n_dates) for s in (1, 2, 3) if i + s < n_dates]
    if long_pair:
        pairs.append((dates[0], dates[-1]))
    return dates, pairs


def _stack(rng, noise=0.02, bias_scale=0.06, motion_rad=0.0, long_pair=True, a1=A1, a2=A2):
    """Unwrapped phases: motion that grows with time, plus the fading bias of the paper's model, plus noise."""
    dates, pairs = _network(long_pair=long_pair)
    position = {d: k for k, d in enumerate(dates)}
    pixel_scale = bias_scale * (0.5 + rng.random(SHAPE))                           # differs from pixel to pixel
    step = pixel_scale[None] * (1.0 + 0.4 * rng.normal(size=(len(dates) - 1, *SHAPE)))
    phase = np.zeros((len(pairs), *SHAPE))
    truth_bias = np.zeros_like(phase)
    for n, (a, b) in enumerate(pairs):
        i, j = position[a], position[b]
        span = j - i
        total = step[i:j].sum(axis=0)
        bias = {1: 1.0, 2: a1, 3: a2}.get(span, 0.0) * total
        truth_bias[n] = bias
        phase[n] = motion_rad * (j - i) + bias + rng.normal(0, noise, SHAPE)
    return dates, pairs, phase, truth_bias


def test_the_constants_and_the_bias_are_recovered_from_the_loops():
    rng = np.random.default_rng(0)
    dates, pairs, phase, truth = _stack(rng)
    result = fc.correct(phase, pairs)
    assert result.a1 == pytest.approx(A1, abs=0.08) and result.a2 == pytest.approx(A2, abs=0.08)
    short = [n for n, (a, b) in enumerate(pairs) if (dates.index(b) - dates.index(a)) <= 3]
    before = np.sqrt(np.mean(truth[short] ** 2))
    after = np.sqrt(np.mean((truth[short] - result.bias_rad[short]) ** 2))
    assert after < 0.5 * before                                                   # most of the bias is gone
    # and the velocity: the bias adds up along the series, the correction takes the accumulation out
    assert abs(np.mean(result.corrected[short] - phase[short] + truth[short])) < 0.1 * before + 0.05


def test_real_motion_is_not_taken_for_bias():
    rng = np.random.default_rng(1)
    dates, pairs, phase, truth = _stack(rng, motion_rad=0.4)                       # 0.4 rad per 12 days of steady motion
    result = fc.correct(phase, pairs)
    short = [n for n, (a, b) in enumerate(pairs) if dates.index(b) - dates.index(a) == 1]
    motion = 0.4
    assert np.mean(result.corrected[short]) == pytest.approx(motion, abs=0.08)     # the motion stays
    assert np.mean(phase[short]) > motion + 0.04                                   # the bias was there and is removed


def test_a_network_without_the_long_interferogram_is_not_corrected_unless_the_constants_are_given():
    rng = np.random.default_rng(2)
    dates, pairs, phase, truth = _stack(rng, long_pair=False)
    with pytest.raises(fc.FadingCorrectionError, match="long interferogram"):
        fc.correct(phase, pairs)
    result = fc.correct(phase, pairs, a1=A1, a2=A2)
    assert result.summary["pixels_corrected"] == SHAPE[0] * SHAPE[1]


def test_pixels_that_miss_a_loop_are_left_as_they_were():
    rng = np.random.default_rng(3)
    dates, pairs, phase, truth = _stack(rng)
    phase[0, 5, 7] = 0.0                                                          # no data in one interferogram
    result = fc.correct(phase, pairs)
    assert not result.solved[5, 7]
    assert np.all(result.corrected[:, 5, 7] == phase[:, 5, 7].astype("float32"))


def test_too_few_dates_are_refused():
    rng = np.random.default_rng(4)
    dates, pairs, phase, truth = _stack(rng)
    with pytest.raises(fc.FadingCorrectionError, match="too few"):
        fc.correct(phase[:3], pairs[:3])


def _write_stack(path, dates, pairs, phase):
    import h5py

    path.parent.mkdir(parents=True, exist_ok=True)
    with h5py.File(path, "w") as handle:
        handle.create_dataset("date", data=np.array([[a.encode(), b.encode()] for a, b in pairs]))
        handle.create_dataset("dropIfgram", data=np.ones(len(pairs), bool))
        handle.create_dataset("unwrapPhase", data=phase.astype("float32"), chunks=(1, 20, SHAPE[1]))
        handle.create_dataset("coherence", data=np.full(phase.shape, 0.8, "float32"))


def test_a_stack_on_disk_is_corrected_in_blocks_like_the_array_and_the_calibration_pair_leaves_the_inversion(tmp_path, monkeypatch):
    import h5py

    monkeypatch.setattr(fc, "ROW_BLOCK", 7)                                       # several blocks, the last one short
    dates, pairs, phase, truth = _stack(np.random.default_rng(5))
    _write_stack(tmp_path / "inputs" / "ifgramStack.h5", dates, pairs, phase)
    summary = fc.correct_stack(tmp_path)
    in_memory = fc.correct(phase.astype("float32"), pairs, reference=None)
    assert summary["a1"] == pytest.approx(in_memory.a1, abs=0.1)                  # the reference pixels differ a little
    with h5py.File(tmp_path / "inputs" / "ifgramStack.h5") as handle:
        corrected = handle["unwrapPhase_phaseClosure"][:]
        kept = handle["dropIfgram"][:]
        original = handle["unwrapPhase"][:]
    short = [n for n, (a, b) in enumerate(pairs) if dates.index(b) - dates.index(a) <= 3]
    # the bias is found relative to the reference pixels, so what is judged is how much it still differs from pixel to pixel
    left = (truth[short] - (original[short] - corrected[short])).mean(axis=0)
    assert left.std() < 0.6 * truth[short].mean(axis=0).std()
    long_index = pairs.index((dates[0], dates[-1]))
    assert not kept[long_index] and kept.sum() == len(pairs) - 1                  # it calibrated, it is not inverted
    assert np.array_equal(corrected[long_index], original[long_index])
    assert fc.read_summary(tmp_path)["calibration_pair"] == [dates[0], dates[-1]]


def test_a_stack_without_a_long_interferogram_is_refused_with_a_reason(tmp_path):
    dates, pairs, phase, truth = _stack(np.random.default_rng(6), long_pair=False)
    _write_stack(tmp_path / "inputs" / "ifgramStack.h5", dates, pairs, phase)
    with pytest.raises(fc.FadingCorrectionError, match="long interferogram"):
        fc.correct_stack(tmp_path)
