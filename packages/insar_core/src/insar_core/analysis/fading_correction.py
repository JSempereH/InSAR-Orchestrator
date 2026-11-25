"""Correct the fading bias of the short interferograms of a network, from its own loop closures.

Multilooking leaves a small phase in short interferograms that is not motion and has the same sign
for as long as the ground keeps changing (soil moisture, vegetation). Added over a year it is
millimetres per year of false motion: see `fading_bias.py`, which measures it. This module
removes it, following Maghsoudi et al. (2022).

The method, in the notation of the paper. Let `d(i)` be the unknown bias of the interferogram
between consecutive dates i and i+1. The interferograms two and three steps long carry a bias
that is a constant fraction of the sum of the biases of the steps they span:

    bias(i, i+2) = a1 * (d(i) + d(i+1))              bias(i, i+3) = a2 * (d(i) + d(i+1) + d(i+2))

The closure of a loop, the wrapped difference between an interferogram and the sum of the shorter
ones over the same dates, then equals (a - 1) times that sum of biases, so every closure is one
linear equation in the `d`. With a closure for each date (2 N - 5 of them for N dates) and N - 1
unknowns the system is overdetermined and is solved by least squares, for all pixels at once
because the matrix does not depend on the pixel. Each interferogram is then corrected by its
share of the bias.

The constants a1 and a2 come from one interferogram that spans the whole period and is taken to
have no bias, as the paper does: the closure of that interferogram against the chain of the
2-step interferograms over the same period, divided by its closure against the chain of the
1-step ones, gives a1 (and the 3-step chain gives a2). So the network needs at least one long
interferogram from the first date of a chain to its last, for example the one a year long.
Without it the constants cannot be estimated from the data and nothing is corrected.

What it does not do. It assumes the same constants everywhere (the paper checked that they show
no spatial pattern), that the long interferogram is free of bias, and that the wrapped closure of
a loop is below pi. Pixels that lack an interferogram of any loop are left as they are. Longer
interferograms than three steps are not corrected.

References
    Maghsoudi, Y., Hooper, A. J., Wright, T. J., Lazecky, M., and Ansari, H. (2022), Characterizing
        and correcting phase biases in short-term, multilooked interferograms, Remote Sensing of
        Environment, 275, 113022.
    Zheng, Y., Fattahi, H., Agram, P., Simons, M., and Rosen, P. (2022), On closure phase and
        systematic bias in multilooked SAR interferometry, IEEE Trans. Geosci. Remote Sens., 60, 5226611.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np

CITATIONS = (
    "Maghsoudi, Y., Hooper, A. J., Wright, T. J., Lazecky, M., and Ansari, H. (2022), Characterizing and correcting "
    "phase biases in short-term, multilooked interferograms, Remote Sensing of Environment, 275, 113022",
    "Zheng, Y., Fattahi, H., Agram, P., Simons, M., and Rosen, P. (2022), On closure phase and systematic bias in "
    "multilooked SAR interferometry, IEEE Trans. Geosci. Remote Sens., 60, 5226611",
)

MIN_DATES = 6                  # N - 1 unknowns need 2 N - 5 equations: N = 5 is the least that is overdetermined
MIN_PIXELS_FOR_RATIO = 200     # fewer pixels coherent over the whole period do not give a ratio worth using
RATIO_RANGE = (0.0, 1.0)       # a longer interferogram carries less bias per step than the sum of the short ones


class FadingCorrectionError(ValueError):
    pass


@dataclass
class FadingCorrection:
    a1: float
    a2: float
    corrected: np.ndarray                 # the phase stack with the bias taken out (same shape as the input)
    bias_rad: np.ndarray                  # n_ifg x rows x columns, the bias removed from each interferogram
    solved: np.ndarray                    # rows x columns, bool: pixels whose loops were all there
    summary: dict = field(default_factory=dict)


def _wrap(phase: np.ndarray) -> np.ndarray:
    return (phase + np.pi) % (2.0 * np.pi) - np.pi


def _index_of_pairs(pairs: Sequence[tuple[str, str]], used: Optional[Sequence[bool]]):
    keep = [True] * len(pairs) if used is None else [bool(u) for u in used]
    dates = sorted({d for i, pair in enumerate(pairs) if keep[i] for d in pair})
    position = {d: k for k, d in enumerate(dates)}
    index = {(position[a], position[b]): i for i, (a, b) in enumerate(pairs) if keep[i]}
    return dates, index


def _chain(index: dict, start: int, step: int, length: int) -> Optional[list[int]]:
    """Indices of the `step`-long interferograms that go from date `start` to date `start + length`, or None if one is missing."""
    chain = []
    for k in range(start, start + length, step):
        if (k, k + step) not in index:
            return None
        chain.append(index[(k, k + step)])
    return chain


def _referenced(closure: np.ndarray, reference: Optional[np.ndarray]) -> np.ndarray:
    """`closure` (wrapped) with the circular mean of the reference pixels taken out.

    The unwrapped phase of an interferogram carries a constant of its own, so a closure has an offset that
    is the same over the whole map; that is not bias. The reference pixels (the most coherent ones, which
    fade least) fix it, and what is left is the bias relative to them."""
    if reference is None or not reference.any():
        return closure
    offset = np.angle(np.mean(np.exp(1j * closure[reference])))
    return _wrap(closure - offset)


def estimate_ratios(phase, index: dict, n_dates: int, valid: Optional[np.ndarray] = None,
                    reference: Optional[np.ndarray] = None) -> tuple[float, float, dict]:
    """a1 and a2 from the longest interferogram that spans a whole number of 6-step chains, taken to have no bias.

    Returns (a1, a2, info). Raises FadingCorrectionError when the network has no such interferogram or too
    few pixels keep their phase in all of it."""
    best = None
    for (first, last), i_long in index.items():
        length = last - first
        if length % 6 or length < 6:
            continue
        chains = [_chain(index, first, step, length) for step in (1, 2, 3)]
        if all(c is not None for c in chains) and (best is None or length > best[0]):
            best = (length, i_long, chains, first)
    if best is None:
        raise FadingCorrectionError(
            "The network has no long interferogram over a stretch the 1, 2 and 3-step interferograms also cover, which "
            "the correction needs to fix its constants. A pair a year long is enough.")
    length, i_long, chains, first = best
    long_phase = np.asarray(phase[i_long], dtype="float64")
    closures, ok = [], long_phase != 0
    for chain in chains:
        total = np.zeros_like(long_phase)
        for i in chain:
            layer = np.asarray(phase[i], dtype="float64")
            ok &= layer != 0
            total += layer
        closures.append(_referenced(_wrap(long_phase - total), reference))
    if valid is not None:
        ok &= np.asarray(valid, bool)
    if int(ok.sum()) < MIN_PIXELS_FOR_RATIO:
        raise FadingCorrectionError(f"Only {int(ok.sum())} pixels keep their phase over the whole period of the long interferogram.")
    x1, x2, x3 = (c[ok] for c in closures)
    # The closure against the 1-step chain is the bias of that chain (the long one has none), and the same
    # against the 2- and 3-step chains is a1 and a2 times it. Pixels with little bias say nothing about the
    # ratio, and a tail of wrapped outliers would pull it, so the fit is made on the pixels in the middle.
    keep = (np.abs(x1) < np.pi * 0.8) & (np.abs(x1) > 0.2)
    if int(keep.sum()) < MIN_PIXELS_FOR_RATIO // 2:
        raise FadingCorrectionError("The long interferogram's closures are too small or too noisy to measure the constants.")
    a1 = float(np.sum(x1[keep] * x2[keep]) / np.sum(x1[keep] ** 2))
    a2 = float(np.sum(x1[keep] * x3[keep]) / np.sum(x1[keep] ** 2))
    info = {"long_pair": i_long, "length_steps": int(length), "ratio_pixels": int(keep.sum()), "first_date": int(first)}
    for name, value in (("a1", a1), ("a2", a2)):
        if not RATIO_RANGE[0] < value < RATIO_RANGE[1]:
            raise FadingCorrectionError(
                f"The constant {name} came out as {value:.2f}, outside 0 to 1: the long interferogram is not free of the bias "
                "or the closures are noise, so nothing is corrected.")
    if a2 > a1:
        raise FadingCorrectionError(f"a2 ({a2:.2f}) is larger than a1 ({a1:.2f}), which would mean the bias grows with the span.")
    return a1, a2, info


def _equations(index: dict, n_dates: int, a1: float, a2: float):
    """The closures (as lists of interferogram indices) and the matrix that ties them to the unknown biases."""
    rows, loops = [], []
    unknowns = n_dates - 1
    for start in range(n_dates - 2):
        if all(p in index for p in ((start, start + 1), (start + 1, start + 2), (start, start + 2))):
            row = np.zeros(unknowns)
            row[start:start + 2] = a1 - 1.0
            rows.append(row)
            loops.append([index[(start, start + 2)], index[(start, start + 1)], index[(start + 1, start + 2)]])
    for start in range(n_dates - 3):
        if all(p in index for p in ((start, start + 1), (start + 1, start + 2), (start + 2, start + 3), (start, start + 3))):
            row = np.zeros(unknowns)
            row[start:start + 3] = a2 - 1.0
            rows.append(row)
            loops.append([index[(start, start + 3)], index[(start, start + 1)], index[(start + 1, start + 2)], index[(start + 2, start + 3)]])
    return np.array(rows), loops


def loop_offsets(phase, loops: list[list[int]], reference: Optional[np.ndarray]) -> np.ndarray:
    """The offset of each loop's closure over the reference pixels (zero when there are none), read layer by layer."""
    offsets = np.zeros(len(loops))
    if reference is None or not reference.any():
        return offsets
    for k, loop in enumerate(loops):
        layers = [np.asarray(phase[i], dtype="float64") for i in loop]
        ok = reference.copy()
        for layer in layers:
            ok &= layer != 0
        if ok.any():
            closure = _wrap(layers[0] - sum(layers[1:]))
            offsets[k] = np.angle(np.mean(np.exp(1j * closure[ok])))
    return offsets


def _correct_block(phase: np.ndarray, index: dict, loops, matrix: np.ndarray, offsets: np.ndarray,
                   n_dates: int, a1: float, a2: float) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """(corrected, bias, solved) for a block of rows of the stack, `phase` being interferograms x rows x columns."""
    shape = phase.shape[1:]
    solved = np.ones(shape, bool)
    closures = np.empty((matrix.shape[0], *shape), dtype="float64")
    for k, loop in enumerate(loops):
        first = phase[loop[0]].astype("float64")
        rest = sum(phase[i].astype("float64") for i in loop[1:])
        for i in loop:
            solved &= phase[i] != 0
        closures[k] = _wrap(first - rest - offsets[k])
    closures[:, ~solved] = 0.0
    step_bias = (np.linalg.pinv(matrix) @ closures.reshape(matrix.shape[0], -1)).reshape(n_dates - 1, *shape)
    step_bias[:, ~solved] = 0.0
    bias = np.zeros(phase.shape, dtype="float32")
    for (start, end), i in index.items():
        span = end - start
        if span == 1:
            bias[i] = step_bias[start]
        elif span == 2:
            bias[i] = a1 * step_bias[start:start + 2].sum(axis=0)
        elif span == 3:
            bias[i] = a2 * step_bias[start:start + 3].sum(axis=0)
    corrected = phase.astype("float32") - bias
    corrected[phase == 0] = 0.0
    return corrected, bias, solved, step_bias


def prepare(phase, pairs: Sequence[tuple[str, str]], used: Optional[Sequence[bool]] = None,
            valid: Optional[np.ndarray] = None, a1: Optional[float] = None, a2: Optional[float] = None,
            reference: Optional[np.ndarray] = None) -> dict:
    """Everything the correction needs that is global to the stack: the constants, the loops, the equations and
    the offset of each loop over the reference pixels. Raises FadingCorrectionError when it cannot be done."""
    dates, index = _index_of_pairs(pairs, used)
    if len(dates) < MIN_DATES:
        raise FadingCorrectionError(f"{len(dates)} dates are too few: the correction needs at least {MIN_DATES}.")
    info: dict = {}
    if a1 is None or a2 is None:
        a1, a2, info = estimate_ratios(phase, index, len(dates), valid, reference)
    matrix, loops = _equations(index, len(dates), a1, a2)
    if matrix.shape[0] < len(dates):
        raise FadingCorrectionError("Too few complete loops of two and three steps to solve for the biases.")
    return {"index": index, "loops": loops, "matrix": matrix, "offsets": loop_offsets(phase, loops, reference),
            "n_dates": len(dates), "a1": a1, "a2": a2, "info": info}


def correct(phase, pairs: Sequence[tuple[str, str]], used: Optional[Sequence[bool]] = None,
            valid: Optional[np.ndarray] = None, a1: Optional[float] = None, a2: Optional[float] = None,
            reference: Optional[np.ndarray] = None) -> FadingCorrection:
    """The stack `phase` (interferograms x rows x columns, radians, zero where there is no value) with the fading bias removed.

    `pairs` are the (first, second) dates 'YYYYMMDD' of each interferogram and `used` which of them the run keeps.
    `valid` limits the pixels the constants are measured on. `reference` is a boolean map of the pixels the closures are
    referenced to (see `_referenced`; `fading_bias.coherent_reference` gives it). Give `a1` and `a2` to skip measuring them."""
    phase = phase if hasattr(phase, "shape") else np.asarray(phase)
    plan = prepare(phase, pairs, used, valid, a1, a2, reference)
    corrected, bias, solved, step_bias = _correct_block(
        np.asarray(phase), plan["index"], plan["loops"], plan["matrix"], plan["offsets"], plan["n_dates"], plan["a1"], plan["a2"])
    return FadingCorrection(a1=plan["a1"], a2=plan["a2"], corrected=corrected, bias_rad=bias, solved=solved,
                            summary=_summary(plan, solved, step_bias))


def _summary(plan: dict, solved: np.ndarray, step_bias: np.ndarray) -> dict:
    return {
        "a1": plan["a1"], "a2": plan["a2"], "loops": len(plan["loops"]), "dates": plan["n_dates"],
        "pixels_corrected": int(solved.sum()), "pixels": int(solved.size),
        "median_abs_step_bias_rad": float(np.median(np.abs(step_bias[:, solved]))) if solved.any() else 0.0,
        **plan["info"],
    }


SUMMARY_FILE = "fading_correction.json"
OUTPUT_DATASET = "unwrapPhase_phaseClosure"      # the dataset MintPy's inversion reads when unwrapError.method is phase_closure
ROW_BLOCK = 128


def read_summary(work_dir) -> Optional[dict]:
    import json
    from pathlib import Path

    try:
        return json.loads((Path(work_dir) / SUMMARY_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def correct_stack(work_dir, source: str = "unwrapPhase") -> dict:
    """Remove the fading bias from the interferogram stack of a MintPy work folder.

    Reads `source` (the unwrapped phases, or the ones with their unwrapping errors corrected), writes the corrected
    phases to `unwrapPhase_phaseClosure` in `inputs/ifgramStack.h5`, takes the interferogram that fixed the constants out
    of the inversion (it only calibrates: a year-long interferogram aliases where the ground moves fast), and writes
    `fading_correction.json`. The stack is processed in blocks of rows, so a whole scene does not have to fit in memory.
    Returns the summary. Raises FadingCorrectionError when the network does not allow it."""
    import json
    from pathlib import Path

    import h5py

    from insar_core.analysis import fading_bias

    work_dir = Path(work_dir)
    path = work_dir / "inputs" / "ifgramStack.h5"
    try:
        handle = h5py.File(path, "r+")
    except OSError as exc:
        raise FadingCorrectionError(f"The interferogram stack cannot be read ({path.name}).") from exc
    with handle:
        if source not in handle or "coherence" not in handle:
            raise FadingCorrectionError("The interferogram stack holds no unwrapped phase or no coherence.")
        pairs = [(a.decode(), b.decode()) for a, b in handle["date"][:]]
        used = handle["dropIfgram"][:].astype(bool)
        phase = handle[source]
        coherence = fading_bias._mean_of_used(handle["coherence"], used)
        in_every = fading_bias._has_phase(phase, [i for i, u in enumerate(used) if u])
        reference = fading_bias.coherent_reference(coherence, in_every)
        mask_file = work_dir / "maskTempCoh.h5"
        valid = None
        if mask_file.is_file():
            with h5py.File(mask_file, "r") as mask_handle:
                valid = mask_handle["mask"][:].astype(bool)
        plan = prepare(phase, pairs, used, valid, reference=reference)
        if OUTPUT_DATASET in handle and OUTPUT_DATASET != source:
            del handle[OUTPUT_DATASET]
        out = handle[OUTPUT_DATASET] if OUTPUT_DATASET in handle else handle.create_dataset(
            OUTPUT_DATASET, shape=phase.shape, dtype="float32", chunks=phase.chunks, compression=phase.compression)
        solved_total, step_values = 0, []
        n_pixels = int(phase.shape[1] * phase.shape[2])
        length = phase.shape[1]
        for first in range(0, length, ROW_BLOCK):
            block = phase[:, first:first + ROW_BLOCK, :]
            corrected, _, solved, step_bias = _correct_block(
                block, plan["index"], plan["loops"], plan["matrix"], plan["offsets"], plan["n_dates"], plan["a1"], plan["a2"])
            out[:, first:first + ROW_BLOCK, :] = corrected
            solved_total += int(solved.sum())
            if solved.any():
                step_values.append(np.abs(step_bias[:, solved]).ravel()[::7])
        drop = handle["dropIfgram"]
        flags = drop[:]
        flags[plan["info"]["long_pair"]] = False
        drop[:] = flags
    sampled = np.concatenate(step_values) if step_values else np.zeros(1)
    summary = {
        "a1": plan["a1"], "a2": plan["a2"], "loops": len(plan["loops"]), "dates": plan["n_dates"],
        "pixels_corrected": solved_total, "pixels": n_pixels,
        "median_abs_step_bias_rad": float(np.median(sampled)), "calibration_pair": list(pairs[plan["info"]["long_pair"]]),
        "ratio_pixels": plan["info"]["ratio_pixels"], "citations": list(CITATIONS),
    }
    (work_dir / SUMMARY_FILE).write_text(json.dumps(summary), encoding="utf-8")
    return summary
