"""A digital elevation model for an area, from the Copernicus GLO-30 tiles on AWS (no login needed).

HyP3 puts a DEM inside every interferogram it returns. Products that do not (NISAR's GUNW) need one
for MintPy, which uses the heights to place the geometry and to estimate DEM errors. GLO-30 is the
30 m Copernicus DEM, published as cloud optimised GeoTIFF tiles of one degree, read here by range
requests so only the pixels inside the area are transferred.

The heights are above the EGM2008 geoid, not the ellipsoid. For MintPy that is immaterial: it needs
relative heights (the difference is a smooth surface, about 50 m in Spain, that the DEM error
estimate and the height-dependent corrections absorb).
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Iterable

BUCKET = "https://copernicus-dem-30m.s3.amazonaws.com"
Bbox = tuple[float, float, float, float]      # min_lon, min_lat, max_lon, max_lat


class DEMError(RuntimeError):
    """The DEM could not be built (no tile covers the area, or the tiles cannot be read)."""


def tile_url(lat: int, lon: int) -> str:
    """URL of the tile whose lower left corner is (lat, lon), both whole degrees."""
    name = f"Copernicus_DSM_COG_10_{'N' if lat >= 0 else 'S'}{abs(lat):02d}_00_{'E' if lon >= 0 else 'W'}{abs(lon):03d}_00_DEM"
    return f"{BUCKET}/{name}/{name}.tif"


def tiles_for(bbox: Bbox) -> list[tuple[int, int]]:
    min_lon, min_lat, max_lon, max_lat = bbox
    return [
        (lat, lon)
        for lat in range(math.floor(min_lat), math.ceil(max_lat))
        for lon in range(math.floor(min_lon), math.ceil(max_lon))
    ]


def copernicus_dem(bbox: Bbox, dest: Path, urls: Iterable[str] | None = None) -> Path:
    """Write the DEM of `bbox` (WGS84 degrees) to `dest` as a GeoTIFF and return it.

    `urls` replaces the tile list, for tests. A tile that does not exist (open sea) is skipped; if
    none exists the area has no land and an error says so. An existing `dest` is kept, because the
    DEM of an area does not change."""
    import rasterio
    from rasterio.errors import RasterioIOError
    from rasterio.merge import merge

    dest = Path(dest)
    if dest.is_file() and dest.stat().st_size > 0:
        return dest
    if not (bbox[0] < bbox[2] and bbox[1] < bbox[3]):
        raise DEMError(f"The area {bbox} is empty")
    candidates = list(urls) if urls is not None else [tile_url(lat, lon) for lat, lon in tiles_for(bbox)]
    opened = []
    try:
        for url in candidates:
            try:
                opened.append(rasterio.open(url))
            except RasterioIOError:
                continue
        if not opened:
            raise DEMError("No Copernicus DEM tile covers this area (open sea, or the tile server cannot be reached).")
        try:
            # Above 50 degrees of latitude the tiles are coarser in longitude (1.5 times at 50 to 60,
            # twice at 60 to 70, and so on). An area across one of those lines has tiles of two
            # resolutions, and merge would take the first one's: name the finest instead.
            finest = (min(abs(d.res[0]) for d in opened), min(abs(d.res[1]) for d in opened))
            data, transform = merge(opened, bounds=bbox, res=finest)
        except Exception as exc:
            raise DEMError(f"Could not read the DEM tiles: {exc}") from exc
        profile = {
            "driver": "GTiff", "height": data.shape[1], "width": data.shape[2], "count": 1, "dtype": "float32",
            "crs": opened[0].crs, "transform": transform, "compress": "deflate", "tiled": True,
            "blockxsize": 256, "blockysize": 256,
        }
        dest.parent.mkdir(parents=True, exist_ok=True)
        draft = dest.with_suffix(".partial.tif")
        with rasterio.open(draft, "w", **profile) as out:
            out.write(data.astype("float32"))
        draft.replace(dest)
        return dest
    finally:
        for dataset in opened:
            dataset.close()
