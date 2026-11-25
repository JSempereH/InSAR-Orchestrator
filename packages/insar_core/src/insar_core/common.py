"""Small pieces several modules need: dates as years, a robust spread, and reading MintPy's files."""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Optional, Sequence

import h5py
import numpy as np

MAD_TO_SIGMA = 1.4826          # a median absolute deviation times this is a standard deviation, for normal noise


def years_since_first(dates: Sequence[str]) -> np.ndarray:
    """Dates written YYYYMMDD as years after the first of them."""
    days = np.array([dt.datetime.strptime(d[:8], "%Y%m%d").toordinal() for d in dates], dtype=float)
    return (days - days[0]) / 365.25


def day_number(date: str) -> int:
    """Days from 1970-01-01 to a date written YYYYMMDD."""
    return (dt.datetime.strptime(date[:8], "%Y%m%d") - dt.datetime(1970, 1, 1)).days


def years_since_epoch(dates: Sequence[str]) -> np.ndarray:
    """Dates written YYYYMMDD as years after 1970 (a common clock for series from different sources)."""
    return np.array([day_number(d) for d in dates], dtype=float) / 365.25


DEGREE_M = 111_320.0           # metres in a degree of latitude


def pixel_metres(geo: dict) -> tuple[float, float]:
    """The size of a pixel in metres along x and y, for a grid described by `x_step`, `y_step`, `y_first`, `length`, `epsg`."""
    if geo["epsg"] == 4326:
        latitude = geo["y_first"] + geo["y_step"] * geo["length"] / 2.0
        return abs(geo["x_step"]) * DEGREE_M * float(np.cos(np.radians(latitude))), abs(geo["y_step"]) * DEGREE_M
    return abs(geo["x_step"]), abs(geo["y_step"])


def robust_std(values: np.ndarray, axis: Optional[int] = -1) -> np.ndarray:
    """A standard deviation that a minority of outliers does not move (from the median absolute deviation)."""
    centre = np.median(values, axis=axis, keepdims=axis is not None)
    return MAD_TO_SIGMA * np.median(np.abs(values - centre), axis=axis)


def read_dataset(path, name: Optional[str] = None) -> Optional[np.ndarray]:
    """The dataset `name` of an HDF5 file (its first one when not named), or None when it is not there."""
    try:
        with h5py.File(path, "r") as handle:
            return handle[name or next(iter(handle.keys()))][:]
    except (OSError, KeyError, StopIteration):
        return None


def read_stack_network(stack_file) -> tuple[list[tuple[str, str]], np.ndarray, Optional[np.ndarray]]:
    """The pairs of dates of a MintPy interferogram stack, which of them are in use, and their baselines.

    Raises OSError or KeyError when the file or its dates are not there. The perpendicular baselines
    are None when the stack does not carry them."""
    with h5py.File(Path(stack_file), "r") as handle:
        pairs = [(a.decode(), b.decode()) for a, b in handle["date"][:]]
        used = handle["dropIfgram"][:].astype(bool) if "dropIfgram" in handle else np.ones(len(pairs), dtype=bool)
        bperp = handle["bperp"][:].astype(float) if "bperp" in handle else None
    return pairs, used, bperp


class PixelCube:
    """A dates x rows x columns dataset of an HDF5 file, read only where it is asked for.

    Comparing a result with another source needs the series of a few thousand pixels, not of the
    whole scene (which is gigabytes). `cube[:, rows, columns]` (arrays, or a single row and column)
    reads one row of the grid at a time, so memory stays at one row of the cube whatever the number
    of pixels, and `shape` is read from the file. Values are multiplied by `scale` (metres to mm)."""

    def __init__(self, path, dataset: str = "timeseries", scale: float = 1.0):
        self.path, self.dataset, self.scale = Path(path), dataset, scale
        with h5py.File(self.path, "r") as handle:
            self.shape = tuple(handle[dataset].shape)

    def __getitem__(self, key):
        if not (isinstance(key, tuple) and len(key) == 3 and key[0] == slice(None)):
            raise IndexError("a PixelCube is read as cube[:, rows, columns]")
        _, rows, columns = key
        rows_arr, cols_arr = np.atleast_1d(rows).astype(int), np.atleast_1d(columns).astype(int)
        out = np.empty((self.shape[0], rows_arr.size), dtype="float32")
        with h5py.File(self.path, "r") as handle:
            data = handle[self.dataset]
            for row in np.unique(rows_arr):
                where = np.flatnonzero(rows_arr == row)
                out[:, where] = data[:, int(row), :][:, cols_arr[where]] * self.scale
        return out[:, 0] if np.isscalar(rows) or np.ndim(rows) == 0 else out
