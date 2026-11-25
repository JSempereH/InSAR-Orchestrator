"""The DEM the pipeline builds from Copernicus tiles."""
import numpy as np
import pytest

from insar_core.pipeline import dem as dem_module


def test_tiles_of_two_resolutions_are_merged_on_the_finer_grid(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_origin

    def tile(name, west, north, x_res, width):
        path = tmp_path / name
        with rasterio.open(path, "w", driver="GTiff", height=100, width=width, count=1, dtype="float32", crs="EPSG:4326",
                           transform=from_origin(west, north, x_res, 0.01)) as out:
            out.write(np.full((1, 100, width), 500.0, "float32"))
        return str(path)

    south = tile("south.tif", 10.0, 50.0, 0.01, 100)                    # below 50 degrees: finer in longitude
    north = tile("north.tif", 10.0, 51.0, 0.015, 67)                    # above: 1.5 times coarser
    for order in ([south, north], [north, south]):
        dest = tmp_path / f"dem_{order[0][-9:-4]}.tif"
        dem_module.copernicus_dem((10.2, 49.8, 10.6, 50.2), dest, urls=order)
        with rasterio.open(dest) as merged:
            assert merged.res[0] == pytest.approx(0.01)                 # whatever tile comes first
            assert np.allclose(merged.read(1), 500.0)
