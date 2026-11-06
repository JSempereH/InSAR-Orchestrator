from app.services import series_analysis
from app.services.series_analysis import with_analysis


def _series(n: int = 30):
    import datetime as dt
    dates = [(dt.date(2020, 1, 7) + dt.timedelta(days=12 * i)).strftime("%Y%m%d") for i in range(n)]
    return {"dates": dates, "values_mm": [-0.5 * i for i in range(n)]}


def test_with_analysis_adds_the_summary_and_keeps_the_series():
    result = with_analysis(_series())
    assert result["dates"] == _series()["dates"]
    assert result["analysis"]["model"]["kind"] == "linear"


def test_with_analysis_is_none_for_a_short_series():
    assert with_analysis(_series(3))["analysis"] is None


def test_a_failing_analysis_never_hides_the_series(monkeypatch):
    def broken(dates, values):
        raise RuntimeError("numerical trouble")

    monkeypatch.setattr(series_analysis, "analyze_time_series", broken)
    result = with_analysis(_series())
    assert result["analysis"] is None
    assert len(result["values_mm"]) == 30


def test_with_analysis_does_not_mutate_its_input():
    original = _series()
    with_analysis(original)
    assert "analysis" not in original
