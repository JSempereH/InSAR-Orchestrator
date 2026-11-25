"""Correction of unwrapping errors from the closure of triplets, on stacks whose errors are known."""
import json

import h5py
import numpy as np
import pytest

from insar_core.analysis import unwrap_correction as uc
from insar_core.pipeline import mintpy_qc

TWO_PI = 2 * np.pi
DATES = [f"2023{m:02d}{d:02d}" for m in range(1, 5) for d in (3, 15, 27)]


def _pairs(dates=DATES, neighbours=3):
    return [(a, b) for i, a in enumerate(dates) for b in dates[i + 1:i + 1 + neighbours]]


def _phases(pairs, shape=(24, 30), noise=0.05, seed=0):
    """Unwrapped phases of a network that closes: a displacement per date, plus a little noise per interferogram."""
    rng = np.random.default_rng(seed)
    dates = sorted({d for pair in pairs for d in pair})
    per_date = {d: rng.normal(0, 3.0, shape) for d in dates}
    stack = np.array([per_date[b] - per_date[a] + rng.normal(0, noise, shape) for a, b in pairs], dtype=np.float32)
    return stack


def _write_stack(work, phase, pairs, reference=(0, 0), kept=None):
    path = work / "inputs" / "ifgramStack.h5"
    path.parent.mkdir(parents=True, exist_ok=True)
    with h5py.File(path, "w") as handle:
        handle.create_dataset("unwrapPhase", data=phase.astype(np.float32))
        handle.create_dataset("date", data=np.array([[a.encode(), b.encode()] for a, b in pairs]))
        handle.create_dataset("dropIfgram", data=np.array(kept if kept is not None else [True] * len(pairs)))
        handle.attrs.update({"REF_Y": reference[0], "REF_X": reference[1], "LENGTH": phase.shape[1], "WIDTH": phase.shape[2]})
    return path


def test_every_triplet_of_the_network_is_a_row_that_closes():
    pairs = _pairs(DATES[:5], neighbours=2)                     # 4 short and 3 long interferograms: 3 triplets
    matrix = uc.triplet_matrix(pairs)
    assert matrix.shape == (3, len(pairs))
    assert (np.abs(matrix).sum(axis=1) == 3).all() and (matrix.sum(axis=1) == 1).all()
    consistent = np.array([float(DATES.index(b) - DATES.index(a)) for a, b in pairs])
    assert np.allclose(matrix @ consistent, 0)


def test_a_chain_of_interferograms_cannot_be_corrected_and_says_why():
    ok, reason = uc.applicable(_pairs(neighbours=1))
    assert not ok and "triplet" in reason
    assert uc.applicable(_pairs(neighbours=3))[0]


def test_a_wrong_interferogram_is_put_back_exactly_and_nothing_else_is_touched():
    pairs = _pairs()
    truth = _phases(pairs)
    phase = truth.copy()
    phase[7, 5:12, 4:20] += TWO_PI                               # a patch one cycle off in one interferogram
    phase[20, 15:22, 10:25] -= 2 * TWO_PI                        # and two cycles off, the other way, in another
    flat = phase.reshape(len(pairs), -1)
    cycles, status, off = uc.correct_phases(flat - flat[:, :1], uc.triplet_matrix(pairs))
    fixed = (flat + TWO_PI * cycles).reshape(phase.shape)
    assert np.allclose(fixed, truth, atol=1e-4)
    touched = (cycles != 0).any(axis=0).reshape(truth.shape[1:])
    expected = np.zeros(truth.shape[1:], dtype=bool)
    expected[5:12, 4:20] = True
    expected[15:22, 10:25] = True
    assert (touched == expected).all()
    assert set(np.unique(status)) == {uc.CLEAN, uc.CORRECTED}
    assert (off.reshape(truth.shape[1:])[expected] > 0).all() and (off.reshape(truth.shape[1:])[~expected] == 0).all()


def test_two_wrong_interferograms_in_the_same_pixels_are_both_corrected():
    pairs = _pairs()
    truth = _phases(pairs)
    phase = truth.copy()
    phase[3, 4:10, 4:10] += TWO_PI
    phase[16, 4:10, 4:10] -= TWO_PI
    flat = phase.reshape(len(pairs), -1)
    cycles, status, _ = uc.correct_phases(flat - flat[:, :1], uc.triplet_matrix(pairs))
    assert np.allclose((flat + TWO_PI * cycles).reshape(phase.shape), truth, atol=1e-4)
    assert (status.reshape(truth.shape[1:])[4:10, 4:10] == uc.CORRECTED).all()


def test_an_interferogram_that_cannot_be_told_from_another_is_left_alone():
    # In a network of two neighbours the first short interferogram and the first long one are in one triplet only
    # and nothing tells them apart: an error in either is seen, counted and not touched.
    pairs = _pairs(DATES[:6], neighbours=2)
    truth = _phases(pairs)
    phase = truth.copy()
    phase[0, 5:9, 5:9] += TWO_PI
    flat = phase.reshape(len(pairs), -1)
    cycles, status, off = uc.correct_phases(flat - flat[:, :1], uc.triplet_matrix(pairs))
    patch = np.zeros(truth.shape[1:], dtype=bool)
    patch[5:9, 5:9] = True
    assert not cycles.any()
    assert (status.reshape(patch.shape)[patch] == uc.AMBIGUOUS).all()
    assert (off.reshape(patch.shape)[patch] == 1).all()


def test_closures_too_noisy_to_read_are_not_made_to_close():
    """A pixel whose closures sit near half a cycle rounds to a whole cycle by chance. Adding cycles
    to it would fit noise, so it is counted and left exactly as it was."""
    pairs = _pairs()
    rng = np.random.default_rng(3)
    phase = _phases(pairs)
    noisy = np.zeros(phase.shape[1:], dtype=bool)
    noisy[10:20, 10:20] = True
    phase[:, noisy] += rng.normal(0, 1.6, (len(pairs), int(noisy.sum()))).astype(np.float32)
    flat = phase.reshape(len(pairs), -1)
    cycles, status, off = uc.correct_phases(flat - flat[:, :1], uc.triplet_matrix(pairs))
    status = status.reshape(noisy.shape)
    assert (off.reshape(noisy.shape)[noisy] > 0).mean() > 0.9             # MintPy's count would call these unwrapping errors
    assert not cycles.any()
    assert (status[noisy] == uc.NOISY).mean() > 0.9
    assert (status[~noisy] == uc.CLEAN).all()


def test_noisy_pixels_inside_a_corrected_patch_get_its_correction():
    """An unwrapping error is a region. A noisy pixel in it cannot be read on its own, but it touches
    pixels that can, and it shows the same error in the triplets of the same interferogram."""
    pairs = _pairs()
    rng = np.random.default_rng(5)
    truth = _phases(pairs, shape=(30, 40))
    other = [i for i in range(len(pairs)) if i != 10 and not set(pairs[i]) & set(pairs[10])]
    # noise in interferograms that share no date with the wrong one: the pixel is unreadable, the error still shows
    scattered = rng.random(truth.shape[1:]) < 0.3
    truth[other[0]][scattered] += 2.2
    phase = truth.copy()
    patch = np.zeros(truth.shape[1:], dtype=bool)
    patch[8:22, 10:30] = True
    phase[10][patch] += TWO_PI
    flat = phase.reshape(len(pairs), -1)
    matrix = uc.triplet_matrix(pairs)
    alone = uc.correct_phases(flat - flat[:, :1], matrix)[0]
    cycles, status, _ = uc.correct_phases(flat - flat[:, :1], matrix, width=truth.shape[2])
    restored = (cycles[10] == -1).reshape(patch.shape)
    assert (alone[10] == -1).reshape(patch.shape)[patch].mean() < 0.85    # without neighbours the noisy ones are left out
    assert restored[patch].all() and not restored[~patch].any()
    assert (status.reshape(patch.shape)[patch & scattered] == uc.GROWN).all()
    assert np.count_nonzero(cycles[np.arange(len(pairs)) != 10]) == 0


def test_pixels_without_data_are_left_as_they_are():
    pairs = _pairs()
    phase = _phases(pairs)
    phase[4, 0:5, :] = 0.0                                       # MintPy's mark for no data
    phase[7, 10:14, 10:14] += TWO_PI
    flat = phase.reshape(len(pairs), -1)
    relative = flat - flat[:, -1:]
    relative[flat == 0] = np.nan
    cycles, status, _ = uc.correct_phases(relative, uc.triplet_matrix(pairs))
    status = status.reshape(phase.shape[1:])
    assert (status[0:5] == uc.NO_DATA).all() and not cycles.reshape(phase.shape)[:, 0:5].any()
    assert (status[10:14, 10:14] == uc.CORRECTED).all()


def test_a_stack_is_corrected_beside_the_original_and_the_account_is_written(tmp_path):
    pairs = _pairs()
    truth = _phases(pairs)
    truth -= truth[:, 2:3, 3:4] - 0.7                            # a constant of its own per interferogram, as HyP3 has
    phase = truth.copy()
    phase[7, 5:12, 4:20] += TWO_PI
    dropped = [True] * len(pairs)
    dropped[-1] = False                                          # an interferogram the network left out
    phase[-1] += 4.0
    path = _write_stack(tmp_path, phase, pairs, reference=(2, 3), kept=dropped)

    summary = uc.correct_stack(tmp_path)

    with h5py.File(path, "r") as handle:
        assert np.allclose(handle["unwrapPhase"][:], phase)                       # the original is not changed
        fixed = handle[uc.DATASET_OUT][:]
        assert "MODIFICATION_TIME" in handle[uc.DATASET_OUT].attrs
    assert np.allclose(fixed[:-1], truth[:-1], atol=1e-4)
    assert np.allclose(fixed[-1], phase[-1])                                      # the dropped one is copied as it is
    assert summary["pixels"]["corrected"] == 7 * 16 and summary["pixels"]["noisy"] == 0
    assert summary["fraction_with_errors_before"] > 0 and summary["fraction_with_errors_after"] == 0
    assert summary["most_corrected_interferograms"][0] == {"pair": "_".join(pairs[7]), "pixels": 7 * 16}
    assert json.loads((tmp_path / uc.SUMMARY_FILE).read_text()) == summary == uc.read_summary(tmp_path)
    with h5py.File(tmp_path / uc.COUNT_FILE, "r") as handle:
        assert not handle["mask"][:].any() and (handle["status"][:] == uc.CORRECTED).sum() == 7 * 16


def test_a_stack_that_cannot_be_corrected_says_why(tmp_path):
    with pytest.raises(uc.UnwrapCorrectionError, match="No interferogram stack"):
        uc.correct_stack(tmp_path)
    pairs = _pairs(neighbours=1)
    _write_stack(tmp_path, _phases(pairs), pairs)
    with pytest.raises(uc.UnwrapCorrectionError, match="triplet"):
        uc.correct_stack(tmp_path)
    assert uc.read_summary(tmp_path) is None


def test_the_quality_check_tells_corrected_errors_from_noise(tmp_path):
    pairs = _pairs()
    rng = np.random.default_rng(2)
    phase = _phases(pairs)
    phase[7, 2:8, 2:12] += TWO_PI
    phase[:, 14:20, 14:26] += rng.normal(0, 1.6, (len(pairs), 6, 12)).astype(np.float32)
    _write_stack(tmp_path, phase, pairs)
    uc.correct_stack(tmp_path)
    with h5py.File(tmp_path / "maskTempCoh.h5", "w") as handle:
        handle.create_dataset("mask", data=np.ones(phase.shape[1:], dtype=bool))
    check = {c["id"]: c for c in mintpy_qc.compute_qc(tmp_path)["checks"]}["unwrapping"]
    assert "corrected in 8.3%" in check["message"]                # 60 of 720 pixels
    assert "noisy" in check["message"] and check["status"] == "ok"
    assert 0.05 < check["value"] < 0.15                           # what still does not close is the noisy patch


def test_a_reference_pixel_without_data_stops_the_correction_with_the_reason(tmp_path):
    pairs = _pairs()
    phase = _phases(pairs)
    phase[3, 0, 0] = 0.0
    _write_stack(tmp_path, phase, pairs)
    with pytest.raises(uc.UnwrapCorrectionError, match="reference pixel"):
        uc.correct_stack(tmp_path)


def test_long_interferograms_are_neither_judged_nor_corrected(tmp_path):
    """Triplets with a long pair are noisy by construction. A good pixel must not be dropped or 'fixed' for them."""
    pairs = _pairs()
    dates = sorted({d for p in pairs for d in p})
    long_pairs = [(dates[0], dates[9]), (dates[3], dates[11])]
    pairs = pairs + long_pairs
    assert uc.short_pairs(pairs) == [True] * (len(pairs) - 2) + [False, False]
    truth = _phases(pairs)
    phase = truth.copy()
    phase[-2:] += np.random.default_rng(0).normal(0, 1.8, phase[-2:].shape).astype("float32")      # the long ones decorrelate
    phase[7, 5:12, 4:20] += TWO_PI                                                                  # a real error in a short one
    _write_stack(tmp_path, phase, pairs)
    summary = uc.correct_stack(tmp_path)
    with h5py.File(tmp_path / "inputs" / "ifgramStack.h5", "r") as handle:
        fixed = handle[uc.DATASET_OUT][:]
    assert np.allclose(fixed[7], truth[7], atol=1e-4)                       # the short error is put back
    assert np.allclose(fixed[-2:], phase[-2:])                              # the long ones are copied as they were
    assert summary["pixels"]["noisy"] == 0 and summary["pixels"]["corrected"] == 7 * 16
    assert summary["interferograms"] == len(pairs) - 2 and summary["interferograms_in_the_stack"] == len(pairs)
