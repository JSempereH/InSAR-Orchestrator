"""The logic of the benchmark that decides whether a result moved. The real cases need the real folders."""
import json

import pytest

from app import benchmark


def _comparison(rmse=3.7, stable_rmse=2.5, bias=2.0):
    return {
        "velocity_rereferenced": {"n": 1000, "rmse_mm_yr": rmse, "mad_mm_yr": 2.2, "pearson_r": 0.92, "slope_b_on_a": 0.86},
        "timeseries_rmse_mm": {"median": 3.9},
        "by_velocity": [
            {"from": None, "to": -3.0, "n": 100, "bias_mm_yr": bias, "rmse_mm_yr": 4.0},
            {"from": -3.0, "to": 3.0, "n": 800, "bias_mm_yr": 0.3, "rmse_mm_yr": stable_rmse},
            {"from": 3.0, "to": None, "n": 100, "bias_mm_yr": -3.0, "rmse_mm_yr": 4.5},
        ],
    }


def _result(case="a", rmse=3.7, bias=2.0, gnss=-14.0, qc_status="ok"):
    return {
        "id": case, "measured_at": "2026-11-23T00:00:00+00:00", "direction": "DESCENDING", "track": "8", "dates": 31,
        "process": {"interferograms": {"in_stack": 90, "used": 87, "longest_used_days": 36}, "unwrap_correction": True,
                    "fading_correction": False, "mask_refinement": False}, "egms": benchmark.egms_metrics(_comparison(rmse=rmse, bias=bias)),
        "gnss": benchmark.gnss_metrics({"stations": [{"name": "LOR1", "gnss_mm_yr": -64.9, "insar_mm_yr": gnss, "dates": 13, "coverage": 0.4}]}),
        "precision": {"sigma_10km_mm_yr": 2.9, "ratio_to_stated": 2.1},
        "qc": {"status": qc_status, "checks": {"steep_phase": {"status": qc_status, "value": 0.9}}},
    }


def test_the_numbers_kept_from_an_egms_comparison_include_the_stable_band_and_the_bias_by_band():
    metrics = benchmark.egms_metrics(_comparison())
    assert metrics["stable_rmse_mm_yr"] == 2.5 and metrics["rmse_mm_yr"] == 3.7
    assert [b["bias_mm_yr"] for b in metrics["bands"]] == [2.0, 0.3, -3.0]


def test_a_run_identical_to_its_baseline_has_nothing_to_report():
    baseline = benchmark.to_baseline([_result()])
    assert benchmark.compare_with_baseline([_result()], baseline) == []
    assert "Nothing moved" in benchmark.format_changes([])


def test_a_number_that_moves_more_than_its_tolerance_is_reported_and_a_small_move_is_not():
    baseline = benchmark.to_baseline([_result()])
    assert benchmark.compare_with_baseline([_result(rmse=3.8)], baseline) == []                    # 0.1 of 0.3 allowed
    moved = benchmark.compare_with_baseline([_result(rmse=4.2, bias=3.5, gnss=-27.0)], baseline)
    names = {m["what"] for m in moved}
    assert names == {"egms.rmse_mm_yr", "egms.band[None,-3.0].band_bias_mm_yr", "gnss.LOR1.gnss_insar_mm_yr"}
    assert "change +0.5" in benchmark.format_changes(moved)


def test_a_quality_check_that_changes_status_is_reported_even_when_the_numbers_do_not_move():
    baseline = benchmark.to_baseline([_result(qc_status="ok")])
    moved = benchmark.compare_with_baseline([_result(qc_status="warn")], baseline)
    assert [m["what"] for m in moved] == ["qc.steep_phase"] and moved[0]["was"] == "ok" and moved[0]["now"] == "warn"


def test_a_case_that_is_new_or_cannot_be_measured_is_said():
    baseline = benchmark.to_baseline([_result("a")])
    assert benchmark.compare_with_baseline([_result("b")], baseline)[0]["note"] == "not in the baseline"
    broken = {"id": "a", "measured_at": "x", "error": "The MintPy result cannot be read"}
    assert benchmark.compare_with_baseline([broken], baseline)[0]["note"] == "could not be measured"
    assert "a" not in benchmark.to_baseline([broken])                                              # never recorded as a baseline


def test_a_measure_that_appears_or_disappears_is_reported():
    baseline = benchmark.to_baseline([_result()])
    without_gnss = _result()
    del without_gnss["gnss"]
    moved = benchmark.compare_with_baseline([without_gnss], baseline)
    assert moved and moved[0]["note"] == "measured before and not now"


def test_a_manifest_with_a_missing_field_or_a_repeated_id_is_refused(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps({"cases": [{"id": "a"}]}))
    with pytest.raises(benchmark.BenchmarkError, match="work_dir"):
        benchmark.load_cases(path)
    path.write_text(json.dumps({"cases": [{"id": "a", "work_dir": "x"}, {"id": "a", "work_dir": "y"}]}))
    with pytest.raises(benchmark.BenchmarkError, match="twice"):
        benchmark.load_cases(path)
    path.write_text(json.dumps({"cases": [{"id": "a", "work_dir": "x", "egms": {"destination": "d"}}]}))
    with pytest.raises(benchmark.BenchmarkError, match="files"):
        benchmark.load_cases(path)
    with pytest.raises(benchmark.BenchmarkError, match="cannot be read"):
        benchmark.load_cases(tmp_path / "missing.json")


def test_the_manifest_that_ships_is_valid():
    cases = benchmark.load_cases(benchmark.Path(__file__).parent.parent / "benchmark" / "cases.json")
    assert {"lorca-desc-2023", "madrid-asc-2023"} <= {c["id"] for c in cases}


def test_the_command_line_writes_a_baseline_then_passes_against_it_and_fails_when_a_result_moves(tmp_path, monkeypatch, capsys):
    manifest = tmp_path / "cases.json"
    manifest.write_text(json.dumps({"cases": [{"id": "a", "work_dir": "x"}]}))
    current = {"result": _result("a")}
    monkeypatch.setattr(benchmark, "run_case", lambda case, with_gnss=True: current["result"])
    baseline = tmp_path / "baseline.json"
    assert benchmark.main(["--cases", str(manifest), "--update-baseline", str(baseline)]) == 0
    assert benchmark.main(["--cases", str(manifest), "--baseline", str(baseline)]) == 0
    current["result"] = _result("a", rmse=5.0)
    assert benchmark.main(["--cases", str(manifest), "--baseline", str(baseline)]) == 1
    assert "egms.rmse_mm_yr" in capsys.readouterr().out
    assert benchmark.main(["--cases", str(manifest), "--only", "nope"]) == 2


@pytest.mark.skipif(not __import__("os").environ.get("RUN_REAL_DATA"), reason="Needs the real folders and takes minutes: set RUN_REAL_DATA=1")
def test_the_real_cases_still_give_the_numbers_of_the_baseline():
    root = benchmark.Path(__file__).parent.parent / "benchmark"
    baseline = json.loads((root / "baseline.json").read_text(encoding="utf-8"))
    cases = [c for c in benchmark.load_cases(root / "cases.json") if c["id"] in baseline]
    results = [benchmark.run_case(case, with_gnss=False) for case in cases]
    moved = [m for m in benchmark.compare_with_baseline(results, baseline) if not m["what"].startswith("gnss.")]
    assert not moved, benchmark.format_changes(moved)
