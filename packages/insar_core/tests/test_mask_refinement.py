"""The stricter mask: what it drops, what it keeps, and that MintPy's own mask is not lost."""
import json

import h5py
import numpy as np
import pytest

from insar_core.analysis import unwrap_correction as uc
from insar_core.pipeline import mask_refinement as mr
from insar_core.pipeline import mintpy_qc

SHAPE = (10, 12)


@pytest.fixture
def run(tmp_path):
    mask = np.ones(SHAPE, dtype=bool)
    mask[0] = False                                             # MintPy left the first row out
    status = np.full(SHAPE, uc.CLEAN, dtype=np.uint8)
    status[0:3, 0:4] = uc.NOISY                                 # 12 pixels, 4 of them already out
    status[5, 5] = uc.NOISY                                     # the reference pixel itself
    status[6, 0:3] = uc.AMBIGUOUS
    status[7, 0:2] = uc.CORRECTED
    coherence = np.full(SHAPE, 0.8, dtype="float32")
    coherence[9] = 0.3
    with h5py.File(tmp_path / mr.MASK_FILE, "w") as handle:
        handle.create_dataset("mask", data=mask)
        handle.attrs.update({"REF_Y": 5, "REF_X": 5})
    with h5py.File(tmp_path / uc.COUNT_FILE, "w") as handle:
        handle.create_dataset("status", data=status)
        handle.create_dataset("mask", data=np.isin(status, (uc.NOISY, uc.AMBIGUOUS)).astype("float32"))
    with h5py.File(tmp_path / "avgSpatialCoh.h5", "w") as handle:
        handle.create_dataset("coherence", data=coherence)
    return tmp_path


def _mask(path):
    with h5py.File(path, "r") as handle:
        return handle["mask"][:].astype(bool)


def test_pixels_whose_closures_cannot_be_read_are_dropped_and_corrected_ones_are_kept(run):
    summary = mr.refine(run)
    mask = _mask(run / mr.MASK_FILE)
    assert not mask[1:3, 0:4].any() and not mask[6, 0:3].any()
    assert mask[7, 0:2].all()                                   # a pixel that was corrected is a good pixel
    assert mask[5, 5]                                           # the reference pixel stays whatever its closures
    assert summary["kept_by_mintpy"] == 108 and summary["dropped"] == {"unreadable_closures": 12}
    assert summary["kept"] == int(mask.sum()) == 108 - 12 + 1
    assert _mask(run / mr.ORIGINAL_FILE).sum() == 108           # MintPy's own mask is beside it
    assert json.loads((run / mr.SUMMARY_FILE).read_text()) == summary == mr.read_summary(run)


def test_a_coherence_limit_is_applied_only_when_asked_and_counted_apart(run):
    summary = mr.refine(run, min_mean_coherence=0.5)
    assert summary["dropped"] == {"unreadable_closures": 12, "low_mean_coherence": 12}
    assert not _mask(run / mr.MASK_FILE)[9].any()
    alone = mr.refine(run, drop_unreadable_closures=False, min_mean_coherence=0.5)
    assert alone["dropped"] == {"low_mean_coherence": 12} and alone["kept"] == 96


def test_refining_twice_starts_from_mintpys_mask_not_from_the_refined_one(run):
    first = mr.refine(run, min_mean_coherence=0.5)
    again = mr.refine(run)
    assert again["kept_by_mintpy"] == first["kept_by_mintpy"] == 108 and again["kept"] == 97
    assert _mask(run / mr.MASK_FILE)[9].all()                   # the coherence limit of the first call is gone


def test_without_the_account_of_the_correction_the_mask_is_left_as_mintpy_made_it(run):
    (run / uc.COUNT_FILE).unlink()
    summary = mr.refine(run)
    assert summary["kept"] == summary["kept_by_mintpy"] and summary["criteria"] == {}
    with pytest.raises(mr.MaskError, match="No mask"):
        mr.refine(run / "nowhere")


def test_the_quality_checks_say_what_the_mask_dropped_and_why(run):
    mr.refine(run)
    checks = {c["id"]: c for c in mintpy_qc.compute_qc(run)["checks"]}
    assert "stricter mask of this app dropped 10.2%" in checks["valid_pixels"]["message"]
    assert "left out of the result" in checks["unwrapping"]["message"] and checks["unwrapping"]["status"] == "ok"
    assert "pixels MintPy kept" in checks["unwrapping"]["message"]


def test_before_a_new_run_mintpys_mask_is_put_back_so_a_refined_one_never_passes_for_it(run):
    import os

    made = (run / mr.MASK_FILE).stat().st_mtime - 500
    os.utime(run / mr.MASK_FILE, (made, made))
    mr.refine(run)
    assert _mask(run / mr.MASK_FILE).sum() == 97
    assert mr.restore(run) is True
    assert _mask(run / mr.MASK_FILE).sum() == 108 and (run / mr.MASK_FILE).stat().st_mtime == pytest.approx(made, abs=1)
    assert not (run / mr.ORIGINAL_FILE).exists() and mr.read_summary(run) is None
    assert mr.restore(run) is False                             # nothing to put back the second time
