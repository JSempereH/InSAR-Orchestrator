"""Perpendicular baselines on the scenes the ASF adapter returns."""
import datetime as dt
import sys
import types

import pytest

from insar_core.adapters import asf as asf_module
from insar_core.adapters.asf import ASFAdapter, _attach_baselines, _repair_ascending_node
from insar_core.models.scene import AOI, SearchParams


class _Result:
    def __init__(self, name, day, track=8, direction="DESCENDING", baseline=None):
        self.properties = {
            "sceneName": name, "fileID": f"{name}-SLC", "startTime": f"{day}T06:11:02Z", "orbit": 1,
            "pathNumber": track, "flightDirection": direction, "polarization": "VV+VH", "bytes": 4_000_000_000,
            "url": f"https://example.invalid/{name}.zip", "fileName": f"{name}.zip",
        }
        self.baseline = baseline if baseline is not None else {}


def _scenes(results):
    return [asf_module._result_to_scene(r) for r in results]


@pytest.fixture()
def fake_calculation(monkeypatch):
    """asf_search's own function, replaced by one that gives each scene 10 m more than the last."""
    calls = []

    def calculate(reference, stack):
        calls.append((reference, [r.properties["sceneName"] for r in stack]))
        for k, item in enumerate(stack):
            item.properties["perpendicularBaseline"] = None if k == 0 else 10.0 * k
        return stack[1:]          # like asf_search: the reference itself is not returned

    module = types.ModuleType("asf_search.baseline")
    module.calculate_perpendicular_baselines = calculate
    monkeypatch.setitem(sys.modules, "asf_search.baseline", module)
    return calls


def test_each_scene_gets_its_baseline_relative_to_the_first_of_its_stack(fake_calculation):
    results = [_Result(f"S{k}", f"2023-01-{3 + 12 * k:02d}" if k < 2 else f"2023-02-{k:02d}") for k in range(4)]
    scenes = _scenes(results)
    _attach_baselines(results, scenes)
    assert [s.perpendicular_baseline_m for s in scenes] == [0.0, 10.0, 20.0, 30.0]       # the anchor is 0 by definition
    assert fake_calculation == [("S0", ["S0", "S1", "S2", "S3"])]


def test_tracks_and_directions_are_separate_stacks_with_their_own_anchor(fake_calculation):
    results = [_Result("A0", "2023-01-03", track=8), _Result("A1", "2023-01-15", track=8),
               _Result("B0", "2023-01-04", track=15, direction="ASCENDING"), _Result("B1", "2023-01-16", track=15, direction="ASCENDING")]
    scenes = _scenes(results)
    _attach_baselines(results, scenes)
    assert [c[0] for c in fake_calculation] == ["A0", "B0"]
    assert [s.perpendicular_baseline_m for s in scenes] == [0.0, 10.0, 0.0, 10.0]


def test_an_absurd_value_is_treated_as_unknown(monkeypatch):
    def calculate(reference, stack):
        stack[1].properties["perpendicularBaseline"] = 250_000.0
        return stack[1:]

    module = types.ModuleType("asf_search.baseline")
    module.calculate_perpendicular_baselines = calculate
    monkeypatch.setitem(sys.modules, "asf_search.baseline", module)
    results = [_Result("S0", "2023-01-03"), _Result("S1", "2023-01-15")]
    scenes = _scenes(results)
    _attach_baselines(results, scenes)
    assert scenes[0].perpendicular_baseline_m == 0.0 and scenes[1].perpendicular_baseline_m is None


def test_a_failing_calculation_leaves_the_baselines_unknown_and_does_not_break_the_search(monkeypatch):
    def calculate(reference, stack):
        raise RuntimeError("no orbit")

    module = types.ModuleType("asf_search.baseline")
    module.calculate_perpendicular_baselines = calculate
    monkeypatch.setitem(sys.modules, "asf_search.baseline", module)
    results = [_Result("S0", "2023-01-03"), _Result("S1", "2023-01-15")]
    scenes = _scenes(results)
    _attach_baselines(results, scenes)
    assert [s.perpendicular_baseline_m for s in scenes] == [None, None]


def test_a_record_with_the_ascending_node_of_the_previous_orbit_is_repaired():
    result = _Result("S0", "2023-01-03", baseline={
        "ascendingNodeTime": "2023-01-03T05:30:00Z",
        "stateVectors": {"positions": {"prePositionTime": "2023-01-03T07:09:29Z"}},     # 5969 s later: one orbit too late
    })
    _repair_ascending_node(result)
    assert result.baseline["ascendingNodeTime"] == "2023-01-03T07:08:49Z"                 # moved by one period of 5929 s


def test_a_normal_record_is_left_alone():
    baseline = {"ascendingNodeTime": "2023-01-03T06:00:00Z", "stateVectors": {"positions": {"prePositionTime": "2023-01-03T06:03:00Z"}}}
    result = _Result("S0", "2023-01-03", baseline=dict(baseline))
    _repair_ascending_node(result)
    assert result.baseline["ascendingNodeTime"] == "2023-01-03T06:00:00Z"
    _repair_ascending_node(_Result("S1", "2023-01-03"))          # no orbit data at all: nothing to do, no error


def test_search_returns_scenes_with_baselines_for_the_scenes_it_keeps(monkeypatch, fake_calculation):
    results = [_Result("S0", "2023-01-03"), _Result("S1", "2023-01-15"), _Result("S1dup", "2023-01-15")]
    adapter = ASFAdapter()
    monkeypatch.setattr(adapter, "_raw_search", lambda params: results)
    params = SearchParams(aoi=AOI.from_bbox(0, 0, 1, 1), date_start=dt.date(2023, 1, 1), date_end=dt.date(2023, 2, 1))
    scenes = adapter.search(params)
    assert [s.granule_name for s in scenes] == ["S0", "S1"]            # one scene per date
    assert [s.perpendicular_baseline_m for s in scenes] == [0.0, 10.0]
    assert fake_calculation[0][1] == ["S0", "S1"]                        # the duplicate never enters the stack


def test_search_keeps_the_scene_that_covers_more_of_the_area(monkeypatch):
    """With two frames on one date, the one covering more of the area is kept, whatever the order."""
    from datetime import date
    from types import SimpleNamespace

    from insar_core.adapters import asf as asf_module
    from insar_core.models.scene import AOI, SearchParams

    def result(name, west, east):
        return SimpleNamespace(
            properties={"fileID": f"{name}-SLC", "sceneName": name, "startTime": "2024-01-05T06:00:00Z",
                        "pathNumber": 8, "flightDirection": "DESCENDING", "polarization": "VV+VH"},
            geometry={"type": "Polygon", "coordinates": [[[west, 0], [east, 0], [east, 1], [west, 1], [west, 0]]]},
        )

    params = SearchParams(aoi=AOI.from_bbox(0.0, 0.0, 1.0, 1.0), date_start=date(2024, 1, 1), date_end=date(2024, 2, 1))
    monkeypatch.setattr(asf_module, "_attach_baselines", lambda results, scenes: None)
    for results in ([result("PARTIAL", 0.8, 2.0), result("FULL", -1.0, 0.9)], [result("FULL", -1.0, 0.9), result("PARTIAL", 0.8, 2.0)]):
        adapter = asf_module.ASFAdapter()
        monkeypatch.setattr(adapter, "_raw_search", lambda p, results=results: results)
        assert [s.granule_name for s in adapter.search(params)] == ["FULL"]
