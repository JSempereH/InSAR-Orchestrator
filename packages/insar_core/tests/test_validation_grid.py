"""Comparison with a gridded velocity (EGMS L3) and the reader of its tiles."""
import zipfile

import numpy as np
import pyproj
import pytest

from insar_core.adapters import egms_l3
from insar_core.analysis import validation as val

# the map: 80 m pixels in UTM 30N over Lorca; the other source: 100 m cells in ETRS89-LAEA over the same ground
MAP = {"x_first": 607000.0, "y_first": 4177000.0, "x_step": 80.0, "y_step": -80.0, "width": 200, "length": 160, "epsg": 32630}
CENTRE = (MAP["x_first"] + 100 * 80.0, MAP["y_first"] - 80 * 80.0)


def _bowl(x, y, depth=-40.0, radius=3000.0):
    """Subsidence bowl in mm/year, as a function of UTM coordinates."""
    return depth * np.exp(-(((x - CENTRE[0]) ** 2 + (y - CENTRE[1]) ** 2) / radius ** 2))


def _map_velocity(offset=0.0, noise=0.0, seed=0):
    columns, rows = np.meshgrid(np.arange(MAP["width"]), np.arange(MAP["length"]))
    x, y = MAP["x_first"] + (columns + 0.5) * 80.0, MAP["y_first"] - (rows + 0.5) * 80.0
    return _bowl(x, y) + offset + np.random.default_rng(seed).normal(0, noise, x.shape)


def _grid():
    to_utm = pyproj.Transformer.from_crs(3035, MAP["epsg"], always_xy=True)
    to_laea = pyproj.Transformer.from_crs(MAP["epsg"], 3035, always_xy=True)
    x0, y0 = to_laea.transform(MAP["x_first"] - 400, MAP["y_first"] + 400)
    x0, y0 = np.floor(x0 / 100) * 100, np.ceil(y0 / 100) * 100
    geo = {"x_first": x0, "y_first": y0, "x_step": 100.0, "y_step": -100.0, "width": 115, "length": 95, "epsg": 3035}
    columns, rows = np.meshgrid(np.arange(geo["width"]), np.arange(geo["length"]))
    ux, uy = to_utm.transform(x0 + (columns + 0.5) * 100.0, y0 - (rows + 0.5) * 100.0)
    return geo, _bowl(ux, uy)


def test_the_same_field_on_two_grids_agrees_and_the_offset_is_stated():
    geo, grid = _grid()
    result = val.compare_with_velocity_grid(geo=MAP, velocity_mm=_map_velocity(offset=7.0), grid_geo=geo, grid_velocity_mm=grid)
    assert result["source"] == "grid" and result["pixels"] > 3000
    rereferenced = result["velocity_rereferenced"]
    assert rereferenced["pearson_r"] > 0.99 and rereferenced["slope_b_on_a"] == pytest.approx(1.0, abs=0.03)
    assert rereferenced["rmse_mm_yr"] < 1.0
    assert result["common_offset_mm_yr"]["mintpy"] - result["common_offset_mm_yr"]["other"] == pytest.approx(7.0, abs=0.7)
    assert result["verdict"]["status"] == "ok"


def test_a_stated_error_is_checked_when_the_map_has_one():
    geo, grid = _grid()
    noisy = _map_velocity(noise=3.0, seed=5)
    result = val.compare_with_velocity_grid(geo=MAP, velocity_mm=noisy, grid_geo=geo, grid_velocity_mm=grid,
                                            std_mm=np.full(noisy.shape, 1.0))
    # the observed spread is about 3 mm/year before averaging over about 1.5 pixels, the stated error 1: too small
    assert result["uncertainty"]["scale"] > 1.3
    assert "too small" in result["uncertainty"]["message"]


def test_maps_that_do_not_overlap_are_refused():
    geo, grid = _grid()
    far = dict(MAP, x_first=MAP["x_first"] + 500000.0)
    with pytest.raises(ValueError, match="cover different areas"):
        val.compare_with_velocity_grid(geo=far, velocity_mm=_map_velocity(), grid_geo=geo, grid_velocity_mm=grid)


def _write_tile(path, east, north, values):
    import rasterio
    from rasterio.transform import from_origin
    with rasterio.open(path, "w", driver="GTiff", height=values.shape[0], width=values.shape[1], count=1, dtype="float32",
                       crs="EPSG:3035", transform=from_origin(east, north, 100.0, 100.0), nodata=-9999.0) as out:
        out.write(values.astype("float32"), 1)


def test_tiles_are_merged_over_the_area_and_missing_cells_become_nan(tmp_path):
    # the real tiles E32N16 and E32N17: the 100 km line y = 1 700 000 crosses the area
    left = 3200000.0
    upper = np.full((1000, 1000), -12.5)
    lower = np.full((1000, 1000), 3.0)
    upper[:, :10] = -9999.0
    _write_tile(tmp_path / "EGMS_L3_E32N17_100km_U_2020_2024_1.tiff", left, 1800000.0, upper)   # north edge
    _write_tile(tmp_path / "EGMS_L3_E32N16_100km_U_2020_2024_1.tiff", left, 1700000.0, lower)
    with zipfile.ZipFile(tmp_path / "upper.zip", "w") as z:
        z.write(tmp_path / "EGMS_L3_E32N17_100km_U_2020_2024_1.tiff", "EGMS_L3_E32N17_100km_U_2020_2024_1.tiff")
        z.writestr("EGMS_L3_E32N17_100km_U_2020_2024_1.csv", "pid\n1\n")          # the big CSV is ignored
    velocity, geo = egms_l3.read_velocity([tmp_path / "upper.zip", tmp_path / "EGMS_L3_E32N16_100km_U_2020_2024_1.tiff"],
                                          (-1.745, 37.6, -1.585, 37.705))
    assert geo["epsg"] == 3035 and geo["x_step"] == 100.0 and geo["y_step"] == -100.0
    assert velocity.shape == (geo["length"], geo["width"])
    assert {-12.5, 3.0} <= set(np.round(velocity[np.isfinite(velocity)], 1))
    assert not np.any(velocity == -9999.0)


def test_a_tile_outside_the_area_is_an_error(tmp_path):
    _write_tile(tmp_path / "far.tiff", 4000000.0, 2000000.0, np.ones((100, 100)))
    with pytest.raises(egms_l3.EGMSL3Error):
        egms_l3.read_velocity([tmp_path / "far.tiff"], (-1.745, 37.6, -1.585, 37.705))


def test_a_file_that_is_not_a_tile_is_refused(tmp_path):
    (tmp_path / "notes.txt").write_text("hello")
    with pytest.raises(egms_l3.EGMSL3Error, match="neither a zip nor a GeoTIFF"):
        egms_l3.read_velocity([tmp_path / "notes.txt"], (-1.745, 37.6, -1.585, 37.705))
