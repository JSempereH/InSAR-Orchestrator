"""The report of a result, from a small synthetic work directory."""
import datetime as dt

import h5py
import numpy as np
import pytest

from insar_core.pipeline import report

from test_mintpy_export import DATES, work  # noqa: F401  (a fixture)


def _add_network(work_dir, drop_last=False):
    (work_dir / "inputs").mkdir(exist_ok=True)
    pairs = [(DATES[0], DATES[1]), (DATES[1], DATES[2]), (DATES[0], DATES[2])]
    with h5py.File(work_dir / "inputs" / "ifgramStack.h5", "w") as handle:
        handle.create_dataset("date", data=np.array([[a.encode(), b.encode()] for a, b in pairs]))
        handle.create_dataset("dropIfgram", data=np.array([True, True, not drop_last]))
        handle.create_dataset("bperp", data=np.array([40.0, -15.0, 25.0], "float32"))


def test_the_facts_of_a_run_are_read_from_its_files(work):  # noqa: F811
    result = report.read_result(work)
    assert result["dates"] == DATES and result["stats"]["kept"] == 47 and result["stats"]["pixels"] == 48
    assert result["reference"]["row"] == 1 and result["reference"]["date"] == "2023-01-03"
    assert result["stats"]["std_median"] == pytest.approx(2.0)
    assert result["corrections"] == []                                  # the plain inversion, nothing removed


def test_the_corrections_are_named_from_the_series_behind_the_velocity(work):  # noqa: F811
    (work / "timeseries.h5").rename(work / "timeseries_ERA5_demErr.h5")
    with h5py.File(work / "velocity.h5", "a") as handle:
        handle.attrs["FILE_PATH"] = "/somewhere/else/timeseries_ERA5_demErr.h5"
    corrections = report.read_result(work)["corrections"]
    assert len(corrections) == 2 and "ERA5" in corrections[0] and "DEM error" in corrections[1]


def test_the_network_gives_one_baseline_per_date_consistent_with_the_pairs(work):  # noqa: F811
    _add_network(work, drop_last=True)
    network = report.read_network(work)
    assert network["dates"] == DATES and list(network["used"]) == [True, True, False]
    baseline = dict(zip(network["dates"], network["baseline"], strict=False))
    assert baseline[DATES[0]] == pytest.approx(0.0, abs=1e-6)
    assert baseline[DATES[1]] - baseline[DATES[0]] == pytest.approx(40.0, abs=1e-3)
    assert baseline[DATES[2]] - baseline[DATES[1]] == pytest.approx(-15.0, abs=1e-3)
    assert network["temporal_days"] == {"median": 12.0, "max": 24}
    assert report.read_network(work / "nowhere") is None


def test_a_run_alone_gets_a_complete_report_that_says_it_was_not_validated(work):  # noqa: F811
    _add_network(work)
    page = report.build_report(work, title="Test area", generated_at=dt.datetime(2026, 1, 2, 3, 4, tzinfo=dt.timezone.utc))
    assert page.startswith("<!doctype html>") and "<h1>Test area</h1>" in page and "Generated 2026-01-02 03:04 UTC" in page
    assert "has not been compared with independent data" in page
    assert "2023-01-03 to 2023-01-27" in page and "row 1, column 2" in page
    assert "Yunjun" in page and "Berardino" in page and "Copernicus Sentinel" in page
    assert "Hersbach" not in page and "Blewitt" not in page             # only what this result used is cited
    assert "http://" not in page and "https://" not in page             # nothing is loaded from outside
    assert chr(0x2014) not in page and chr(0x2013) not in page


def test_figures_are_inside_the_page(work):  # noqa: F811
    pytest.importorskip("matplotlib")
    _add_network(work)
    page = report.build_report(work)
    assert page.count("data:image/png;base64,") == 3


def test_project_validation_and_software_are_reported_and_cited(work):  # noqa: F811
    (work / "timeseries.h5").rename(work / "timeseries_ERA5.h5")
    with h5py.File(work / "velocity.h5", "a") as handle:
        handle.attrs["FILE_PATH"] = "timeseries_ERA5.h5"
    provenance = {
        "project": {"name": "Lorca <2023>", "description": "Subsidence & pumping", "track_number": 8, "flight_direction": "DESCENDING"},
        "engine": {"id": "hyp3_burst", "label": "Sentinel-1 through HyP3, by bursts", "sensor": "sentinel-1", "band": "C"},
        "batches": [{"parameters": {"processor": "HyP3 INSAR_ISCE_MULTI_BURST", "hyp3": {"looks": "20x4"},
                                    "pairing": {"method": "temporal_neighbors", "max_temporal_neighbors": 3}}}],
        "last_mintpy_run": {"software": {"insar-core": "0.1.0", "mintpy": "1.6.4"},
                            "mintpy": {"options": {"mintpy.deramp": "linear", "mintpy.load.processor": "hyp3", "mintpy.reference.yx": "auto"}}},
    }
    stats = {"n": 500, "rmse_mm_yr": 1.8, "pearson_r": 0.94, "slope_b_on_a": 0.9, "slope_orthogonal": 0.97, "bias_mm_yr": 0.0}
    validation = {
        "egms": {"source_name": "EGMS L2B", "verdict": {"status": "ok", "message": "MintPy recovers the same pattern."},
                 "window": ["20230103", "20230127"], "dates": 3, "velocity_rereferenced": stats,
                 "common_offset_mm_yr": {"mintpy": 0.4, "other": -0.2}, "computed_at": "2026-01-01T10:00:00+00:00",
                 "uncertainty": {"message": "The stated error is about 2.6 times too small.", "recommended_scale": 2.6}},
        "gnss": {"source_name": "GNSS (Nevada Geodetic Laboratory)", "verdict": {"status": "warn", "message": "2 stations."},
                 "common_offset_mm_yr": 1.1, "computed_at": "2026-01-01T10:05:00+00:00",
                 "plate": {"plate": "EURA", "name": "Eurasia"}, "citations": ["Blewitt et al. (2018)", "Altamimi et al. (2017)"],
                 "stations": [{"name": "LORC", "dates": 3, "gnss_mm_yr": -80.0, "insar_mm_yr": -76.5, "series_rmse_mm": 4.2,
                               "difference_rereferenced_mm_yr": 0.5,
                               "steps": [{"date": "20230110", "kind": "equipment", "action": "removed", "detail": "Antenna"}]}],
                 "calibration": {"applied": True, "offset_mm_yr": -1.1, "offset_sigma_mm_yr": 0.7, "rmse_before_mm_yr": 2.0,
                                 "loo_rmse_after_mm_yr": 1.2, "verdict": {"status": "ok", "message": "Shift applied."}}},
        "other": None,
    }
    page = report.build_report(work, provenance=provenance, validation=validation)
    assert "Lorca &lt;2023&gt;" in page and "Subsidence &amp; pumping" in page          # text from a person is escaped
    assert "Tropospheric delay removed with ERA5" in page and "Hersbach" in page and "Jolivet" in page
    assert "by bursts" in page and "Rosen" in page and "Werner" not in page             # ISCE2 for bursts, not GAMMA
    assert "0.97" in page and "2.60" in page and "Eurasia plate" in page
    assert "LORC" in page and "2023-01-10 equipment, removed" in page
    assert "Blewitt et al. (2018)" in page and "Crosetto" in page
    assert "mintpy.deramp = linear" in page and "mintpy.reference.yx" not in page.split("as JSON")[0]
    assert "has not been compared" not in page


def test_a_folder_without_results_is_refused(tmp_path):
    with pytest.raises(FileNotFoundError):
        report.build_report(tmp_path)


def test_the_report_is_written_whole(work, tmp_path):  # noqa: F811
    out = report.write_report(work, tmp_path / "out" / "report.html", title="X")
    assert out.read_text(encoding="utf-8").endswith("</html>") and not list(out.parent.glob("*.draft.html"))
