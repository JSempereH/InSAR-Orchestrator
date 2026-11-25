"""A stricter mask for the result than MintPy's temporal coherence alone.

Why. MintPy keeps a pixel when its temporal coherence passes a threshold (0.7 by default). That
number measures how well the series of the pixel reproduces its interferograms, and with the three
or so interferograms per date of a usual network there is little left over to disagree: on real
stacks nearly every pixel scores above 0.9, the noisy ones included. The triplets of interferograms
are a sharper test. A pixel whose closures are off by about half a cycle has a phase that is not
defined, whatever its temporal coherence says (see insar_core.analysis.unwrap_correction, which
finds those pixels and leaves them uncorrected for that reason).

What it was measured to do. On a one-year Sentinel-1 stack over the Guadalentin basin, compared
with EGMS pixel by pixel, the pixels with noisy closures were 7.5% of those MintPy kept. Their
velocities differed from EGMS by 5.8 mm/year (root mean square) against 4.0 for the rest, were
1.3 mm/year lower in the median, and one in eleven was more than 10 mm/year off, against one in
fifty. A low mean spatial coherence, once those pixels were out, marked a further tail of the same
kind but weaker, and what is "low" depends on the ground, so that criterion is offered and not
applied unless asked for.

What it does. It writes the refined mask where MintPy's was (maskTempCoh.h5), because every reader
of the result, in this library and outside it, takes that file as the mask. MintPy's own is kept
beside it as maskTempCoh_mintpy.h5. The velocities and the series are not touched: a pixel that is
dropped still has its values in the files. The reference pixel is never dropped.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Optional

import h5py
import numpy as np

from insar_core.analysis import unwrap_correction
from insar_core.common import read_dataset

MASK_FILE = "maskTempCoh.h5"
ORIGINAL_FILE = "maskTempCoh_mintpy.h5"
SUMMARY_FILE = "mask_refinement.json"
_UNREADABLE = (unwrap_correction.NOISY, unwrap_correction.AMBIGUOUS, unwrap_correction.UNEXPLAINED, unwrap_correction.UNSOLVED)


class MaskError(RuntimeError):
    """The mask cannot be refined, and the message says why."""


_dataset = read_dataset


def refine(work_dir, drop_unreadable_closures: bool = True, min_mean_coherence: Optional[float] = None) -> dict:
    """Refine the mask of the MintPy run in `work_dir`. Returns what was dropped and why.

    `drop_unreadable_closures` drops the pixels whose triplets do not close and could not be
    corrected; it needs the account the unwrapping correction leaves, and is passed over when that
    correction did not run. `min_mean_coherence` drops the pixels whose mean spatial coherence over
    the interferograms is under it. Calling it again starts from MintPy's own mask, not from the
    refined one."""
    work_dir = Path(work_dir)
    mask_file, original_file = work_dir / MASK_FILE, work_dir / ORIGINAL_FILE
    if not mask_file.exists():
        raise MaskError(f"No mask in {work_dir}")
    if not original_file.exists():
        shutil.copy2(mask_file, original_file)
    original = _dataset(original_file, "mask")
    if original is None:
        raise MaskError(f"{original_file} holds no mask")
    original = original.astype(bool)
    keep = original.copy()
    dropped: dict[str, int] = {}
    criteria: dict = {}

    status = _dataset(work_dir / unwrap_correction.COUNT_FILE, "status") if drop_unreadable_closures else None
    if status is not None and status.shape == keep.shape:
        unreadable = np.isin(status, _UNREADABLE)
        dropped["unreadable_closures"] = int((keep & unreadable).sum())
        keep &= ~unreadable
        criteria["unreadable_closures"] = True
    if min_mean_coherence is not None:
        coherence = _dataset(work_dir / "avgSpatialCoh.h5", "coherence")
        if coherence is None or coherence.shape != keep.shape:
            raise MaskError("The mean spatial coherence of this run is not there to mask by.")
        low = ~(coherence >= float(min_mean_coherence))
        dropped["low_mean_coherence"] = int((keep & low).sum())
        keep &= ~low
        criteria["min_mean_coherence"] = float(min_mean_coherence)

    with h5py.File(mask_file, "r+") as handle:
        try:
            ref = (int(handle.attrs["REF_Y"]), int(handle.attrs["REF_X"]))
        except KeyError:
            ref = None
        if ref is not None and original[ref]:
            keep[ref] = True                      # every value is relative to this pixel: it stays
        handle["mask"][...] = keep.astype(handle["mask"].dtype)
    summary = {
        "criteria": criteria, "kept_by_mintpy": int(original.sum()), "kept": int(keep.sum()),
        "dropped": dropped, "fraction_dropped": round(1.0 - float(keep.sum()) / max(1, int(original.sum())), 4),
        "original_mask": ORIGINAL_FILE, "computed_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
    }
    (work_dir / SUMMARY_FILE).write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return summary


def restore(work_dir) -> bool:
    """Put MintPy's own mask back where it was and forget the refinement. True when there was one.

    Called before a run starts. MintPy remakes its mask only when it finds it out of date, and a
    refined mask is newer than what it was made from: left in place it would pass for MintPy's own
    in the next run, and the refinement of that run would start from it."""
    work_dir = Path(work_dir)
    original = work_dir / ORIGINAL_FILE
    (work_dir / SUMMARY_FILE).unlink(missing_ok=True)
    if not original.exists():
        return False
    shutil.copy2(original, work_dir / MASK_FILE)             # with its dates, so MintPy judges it as it would have
    original.unlink()
    return True


def read_summary(work_dir) -> Optional[dict]:
    try:
        return json.loads((Path(work_dir) / SUMMARY_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
