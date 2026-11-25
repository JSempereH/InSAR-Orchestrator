"""The motion of the tectonic plates, to take it out of a GNSS velocity.

A GNSS velocity in a global frame (IGS14, IGS20) is mostly the motion of the plate the station
sits on: about 25 mm/year towards the north-east in Spain. InSAR does not see it, because every
pixel of a small area shares it and InSAR is relative to a pixel of that same area. To compare
the two, or to tie one to the other, the plate has to come out of the GNSS velocity.

The plate motion is a rigid rotation about an axis through the centre of the Earth, so three
numbers per plate give its velocity anywhere. The numbers here are the ITRF2014 plate motion
model (Altamimi et al., 2017), the rotation of 11 plates in the ITRF2014 frame. IGS14 is aligned
to ITRF2014, and IGS20 (ITRF2020) differs from it by a few tenths of a millimetre per year, well
under the error of a station velocity over a few years.

What this does not do: the model's small translation rate of the origin (under 1 mm/year) is not
applied, and a station inside a deforming plate boundary (most of the Mediterranean coast is a
few mm/year off rigid Eurasia or Nubia) keeps that difference, which is real motion of the ground
relative to the plate and is what a ground motion map should show.
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np

CITATION = ("Altamimi, Z., Metivier, L., Rebischung, P., Rouby, H., and Collilieux, X. (2017), ITRF2014 plate motion model, "
            "Geophys. J. Int., 209(3), 1906-1912")

# Rotation rate about the X, Y and Z axes in milliarcseconds per year (Altamimi et al., 2017, table 1).
PLATES_MAS_YR: dict[str, tuple[float, float, float]] = {
    "ANTA": (-0.248, -0.324, 0.675),     # Antarctica
    "ARAB": (1.154, -0.136, 1.444),      # Arabia
    "AUST": (1.510, 1.182, 1.215),       # Australia
    "EURA": (-0.085, -0.531, 0.770),     # Eurasia
    "INDI": (1.154, -0.005, 1.454),      # India
    "NAZC": (-0.333, -1.544, 1.623),     # Nazca
    "NOAM": (0.024, -0.694, -0.063),     # North America
    "NUBI": (0.099, -0.614, 0.733),      # Nubia (Africa west of the rift)
    "PCFC": (-0.409, 1.047, -2.169),     # Pacific
    "SOAM": (-0.270, -0.301, -0.140),    # South America
    "SOMA": (-0.121, -0.794, 0.884),     # Somalia (Africa east of the rift)
}
PLATE_NAMES = {
    "ANTA": "Antarctica", "ARAB": "Arabia", "AUST": "Australia", "EURA": "Eurasia", "INDI": "India", "NAZC": "Nazca",
    "NOAM": "North America", "NUBI": "Nubia", "PCFC": "Pacific", "SOAM": "South America", "SOMA": "Somalia",
}
_MAS_TO_RAD = math.pi / (180.0 * 3600.0 * 1000.0)
_A, _F = 6378137.0, 1.0 / 298.257222101          # GRS80 ellipsoid
# A station on a rigid plate agrees with the model to a millimetre or two per year. Beyond this the
# station is not on the rigid part of any plate of the model (a plate boundary, or a plate it lacks).
RIGID_PLATE_LIMIT_MM_YR = 5.0


def plate_velocity(plate: str, lat: float, lon: float) -> tuple[float, float]:
    """East and north velocity (mm/year) of `plate` at a point, in the ITRF2014 frame."""
    if plate not in PLATES_MAS_YR:
        raise ValueError(f"Unknown plate '{plate}'. Known: {', '.join(sorted(PLATES_MAS_YR))}")
    phi, lam = math.radians(lat), math.radians(lon)
    e2 = _F * (2.0 - _F)
    n = _A / math.sqrt(1.0 - e2 * math.sin(phi) ** 2)
    position = np.array([n * math.cos(phi) * math.cos(lam), n * math.cos(phi) * math.sin(lam), n * (1.0 - e2) * math.sin(phi)])
    omega = np.array(PLATES_MAS_YR[plate]) * _MAS_TO_RAD
    velocity = np.cross(omega, position) * 1000.0                      # metres to millimetres
    east = -math.sin(lam) * velocity[0] + math.cos(lam) * velocity[1]
    north = (-math.sin(phi) * math.cos(lam) * velocity[0] - math.sin(phi) * math.sin(lam) * velocity[1]
             + math.cos(phi) * velocity[2])
    return float(east), float(north)


def best_plate(stations: list[dict]) -> Optional[dict]:
    """The plate whose motion is closest to the horizontal velocity of `stations`.

    Each station is `{"lat", "lon", "east", "north"}` (mm/year, global frame). The plate is the one
    with the smallest median misfit over the stations, which needs no map of plate boundaries: the
    plates differ by tens of mm/year, so the right one stands out. Returns `{"plate", "name",
    "misfit_mm_yr", "rigid"}`, where `rigid` is False when even the best plate is more than
    RIGID_PLATE_LIMIT_MM_YR away, or None without stations."""
    if not stations:
        return None
    best: Optional[tuple[float, str]] = None
    for plate in PLATES_MAS_YR:
        misfits = []
        for s in stations:
            east, north = plate_velocity(plate, s["lat"], s["lon"])
            misfits.append(math.hypot(s["east"] - east, s["north"] - north))
        score = float(np.median(misfits))
        if best is None or score < best[0]:
            best = (score, plate)
    assert best is not None
    return {"plate": best[1], "name": PLATE_NAMES[best[1]], "misfit_mm_yr": best[0], "rigid": best[0] <= RIGID_PLATE_LIMIT_MM_YR}
