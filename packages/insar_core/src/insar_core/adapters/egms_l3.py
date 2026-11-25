"""Read the EGMS Level 3 products: the vertical and east-west mean velocity on a 100 m grid.

EGMS L3 comes in 100 km tiles named after the south-west corner in the ETRS89-LAEA grid (EPSG:3035). Each
download is a zip with a CSV (every point and its time series, hundreds of megabytes), a small GeoTIFF with the mean
velocity in mm/year on the 100 m grid, and an XML. Only the GeoTIFF is read here. The velocity is calibrated to GNSS
in the European reference frame, so plate motion is already out of it, and -9999 marks a cell without data.
"""
from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path
from typing import Iterable

import numpy as np
import pyproj

NODATA = -9999.0


class EGMSL3Error(RuntimeError):
    """A tile that cannot be read, or an area the tiles do not cover."""


def _tiffs(paths: Iterable, workdir: Path) -> list[Path]:
    found: list[Path] = []
    for path in map(Path, paths):
        if path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as archive:
                members = [m for m in archive.namelist() if m.lower().endswith((".tif", ".tiff"))]
                if not members:
                    raise EGMSL3Error(f"{path.name} holds no GeoTIFF")
                for member in members:
                    # the file name only: a zip member's own path is never trusted
                    target = workdir / Path(member).name
                    target.write_bytes(archive.read(member))
                    found.append(target)
        elif path.suffix.lower() in (".tif", ".tiff"):
            found.append(path)
        else:
            raise EGMSL3Error(f"{path.name} is neither a zip nor a GeoTIFF")
    if not found:
        raise EGMSL3Error("No EGMS L3 tile given")
    return found


def read_velocity(paths: Iterable, bbox: tuple[float, float, float, float]) -> tuple[np.ndarray, dict]:
    """Mean velocity (mm/year, NaN where there is none) over `bbox` (min_lon, min_lat, max_lon, max_lat), merged from
    the tiles in `paths` (zips or GeoTIFFs), and its grid as `{x_first, y_first, x_step, y_step, width, length, epsg}`."""
    import rasterio
    from rasterio.merge import merge

    to_laea = pyproj.Transformer.from_crs(4326, 3035, always_xy=True)
    corners = [to_laea.transform(lon, lat) for lon, lat in ((bbox[0], bbox[1]), (bbox[2], bbox[1]), (bbox[2], bbox[3]), (bbox[0], bbox[3]))]
    xs, ys = zip(*corners, strict=False)
    # snap to the 100 m grid so the result lines up with the tiles
    step = 100.0
    bounds = (np.floor(min(xs) / step) * step, np.floor(min(ys) / step) * step,
              np.ceil(max(xs) / step) * step, np.ceil(max(ys) / step) * step)
    with tempfile.TemporaryDirectory(prefix="egms-l3-") as tmp:
        tiffs = _tiffs(paths, Path(tmp))
        sources = [rasterio.open(t) for t in tiffs]
        try:
            for source in sources:
                if source.crs is None or source.crs.to_epsg() != 3035:
                    raise EGMSL3Error(f"{Path(source.name).name} is not in ETRS89-LAEA (EPSG:3035)")
            data, transform = merge(sources, bounds=bounds, nodata=NODATA, res=step)
        finally:
            for source in sources:
                source.close()
    velocity = data[0].astype("float64")
    velocity[velocity == NODATA] = np.nan
    if not np.isfinite(velocity).any():
        raise EGMSL3Error("The EGMS L3 tiles have no value inside this area")
    geo = {"x_first": float(transform.c), "y_first": float(transform.f), "x_step": float(transform.a), "y_step": float(transform.e),
           "width": int(velocity.shape[1]), "length": int(velocity.shape[0]), "epsg": 3035}
    return velocity, geo
