"""The MintPy options a person may change from the app, and their validation.

MintPy has about 150 options. Most of them are best left alone, and a wrong value of
some of them makes a run fail late and with a message that does not say why. This
module lists the ones worth exposing, with the values each accepts, so the app can
show a form and the backend can refuse a bad value before anything starts.

Every option here is a `mintpy.*` key of smallbaselineApp.cfg (MintPy 1.6). A value
of "auto" leaves the choice to MintPy, which is what a run without options does.
"""
from __future__ import annotations

import math
from typing import Optional

# `engines`, when present, lists the engines (insar_core.engines) the option applies to; without it the
# option applies to all of them. The ionosphere option needs the split-spectrum layer a NISAR GUNW carries
# and does nothing for a HyP3 interferogram, so it is only offered where it can work.
#
# A key that starts with APP_PREFIX is a step this app makes itself, not an option of MintPy: it is checked
# here like the others and never written to smallbaselineApp.cfg (see `split_app_options`).
#
# kind: "choice" (one of `choices`), "count" (auto, no, or an integer of at least 1),
# "fraction" (auto or a number from 0 to 1) or "length" (auto, no, or a number above 0).
OPTIONS: list[dict] = [
    {
        "key": "mintpy.troposphericDelay.method", "kind": "choice", "group": "Corrections",
        "label": "Troposphere correction",
        "choices": ["auto", "pyaps", "height_correlation", "no"],
        "help": "pyaps uses ERA5 weather data, height_correlation fits the delay to the terrain height "
                "and needs no weather data, no leaves it out.",
    },
    {
        "key": "mintpy.ionosphericDelay.method", "kind": "choice", "group": "Corrections", "engines": ("nisar_gunw",),
        "label": "Ionosphere correction",
        "choices": ["auto", "split_spectrum", "no"],
        "help": "L-band is much more sensitive to the ionosphere than C-band. split_spectrum uses the ionosphere layer "
                "NASA includes in each NISAR interferogram, and is on by default for NISAR.",
    },
    {
        "key": "mintpy.deramp", "kind": "choice", "group": "Corrections",
        "label": "Remove a ramp",
        "choices": ["auto", "no", "linear", "quadratic"],
        "help": "Removes a plane or a surface from every date. It also removes real long-wavelength "
                "motion, so leave it off for an area that deforms over its whole extent.",
    },
    {
        "key": "mintpy.topographicResidual", "kind": "choice", "group": "Corrections",
        "label": "DEM error correction",
        "choices": ["auto", "yes", "no"],
        "help": "Estimates and removes the height error of the DEM.",
    },
    {
        "key": "mintpy.unwrapError.method", "kind": "choice", "group": "Network",
        "label": "Unwrapping error correction",
        "choices": ["auto", "phase_closure", "no"],
        "help": "phase_closure finds the pixels that are off by whole cycles from the triplets of interferograms "
                "that should close, and corrects the ones it can read without doubt. It needs a network where each "
                "date is joined to at least two others. auto uses it when the network allows it.",
    },
    {
        "key": "mintpy.network.tempBaseMax", "kind": "count", "group": "Network",
        "label": "Longest temporal baseline (days)",
        "help": "Interferograms spanning more days than this are dropped before the inversion.",
    },
    {
        "key": "mintpy.network.perpBaseMax", "kind": "length", "group": "Network",
        "label": "Longest perpendicular baseline (m)",
        "help": "Interferograms with a larger perpendicular baseline are dropped.",
    },
    {
        "key": "mintpy.network.connNumMax", "kind": "count", "group": "Network",
        "label": "Most neighbours per date",
        "help": "Keeps only this many interferograms for each acquisition.",
    },
    {
        "key": "mintpy.networkInversion.weightFunc", "kind": "choice", "group": "Inversion",
        "label": "Inversion weights",
        "choices": ["auto", "var", "fim", "coh", "no"],
        "help": "var weights by the phase variance from coherence, fim by the Fisher information, "
                "coh by the coherence, no does not weight.",
    },
    {
        "key": "mintpy.networkInversion.minTempCoh", "kind": "fraction", "group": "Inversion",
        "label": "Lowest temporal coherence",
        "help": "Pixels below this temporal coherence are masked out of the velocity map. Raise it for a "
                "cleaner map, lower it to keep more pixels.",
    },
]

APP_PREFIX = "app."
MASK_CLOSURES_KEY = "app.mask.unreadableClosures"
MASK_COHERENCE_KEY = "app.mask.minMeanCoherence"
FADING_KEY = "app.fading.correction"
OPTIONS += [
    {
        "key": FADING_KEY, "kind": "choice", "group": "Fading bias",
        "label": "Correct the fading bias of short interferograms",
        "choices": ["auto", "no", "yes"],
        "help": "Removes the phase that multilooking leaves in short interferograms (soil moisture, vegetation), which adds up "
                "to millimetres per year of false motion over crops and forest (Maghsoudi et al., 2022). It needs one "
                "interferogram that spans the whole period (a year, say) to fix its constants, and uses it for that only. "
                "`auto` is `no`: on the one basin checked against GNSS and EGMS (Lorca, a subsiding basin) it moved the "
                "velocity away from both, so check the result against an independent source before relying on it.",
    },
    {
        "key": MASK_CLOSURES_KEY, "kind": "choice", "group": "Mask",
        "label": "Drop pixels whose triplets do not close",
        "choices": ["auto", "yes", "no"],
        "help": "Leaves out of the result the pixels whose triplets of interferograms do not close and could not be "
                "corrected: their phase is noise, whatever their temporal coherence. On by default when the unwrapping "
                "error correction ran and the network has no long interferograms: with them, the pixels it would drop are "
                "constrained by the long pairs and come out as good as the rest. `yes` applies it anyway.",
    },
    {
        "key": MASK_COHERENCE_KEY, "kind": "fraction", "group": "Mask",
        "label": "Lowest mean spatial coherence",
        "help": "Also leaves out the pixels whose coherence, averaged over the interferograms, is under this. Not applied "
                "unless set: what counts as low depends on the ground.",
    },
]

_BY_KEY = {option["key"]: option for option in OPTIONS}
_REFERENCE_KEY = "mintpy.reference.lalo"


def _check_count(label: str, value: str) -> str:
    if value in ("auto", "no"):
        return value
    try:
        number = int(value)
    except ValueError:
        raise ValueError(f"{label}: '{value}' is not auto, no or a whole number") from None
    if number < 1:
        raise ValueError(f"{label}: must be at least 1")
    return str(number)


def _check_length(label: str, value: str) -> str:
    if value in ("auto", "no"):
        return value
    try:
        number = float(value)
    except ValueError:
        raise ValueError(f"{label}: '{value}' is not auto, no or a number") from None
    if not (math.isfinite(number) and number > 0):
        raise ValueError(f"{label}: must be above 0")
    return f"{number:g}"


def _check_fraction(label: str, value: str) -> str:
    if value == "auto":
        return value
    try:
        number = float(value)
    except ValueError:
        raise ValueError(f"{label}: '{value}' is not auto or a number from 0 to 1") from None
    if not 0.0 <= number <= 1.0:
        raise ValueError(f"{label}: must be between 0 and 1")
    return f"{number:g}"


def options_for(engine_id: Optional[str]) -> list[dict]:
    """The options that apply to this engine."""
    return [o for o in OPTIONS if "engines" not in o or (engine_id or "hyp3_gamma") in o["engines"]]


def validate_options(options: Optional[dict], engine_id: Optional[str] = None) -> dict[str, str]:
    """The options as MintPy writes them, or a ValueError that names the first bad one.

    Unknown keys are refused, so a typo is not silently ignored. A value of "auto"
    is dropped, since leaving an option out is the same thing.
    """
    checked: dict[str, str] = {}
    for key, raw in (options or {}).items():
        option = _BY_KEY.get(key)
        if option is None or option not in options_for(engine_id):
            raise ValueError(f"'{key}' is not an option the app lets you change")
        value = str(raw).strip()
        label = option["label"]
        if option["kind"] == "choice":
            if value not in option["choices"]:
                raise ValueError(f"{label}: '{value}' is not one of {', '.join(option['choices'])}")
        elif option["kind"] == "count":
            value = _check_count(label, value)
        elif option["kind"] == "length":
            value = _check_length(label, value)
        else:
            value = _check_fraction(label, value)
        if value != "auto":
            checked[key] = value
    return checked


def split_app_options(options: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """The options MintPy takes and the ones this app acts on itself."""
    mintpy = {k: v for k, v in options.items() if not k.startswith(APP_PREFIX)}
    return mintpy, {k: v for k, v in options.items() if k.startswith(APP_PREFIX)}


def reference_option(reference: Optional[dict]) -> dict[str, str]:
    """`{"lat": ..., "lon": ...}` as the MintPy option that fixes the reference pixel."""
    if not reference:
        return {}
    try:
        lat, lon = float(reference["lat"]), float(reference["lon"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("The reference point needs a latitude and a longitude") from None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise ValueError("The reference point is outside the valid latitude and longitude range")
    return {_REFERENCE_KEY: f"{lat:.6f},{lon:.6f}"}
