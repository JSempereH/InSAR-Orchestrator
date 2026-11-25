"""Vertical and east-west motion from two tracks: recovered from known motion, and refused when it cannot be."""
import h5py
import numpy as np
import pytest

from insar_core.analysis import decomposition as dc
from insar_core.analysis.validation import los_unit_vector

SIZE = 60


def _geo(x_first=600000.0, y_first=4180000.0):
    return {"x_first": x_first, "y_first": y_first, "x_step": 80.0, "y_step": -80.0, "width": SIZE, "length": SIZE, "epsg": 32630}


def _truth():
    rows, columns = np.mgrid[0:SIZE, 0:SIZE].astype(float)
    vertical = -30.0 * np.exp(-(((rows - 30) / 12) ** 2 + ((columns - 30) / 12) ** 2))      # a subsidence bowl, mm/year
    east = 4.0 * (columns - 30) / 30.0                                                      # a gentle east-west gradient
    return vertical, east


def _product(direction, vertical, east, north=0.0, offset=0.0, geo=None, std=1.0):
    azimuth, incidence = (100.0, 38.0) if direction == "ascending" else (-100.0, 38.4)
    w = los_unit_vector(np.full((SIZE, SIZE), incidence), np.full((SIZE, SIZE), azimuth))
    velocity = w[0] * east + w[1] * north + w[2] * vertical + offset
    return dc.LosProduct(
        velocity=velocity, std=np.full((SIZE, SIZE), std), incidence=np.full((SIZE, SIZE), incidence),
        azimuth=np.full((SIZE, SIZE), azimuth), geo=geo or _geo(), direction=direction, label=direction,
        reference={"lat": 0, "lon": 0},
    )


# the reference is, by default, the pixel (5, 5) in the grid above
def _reference(geo=None, row=5, column=5):
    import pyproj
    geo = geo or _geo()
    x, y = geo["x_first"] + (column + 0.5) * 80.0, geo["y_first"] - (row + 0.5) * 80.0
    lon, lat = pyproj.Transformer.from_crs(geo["epsg"], 4326, always_xy=True).transform(x, y)
    return {"lat": lat, "lon": lon}


def test_known_motion_is_recovered_up_to_the_reference():
    vertical, east = _truth()
    result = dc.decompose(_product("ascending", vertical, east, offset=3.0), _product("descending", vertical, east, offset=-2.0),
                          reference=_reference())
    # the truth at the reference is not zero, so the result is the truth minus its value there
    expected_up = vertical - vertical[3:8, 3:8].mean()
    expected_east = east - east[3:8, 3:8].mean()
    keep = np.isfinite(result.vertical)
    assert keep.sum() > 0.95 * SIZE * SIZE
    assert np.abs(result.vertical[keep] - expected_up[keep]).max() < 0.05
    assert np.abs(result.east[keep] - expected_east[keep]).max() < 0.05


def test_the_order_of_the_two_results_does_not_matter():
    vertical, east = _truth()
    a, d = _product("ascending", vertical, east), _product("descending", vertical, east)
    one = dc.decompose(a, d, reference=_reference())
    other = dc.decompose(d, a, reference=_reference())
    assert np.allclose(one.vertical, other.vertical, equal_nan=True)
    assert np.allclose(one.east, other.east, equal_nan=True)


def test_two_tracks_of_the_same_direction_are_refused():
    vertical, east = _truth()
    with pytest.raises(dc.DecompositionError, match="one ascending and one descending"):
        dc.decompose(_product("ascending", vertical, east), _product("ascending", vertical, east), reference=_reference())


def test_a_grid_that_is_offset_by_half_a_pixel_is_resampled():
    vertical, east = _truth()
    shifted = _geo(x_first=600040.0, y_first=4179960.0)
    descending = _product("descending", vertical, east, geo=shifted)
    result = dc.decompose(_product("ascending", vertical, east), descending, reference=_reference())
    inside = np.isfinite(result.vertical)
    assert inside.sum() > 0.9 * SIZE * SIZE
    # smooth fields lose little to a bilinear half-pixel shift
    expected = vertical - vertical[3:8, 3:8].mean()
    assert np.median(np.abs(result.vertical[inside] - expected[inside])) < 0.6


def test_a_reference_with_no_valid_pixels_is_refused():
    vertical, east = _truth()
    ascending = _product("ascending", vertical, east)
    ascending.velocity[:10, :10] = np.nan
    with pytest.raises(dc.DecompositionError, match="no valid pixels"):
        dc.decompose(ascending, _product("descending", vertical, east), reference=_reference())


def test_uncertainty_is_carried_through_and_scaled():
    vertical, east = _truth()
    a, d = _product("ascending", vertical, east, std=1.0), _product("descending", vertical, east, std=1.0)
    one = dc.decompose(a, d, reference=_reference())
    three = dc.decompose(a, d, reference=_reference(), std_scale=3.0)
    k = np.isfinite(one.vertical_std)
    assert np.allclose(three.vertical_std[k], 3 * one.vertical_std[k])
    # with similar geometry the vertical error is about 1 / (2 cos(incidence)) of the LOS error times root two
    assert 0.5 < np.nanmedian(one.vertical_std) < 1.5
    assert np.nanmedian(one.east_std) > np.nanmedian(one.vertical_std) * 0.5


def test_north_motion_leaks_by_the_amount_the_summary_states():
    vertical, east = _truth()
    columns = np.mgrid[0:SIZE, 0:SIZE][1].astype(float)
    north = 10.0 * (columns - 30.0) / 30.0                    # north motion that differs across the area
    clean = dc.decompose(_product("ascending", vertical, east), _product("descending", vertical, east), reference=_reference())
    moved = dc.decompose(_product("ascending", vertical, east, north=north), _product("descending", vertical, east, north=north),
                         reference=_reference())
    keep = np.isfinite(clean.vertical) & (np.abs(north - north[3:8, 3:8].mean()) > 2.0)
    leak = clean.summary["north_leakage"]
    difference = np.abs(north - north[3:8, 3:8].mean())[keep]
    measured_up = np.median(np.abs(moved.vertical[keep] - clean.vertical[keep]) / difference)
    measured_east = np.median(np.abs(moved.east[keep] - clean.east[keep]) / difference)
    assert measured_up == pytest.approx(leak["into_vertical"], rel=0.05)
    assert measured_east == pytest.approx(leak["into_east"], rel=0.05)
    assert leak["into_vertical"] < 0.35 and leak["into_east"] < 0.35


def test_a_poorly_conditioned_pair_is_masked():
    vertical, east = _truth()
    ascending = _product("ascending", vertical, east)
    descending = _product("descending", vertical, east)
    # make the second line of sight almost the same as the first one
    descending.azimuth[:] = 100.0
    descending.incidence[:] = 38.2
    with pytest.raises(dc.DecompositionError, match="too alike"):
        dc.decompose(ascending, descending, reference=_reference())


def test_a_reference_outside_the_area_is_refused():
    vertical, east = _truth()
    with pytest.raises(dc.DecompositionError, match="outside"):
        dc.decompose(_product("ascending", vertical, east), _product("descending", vertical, east),
                     reference={"lat": 10.0, "lon": 10.0})


def test_geotiffs_are_written_with_the_assumptions(tmp_path):
    import rasterio
    vertical, east = _truth()
    result = dc.decompose(_product("ascending", vertical, east), _product("descending", vertical, east), reference=_reference())
    written = dc.write_geotiffs(result, tmp_path)
    assert set(written) == set(dc.LAYERS)
    with rasterio.open(written["vertical"]) as src:
        assert src.crs.to_epsg() == 32630 and src.count == 1
        data = src.read(1)
        assert np.isfinite(data).sum() == np.isfinite(result.vertical).sum()
        assert "north motion is zero" in src.tags()["assumptions"]


def test_read_los_product_reads_a_mintpy_run(tmp_path):
    work = tmp_path / "mintpy"
    (work / "inputs").mkdir(parents=True)
    geo = _geo()
    attrs = {"X_FIRST": geo["x_first"], "Y_FIRST": geo["y_first"], "X_STEP": 80.0, "Y_STEP": -80.0, "WIDTH": SIZE, "LENGTH": SIZE,
             "EPSG": "32630", "HEADING": "-12.0", "REF_Y": 5, "REF_X": 5}
    with h5py.File(work / "velocity.h5", "w") as handle:
        handle.attrs.update(attrs)
        handle["velocity"] = np.full((SIZE, SIZE), 0.002, "float32")
        handle["velocityStd"] = np.full((SIZE, SIZE), 0.0005, "float32")
    with h5py.File(work / "maskTempCoh.h5", "w") as handle:
        mask = np.ones((SIZE, SIZE), bool)
        mask[:5] = False
        handle["mask"] = mask
    with h5py.File(work / "inputs" / "geometryGeo.h5", "w") as handle:
        handle["incidenceAngle"] = np.full((SIZE, SIZE), 38.0, "float32")
        handle["azimuthAngle"] = np.full((SIZE, SIZE), 100.0, "float32")
    product = dc.read_los_product(work, label="asc")
    assert product.direction == "ascending"
    assert np.nanmedian(product.velocity) == pytest.approx(2.0)          # metres to millimetres
    assert np.nanmedian(product.std) == pytest.approx(0.5)
    assert np.isnan(product.velocity[0, 0])                               # masked by temporal coherence
    assert product.reference and "lat" in product.reference


def test_a_run_without_mintpy_results_is_refused(tmp_path):
    with pytest.raises(dc.DecompositionError, match="run MintPy"):
        dc.read_los_product(tmp_path)


def test_without_a_given_reference_a_mintpy_one_is_used_whatever_the_order():
    vertical, east = _truth()
    a, d = _product("ascending", vertical, east, offset=1.0), _product("descending", vertical, east, offset=-2.0)
    a.reference = dict(_reference(), row=5, column=5)
    d.reference = {"lat": 10.0, "lon": 10.0}                       # not in the area: must not be the one used
    one, other = dc.decompose(a, d), dc.decompose(d, a)
    assert one.summary["reference"]["lat"] == pytest.approx(_reference()["lat"])
    assert one.summary["reference"]["lat"] == other.summary["reference"]["lat"]
    assert np.allclose(one.vertical, other.vertical, equal_nan=True)


def test_the_second_reference_point_is_used_when_the_first_has_no_value_in_both_maps():
    vertical, east = _truth()
    a, d = _product("ascending", vertical, east), _product("descending", vertical, east)
    a.reference = _reference()
    d.reference = _reference(row=20, column=20)                    # the descending result's own reference
    d.velocity[:10, :10] = np.nan                                  # the ascending result's reference lies in this hole
    result = dc.decompose(a, d)
    assert result.summary["reference"]["lat"] == pytest.approx(d.reference["lat"])


def test_when_no_candidate_works_the_message_says_why():
    vertical, east = _truth()
    a, d = _product("ascending", vertical, east), _product("descending", vertical, east)
    a.reference = _reference()
    d.reference = _reference(row=6, column=6)
    d.velocity[:10, :10] = np.nan                                  # both references lie in the hole
    with pytest.raises(dc.DecompositionError, match="no valid pixels"):
        dc.decompose(a, d)


def _decompose(first_dates, second_dates):
    vertical, east = _truth()
    ascending, descending = _product("ascending", vertical, east), _product("descending", vertical, east)
    ascending.start_date, ascending.end_date = first_dates
    descending.start_date, descending.end_date = second_dates
    return dc.decompose(ascending, descending, reference=_reference())


def test_results_over_the_same_period_give_no_warning():
    summary = _decompose(("20200101", "20230101"), ("20200107", "20221226")).summary
    assert summary["warnings"] == [] and summary["period_overlap"]["share"] > 0.95


def test_results_over_different_periods_are_warned_about():
    summary = _decompose(("20180101", "20210101"), ("20200101", "20230101")).summary
    assert summary["period_overlap"]["share"] == pytest.approx(1 / 3, abs=0.01)
    assert "share only 33%" in summary["warnings"][0]
    summary = _decompose(("20180101", "20190101"), ("20200101", "20230101")).summary
    assert summary["period_overlap"]["common_days"] == 0 and "do not overlap" in summary["warnings"][0]


def test_results_without_dates_say_the_period_could_not_be_checked():
    assert "could not be checked" in _decompose(("", ""), ("20200101", "20230101")).summary["warnings"][0]


def test_decomposition_in_single_precision_matches_double_precision():
    """Results are read as float32 to halve the memory; the answer must not move by more than a thousandth of a mm/year."""
    vertical, east = _truth()
    double = dc.decompose(_product("ascending", vertical, east), _product("descending", vertical, east), reference=_reference())
    single_products = []
    for direction in ("ascending", "descending"):
        product = _product(direction, vertical, east)
        for name in ("velocity", "std", "incidence", "azimuth"):
            setattr(product, name, getattr(product, name).astype("float32"))
        single_products.append(product)
    single = dc.decompose(*single_products, reference=_reference())
    assert single.vertical.dtype == np.float32
    assert np.nanmax(np.abs(single.vertical - double.vertical)) < 1e-3
    assert np.nanmax(np.abs(single.east - double.east)) < 1e-3
