"""ERA5 from the ARCO-ERA5 archive: the GRIB it writes, the cdsapi stand-in and the source choice."""
import importlib.util
import os
import sys
from pathlib import Path

import numpy as np
import pytest

from insar_core.pipeline import era5_arco, mintpy_adapter

pygrib = pytest.importorskip("pygrib")

LEVELS = np.array([1, 500, 850, 1000])
LATS = np.arange(90.0, -90.0001, -0.25)
LONS = np.arange(0.0, 360.0, 0.25)


class _Variable:
    """Array indexed [time, level, row, column] whose values depend on the position only."""

    def __init__(self, offset, scale, nan=False):
        self.offset, self.scale, self.nan = offset, scale, nan

    def __getitem__(self, key):
        _, _, rows, _ = key
        row = np.arange(len(LATS))[rows]
        out = (
            self.offset
            + np.arange(len(LEVELS))[:, None, None] * self.scale * 100
            + row[None, :, None] * self.scale
            + np.arange(len(LONS))[None, None, :] * self.scale * 0.1
        )
        return np.full_like(out, np.nan) if self.nan else out


def _archive(nan=False):
    return {
        "level": np.asarray(LEVELS), "latitude": LATS, "longitude": LONS,
        "geopotential": _Variable(1000.0, 1.0, nan), "temperature": _Variable(250.0, 0.01, nan),
        "specific_humidity": _Variable(0.001, 1e-5, nan),
    }


def _request(area, day="03", variables=("geopotential", "temperature", "specific_humidity")):
    return {
        "variable": list(variables), "year": ["2023"], "month": ["01"], "day": [day], "time": ["18:00"],
        "pressure_level": [str(v) for v in LEVELS], "area": area,
    }


def test_the_file_has_every_level_in_the_order_pyaps_reads(tmp_path):
    target = tmp_path / "era5.grb"
    era5_arco.retrieve(_request([41.0, -4.0, 39.0, -2.0]), target, archive=_archive())

    messages = pygrib.open(str(target)).read()
    assert [(m.shortName, m.level) for m in messages] == [
        (name, level) for level in LEVELS for name in ("z", "t", "q")
    ]
    assert {(m.dataDate, m.dataTime) for m in messages} == {(20230103, 1800)}
    assert not (tmp_path / "era5.grb.part").exists()


def test_the_grid_is_the_requested_area(tmp_path):
    target = tmp_path / "era5.grb"
    era5_arco.retrieve(_request([41.0, -4.0, 39.0, -2.0]), target, archive=_archive())

    lats, lons = pygrib.open(str(target)).read(1)[0].latlons()
    assert lats.shape == (9, 9)
    assert (lats[0, 0], lats[-1, 0]) == (41.0, 39.0)
    assert (lons[0, 0], lons[0, -1]) == (-4.0, -2.0)


def test_values_come_from_the_archive_at_the_right_position(tmp_path):
    target = tmp_path / "era5.grb"
    era5_arco.retrieve(_request([41.0, -4.0, 39.0, -2.0]), target, archive=_archive())

    t_850 = next(m for m in pygrib.open(str(target)).read() if m.shortName == "t" and m.level == 850)
    row0 = int(round((90.0 - 41.0) / 0.25))
    col0 = int(round(356.0 / 0.25))                       # -4 degrees east is 356
    expected = 250.0 + 2 * 0.01 * 100 + (row0 + np.arange(9))[:, None] * 0.01 + (col0 + np.arange(9))[None, :] * 0.001
    assert np.abs(t_850.values - expected).max() < 0.01   # 16 bit packing of a small range


def test_an_area_across_the_prime_meridian_wraps_the_columns(tmp_path):
    target = tmp_path / "era5.grb"
    era5_arco.retrieve(_request([41.0, -1.0, 40.0, 1.0]), target, archive=_archive())

    z = pygrib.open(str(target)).read(1)[0]
    lats, lons = z.latlons()
    assert (lons[0, 0], lons[0, -1]) == (-1.0, 1.0)
    first_column = 1000.0 + 0 + int(round((90.0 - 41.0) / 0.25)) * 1.0 + 1436 * 0.1   # -1 degree is column 1436
    assert abs(z.values[0, 0] - first_column) < 0.1
    last_column = 1000.0 + int(round((90.0 - 41.0) / 0.25)) * 1.0 + 4 * 0.1            # +1 degree is column 4
    assert abs(z.values[0, -1] - last_column) < 0.1


def test_a_date_the_archive_does_not_have_yet_fails_with_a_clear_message(tmp_path):
    with pytest.raises(RuntimeError, match="no geopotential for 2023-01-03 18:00"):
        era5_arco.retrieve(_request([41.0, -4.0, 39.0, -2.0]), tmp_path / "x.grb", archive=_archive(nan=True))
    assert not list(tmp_path.iterdir())     # nothing half written for PyAPS to pick up


def test_a_variable_that_is_not_supported_is_refused(tmp_path):
    with pytest.raises(ValueError, match="relative_humidity"):
        era5_arco.retrieve(_request([41.0, -4.0, 39.0, -2.0], variables=("relative_humidity",)), tmp_path / "x.grb", archive=_archive())


def test_an_area_outside_the_grid_is_refused(tmp_path):
    with pytest.raises(ValueError, match="outside"):
        era5_arco.retrieve(_request([95.0, -4.0, 39.0, -2.0]), tmp_path / "x.grb", archive=_archive())


# ── the cdsapi stand-in ─────────────────────────────────────────────────────

def _load_shim():
    path = Path(mintpy_adapter._CDS_SHIM_DIR) / "cdsapi" / "__init__.py"
    spec = importlib.util.spec_from_file_location("shim_cdsapi_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_shim_answers_the_request_pyaps_sends(tmp_path, monkeypatch):
    shim = _load_shim()
    seen = {}
    monkeypatch.setattr(shim._era5, "retrieve", lambda request, target: seen.update(request=request, target=target))

    client = shim.Client()      # PyAPS builds it with no arguments, or with a url and key
    shim.Client(url="https://example.invalid", key="x")
    result = client.retrieve("reanalysis-era5-pressure-levels", {"year": ["2023"]}, target=str(tmp_path / "a.grb"))

    assert seen == {"request": {"year": ["2023"]}, "target": str(tmp_path / "a.grb")}
    assert result == str(tmp_path / "a.grb")


def test_the_shim_refuses_other_datasets():
    with pytest.raises(ValueError, match="reanalysis-era5-single-levels"):
        _load_shim().Client().retrieve("reanalysis-era5-single-levels", {}, target="x")


def test_the_shim_is_found_first_only_when_the_archive_is_the_source(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/somewhere/else")
    assert mintpy_adapter._mintpy_env("arco")["PYTHONPATH"].split(os.pathsep) == [
        str(mintpy_adapter._CDS_SHIM_DIR), "/somewhere/else"]
    assert mintpy_adapter._mintpy_env("cds")["PYTHONPATH"] == "/somewhere/else"
    assert mintpy_adapter._mintpy_env(None)["PYTHONPATH"] == "/somewhere/else"


def test_the_installed_shim_is_a_package_named_cdsapi():
    assert (Path(mintpy_adapter._CDS_SHIM_DIR) / "cdsapi" / "__init__.py").is_file()
    assert sys.modules.get("cdsapi") is None or Path(mintpy_adapter._CDS_SHIM_DIR) not in Path(sys.modules["cdsapi"].__file__).parents


# ── which source is used ────────────────────────────────────────────────────

@pytest.fixture
def fresh_choice(monkeypatch):
    monkeypatch.setitem(mintpy_adapter._era5_checked, "at", None)
    monkeypatch.setitem(mintpy_adapter._era5_checked, "source", None)


def test_the_archive_is_preferred_then_the_cds_then_nothing(fresh_choice, monkeypatch):
    monkeypatch.setattr(mintpy_adapter, "cds_credentials_available", lambda: True)
    monkeypatch.setattr(era5_arco, "archive_reachable", lambda: True)
    assert mintpy_adapter.era5_source() == "arco"

    monkeypatch.setitem(mintpy_adapter._era5_checked, "at", None)
    monkeypatch.setattr(era5_arco, "archive_reachable", lambda: False)
    assert mintpy_adapter.era5_source() == "cds"

    monkeypatch.setitem(mintpy_adapter._era5_checked, "at", None)
    monkeypatch.setattr(mintpy_adapter, "cds_credentials_available", lambda: False)
    assert mintpy_adapter.era5_source() is None


def test_the_answer_is_kept_for_a_few_minutes(fresh_choice, monkeypatch):
    calls = []
    monkeypatch.setattr(mintpy_adapter, "cds_credentials_available", lambda: False)
    monkeypatch.setattr(era5_arco, "archive_reachable", lambda: calls.append(1) or True)
    assert mintpy_adapter.era5_source() == "arco"
    assert mintpy_adapter.era5_source() == "arco"
    assert len(calls) == 1
