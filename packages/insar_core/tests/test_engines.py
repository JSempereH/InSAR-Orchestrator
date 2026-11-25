"""Engines: what they declare, how MintPy is pointed at their files, and the NISAR preparation."""
from pathlib import Path

import pytest

from insar_core import engines
from insar_core.engines import Availability, Engine, EngineInfo, get_engine, list_engines, register_engine
from insar_core.pipeline import dem as dem_module
from insar_core.pipeline.mintpy_adapter import MintPyAdapter

GEOMETRY = {"type": "Polygon", "coordinates": [[[-1.70, 37.62], [-1.62, 37.62], [-1.62, 37.68], [-1.70, 37.68], [-1.70, 37.62]]]}


def test_the_two_engines_are_registered_and_the_default_is_hyp3():
    assert {e.info.id for e in list_engines()} >= {"hyp3_gamma", "nisar_gunw"}
    assert get_engine(None).info.id == "hyp3_gamma" and get_engine("nisar_gunw").info.sensor == "nisar"


def test_an_unknown_engine_names_the_ones_that_exist():
    with pytest.raises(KeyError, match="hyp3_gamma"):
        get_engine("isce2")


def test_what_each_engine_says_about_itself_is_complete():
    for engine in list_engines():
        described = engine.describe()
        assert {"id", "label", "sensor", "band", "acquisition", "costs_credits", "credentials", "available", "wavelength_m"} <= set(described)
    assert get_engine("hyp3_gamma").info.costs_credits and not get_engine("nisar_gunw").info.costs_credits
    assert get_engine("nisar_gunw").info.band == "L" and get_engine("nisar_gunw").info.wavelength_m == pytest.approx(0.238, abs=0.001)


def test_hyp3_options_are_exactly_what_the_app_always_wrote(tmp_path):
    options = get_engine("hyp3_gamma").load_options(tmp_path / "data", tmp_path / "work")
    data = (tmp_path / "data").resolve()
    assert options == {
        "mintpy.load.processor": "hyp3",
        "mintpy.load.unwFile": f"{data}/*/*unw_phase.tif", "mintpy.load.corFile": f"{data}/*/*corr.tif",
        "mintpy.load.demFile": f"{data}/*/*dem.tif", "mintpy.load.incAngleFile": f"{data}/*/*lv_theta.tif",
        "mintpy.load.azAngleFile": f"{data}/*/*lv_phi.tif", "mintpy.load.waterMaskFile": f"{data}/*/*water_mask.tif",
    }


def test_nisar_options_point_mintpy_at_the_gunw_files_and_the_dem(tmp_path):
    options = get_engine("nisar_gunw").load_options(tmp_path / "data", tmp_path / "work")
    assert options["mintpy.load.processor"] == "nisar" and options["mintpy.load.frequency"] == "A"
    assert options["mintpy.load.unwFile"].endswith("/data/*GUNW*.h5")
    assert options["mintpy.load.demFile"] == str((tmp_path / "work").resolve() / "dem/copernicus_dem.tif")


def test_the_config_file_is_written_from_the_engine(tmp_path):
    hyp3 = MintPyAdapter(tmp_path / "d", tmp_path / "w1").write_config(geometry=GEOMETRY)
    assert hyp3.read_text().startswith("mintpy.load.processor      = hyp3\n")
    assert "mintpy.load.unwFile        = " in hyp3.read_text() and "mintpy.subset.lalo" in hyp3.read_text()
    nisar = MintPyAdapter(tmp_path / "d", tmp_path / "w2", engine=get_engine("nisar_gunw")).write_config(
        geometry=GEOMETRY, extra_options={"mintpy.deramp": "linear"})
    text = nisar.read_text()
    assert "mintpy.load.processor      = nisar" in text and "GUNW" in text and "mintpy.deramp" in text and "mintpy.subset.lalo" in text
    assert "hyp3" not in text


def test_the_nisar_engine_is_available_when_mintpy_can_read_it():
    pytest.importorskip("mintpy.prep_nisar")
    assert get_engine("nisar_gunw").availability() == Availability(True)


def test_the_nisar_engine_says_why_it_is_unavailable_when_mintpy_is_too_old(monkeypatch):
    import builtins
    real = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "mintpy.prep_nisar":
            raise ImportError(name)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    result = get_engine("nisar_gunw").availability()
    assert not result.ok and "prep_nisar" in result.reason


def test_nisar_preparation_builds_the_dem_for_the_area_and_counts_the_granules(tmp_path, monkeypatch):
    data, work = tmp_path / "data", tmp_path / "work"
    data.mkdir()
    for name in ("NISAR_L2_PR_GUNW_a.h5", "NISAR_L2_PR_GUNW_b.h5", "NISAR_L2_PR_GUNW_a_QA_STATS.h5.txt"):
        (data / name).write_bytes(b"x")
    seen = {}
    monkeypatch.setattr(dem_module, "copernicus_dem", lambda bbox, dest: seen.update(bbox=bbox, dest=dest) or dest)
    adapter = MintPyAdapter(data, work, engine=get_engine("nisar_gunw"))
    assert adapter.prepare_inputs(GEOMETRY) == 2
    assert seen["dest"] == work / "dem/copernicus_dem.tif"
    west, south, east, north = seen["bbox"]
    assert west < -1.70 and east > -1.62 and south < 37.62 and north > 37.68      # the area plus a margin


def test_nisar_preparation_without_granules_or_area_says_what_is_missing(tmp_path):
    adapter = MintPyAdapter(tmp_path / "data", tmp_path / "work", engine=get_engine("nisar_gunw"))
    (tmp_path / "data").mkdir()
    with pytest.raises(FileNotFoundError, match="GUNW"):
        adapter.prepare_inputs(GEOMETRY)
    (tmp_path / "data" / "NISAR_L2_PR_GUNW_a.h5").write_bytes(b"x")
    with pytest.raises(ValueError, match="area of interest"):
        adapter.prepare_inputs(None)


def test_hyp3_preparation_is_the_existing_unzip(tmp_path, monkeypatch):
    adapter = MintPyAdapter(tmp_path, tmp_path / "work")
    monkeypatch.setattr(adapter, "unzip_all", lambda geometry=None: 7)
    assert adapter.prepare_inputs(GEOMETRY) == 7


def test_a_third_engine_plugs_in_without_touching_the_adapter(tmp_path):
    class Local(Engine):
        info = EngineInfo(id="local_test", label="Local", sensor="sentinel-1", band="C", product="p", acquisition="ready_made",
                          description="d", credentials=(), costs_credits=False, mintpy_processor="isce", wavelength_m=0.055)

        def load_options(self, data_dir, work_dir):
            return {"mintpy.load.processor": "isce", "mintpy.load.unwFile": str(data_dir / "*.unw")}

        def prepare_inputs(self, adapter, geometry):
            return 3

        def has_inputs(self, data_dir):
            return True

    try:
        register_engine(Local())
        adapter = MintPyAdapter(tmp_path, tmp_path / "w", engine=get_engine("local_test"))
        assert adapter.prepare_inputs(None) == 3
        assert "isce" in adapter.write_config().read_text()
    finally:
        engines._ENGINES.pop("local_test", None)


# ── the DEM ──────────────────────────────────────────────────────────────────

def _tile(path: Path, west: float, south: float, size=1.0, value=100.0):
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    n = 36
    data = np.full((1, n, n), value, "float32")
    with rasterio.open(path, "w", driver="GTiff", height=n, width=n, count=1, dtype="float32", crs="EPSG:4326",
                       transform=from_origin(west, south + size, size / n, size / n)) as out:
        out.write(data)
    return str(path)


def test_tile_names_follow_the_copernicus_convention():
    assert dem_module.tile_url(37, -2).endswith("Copernicus_DSM_COG_10_N37_00_W002_00_DEM/Copernicus_DSM_COG_10_N37_00_W002_00_DEM.tif")
    assert "S05_00_E010_00" in dem_module.tile_url(-5, 10)
    assert dem_module.tiles_for((-1.9, 37.1, -0.8, 37.8)) == [(37, -2), (37, -1)]
    assert dem_module.tiles_for((-1.9, 37.1, -1.2, 37.8)) == [(37, -2)]       # the area stops before the next tile


def test_the_dem_is_cut_from_the_tiles_that_cover_the_area(tmp_path):
    import rasterio

    tiles = [_tile(tmp_path / "a.tif", -2.0, 37.0, value=100.0), _tile(tmp_path / "b.tif", -1.0, 37.0, value=300.0)]
    out = dem_module.copernicus_dem((-1.5, 37.2, -0.5, 37.6), tmp_path / "out" / "dem.tif", urls=tiles)
    with rasterio.open(out) as dem:
        data = dem.read(1)
        assert dem.crs.to_epsg() == 4326 and dem.bounds.left == pytest.approx(-1.5, abs=0.05) and dem.bounds.right == pytest.approx(-0.5, abs=0.05)
        assert set(data.ravel().tolist()) == {100.0, 300.0}


def test_an_existing_dem_is_not_built_again(tmp_path):
    existing = tmp_path / "dem.tif"
    existing.write_bytes(b"already here")
    assert dem_module.copernicus_dem((0, 0, 1, 1), existing, urls=[]) == existing and existing.read_bytes() == b"already here"


def test_an_area_with_no_tile_is_an_error_that_says_so(tmp_path):
    with pytest.raises(dem_module.DEMError, match="No Copernicus DEM tile"):
        dem_module.copernicus_dem((0, 0, 1, 1), tmp_path / "dem.tif", urls=[str(tmp_path / "missing.tif")])
    with pytest.raises(dem_module.DEMError, match="empty"):
        dem_module.copernicus_dem((1, 1, 1, 1), tmp_path / "dem.tif", urls=[])


def test_nisar_applies_the_ionosphere_correction_by_default_and_hyp3_adds_nothing():
    assert get_engine("nisar_gunw").default_mintpy_options() == {"mintpy.ionosphericDelay.method": "split_spectrum"}
    assert get_engine("hyp3_gamma").default_mintpy_options() == {}
    assert get_engine("hyp3_gamma").has_inputs(Path("/nonexistent")) is False


def test_each_engine_knows_what_counts_as_an_input(tmp_path):
    (tmp_path / "nisar").mkdir()
    assert not get_engine("nisar_gunw").has_inputs(tmp_path / "nisar")
    (tmp_path / "nisar" / "NISAR_L2_PR_GUNW_a.h5").write_bytes(b"x")
    assert get_engine("nisar_gunw").has_inputs(tmp_path / "nisar") and not get_engine("hyp3_gamma").has_inputs(tmp_path / "nisar")
    (tmp_path / "hyp3").mkdir()
    (tmp_path / "hyp3" / "pair.zip").write_bytes(b"x")
    assert get_engine("hyp3_gamma").has_inputs(tmp_path / "hyp3")
