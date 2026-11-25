"""Exports of a MintPy result, from a small synthetic work directory."""
import csv
import h5py
import numpy as np
import pytest

from insar_core.pipeline import mintpy_export as ex

LENGTH, WIDTH = 6, 8
DATES = ["20230103", "20230115", "20230127"]
X0, Y0, STEP = 600000.0, 4170000.0, 80.0


def _h5(path, **datasets):
    attrs = datasets.pop("attrs", {})
    with h5py.File(path, "w") as handle:
        for name, data in datasets.items():
            handle.create_dataset(name, data=data)
        handle.attrs.update(attrs)


@pytest.fixture()
def work(tmp_path):
    geo = {"X_FIRST": X0, "Y_FIRST": Y0, "X_STEP": STEP, "Y_STEP": -STEP, "WIDTH": WIDTH, "LENGTH": LENGTH,
           "EPSG": 32630, "REF_Y": 1, "REF_X": 2, "REF_DATE": "20230103", "ORBIT_DIRECTION": "DESCENDING",
           "START_DATE": DATES[0], "END_DATE": DATES[-1], "WAVELENGTH": 0.0555}
    velocity = (np.arange(LENGTH * WIDTH, dtype="float32").reshape(LENGTH, WIDTH) - 20) / 1000.0   # m/yr
    cube = np.stack([velocity * k for k in range(len(DATES))])
    _h5(tmp_path / "velocity.h5", velocity=velocity, velocityStd=np.full_like(velocity, 0.002), attrs={**geo, "FILE_PATH": str(tmp_path / "timeseries.h5")})
    _h5(tmp_path / "timeseries.h5", timeseries=cube, date=np.array([d.encode() for d in DATES]), attrs=geo)
    mask = np.ones((LENGTH, WIDTH), bool)
    mask[0, 0] = False
    _h5(tmp_path / "maskTempCoh.h5", mask=mask)
    _h5(tmp_path / "temporalCoherence.h5", temporalCoherence=np.full((LENGTH, WIDTH), 0.9, "float32"))
    _h5(tmp_path / "avgSpatialCoh.h5", coherence=np.full((LENGTH, WIDTH), 0.8, "float32"))
    return tmp_path


def test_geotiff_holds_the_velocity_in_mm_per_year_in_the_results_own_crs(work, tmp_path):
    rasterio = pytest.importorskip("rasterio")
    out = ex.export(work, tmp_path / "out", "geotiff", layer="velocity")
    with rasterio.open(out) as raster:
        assert raster.crs.to_epsg() == 32630
        assert tuple(raster.transform)[:6] == (STEP, 0.0, X0, 0.0, -STEP, Y0)
        data = raster.read(1)
        assert raster.tags()["units"] == "mm/year" and raster.tags()["reference_date"] == "2023-01-03"
    assert np.isnan(data[0, 0])                         # rejected by the coherence mask
    assert data[1, 1] == pytest.approx((1 * WIDTH + 1 - 20), abs=1e-3)   # m/yr to mm/yr


def test_unmasked_keeps_the_pixels_the_mask_rejects(work, tmp_path):
    rasterio = pytest.importorskip("rasterio")
    out = ex.export(work, tmp_path / "out", "geotiff", layer="velocity", masked=False)
    with rasterio.open(out) as raster:
        assert raster.read(1)[0, 0] == pytest.approx(-20.0, abs=1e-3)
    assert out.name == "mintpy_velocity_unmasked.tif"


def test_the_timeseries_geotiff_has_a_band_per_date_named_by_it(work, tmp_path):
    rasterio = pytest.importorskip("rasterio")
    with rasterio.open(ex.export(work, tmp_path / "out", "geotiff", layer="timeseries")) as raster:
        assert raster.count == 3 and list(raster.descriptions) == DATES
        assert raster.read(3)[1, 1] == pytest.approx(2 * (1 * WIDTH + 1 - 20), abs=1e-3)


def test_the_mask_layer_is_a_plain_integer_raster(work, tmp_path):
    rasterio = pytest.importorskip("rasterio")
    with rasterio.open(ex.export(work, tmp_path / "out", "geotiff", layer="mask")) as raster:
        assert raster.dtypes[0] == "uint8" and raster.read(1).sum() == LENGTH * WIDTH - 1


def test_csv_has_one_row_per_kept_pixel_with_positions_in_wgs84_and_a_column_per_date(work, tmp_path):
    out = ex.export(work, tmp_path / "out", "csv")
    lines = out.read_text().splitlines()
    assert any(line.startswith("# reference_date: 2023-01-03") for line in lines)
    rows = list(csv.DictReader(line for line in lines if not line.startswith("#")))
    assert len(rows) == LENGTH * WIDTH - 1                       # the masked pixel is left out
    assert {"latitude", "longitude", "velocity_mm_yr", "velocity_std_mm_yr", "d20230103", "d20230127"} <= set(rows[0])
    first = next(r for r in rows if (r["row"], r["column"]) == ("1", "1"))
    assert float(first["velocity_mm_yr"]) == pytest.approx(1 * WIDTH + 1 - 20, abs=1e-3)
    assert float(first["velocity_std_mm_yr"]) == pytest.approx(2.0, abs=1e-3)
    assert 37.0 < float(first["latitude"]) < 39.0 and -4.0 < float(first["longitude"]) < 0.0
    assert float(first["d20230127"]) == pytest.approx(2 * float(first["velocity_mm_yr"]), abs=1e-3)


def test_csv_without_the_series_has_no_date_columns(work, tmp_path):
    rows = list(csv.DictReader(line for line in ex.export(work, tmp_path / "out", "csv", include_series=False).read_text().splitlines() if not line.startswith("#")))
    assert not any(name.startswith("d2023") for name in rows[0])


def test_pixel_positions_are_centres_half_a_step_inside_the_corner(work, tmp_path):
    import pyproj
    out = ex.export(work, tmp_path / "out", "csv", include_series=False)
    rows = list(csv.DictReader(line for line in out.read_text().splitlines() if not line.startswith("#")))
    row = next(r for r in rows if (r["row"], r["column"]) == ("0", "1"))
    x, y = pyproj.Transformer.from_crs(4326, 32630, always_xy=True).transform(float(row["longitude"]), float(row["latitude"]))
    assert x == pytest.approx(X0 + 1.5 * STEP, abs=0.5) and y == pytest.approx(Y0 - 0.5 * STEP, abs=0.5)


def test_geopackage_has_the_points_the_attributes_and_the_reference(work, tmp_path):
    ogr = pytest.importorskip("osgeo.ogr")
    out = ex.export(work, tmp_path / "out", "gpkg", include_series=True)
    source = ogr.Open(str(out))
    layer = source.GetLayer("mintpy_points")
    assert layer.GetFeatureCount() == LENGTH * WIDTH - 1
    assert layer.GetSpatialRef().GetAuthorityCode(None) == "4326"
    feature = layer.GetNextFeature()
    assert feature.GetField("velocity_mm_yr") is not None and feature.GetField("d20230127") is not None
    assert source.GetMetadataItem("reference_date") == "2023-01-03"


def test_netcdf_follows_cf_with_time_y_x_and_a_crs(work, tmp_path):
    gdal = pytest.importorskip("osgeo.gdal")
    gdal.UseExceptions()
    out = ex.export(work, tmp_path / "out", "netcdf")
    dataset = gdal.OpenEx(str(out), gdal.OF_MULTIDIM_RASTER)
    root = dataset.GetRootGroup()
    velocity = root.OpenMDArray("velocity")
    assert [d.GetName() for d in velocity.GetDimensions()] == ["y", "x"]
    assert velocity.GetUnit() == "mm/year" and "UTM zone 30N" in velocity.GetSpatialRef().GetName()
    cube = root.OpenMDArray("displacement")
    assert [d.GetName() for d in cube.GetDimensions()] == ["time", "y", "x"]
    assert root.GetAttribute("Conventions").Read() == "CF-1.8"
    assert cube.ReadAsArray()[2, 1, 1] == pytest.approx(2 * (1 * WIDTH + 1 - 20), abs=1e-3)
    raster = gdal.Open(f'NETCDF:"{out}":velocity')
    assert raster.GetGeoTransform() == (X0, STEP, 0.0, Y0, 0.0, -STEP)


def test_a_bad_request_is_refused(work, tmp_path):
    with pytest.raises(ex.ExportError, match="format"):
        ex.export(work, tmp_path / "out", "shapefile")
    with pytest.raises(ex.ExportError, match="layer"):
        ex.export(work, tmp_path / "out", "geotiff", layer="nope")


def test_missing_results_are_a_clear_error_not_a_crash(tmp_path):
    with pytest.raises(ex.ExportError):
        ex.export(tmp_path, tmp_path / "out", "geotiff")


def test_every_pixel_masked_out_is_reported(work, tmp_path):
    _h5(work / "maskTempCoh.h5", mask=np.zeros((LENGTH, WIDTH), bool))
    with pytest.raises(ex.ExportError, match="no pixels"):
        ex.export(work, tmp_path / "out", "csv")


def test_the_calibrated_velocity_adds_the_gnss_shift_and_says_so(work, tmp_path):
    rasterio = pytest.importorskip("rasterio")
    out = ex.export(work, tmp_path / "out", "geotiff", layer="velocity_calibrated", corrections={"offset_mm_yr": 4.5})
    with rasterio.open(out) as raster:
        data = raster.read(1)
        assert data[1, 1] == pytest.approx((1 * WIDTH + 1 - 20) + 4.5, abs=1e-3)
        assert raster.tags()["calibration_shift_mm_yr"] == "+4.500"
    assert np.isnan(data[0, 0])


def test_the_calibrated_error_is_the_stated_error_times_the_measured_factor(work, tmp_path):
    rasterio = pytest.importorskip("rasterio")
    out = ex.export(work, tmp_path / "out", "geotiff", layer="velocity_std_calibrated", corrections={"std_scale": 2.5})
    with rasterio.open(out) as raster:
        assert raster.read(1)[1, 1] == pytest.approx(2.0 * 2.5, abs=1e-3)       # 0.002 m/yr stated, in mm/yr, times 2.5
        assert raster.tags()["uncertainty_scale"] == "2.500"


def test_a_calibrated_layer_without_its_calibration_is_refused_with_the_reason(work, tmp_path):
    with pytest.raises(ex.ExportError, match="GNSS"):
        ex.export(work, tmp_path / "out", "geotiff", layer="velocity_calibrated")
    with pytest.raises(ex.ExportError, match="EGMS"):
        ex.export(work, tmp_path / "out", "geotiff", layer="velocity_std_calibrated", corrections={"offset_mm_yr": 1.0})


def test_csv_with_a_step_keeps_a_regular_grid_of_the_right_pixels(work, tmp_path, monkeypatch):
    monkeypatch.setattr(ex, "_CHUNK_ROWS", 4)                           # blocks that the step does not divide
    out = ex.export_csv(work, tmp_path / "points.csv", masked=False, include_series=True, step=3)
    with open(out, encoding="utf-8") as handle:
        rows = list(csv.DictReader(line for line in handle if not line.startswith("#")))
    assert sorted({int(r["row"]) for r in rows}) == [0, 3] and sorted({int(r["column"]) for r in rows}) == [0, 3, 6]
    with h5py.File(work / "velocity.h5") as handle:
        velocity = handle["velocity"][:] * 1000.0
    for r in rows:
        assert float(r["velocity_mm_yr"]) == pytest.approx(velocity[int(r["row"]), int(r["column"])], rel=1e-3, abs=1e-3)
        assert float(r["d20230127"]) == pytest.approx(2 * velocity[int(r["row"]), int(r["column"])], rel=1e-3, abs=1e-3)


def test_csv_of_a_result_without_a_stated_error_is_still_written(work, tmp_path):
    with h5py.File(work / "velocity.h5", "a") as handle:
        del handle["velocityStd"]
    out = ex.export_csv(work, tmp_path / "points.csv", include_series=False)
    with open(out, encoding="utf-8") as handle:
        rows = list(csv.DictReader(line for line in handle if not line.startswith("#")))
    assert rows and all(r["velocity_std_mm_yr"] == "nan" for r in rows)


def test_netcdf_cube_written_in_blocks_equals_the_source(work, tmp_path, monkeypatch):
    pytest.importorskip("osgeo")
    monkeypatch.setattr(ex, "_NETCDF_BLOCK_BYTES", 1)                   # one date per block
    out = ex.export_netcdf(work, tmp_path / "results.nc")
    with h5py.File(out) as written, h5py.File(work / "timeseries.h5") as source:
        cube = source["timeseries"][:] * 1000.0
        cube[:, 0, 0] = np.nan                                          # the masked pixel
        assert np.allclose(written["displacement"][:], cube, equal_nan=True)
        assert written["displacement"].compression == "gzip"
