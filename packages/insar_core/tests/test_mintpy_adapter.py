import zipfile

import numpy as np
import pyproj
import pytest
import rasterio
from rasterio.transform import Affine

from insar_core.pipeline.mintpy_adapter import (
    MintPyAdapter,
    _extract_and_crop,
    _is_metadata_sidecar,
    _native_crop_window,
    _subset_lalo,
)

_SQUARE_AOI = {
    "type": "Polygon",
    "coordinates": [[[-3.72, 40.40], [-3.67, 40.40], [-3.67, 40.43], [-3.72, 40.43], [-3.72, 40.40]]],
}

# Matches the real HyP3/MintPy output measured in this repo (see
# test_mintpy_timeseries.py's own UTM constants): UTM zone 30N, 80 m pixels.
_UTM_EPSG = 32630
_UTM_X_ORIGIN, _UTM_Y_ORIGIN = 430000.0, 4480000.0
_UTM_PIXEL = 80.0


def _write_tif(path, data, transform):
    with rasterio.open(
        path, "w", driver="GTiff", width=data.shape[1], height=data.shape[0],
        count=1, dtype="float32", crs=f"EPSG:{_UTM_EPSG}", transform=transform,
    ) as dst:
        dst.write(data, 1)


def _write_utm_tif(path, *, size=20, fill_value=None):
    """A small synthetic single-band GeoTIFF on the same grid/CRS as a real
    HyP3 product, with a distinct, checkable value at every pixel (its own
    row*size+col index) unless `fill_value` is given."""
    if fill_value is not None:
        data = np.full((size, size), fill_value, dtype="float32")
    else:
        data = np.arange(size * size, dtype="float32").reshape(size, size)
    _write_tif(path, data, Affine(_UTM_PIXEL, 0.0, _UTM_X_ORIGIN, 0.0, -_UTM_PIXEL, _UTM_Y_ORIGIN))
    return data


def _wgs84_bbox_around_pixels(row_min, row_max, col_min, col_max):
    """The WGS84 bbox that exactly covers pixel rows/cols [row_min, row_max)
    x [col_min, col_max) on the synthetic grid `_write_utm_tif` produces."""
    min_x = _UTM_X_ORIGIN + col_min * _UTM_PIXEL
    max_x = _UTM_X_ORIGIN + col_max * _UTM_PIXEL
    max_y = _UTM_Y_ORIGIN - row_min * _UTM_PIXEL
    min_y = _UTM_Y_ORIGIN - row_max * _UTM_PIXEL
    transformer = pyproj.Transformer.from_crs(_UTM_EPSG, 4326, always_xy=True)
    lon1, lat1 = transformer.transform(min_x, min_y)
    lon2, lat2 = transformer.transform(max_x, max_y)
    return (min(lon1, lon2), min(lat1, lat2), max(lon1, lon2), max(lat1, lat2))


def test_subset_lalo_pads_around_the_aoi():
    value = _subset_lalo(_SQUARE_AOI)
    assert value is not None
    lat_part, lon_part = value.split(",")
    min_lat, max_lat = (float(x) for x in lat_part.split(":"))
    min_lon, max_lon = (float(x) for x in lon_part.split(":"))

    # Padded outward on every side, not just clamped to the polygon's own extent.
    assert min_lat < 40.40 and max_lat > 40.43
    assert min_lon < -3.72 and max_lon > -3.67


def test_subset_lalo_accepts_a_feature_wrapper():
    feature = {"type": "Feature", "geometry": _SQUARE_AOI, "properties": {}}
    assert _subset_lalo(feature) == _subset_lalo(_SQUARE_AOI)


def test_subset_lalo_returns_none_for_unusable_geometry():
    assert _subset_lalo({"type": "Polygon", "coordinates": []}) is None
    assert _subset_lalo({}) is None


def test_write_config_includes_subset_when_geometry_given(tmp_path):
    adapter = MintPyAdapter(downloads_dir=tmp_path / "downloads", work_dir=tmp_path / "work")
    config_path = adapter.write_config(geometry=_SQUARE_AOI)

    text = config_path.read_text()
    assert "mintpy.subset.lalo" in text
    assert _subset_lalo(_SQUARE_AOI) in text


def test_write_config_omits_subset_without_geometry(tmp_path):
    adapter = MintPyAdapter(downloads_dir=tmp_path / "downloads", work_dir=tmp_path / "work")
    config_path = adapter.write_config()

    assert "mintpy.subset.lalo" not in config_path.read_text()


def test_native_crop_window_matches_expected_pixel_range(tmp_path):
    tif_path = tmp_path / "src.tif"
    _write_utm_tif(tif_path, size=20)
    # Bbox is built by reprojecting these pixel edges to WGS84 and back, so
    # floating point round-trip noise can land the recovered window a pixel
    # off on either edge when the edge sits (almost) exactly on a pixel
    # boundary - not a concern in real use, where a padded AOI bbox never
    # lines up exactly with a scene's pixel grid. Assert containment plus a
    # small, bounded amount of slop instead of exact equality.
    with rasterio.open(tif_path) as ds:
        window = _native_crop_window(ds, _wgs84_bbox_around_pixels(row_min=5, row_max=10, col_min=3, col_max=8))

    xoff, yoff, xsize, ysize = window
    assert 1 <= xoff <= 3
    assert 3 <= yoff <= 5
    assert xoff + xsize >= 8
    assert yoff + ysize >= 10
    assert xsize <= 7 and ysize <= 7  # cropped, nowhere near the full 20x20 scene


def test_native_crop_window_returns_none_outside_raster(tmp_path):
    tif_path = tmp_path / "src.tif"
    _write_utm_tif(tif_path, size=20)
    # Far south of the synthetic grid's own extent (grid covers ~40.4N-40.5N-ish for this origin).
    with rasterio.open(tif_path) as ds:
        window = _native_crop_window(ds, (-10.0, -10.0, -9.0, -9.0))

    assert window is None


def test_extract_and_crop_preserves_pixel_values_and_resolution(tmp_path):
    src_tif = tmp_path / "src.tif"
    data = _write_utm_tif(src_tif, size=20)
    bbox = _wgs84_bbox_around_pixels(row_min=5, row_max=10, col_min=3, col_max=8)

    zip_path = tmp_path / "scene.zip"
    member_name = "scene/scene_unw_phase.tif"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.write(src_tif, member_name)

    dest_dir = tmp_path / "extracted"
    _extract_and_crop(zip_path, member_name, dest_dir, bbox)

    cropped_path = dest_dir / member_name
    assert cropped_path.exists()

    with rasterio.open(cropped_path) as ds:
        gt = ds.transform
        cropped_values = ds.read(1)
        width, height = ds.width, ds.height
    assert gt.a == pytest.approx(_UTM_PIXEL)  # native resolution preserved, not resampled
    assert gt.e == pytest.approx(-_UTM_PIXEL)
    assert width <= 8 and height <= 8  # cropped, nowhere near the full 20x20 scene

    # Recover this crop's own pixel offset into the source grid from its
    # geotransform (see test_native_crop_window_... for why this isn't
    # asserted as an exact, hardcoded window).
    xoff = round((gt.c - _UTM_X_ORIGIN) / _UTM_PIXEL)
    yoff = round((_UTM_Y_ORIGIN - gt.f) / _UTM_PIXEL)
    assert xoff + width >= 8 and yoff + height >= 10  # covers the requested range

    expected = data[yoff:yoff + cropped_values.shape[0], xoff:xoff + cropped_values.shape[1]]
    assert np.array_equal(cropped_values, expected)  # bit-for-bit, no interpolation


def test_extract_and_crop_raises_when_no_overlap(tmp_path):
    src_tif = tmp_path / "src.tif"
    _write_utm_tif(src_tif, size=20)

    zip_path = tmp_path / "scene.zip"
    member_name = "scene/scene_unw_phase.tif"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.write(src_tif, member_name)

    dest_dir = tmp_path / "extracted"
    with pytest.raises(RuntimeError, match="does not overlap"):
        _extract_and_crop(zip_path, member_name, dest_dir, (-10.0, -10.0, -9.0, -9.0))


def test_is_metadata_sidecar_distinguishes_real_metadata_from_the_readme():
    assert _is_metadata_sidecar("S1AA_fake_scene/S1AA_fake_scene.txt")
    assert not _is_metadata_sidecar("S1AA_fake_scene/S1AA_fake_scene.README.md.txt")
    assert not _is_metadata_sidecar("S1AA_fake_scene/S1AA_fake_scene_unw_phase.tif")


def _write_zip_scene(zip_path, *, size=20):
    """A synthetic HyP3-style zip: the six raster files MintPy needs, the
    `.txt` metadata sidecar it also needs (see _is_metadata_sidecar), and a
    couple of files it doesn't - one of them the real, easy-to-confuse-with-
    the-metadata-file `.README.md.txt` HyP3 also ships - all under one
    interferogram subdirectory, mirroring the layout write_config's glob
    patterns expect (`<data_dir>/*/*unw_phase.tif` etc)."""
    scene = "S1AA_fake_scene"
    needed = (
        "unw_phase.tif", "corr.tif", "dem.tif",
        "lv_theta.tif", "lv_phi.tif", "water_mask.tif",
    )
    with zipfile.ZipFile(zip_path, "w") as zf:
        for suffix in needed:
            tif_path = zip_path.parent / f"_tmp_{suffix}"
            _write_utm_tif(tif_path, size=size)
            zf.write(tif_path, f"{scene}/{scene}_{suffix}")
            tif_path.unlink()
        zf.writestr(f"{scene}/{scene}_rgb_browse.png", b"not a real image")
        zf.writestr(f"{scene}/{scene}.txt", b"Reference Granule: fake\nSecondary Granule: fake\n")
        zf.writestr(f"{scene}/{scene}.README.md.txt", b"# metadata nobody needs")
    return scene


def test_unzip_all_crops_to_aoi_and_skips_unneeded_files(tmp_path):
    downloads_dir = tmp_path / "downloads"
    downloads_dir.mkdir()
    scene = _write_zip_scene(downloads_dir / "S1AA_fake_scene.zip", size=20)
    bbox = _wgs84_bbox_around_pixels(row_min=5, row_max=10, col_min=3, col_max=8)

    # _padded_bbox pads outward, so build a geometry whose own (unpadded)
    # extent already matches this exact bbox - the padding only makes the
    # kept window larger, never smaller, so every needed file must still be
    # extracted and at least this cropped, never bigger than the full scene.
    min_lon, min_lat, max_lon, max_lat = bbox
    geometry = {
        "type": "Polygon",
        "coordinates": [[[min_lon, min_lat], [max_lon, min_lat], [max_lon, max_lat], [min_lon, max_lat], [min_lon, min_lat]]],
    }

    adapter = MintPyAdapter(downloads_dir=downloads_dir, work_dir=tmp_path / "work")
    count = adapter.unzip_all(geometry=geometry)

    assert count == 1
    extracted_dir = downloads_dir / scene
    extracted_files = {p.name for p in extracted_dir.iterdir()}
    assert extracted_files == {
        f"{scene}_unw_phase.tif", f"{scene}_corr.tif", f"{scene}_dem.tif",
        f"{scene}_lv_theta.tif", f"{scene}_lv_phi.tif", f"{scene}_water_mask.tif",
        f"{scene}.txt",
    }

    with rasterio.open(extracted_dir / f"{scene}_unw_phase.tif") as ds:
        size = (ds.width, ds.height)
    assert size[0] < 20 and size[1] < 20  # cropped, not full scene resolution

    # The metadata sidecar is plain text - extracted verbatim, never cropped.
    assert (extracted_dir / f"{scene}.txt").read_bytes() == b"Reference Granule: fake\nSecondary Granule: fake\n"

    assert not (downloads_dir / "S1AA_fake_scene.zip").exists()  # captured in the crop, no longer needed


def test_unzip_all_without_geometry_filters_but_does_not_crop(tmp_path):
    downloads_dir = tmp_path / "downloads"
    downloads_dir.mkdir()
    scene = _write_zip_scene(downloads_dir / "S1AA_fake_scene.zip", size=20)

    adapter = MintPyAdapter(downloads_dir=downloads_dir, work_dir=tmp_path / "work")
    adapter.unzip_all()

    extracted_dir = downloads_dir / scene
    extracted_files = {p.name for p in extracted_dir.iterdir()}
    assert extracted_files == {
        f"{scene}_unw_phase.tif", f"{scene}_corr.tif", f"{scene}_dem.tif",
        f"{scene}_lv_theta.tif", f"{scene}_lv_phi.tif", f"{scene}_water_mask.tif",
        f"{scene}.txt",
    }

    with rasterio.open(extracted_dir / f"{scene}_unw_phase.tif") as ds:
        size = (ds.width, ds.height)
    assert size == (20, 20)  # full resolution kept when there's no AOI to crop to

    assert (downloads_dir / "S1AA_fake_scene.zip").exists()  # nothing was cropped, so nothing to reclaim yet


def test_unzip_all_is_idempotent(tmp_path):
    downloads_dir = tmp_path / "downloads"
    downloads_dir.mkdir()
    scene = _write_zip_scene(downloads_dir / "S1AA_fake_scene.zip", size=20)

    adapter = MintPyAdapter(downloads_dir=downloads_dir, work_dir=tmp_path / "work")
    adapter.unzip_all()
    marker = downloads_dir / scene / f"{scene}_unw_phase.tif"
    first_mtime = marker.stat().st_mtime_ns

    adapter.unzip_all(geometry=_SQUARE_AOI)  # even with geometry now given, must not re-extract
    assert marker.stat().st_mtime_ns == first_mtime


def test_unzip_all_preserves_zip_when_a_member_fails_to_crop(tmp_path):
    """One bad member (here: a file whose footprint doesn't overlap the AOI
    at all, e.g. a corrupt or mismatched product) must not cost the whole
    zip - unzip_all should raise instead of deleting a zip whose data was
    only partially captured, so a re-run has something to recover from."""
    downloads_dir = tmp_path / "downloads"
    downloads_dir.mkdir()
    zip_path = downloads_dir / "S1AA_fake_scene.zip"
    scene = "S1AA_fake_scene"
    good_suffixes = ("unw_phase.tif", "corr.tif", "dem.tif", "lv_theta.tif", "lv_phi.tif")

    with zipfile.ZipFile(zip_path, "w") as zf:
        for suffix in good_suffixes:
            tif_path = tmp_path / f"_tmp_{suffix}"
            _write_utm_tif(tif_path, size=20)
            zf.write(tif_path, f"{scene}/{scene}_{suffix}")
            tif_path.unlink()

        # water_mask.tif on a completely different (non-overlapping) grid.
        bad_tif = tmp_path / "_tmp_water_mask.tif"
        _write_tif(bad_tif, np.zeros((5, 5), dtype="float32"), Affine(80.0, 0.0, 0.0, 0.0, -80.0, 0.0))
        zf.write(bad_tif, f"{scene}/{scene}_water_mask.tif")

    bbox = _wgs84_bbox_around_pixels(row_min=5, row_max=10, col_min=3, col_max=8)
    min_lon, min_lat, max_lon, max_lat = bbox
    geometry = {
        "type": "Polygon",
        "coordinates": [[[min_lon, min_lat], [max_lon, min_lat], [max_lon, max_lat], [min_lon, max_lat], [min_lon, min_lat]]],
    }

    adapter = MintPyAdapter(downloads_dir=downloads_dir, work_dir=tmp_path / "work")
    with pytest.raises(RuntimeError):
        adapter.unzip_all(geometry=geometry)

    assert zip_path.exists()  # partial capture - the source must not be discarded


def test_unzip_all_leaves_no_partial_directory_after_failure(tmp_path):
    """A failure midway must not leave a half-extracted directory behind:
    unzip_all skips any zip whose output directory exists, so a partial one
    would silently be treated as done on the next run."""
    downloads_dir = tmp_path / "downloads"
    downloads_dir.mkdir()
    scene = "S1AA_fake_scene"
    with zipfile.ZipFile(downloads_dir / f"{scene}.zip", "w") as zf:
        good = tmp_path / "_good.tif"
        _write_utm_tif(good, size=20)
        zf.write(good, f"{scene}/{scene}_unw_phase.tif")
        bad = tmp_path / "_bad.tif"
        _write_tif(bad, np.zeros((5, 5), dtype="float32"), Affine(80.0, 0.0, 0.0, 0.0, -80.0, 0.0))
        zf.write(bad, f"{scene}/{scene}_water_mask.tif")

    min_lon, min_lat, max_lon, max_lat = _wgs84_bbox_around_pixels(5, 10, 3, 8)
    geometry = {"type": "Polygon", "coordinates": [[[min_lon, min_lat], [max_lon, min_lat], [max_lon, max_lat], [min_lon, max_lat], [min_lon, min_lat]]]}
    adapter = MintPyAdapter(downloads_dir=downloads_dir, work_dir=tmp_path / "work")
    with pytest.raises(RuntimeError):
        adapter.unzip_all(geometry=geometry)

    assert not (downloads_dir / scene).exists()
    assert not any(p.name.startswith(".") for p in downloads_dir.iterdir())
    assert (downloads_dir / f"{scene}.zip").exists()
