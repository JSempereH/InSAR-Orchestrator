"""Correction of phase unwrapping errors, pixel by pixel, from the closure of interferogram triplets.

What an unwrapping error is. An unwrapped interferogram is right up to a whole number of cycles
per pixel. Where the unwrapper chose that number wrong, the pixel is off by a multiple of 2 pi,
which is 28 mm for Sentinel-1. The error does not average out: it goes straight into the
displacement of the dates the interferogram joins.

How it shows. Three interferograms that join three dates in a loop must add up to zero, apart
from a small non-integer part (noise, and the fading signal of insar_core.analysis.fading_bias).
With an unwrapping error in one of them the sum is off by a whole number of cycles. That integer
is known exactly for every triplet of every pixel.

How it is corrected. Let C be the matrix that forms the closures (one row per triplet, +1, +1 and
-1 on its three interferograms) and c the integers found. The whole numbers u to add to the
interferograms must satisfy C u = -c. Many u do, so the one with the fewest corrections is taken
(least sum of |u|): it is far more likely that one interferogram is wrong than that several are
wrong in a way that happens to look the same. This is the integer problem of Yunjun et al. (2019,
section 3.2), which MintPy solves once per connected region of the unwrapper. Here it is solved
per pixel, because the products of HyP3 INSAR_GAMMA do not carry the connected components MintPy's
routine needs (it stops with an error on them), and because the errors seen in real stacks are
often patches smaller than a region.

What is and is not corrected:

* A pixel is corrected only where its closures can be read. The integer of a triplet is its
  closure rounded to whole cycles, and that rounding is only safe when what is left over is small.
  Where the leftover of any triplet of the pixel exceeds QUIET_RAD the pixel is noisy: its phase
  is poorly defined, a closure near half a cycle rounds either way by chance, and "correcting" it
  would fit noise and make a bad pixel look consistent. On real HyP3 stacks most of the pixels
  MintPy counts as having non-zero integer ambiguity are of this kind (their leftover is near half
  a cycle), and adding cycles to them moved them away from EGMS, not towards it. They are left as
  they were and counted as noisy.
* A noisy pixel is still corrected when it touches a patch that was corrected and shows the same
  error: every triplet of the interferogram the patch corrected is off by the same cycles in it.
  An unwrapping error is a region, and the noisy pixels inside it carry it too.
* A pixel is corrected only when the answer is the only one of its size. An interferogram that is
  in a single triplet cannot be told from the other two of that triplet, and such a pixel is left
  as it was and counted as ambiguous.
* A pixel whose integers no small set of corrections explains (none exists, or it would touch more
  than MAX_CORRECTED_SHARE of the interferograms) is left as it was and counted as unexplained.
* An interferogram that is in no triplet cannot be checked at all.
* An error shared by every interferogram that spans a date (for example a patch that all of them
  unwrap one cycle off) closes perfectly and is invisible to this method, as it is to MintPy's.

Short interferograms only. The correction works on the interferograms whose time span is at most
SHORT_SPAN_REVISITS revisit intervals (the median gap between consecutive dates), and leaves longer
ones as they are. A long interferogram decorrelates, so the triplets that contain one are noisy
by construction: their closures round to whole cycles by chance, a pixel that is perfectly good
shows triplets that do not close, and the correction would "fix" it. Measured on the Guadalentin
basin: with long pairs added, judging every triplet dropped 10% of the pixels MintPy kept, and
those were hardly worse against EGMS than the rest (RMS 3.96 against 3.65 mm/year); judged on the
short interferograms alone, as before they were added, the pixels dropped were clearly worse. An
unwrapping error in a long interferogram is therefore not corrected.

The corrected phases are written next to the original ones in the stack, under the name MintPy's
inversion reads when its phase closure method is on. The original phases are never changed.

Reference
    Yunjun, Z., Fattahi, H., and Amelung, F. (2019), Small baseline InSAR time series analysis:
        Unwrapping error correction and noise reduction, Computers and Geosciences, 133, 104331.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import h5py
import numpy as np

from insar_core.common import read_stack_network

DATASET_IN = "unwrapPhase"
DATASET_OUT = "unwrapPhase_phaseClosure"
SUMMARY_FILE = "unwrap_correction.json"
COUNT_FILE = "numTriNonzeroIntAmbiguity_corrected.h5"

QUIET_RAD = np.pi / 2           # the largest leftover of a closure, after whole cycles, at which its integer is trusted
MAX_CORRECTED_SHARE = 0.15      # of the interferograms of a pixel; beyond it the integers are not a few unwrapping errors
MAX_CYCLES = 3                  # the largest whole number of cycles one interferogram is corrected by
SHORT_SPAN_REVISITS = 4         # an interferogram of up to this many revisit intervals is short (see the notes above)
MIN_SHARE_IN_TRIPLETS = 0.5     # of the interferograms; under it the network is too open for the method
_BLOCK_PIXELS = 200_000
_TWO_PI = 2.0 * np.pi

# what happened to a pixel
CLEAN, CORRECTED, AMBIGUOUS, NOISY, UNSOLVED, NO_DATA, UNEXPLAINED, GROWN = 0, 1, 2, 3, 4, 5, 6, 7


class UnwrapCorrectionError(RuntimeError):
    """The stack cannot be corrected, and the message says why."""


def triplet_matrix(pairs: list[tuple[str, str]]) -> np.ndarray:
    """One row per triplet of interferograms that closes: +1, +1 and -1 on (a,b), (b,c) and (a,c)."""
    index = {pair: i for i, pair in enumerate(pairs)}
    after: dict[str, list[str]] = {}
    for first, second in pairs:
        after.setdefault(first, []).append(second)
    rows = []
    for (a, b), i in index.items():
        for c in after.get(b, ()):
            k = index.get((a, c))
            if k is not None:
                row = np.zeros(len(pairs), dtype=np.int8)
                row[i], row[index[(b, c)]], row[k] = 1, 1, -1
                rows.append(row)
    return np.array(rows, dtype=np.int8).reshape(len(rows), len(pairs))


def applicable(pairs: list[tuple[str, str]]) -> tuple[bool, str]:
    """Whether the network has enough triplets for the correction, and a sentence that says why not."""
    if not pairs:
        return False, "There are no interferograms."
    matrix = triplet_matrix(pairs)
    share = float((np.abs(matrix).sum(axis=0) > 0).mean()) if len(matrix) else 0.0
    if share < MIN_SHARE_IN_TRIPLETS:
        return False, (f"Only {share:.0%} of the interferograms are part of a triplet that closes. Unwrapping errors "
                       "can only be found in a network where each date is joined to at least two others on each side.")
    return True, f"{share:.0%} of the interferograms are part of a triplet that closes."


def closure_integers(phase: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """The whole number of cycles each triplet is off by. `phase` is (interferograms, pixels), in radians."""
    return _closures(phase, matrix)[0]


def _closures(phase: np.ndarray, matrix: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The whole cycles of each closure (triplets, pixels) and the largest leftover of each pixel, in radians."""
    closure = matrix.astype(np.float32) @ phase.astype(np.float32)
    integers = np.rint(closure / _TWO_PI)
    leftover = np.abs(closure - _TWO_PI * integers).max(axis=0) if len(matrix) else np.zeros(phase.shape[1], dtype=np.float32)
    return integers.astype(np.int8), leftover


def _single_edge_table(matrix: np.ndarray) -> dict[bytes, tuple[int, int]]:
    """Closure patterns that one wrong interferogram makes, and which interferogram and by how many cycles.

    A pattern that two interferograms make equally is stored as ambiguous (interferogram -1)."""
    table: dict[bytes, tuple[int, int]] = {}
    for edge in range(matrix.shape[1]):
        column = matrix[:, edge]
        if not column.any():
            continue
        for cycles in range(-MAX_CYCLES, MAX_CYCLES + 1):
            if cycles == 0:
                continue
            key = (column * cycles).astype(np.int8).tobytes()
            # the correction is minus the error
            table[key] = (-1, 0) if key in table else (edge, -cycles)
    return table


def _least_corrections(matrix: np.ndarray, target: np.ndarray, weights: np.ndarray) -> Optional[np.ndarray]:
    """The whole numbers u with matrix u = target and the least weighted sum of |u|, or None."""
    from scipy.optimize import Bounds, LinearConstraint, milp

    n = matrix.shape[1]
    stacked = np.hstack([matrix, -matrix]).astype(float)
    result = milp(c=np.concatenate([weights, weights]), constraints=LinearConstraint(stacked, target, target),
                  integrality=np.ones(2 * n), bounds=Bounds(0, MAX_CYCLES), options={"time_limit": 5.0})
    if not result.success or result.x is None:
        return None
    solution = np.rint(result.x[:n] - result.x[n:]).astype(np.int8)
    return solution if np.array_equal(matrix @ solution, target) else None


def solve_patterns(matrix: np.ndarray, patterns: np.ndarray, time_budget_s: float = 600.0) -> tuple[np.ndarray, np.ndarray]:
    """The corrections for each row of `patterns` (closure integers of one pixel), and what happened to it.

    Returns (corrections of shape (patterns, interferograms), status per pattern)."""
    n_ifg = matrix.shape[1]
    corrections = np.zeros((len(patterns), n_ifg), dtype=np.int8)
    status = np.full(len(patterns), UNSOLVED, dtype=np.uint8)
    table = _single_edge_table(matrix)
    most = max(1, int(MAX_CORRECTED_SHARE * n_ifg))
    rng = np.random.default_rng(0)
    # Two sets of slightly different weights: too small to change how many corrections are least,
    # large enough to choose differently between two answers of the same size.
    weights = [1.0 + 1e-3 * rng.random(n_ifg) for _ in range(2)]
    exact = matrix.astype(np.int64)
    started = time.monotonic()
    hard = []
    for p, pattern in enumerate(patterns):
        if not pattern.any():
            status[p] = CLEAN
            continue
        found = table.get(pattern.tobytes())
        if found is not None:
            edge, cycles = found
            if edge < 0:
                status[p] = AMBIGUOUS
            else:
                corrections[p, edge] = cycles
                status[p] = CORRECTED
            continue
        hard.append(p)
    # the patterns with most triplets off last: they are the slowest to solve and the least likely to have an answer
    hard.sort(key=lambda p: int(np.count_nonzero(patterns[p])))
    for p in hard:
        if time.monotonic() - started > time_budget_s:
            break
        target = -patterns[p].astype(np.int64)
        first = _least_corrections(exact, target, weights[0])
        if first is None or np.count_nonzero(first) > most:
            status[p] = UNEXPLAINED
            continue
        second = _least_corrections(exact, target, weights[1])
        if second is None or not np.array_equal(first, second):
            status[p] = AMBIGUOUS
            continue
        corrections[p] = first
        status[p] = CORRECTED
    return corrections, status


def _grow(cycles: np.ndarray, status: np.ndarray, integers: np.ndarray, matrix: np.ndarray, width: int) -> None:
    """Carry each correction to the noisy pixels that touch its patch and show the same error. In place.

    `cycles` is (pixels, interferograms), `integers` (pixels, triplets), both of the pixels of a
    block of rows `width` pixels wide."""
    from scipy import ndimage

    noisy = status == NOISY
    if not noisy.any():
        return
    seeds = status == CORRECTED
    shape = (-1, width)
    everywhere = np.ones((3, 3), dtype=bool)
    for edge in np.flatnonzero(np.any(cycles[seeds] != 0, axis=0)):
        triplets = np.flatnonzero(matrix[:, edge])
        for value in np.unique(cycles[seeds, edge]):
            if value == 0:
                continue
            seed = seeds & (cycles[:, edge] == value)
            # the error this correction undoes, as it shows in the triplets of the interferogram
            shows = np.all(integers[:, triplets] == (-int(value) * matrix[triplets, edge])[None, :], axis=1)
            candidate = (status == NOISY) | ((status == GROWN) & (cycles[:, edge] == 0))
            reached = ndimage.binary_propagation(seed.reshape(shape), structure=everywhere,
                                                 mask=(seed | (candidate & shows)).reshape(shape)).ravel() & ~seed
            cycles[reached, edge] = value
            status[reached] = GROWN


def correct_phases(phase: np.ndarray, matrix: np.ndarray, time_budget_s: float = 600.0,
                   width: Optional[int] = None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Correct `phase` (interferograms, pixels), already relative to the reference pixel.

    `width`, when the pixels are whole rows of an image that wide, lets a correction reach the noisy
    pixels that touch its patch (see the notes of this module).
    A pixel with a NaN in any interferogram has no data there and is left alone, and so
    is a pixel whose closures are too noisy to read (see QUIET_RAD).
    Returns (whole cycles added, of the shape of `phase`; status per pixel; triplets off per pixel before)."""
    n_ifg, n_pix = phase.shape
    cycles = np.zeros((n_ifg, n_pix), dtype=np.int8)
    status = np.full(n_pix, NO_DATA, dtype=np.uint8)
    off_before = np.zeros(n_pix, dtype=np.int32)
    has_data = np.all(np.isfinite(phase), axis=0)
    if not has_data.any() or not len(matrix):
        return cycles, status, off_before
    integers, leftover = _closures(phase[:, has_data], matrix)
    integers = integers.T                                                         # (pixels, triplets)
    off = np.count_nonzero(integers, axis=1)
    off_before[has_data] = off
    quiet = leftover < QUIET_RAD
    pixel_status = np.where(off == 0, CLEAN, NOISY).astype(np.uint8)
    pixel_cycles = np.zeros((int(has_data.sum()), n_ifg), dtype=np.int8)
    readable = quiet & (off > 0)
    if readable.any():
        patterns, inverse = np.unique(integers[readable], axis=0, return_inverse=True)
        solved, pattern_status = solve_patterns(matrix, patterns, time_budget_s)
        inverse = np.asarray(inverse).ravel()
        pixel_status[readable] = pattern_status[inverse]
        pixel_cycles[readable] = solved[inverse]
    status[has_data] = pixel_status
    cycles[:, has_data] = pixel_cycles.T
    if width and (status == CORRECTED).any():
        all_integers = np.zeros((n_pix, len(matrix)), dtype=np.int8)
        all_integers[has_data] = integers
        grown = np.ascontiguousarray(cycles.T)
        _grow(grown, status, all_integers, matrix, width)
        cycles = np.ascontiguousarray(grown.T)
    return cycles, status, off_before


def read_network(stack_file: Path) -> tuple[list[tuple[str, str]], np.ndarray]:
    """The pairs of dates of the stack and which of them are in use."""
    pairs, used, _ = read_stack_network(stack_file)
    return pairs, used


def short_pairs(pairs: list[tuple[str, str]]) -> list[bool]:
    """Which pairs are short: spanning at most SHORT_SPAN_REVISITS revisit intervals."""
    import datetime as dt

    dates = sorted({d for pair in pairs for d in pair})
    if len(dates) < 2:
        return [True] * len(pairs)
    day = {d: dt.datetime.strptime(d[:8], "%Y%m%d").toordinal() for d in dates}
    gaps = np.diff([day[d] for d in dates])
    limit = SHORT_SPAN_REVISITS * float(np.median(gaps))
    return [abs(day[b] - day[a]) <= limit for a, b in pairs]


def correct_stack(work_dir, time_budget_s: float = 900.0, dataset_in: str = DATASET_IN, dataset_out: str = DATASET_OUT) -> dict:
    """Correct the stack of the MintPy run in `work_dir` and write the corrected phases beside the original.

    The stack must already have its reference pixel (MintPy's `reference_point` step). Writes
    `dataset_out` in inputs/ifgramStack.h5, the summary as unwrap_correction.json and the number of
    triplets still off per pixel as numTriNonzeroIntAmbiguity_corrected.h5. Returns the summary."""
    work_dir = Path(work_dir)
    stack_file = work_dir / "inputs" / "ifgramStack.h5"
    if not stack_file.exists():
        raise UnwrapCorrectionError(f"No interferogram stack in {work_dir}")
    pairs, used = read_network(stack_file)
    in_use = np.array([bool(u and s) for u, s in zip(used, short_pairs(pairs), strict=True)])
    kept = [pair for pair, keep in zip(pairs, in_use, strict=True) if keep]
    ok, reason = applicable(kept)
    if not ok:
        raise UnwrapCorrectionError(reason)
    matrix = triplet_matrix(kept)
    kept_index = np.flatnonzero(in_use)
    started = time.monotonic()
    with h5py.File(stack_file, "r+") as handle:
        source = handle[dataset_in]
        try:
            ref_y, ref_x = int(handle.attrs["REF_Y"]), int(handle.attrs["REF_X"])
        except KeyError:
            raise UnwrapCorrectionError("The stack has no reference pixel yet.") from None
        n_ifg, length, width = source.shape
        if dataset_out in handle:
            del handle[dataset_out]
        target = handle.create_dataset(dataset_out, shape=source.shape, dtype=np.float32, chunks=source.chunks or True)
        reference = source[:, ref_y, ref_x].astype(np.float32)
        if not np.all(np.isfinite(reference[kept_index]) & (reference[kept_index] != 0)):
            # every closure is taken relative to this pixel: without it in one interferogram none can be read
            raise UnwrapCorrectionError("The reference pixel has no data in some interferograms.")
        status = np.full((length, width), NO_DATA, dtype=np.uint8)
        off_before = np.zeros((length, width), dtype=np.int32)
        off_after = np.zeros((length, width), dtype=np.int32)
        corrected_per_ifg = np.zeros(n_ifg, dtype=np.int64)
        rows_per_block = max(1, _BLOCK_PIXELS // width)
        for row in range(0, length, rows_per_block):
            rows = slice(row, min(length, row + rows_per_block))
            block = source[:, rows, :].astype(np.float32)
            shape = block.shape
            flat = block.reshape(n_ifg, -1)
            no_data = ~np.isfinite(flat) | (flat == 0)
            relative = flat[kept_index] - reference[kept_index, None]
            relative[no_data[kept_index]] = np.nan                  # a zero is MintPy's mark for no data
            remaining = max(5.0, time_budget_s - (time.monotonic() - started))
            cycles, block_status, before = correct_phases(relative, matrix, remaining, width=width)
            fixed = flat.copy()
            fixed[kept_index] += _TWO_PI * cycles
            fixed[no_data] = flat[no_data]
            target[:, rows, :] = fixed.reshape(shape)
            after = np.zeros_like(before)
            examined = block_status != NO_DATA
            if examined.any():
                after[examined] = np.count_nonzero(
                    closure_integers(relative[:, examined] + _TWO_PI * cycles[:, examined], matrix), axis=0)
            status[rows] = block_status.reshape(shape[1:])
            off_before[rows] = before.reshape(shape[1:])
            off_after[rows] = after.reshape(shape[1:])
            corrected_per_ifg[kept_index] += np.count_nonzero(cycles, axis=1)
        for key, value in source.attrs.items():
            target.attrs[key] = value
        target.attrs["MODIFICATION_TIME"] = str(time.time())
        attrs = dict(handle.attrs)

    examined = status != NO_DATA
    n = int(examined.sum())
    counts = {name: int((status == code).sum()) for name, code in
              (("clean", CLEAN), ("corrected", CORRECTED), ("corrected_with_neighbours", GROWN), ("ambiguous", AMBIGUOUS),
               ("unexplained", UNEXPLAINED),
               ("noisy", NOISY), ("unsolved", UNSOLVED))}
    worst = np.argsort(corrected_per_ifg)[::-1][:5]
    summary = {
        "method": "phase closure per pixel", "dataset": dataset_out,
        "interferograms": int(in_use.sum()), "interferograms_in_the_stack": int(used.sum()), "triplets": int(len(matrix)), "network": reason,
        "pixels_examined": n, "pixels_without_data": int((~examined).sum()),
        "pixels": counts,
        "fraction_with_errors_before": round(float((off_before[examined] > 0).mean()), 4) if n else None,
        "fraction_with_errors_after": round(float((off_after[examined] > 0).mean()), 4) if n else None,
        "most_corrected_interferograms": [
            {"pair": f"{pairs[i][0]}_{pairs[i][1]}", "pixels": int(corrected_per_ifg[i])} for i in worst if corrected_per_ifg[i] > 0],
        "seconds": round(time.monotonic() - started, 1),
        "computed_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
    }
    with h5py.File(work_dir / COUNT_FILE, "w") as out:
        for key, value in attrs.items():
            out.attrs[key] = value
        out.attrs["FILE_TYPE"] = "mask"
        out.attrs["UNIT"] = "1"
        out.create_dataset("mask", data=off_after.astype(np.float32), compression="gzip")
        out.create_dataset("status", data=status, compression="gzip")
    (work_dir / SUMMARY_FILE).write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return summary


def read_summary(work_dir) -> Optional[dict]:
    """The summary of the correction of the run in `work_dir`, or None when there was none."""
    try:
        return json.loads((Path(work_dir) / SUMMARY_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
