"""Fading bias: the error that short interferograms add to a velocity, measured from closure phases.

What it is. The phases of three interferograms that form a loop (dates 1-2, 2-3 and 1-3) should
add up to zero. For a single scatterer they do. For a multilooked pixel they do not, because the
average over the looks is taken before the phase and the scatterers inside the pixel change in
different ways (soil moisture, vegetation). The part that does not close is small in each
interferogram, but it is systematic: it has the same sign date after date, it is largest in the
shortest interferograms and it fades as the interval grows. A time series built from short
interferograms adds it up, and the result is a velocity that is off by millimetres per year over
vegetated or agricultural ground while looking perfectly clean (De Zan et al., 2015; Ansari et
al., 2021; Zheng et al., 2022).

What is measured here. For a connection level n, the sequential closure phase of date k is

    closure_k = phase(k, k+1) + phase(k+1, k+2) + ... + phase(k+n-1, k+n) - phase(k, k+n)

(Zheng et al., 2022, equation 21): what the chain of shortest interferograms accumulates over
n steps, in excess of the one interferogram that spans them. Its average over the series, divided
by the time the loop spans, is a rate: how much faster the shortest interferograms make the
ground appear to move than the interferograms n steps long do. That rate is reported per pixel
in mm/year (`velocity_bias_mm_yr`).

How to read it.

* It is a lower bound of the bias of a velocity made from the shortest interferograms, not the
  bias itself: the n-step interferograms it is measured against are biased too, only less. With
  interferograms of at most 36 days (three 12-day steps) a good part of the bias is still in the
  reference. The bias of the app's usual network, which mixes 1, 2 and 3 steps, is smaller than
  the figure for the shortest ones alone, and of the same sign.
* It needs every interferogram of the loop. The network "each date with the next N" has them all
  up to N steps.
* The closures are referenced first. The unwrapped phase of a HyP3 interferogram carries a
  constant of its own (measured on real stacks: the closure of the raw phases is uniform noise),
  so without a common reference the loops do not close anywhere. The reference is the set of the
  most coherent pixels of the area (the top 5%), not one pixel: buildings and rock keep their
  phase and fade least, and the average of many pixels does not pass the noise of one on to all
  the others. The result is the bias relative to those pixels.
* A closure is noisy, and the rate divides it by a short time: with 100 loops of 19 days a noise
  of 0.2 rad per closure is 2 mm/year in the rate of one pixel. Every pixel therefore gets an
  uncertainty from the scatter of its own closures, and only what stands three of them out is
  called bias. The summary states that noise floor: a bias below it cannot be seen with this
  network, which is not the same as there being none.
* An unwrapping error adds a whole number of cycles to a closure and is removed by wrapping the
  closure, so it does not count as bias here.

The cure is not in this module. `fading_correction.py` removes it from the short interferograms
following Maghsoudi et al. (2022), with the caveat in its docstring; longer interferograms, which
the pair planner can add, reduce it but lose fast motion; and estimating the phases from the full
set of interferograms of every pixel (phase linking) is what the literature calls insensitive to
it, which this library does not do.

References
    De Zan, F., Zonno, M., and Lopez-Dekker, P. (2015), Phase inconsistencies and multiple
        scattering in SAR interferometry, IEEE Trans. Geosci. Remote Sens., 53(12), 6608-6616.
    Ansari, H., De Zan, F., and Parizzi, A. (2021), Study of systematic bias in measuring surface
        deformation with SAR interferometry, IEEE Trans. Geosci. Remote Sens., 59(2), 1285-1301.
    Zheng, Y., Fattahi, H., Agram, P., Simons, M., and Rosen, P. (2022), On closure phase and
        systematic bias in multilooked SAR interferometry, IEEE Trans. Geosci. Remote Sens., 60, 5226611.
    Maghsoudi, Y., Hooper, A. J., Wright, T. J., Lazecky, M., and Ansari, H. (2022), Characterizing
        and correcting phase biases in short-term, multilooked interferograms, Remote Sensing of
        Environment, 275, 113022.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

import h5py
import numpy as np

CITATIONS = (
    "Ansari, H., De Zan, F., and Parizzi, A. (2021), Study of systematic bias in measuring surface deformation with SAR "
    "interferometry, IEEE Trans. Geosci. Remote Sens., 59(2), 1285-1301",
    "Zheng, Y., Fattahi, H., Agram, P., Simons, M., and Rosen, P. (2022), On closure phase and systematic bias in multilooked "
    "SAR interferometry, IEEE Trans. Geosci. Remote Sens., 60, 5226611",
    "Maghsoudi, Y., Hooper, A. J., Wright, T. J., Lazecky, M., and Ansari, H. (2022), Characterizing and correcting phase "
    "biases in short-term, multilooked interferograms, Remote Sensing of Environment, 275, 113022",
)

MIN_LOOPS = 5                 # fewer closures than this do not make an average worth reading
# A pixel is called biased when its average closure stands this many of its own standard errors
# away from zero. By chance alone about 0.3% of the pixels do.
NUM_SIGMA = 3.0
REFERENCE_PERCENTILE = 95.0   # the closures are referenced to the pixels above this percentile of mean coherence
MIN_REFERENCE_PIXELS = 20
# Rules of thumb for the check.
BIASED_FRACTION_WARN = 0.05   # share of the kept pixels with a significant bias
BIASED_FRACTION_FAIL = 0.25
BIAS_FAIL_MM_YR = 3.0         # and their median size, for a failure
AREA_BIAS_WARN_MM_YR = 1.0    # median over the area, signed: a bias the whole area shares


class FadingBiasError(ValueError):
    """The stack cannot give a closure phase, and the message says why."""


@dataclass
class FadingBias:
    """The closure phase of a stack at one connection level, and the velocity bias it implies."""
    connection: int
    loops: int
    span_days: float                       # mean time one loop spans
    wavelength_m: float
    mean_closure_rad: np.ndarray           # rows x columns, circular mean of the closures
    consistency: np.ndarray                # rows x columns, 0 to 1: 1 when every loop gives the same closure
    velocity_bias_mm_yr: np.ndarray        # rows x columns, positive towards the satellite
    sigma_mm_yr: np.ndarray                # rows x columns, standard error of the bias from the scatter of the closures
    biased: np.ndarray                     # rows x columns, bool: the bias stands NUM_SIGMA errors out
    valid: np.ndarray                      # rows x columns, bool: pixels with phase in every loop
    summary: dict = field(default_factory=dict)


def closure_loops(pairs: Sequence[tuple[str, str]], connection: int, used: Optional[Sequence[bool]] = None) -> list[list[int]]:
    """Indices into `pairs` of the interferograms of each sequential loop of `connection` steps.

    Each loop is the `connection` shortest interferograms of consecutive dates followed by the one
    that spans them. A loop is left out when one of its interferograms is missing or not used."""
    if connection < 2:
        raise FadingBiasError("A closure loop needs a connection level of at least 2")
    keep = [True] * len(pairs) if used is None else [bool(u) for u in used]
    index = {pair: i for i, pair in enumerate(pairs) if keep[i]}
    dates = sorted({d for pair in index for d in pair})
    loops = []
    for start in range(len(dates) - connection):
        wanted = [(dates[start + j], dates[start + j + 1]) for j in range(connection)] + [(dates[start], dates[start + connection])]
        if all(pair in index for pair in wanted):
            loops.append([index[pair] for pair in wanted])
    return loops


def largest_connection(pairs: Sequence[tuple[str, str]], used: Optional[Sequence[bool]] = None, limit: int = 30) -> int:
    """The highest connection level with at least MIN_LOOPS loops, or 0 when there is none."""
    best = 0
    for connection in range(2, limit + 1):
        if len(closure_loops(pairs, connection, used)) >= MIN_LOOPS:
            best = connection
        elif best:
            break
    return best


def _days(first: str, second: str) -> int:
    return (dt.datetime.strptime(second, "%Y%m%d") - dt.datetime.strptime(first, "%Y%m%d")).days


def _layer(phase, index: int) -> np.ndarray:
    """One interferogram, read from memory or, for an HDF5 dataset, from disk (so the stack never has to fit in memory)."""
    return np.asarray(phase[index])


def _has_phase(phase, indices) -> np.ndarray:
    """Pixels that have a finite, non-zero phase in every interferogram of `indices`."""
    valid = np.ones(phase.shape[1:], bool)
    for index in indices:
        layer = _layer(phase, index)
        valid &= (layer != 0) & np.isfinite(layer)
    return valid


def _closure_statistics(phase: np.ndarray, pairs, loops: list[list[int]], reference: Optional[np.ndarray]):
    """Circular mean, consistency and standard error of the mean closure of each pixel, and the loops' mean span."""
    total = np.zeros(phase.shape[1:], np.complex128)
    spans = []
    for loop in loops:
        closure = sum(_layer(phase, i).astype(np.float64) for i in loop[:-1]) - _layer(phase, loop[-1])
        unit = np.exp(1j * closure)
        if reference is not None:
            offset = unit[reference].mean()
            unit = unit * np.conj(offset / abs(offset)) if abs(offset) > 0 else unit
        total += unit
        spans.append(_days(*pairs[loop[-1]]))
    average = total / len(loops)
    consistency = np.abs(average)
    # Circular standard deviation of one closure, sqrt(-2 ln R), and the error of the mean of the loops.
    # Consecutive loops are not independent: loop k and loop k+1 share all but one of their short
    # interferograms. Only one loop in `connection` is new information, so that is the number of
    # loops the error is divided by. (Counting them all called 9% of the pixels of a city biased
    # where two halves of the series did not agree on a single one.)
    connection = len(loops[0]) - 1
    independent = max(len(loops) / connection, 1.0)
    spread = np.sqrt(-2.0 * np.log(np.clip(consistency, 1e-6, 1.0)))
    return np.angle(average), consistency, spread / np.sqrt(independent), float(np.mean(spans))


def estimate_from_phases(
    phase: np.ndarray, pairs: Sequence[tuple[str, str]], wavelength_m: float, reference: Optional[np.ndarray],
    connection: Optional[int] = None, used: Optional[Sequence[bool]] = None, mask: Optional[np.ndarray] = None,
    coherence: Optional[np.ndarray] = None,
) -> FadingBias:
    """The fading bias of a stack of unwrapped (or wrapped) phases.

    `phase` is interferograms x rows x columns in radians, zero where there is no value, `pairs`
    the (first date, second date) of each as 'YYYYMMDD'. `reference` is a boolean map of the
    pixels the closures are referenced to, or None when the phases already share a reference
    (wrapped phases straight from the SLCs do). `connection` is the level to measure at (the
    largest the network allows when left out) and `mask` limits the summary to the pixels a
    result keeps. `coherence` (rows x columns, the mean of each pixel) adds the bias of the least
    and of the most coherent third of the pixels to the summary: fading is a property of ground
    that changes, so that is where it shows."""
    if not hasattr(phase, "shape"):
        phase = np.asarray(phase)
    if connection is None:
        connection = largest_connection(pairs, used)
        if not connection:
            raise FadingBiasError(
                "The network has no closed loops: each date must be paired with the next two or more, and every one of "
                "those interferograms must be there.")
    loops = closure_loops(pairs, connection, used)
    if len(loops) < MIN_LOOPS:
        raise FadingBiasError(f"Only {len(loops)} loops of {connection} steps are complete; at least {MIN_LOOPS} are needed.")

    needed = sorted({i for loop in loops for i in loop})
    valid = _has_phase(phase, needed)
    if reference is not None:
        reference = np.asarray(reference, bool) & valid
        if int(reference.sum()) < 1:
            raise FadingBiasError("None of the reference pixels has phase in every interferogram of the loops, so the "
                                  "interferograms cannot be put on a common reference.")
    mean_closure, consistency, error, span_days = _closure_statistics(phase, pairs, loops, reference)
    # phase to displacement towards the satellite: -wavelength / (4 pi), as MintPy does
    to_rate = wavelength_m / (4.0 * np.pi) / (span_days / 365.25) * 1000.0
    velocity = -to_rate * mean_closure
    sigma = to_rate * error
    biased = valid & (np.abs(velocity) > NUM_SIGMA * sigma)
    for array in (mean_closure, consistency, velocity, sigma):
        array[~valid] = np.nan

    kept = valid & (np.asarray(mask, bool) if mask is not None else True)
    values = velocity[kept]
    summary: dict = {
        "connection": connection, "loops": len(loops), "span_days": span_days, "pixels": int(kept.sum()),
        "reference_pixels": int(reference.sum()) if reference is not None else None,
    }
    if values.size:
        significant = kept & biased
        summary.update({
            "median_mm_yr": float(np.median(values)),
            "p05_mm_yr": float(np.percentile(values, 5)), "p95_mm_yr": float(np.percentile(values, 95)),
            "noise_floor_mm_yr": float(np.median(sigma[kept])),
            "biased_fraction": float(biased[kept].mean()),
            "biased_median_abs_mm_yr": float(np.median(np.abs(velocity[significant]))) if significant.any() else 0.0,
        })
        if coherence is not None:
            level = np.asarray(coherence, float)
            low, high = np.percentile(level[kept], [100.0 / 3.0, 200.0 / 3.0])
            summary["low_coherence_median_mm_yr"] = float(np.median(velocity[kept & (level <= low)]))
            summary["high_coherence_median_mm_yr"] = float(np.median(velocity[kept & (level >= high)]))
    return FadingBias(connection=connection, loops=len(loops), span_days=span_days, wavelength_m=wavelength_m,
                      mean_closure_rad=mean_closure.astype("float32"), consistency=consistency.astype("float32"),
                      velocity_bias_mm_yr=velocity.astype("float32"), sigma_mm_yr=sigma.astype("float32"),
                      biased=biased, valid=valid, summary=summary)


def coherent_reference(coherence: np.ndarray, valid: Optional[np.ndarray] = None) -> np.ndarray:
    """The most coherent pixels of the area, as a boolean map: what the closures are referenced to.

    `coherence` is the mean coherence of each pixel (rows x columns). The pixels above the
    REFERENCE_PERCENTILE of the valid ones are taken, and never fewer than MIN_REFERENCE_PIXELS."""
    coherence = np.asarray(coherence, float)
    usable = np.isfinite(coherence) & (coherence > 0) & (np.asarray(valid, bool) if valid is not None else True)
    if not usable.any():
        return usable
    cut = np.percentile(coherence[usable], REFERENCE_PERCENTILE)
    chosen = usable & (coherence >= cut)
    if int(chosen.sum()) < MIN_REFERENCE_PIXELS:
        order = np.sort(coherence[usable])[::-1]
        chosen = usable & (coherence >= order[min(MIN_REFERENCE_PIXELS, order.size) - 1])
    return chosen


def estimate(work_dir, connection: Optional[int] = None) -> FadingBias:
    """The fading bias of the MintPy run in `work_dir`, from its interferogram stack.

    Uses the interferograms MintPy kept, the most coherent pixels of the stack as the reference
    and, for the summary, the pixels the run's temporal coherence mask keeps."""
    work_dir = Path(work_dir)
    path = work_dir / "inputs" / "ifgramStack.h5"
    try:
        with h5py.File(path, "r") as handle:
            pairs = [(a.decode(), b.decode()) for a, b in handle["date"][:]]
            used = handle["dropIfgram"][:].astype(bool)
            wavelength = float(handle.attrs["WAVELENGTH"])
            if "unwrapPhase" not in handle:
                raise FadingBiasError("The interferogram stack holds no unwrapped phase")
            phase = handle["unwrapPhase"]
            # Layer by layer: a whole scene's stack is several GB and would not have to be in memory at once.
            coherence = _mean_of_used(handle["coherence"], used) if "coherence" in handle else None
            if coherence is None:
                raise FadingBiasError("The interferogram stack holds no coherence, which is needed to choose the reference pixels")
            in_every = _has_phase(phase, [i for i, u in enumerate(used) if u])
            mask = None
            mask_file = work_dir / "maskTempCoh.h5"
            if mask_file.is_file():
                with h5py.File(mask_file, "r") as mask_handle:
                    mask = mask_handle["mask"][:].astype(bool)
            return estimate_from_phases(phase, pairs, wavelength, coherent_reference(coherence, in_every),
                                        connection=connection, used=used, mask=mask, coherence=coherence)
    except (OSError, KeyError) as exc:
        raise FadingBiasError(f"The interferogram stack of this run cannot be read ({path.name}): run MintPy first") from exc


def _mean_of_used(dataset, used) -> np.ndarray:
    kept = [i for i, u in enumerate(used) if u]
    total = np.zeros(dataset.shape[1:], np.float64)
    for index in kept:
        total += np.asarray(dataset[index], dtype=np.float64)
    return total / max(len(kept), 1)


def check(result: FadingBias) -> dict:
    """The result as one line for the quality checks: fine, look at this, or a problem."""
    summary = result.summary
    label = "Fading bias of short interferograms"
    if not summary.get("pixels"):
        return {"id": "fading_bias", "label": label, "value": None, "unit": "mm/year",
                "status": "warn", "message": "No pixel has phase in every loop, so the bias could not be measured."}
    fraction, size = summary["biased_fraction"], summary["biased_median_abs_mm_yr"]
    area, floor = summary["median_mm_yr"], summary["noise_floor_mm_yr"]
    if fraction > BIASED_FRACTION_FAIL and size > BIAS_FAIL_MM_YR:
        status = "fail"
    elif fraction > BIASED_FRACTION_WARN or abs(area) > AREA_BIAS_WARN_MM_YR or abs(summary.get("low_coherence_median_mm_yr", 0.0)) > BIAS_FAIL_MM_YR:
        status = "warn"
    else:
        status = "ok"
    measured = (f"Over the whole area the shortest interferograms differ from the {result.span_days:.0f}-day ones by "
                f"{area:+.1f} mm/year (median)")
    if "low_coherence_median_mm_yr" in summary:
        measured += (f": {summary['low_coherence_median_mm_yr']:+.1f} in the least coherent third of the pixels and "
                     f"{summary['high_coherence_median_mm_yr']:+.1f} in the most coherent third")
    measured += f". {fraction:.1%} of the pixels show a bias of their own" + (f", of {size:.1f} mm/year" if fraction >= 0.01 else "") + "."

    advice = {
        "ok": " The velocity is not driven by short-interval effects that this network can see.",
        "warn": " Where the bias map shows it, a velocity of that size should not be read as ground motion. The option "
                "to correct the fading bias can remove it where the ground is slow; longer pairs help too but lose fast motion.",
        "fail": " The bias is widespread and of the size of a real signal. Try the option to correct the fading bias, and compare "
                "the result with an independent source.",
    }[status]
    limit = (f" One pixel is measured to {floor:.1f} mm/year, so a smaller bias goes unseen, and the figure is a lower bound: "
             "the longer interferograms it is measured against are biased too.")
    return {"id": "fading_bias", "label": label, "value": round(fraction, 3), "unit": "fraction of pixels", "status": status,
            "message": measured + advice + limit}


def write_geotiff(result: FadingBias, geo: dict, dest) -> Path:
    """The bias map (mm/year) as a cloud optimised GeoTIFF, with its standard error as a second band."""
    import pyproj
    import rasterio
    import rasterio.shutil
    from rasterio.transform import Affine

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    draft = dest.with_suffix(".draft.tif")
    transform = Affine(geo["x_step"], 0.0, geo["x_first"], 0.0, geo["y_step"], geo["y_first"])
    with rasterio.open(draft, "w", driver="GTiff", height=geo["length"], width=geo["width"], count=2, dtype="float32",
                       crs=pyproj.CRS.from_epsg(geo["epsg"]), transform=transform, nodata=float("nan")) as out:
        out.write(result.velocity_bias_mm_yr, 1)
        out.write(result.sigma_mm_yr, 2)
        out.set_band_description(1, "fading_bias_mm_yr")
        out.set_band_description(2, "fading_bias_standard_error_mm_yr")
        out.update_tags(
            units="mm/year", connection=str(result.connection), loops=str(result.loops), loop_span_days=f"{result.span_days:.1f}",
            description="Velocity the shortest interferograms add relative to the ones that span the loop; a lower bound of the fading bias",
            sign_convention="positive values move towards the satellite", reference="relative to the most coherent pixels of the area")
    try:
        rasterio.shutil.copy(draft, dest, driver="COG", compress="DEFLATE", overview_resampling="average")
    finally:
        draft.unlink(missing_ok=True)
    return dest
