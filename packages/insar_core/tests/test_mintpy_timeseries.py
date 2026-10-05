import h5py
import numpy as np
import pytest

from insar_core.pipeline.mintpy_timeseries import (
    locate_timeseries_file,
    locate_velocity_file,
    read_point_time_series,
    read_velocity_points,
)

# A tiny 3x3 pixel grid. Y_STEP is negative because raster rows run north to
# south, same convention MintPy/GDAL use for a geocoded product.
_X_FIRST, _Y_FIRST = 10.0, 50.0
_X_STEP, _Y_STEP = 0.01, -0.01
_DATES = [b"20200101", b"20200107", b"20200113"]


def _write_timeseries_h5(path, *, geocoded: bool = True):
    with h5py.File(path, "w") as f:
        if geocoded:
            f.attrs["X_FIRST"] = _X_FIRST
            f.attrs["Y_FIRST"] = _Y_FIRST
            f.attrs["X_STEP"] = _X_STEP
            f.attrs["Y_STEP"] = _Y_STEP
            f.attrs["WIDTH"] = 3
            f.attrs["LENGTH"] = 3
        f.create_dataset("date", data=_DATES)
        # shape (n_dates, length, width), values in meters. Pixel (1, 1) is
        # given a distinct, easy-to-check series; everything else is zero.
        data = np.zeros((3, 3, 3), dtype="float32")
        data[:, 1, 1] = [0.0, 0.001, 0.002]  # 0, 1, 2 mm
        f.create_dataset("timeseries", data=data)


def test_locate_prefers_geocoded_subfolder(tmp_path):
    (tmp_path / "geo").mkdir()
    geo_file = tmp_path / "geo" / "geo_timeseries.h5"
    geo_file.write_bytes(b"")
    (tmp_path / "timeseries.h5").write_bytes(b"")

    assert locate_timeseries_file(tmp_path) == geo_file


def test_locate_falls_back_to_plain_timeseries(tmp_path):
    plain_file = tmp_path / "timeseries.h5"
    plain_file.write_bytes(b"")

    assert locate_timeseries_file(tmp_path) == plain_file


def test_locate_raises_when_nothing_exists(tmp_path):
    with pytest.raises(FileNotFoundError):
        locate_timeseries_file(tmp_path)


def test_read_point_time_series_converts_meters_to_mm(tmp_path):
    _write_timeseries_h5(tmp_path / "timeseries.h5")

    # Pixel (1, 1) sits at lat = Y_FIRST + 1 * Y_STEP, lon = X_FIRST + 1 * X_STEP.
    lat = _Y_FIRST + 1 * _Y_STEP
    lon = _X_FIRST + 1 * _X_STEP

    series = read_point_time_series(tmp_path, lat, lon)
    assert series["dates"] == ["20200101", "20200107", "20200113"]
    assert series["values_mm"] == pytest.approx([0.0, 1.0, 2.0])


def test_read_point_time_series_rejects_ungeocoded_output(tmp_path):
    _write_timeseries_h5(tmp_path / "timeseries.h5", geocoded=False)

    with pytest.raises(ValueError, match="geocoding"):
        read_point_time_series(tmp_path, 50.0, 10.0)


def test_read_point_time_series_rejects_out_of_bounds_point(tmp_path):
    _write_timeseries_h5(tmp_path / "timeseries.h5")

    with pytest.raises(ValueError, match="outside"):
        read_point_time_series(tmp_path, lat=1.0, lon=1.0)


def _write_velocity_h5(path, length=3, width=3, *, geocoded: bool = True):
    with h5py.File(path, "w") as f:
        if geocoded:
            f.attrs["X_FIRST"] = _X_FIRST
            f.attrs["Y_FIRST"] = _Y_FIRST
            f.attrs["X_STEP"] = _X_STEP
            f.attrs["Y_STEP"] = _Y_STEP
            f.attrs["WIDTH"] = width
            f.attrs["LENGTH"] = length
        data = np.full((length, width), 0.002, dtype="float32")  # 2 mm/yr everywhere
        data[0, 0] = np.nan  # a masked-out pixel (water/decorrelated), must be skipped
        f.create_dataset("velocity", data=data)


def test_locate_velocity_prefers_geocoded_subfolder(tmp_path):
    (tmp_path / "geo").mkdir()
    geo_file = tmp_path / "geo" / "geo_velocity.h5"
    geo_file.write_bytes(b"")
    (tmp_path / "velocity.h5").write_bytes(b"")

    assert locate_velocity_file(tmp_path) == geo_file


def test_read_velocity_points_skips_nan_and_converts_units(tmp_path):
    _write_velocity_h5(tmp_path / "velocity.h5")

    fc = read_velocity_points(tmp_path)
    assert fc["type"] == "FeatureCollection"
    # 3x3 grid, one NaN pixel masked out -> 8 features left.
    assert len(fc["features"]) == 8
    assert all(f["properties"]["velocity"] == pytest.approx(2.0) for f in fc["features"])
    lons = {f["geometry"]["coordinates"][0] for f in fc["features"]}
    lats = {f["geometry"]["coordinates"][1] for f in fc["features"]}
    assert lons == {_X_FIRST, _X_FIRST + _X_STEP, _X_FIRST + 2 * _X_STEP}
    assert lats == {_Y_FIRST, _Y_FIRST + _Y_STEP, _Y_FIRST + 2 * _Y_STEP}


def test_read_velocity_points_decimates_large_grids(tmp_path):
    """A real HyP3-derived grid measured in this repo is 3619x2817 (~10.2M
    pixels); this checks the decimation logic keeps the result well below
    the map-display cap without needing an actual multi-million-pixel file."""
    from insar_core.pipeline import mintpy_timeseries

    length, width = 2000, 2000  # 4M pixels, comfortably over the 50k cap
    _write_velocity_h5(tmp_path / "velocity.h5", length=length, width=width)

    fc = read_velocity_points(tmp_path)
    assert 0 < len(fc["features"]) <= mintpy_timeseries._MAX_POINTS


def test_read_velocity_points_rejects_ungeocoded_output(tmp_path):
    _write_velocity_h5(tmp_path / "velocity.h5", geocoded=False)

    with pytest.raises(ValueError, match="geocoding"):
        read_velocity_points(tmp_path)


# Real values measured from an actual HyP3/MintPy run in this repo
# ("Madrid 2025-2026"): the geocoded output is UTM zone 30N, not WGS84
# lon/lat degrees - X_FIRST/Y_FIRST are meters, not degrees. This is the bug
# that shipped first: treating those meters as if they were degrees put
# points at nonsensical coordinates (e.g. "longitude" 437560).
_UTM_EPSG = 32630
_UTM_X_FIRST, _UTM_Y_FIRST = 437560.0, 4476840.0
_UTM_X_STEP, _UTM_Y_STEP = 80.0, -80.0
# The known-correct WGS84 location of (_UTM_X_FIRST, _UTM_Y_FIRST), computed
# independently with pyproj and confirmed against the real run's own AOI.
_UTM_ORIGIN_LON, _UTM_ORIGIN_LAT = -3.7362352736146933, 40.439864847072066


def _write_utm_velocity_h5(path):
    with h5py.File(path, "w") as f:
        f.attrs["X_FIRST"] = _UTM_X_FIRST
        f.attrs["Y_FIRST"] = _UTM_Y_FIRST
        f.attrs["X_STEP"] = _UTM_X_STEP
        f.attrs["Y_STEP"] = _UTM_Y_STEP
        f.attrs["WIDTH"] = 3
        f.attrs["LENGTH"] = 3
        f.attrs["EPSG"] = _UTM_EPSG
        data = np.full((3, 3), 0.002, dtype="float32")
        f.create_dataset("velocity", data=data)


def test_read_velocity_points_reprojects_utm_to_wgs84(tmp_path):
    _write_utm_velocity_h5(tmp_path / "velocity.h5")

    fc = read_velocity_points(tmp_path)
    assert len(fc["features"]) == 9

    origin = next(
        f for f in fc["features"]
        if f["geometry"]["coordinates"] == pytest.approx([_UTM_ORIGIN_LON, _UTM_ORIGIN_LAT], abs=1e-6)
    )
    assert origin["properties"]["velocity"] == pytest.approx(2.0)

    # Every point must land as real WGS84 degrees, never raw UTM meters.
    for feature in fc["features"]:
        lon, lat = feature["geometry"]["coordinates"]
        assert -180 <= lon <= 180
        assert -90 <= lat <= 90


def test_read_point_time_series_reprojects_utm_query_point(tmp_path):
    with h5py.File(tmp_path / "timeseries.h5", "w") as f:
        f.attrs["X_FIRST"] = _UTM_X_FIRST
        f.attrs["Y_FIRST"] = _UTM_Y_FIRST
        f.attrs["X_STEP"] = _UTM_X_STEP
        f.attrs["Y_STEP"] = _UTM_Y_STEP
        f.attrs["WIDTH"] = 3
        f.attrs["LENGTH"] = 3
        f.attrs["EPSG"] = _UTM_EPSG
        f.create_dataset("date", data=_DATES)
        data = np.zeros((3, 3, 3), dtype="float32")
        data[:, 0, 0] = [0.0, 0.001, 0.002]
        f.create_dataset("timeseries", data=data)

    # Query with the real-world WGS84 lat/lon a user's map click would send,
    # not the file's native UTM coordinates.
    series = read_point_time_series(tmp_path, lat=_UTM_ORIGIN_LAT, lon=_UTM_ORIGIN_LON)
    assert series["values_mm"] == pytest.approx([0.0, 1.0, 2.0])
