"""Sentinel-1 by bursts: the search, the price, the submission and the engine.

The catalogue entries are shaped like the ones the ASF burst catalogue returns (checked against a
live search over Madrid): one entry per burst and polarization, with the burst identifier under
`burst.fullBurstID` and a name like S1_172670_IW2_20240209T061840_VV_9822-BURST."""
from datetime import date
from types import SimpleNamespace

import pytest

from insar_core.adapters import asf_burst
from insar_core.adapters import hyp3 as hyp3_module
from insar_core.adapters.asf_burst import ASFBurstAdapter, BurstError, burst_credits
from insar_core.engines import get_engine
from insar_core.models.job import JobStatus
from insar_core.models.scene import AOI, SearchParams
from insar_core.pipeline.orchestrator import InSAROrchestrator, credits_per_pair_for

BURSTS = (("172669", "IW2"), ("172669", "IW3"), ("172670", "IW2"))
PARAMS = SearchParams(aoi=AOI.from_bbox(-3.75, 40.38, -3.65, 40.46), date_start=date(2024, 1, 1), date_end=date(2024, 3, 1))


def _entry(day, number, swath, polarization="VV", track=81, direction="DESCENDING"):
    name = f"S1_{number}_{swath}_{day.replace('-', '')}T061840_{polarization}_AB12-BURST"
    return SimpleNamespace(
        properties={"sceneName": name, "fileID": name, "startTime": f"{day}T06:18:41Z", "pathNumber": track,
                    "flightDirection": direction, "polarization": polarization, "orbit": 52478, "bytes": "160000000",
                    "burst": {"fullBurstID": f"{track:03d}_{number}_{swath}"}},
        geometry={"type": "Polygon", "coordinates": []},
    )


def _catalogue(days, bursts=BURSTS, **kwargs):
    return [_entry(day, number, swath, polarization, **kwargs) for day in days for number, swath in bursts for polarization in ("VV", "VH")]


def _adapter(monkeypatch, results):
    adapter = ASFBurstAdapter()
    monkeypatch.setattr(adapter, "_raw_search", lambda params: results)
    monkeypatch.setattr(asf_burst, "_attach_baselines", lambda results, scenes: None)
    return adapter


# ── search ───────────────────────────────────────────────────────────────────

def test_each_date_is_one_scene_made_of_its_co_polarized_bursts(monkeypatch):
    scenes = _adapter(monkeypatch, _catalogue(["2024-01-04", "2024-01-16", "2024-01-28"])).search(PARAMS)
    assert [s.acquisition_date for s in scenes] == [date(2024, 1, 4), date(2024, 1, 16), date(2024, 1, 28)]
    first = scenes[0]
    assert first.burst_ids == ("081_172669_IW2", "081_172669_IW3", "081_172670_IW2")
    names = asf_burst.split_granules(first.granule_name)
    assert len(names) == 3 and all(n.endswith("_VV_AB12-BURST") for n in names)        # the cross-polarized ones are out
    assert [n.split("_")[1:3] for n in names] == [["172669", "IW2"], ["172669", "IW3"], ["172670", "IW2"]]
    assert first.track_number == 81 and first.size_mb == pytest.approx(480.0)


def test_the_order_of_the_bursts_does_not_depend_on_the_catalogue(monkeypatch):
    results = _catalogue(["2024-01-04", "2024-01-16"])
    forward = _adapter(monkeypatch, results).search(PARAMS)
    backward = _adapter(monkeypatch, list(reversed(results))).search(PARAMS)
    assert [s.granule_name for s in forward] == [s.granule_name for s in backward]


def test_a_date_that_lacks_a_burst_of_the_stack_is_left_out(monkeypatch):
    days = [f"2024-01-{d:02d}" for d in (4, 16, 28)] + ["2024-02-09", "2024-02-21"]
    results = [r for r in _catalogue(days)
               if not (r.properties["startTime"].startswith("2024-01-16") and r.properties["burst"]["fullBurstID"].endswith("172670_IW2"))]
    scenes = _adapter(monkeypatch, results).search(PARAMS)
    assert date(2024, 1, 16) not in [s.acquisition_date for s in scenes] and len(scenes) == 4
    assert all(len(s.burst_ids) == 3 for s in scenes)


def test_a_burst_seen_on_few_dates_is_not_part_of_the_stack(monkeypatch):
    days = [f"2024-01-{d:02d}" for d in (4, 16, 28)] + ["2024-02-09", "2024-02-21", "2024-03-04"]
    rare = [_entry("2024-01-04", "172671", "IW2")]                      # the area touches it on one date only
    scenes = _adapter(monkeypatch, _catalogue(days) + rare).search(PARAMS)
    assert len(scenes) == 6 and all("081_172671_IW2" not in s.burst_ids for s in scenes)


def test_two_tracks_are_refused_with_what_to_do(monkeypatch):
    results = _catalogue(["2024-01-04"]) + _catalogue(["2024-01-05"], track=154)
    with pytest.raises(BurstError, match="more than one track"):
        _adapter(monkeypatch, results).search(PARAMS)


def test_an_area_of_more_than_fifteen_bursts_is_refused(monkeypatch):
    many = tuple((f"{172600 + k}", "IW2") for k in range(16))
    with pytest.raises(BurstError, match="16 bursts"):
        _adapter(monkeypatch, _catalogue(["2024-01-04", "2024-01-16"], bursts=many)).search(PARAMS)


def test_tracks_are_counted_by_date_not_by_burst(monkeypatch):
    results = _catalogue(["2024-01-04", "2024-01-16"]) + _catalogue(["2024-01-05"], track=154)
    tracks = _adapter(monkeypatch, results).available_tracks(PARAMS)
    assert [(t.track_number, t.scene_count) for t in tracks] == [(81, 2), (154, 1)]


# ── price ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bursts, looks, credits", [
    (1, "20x4", 1), (3, "20x4", 1), (4, "20x4", 1), (5, "20x4", 5), (13, "20x4", 10),
    (3, "10x2", 1), (4, "10x2", 5), (2, "5x1", 5), (15, "5x1", 110), (1, "5x1", 1),
])
def test_credits_follow_the_hyp3_table(bursts, looks, credits):
    assert burst_credits(bursts, looks) == credits


def test_credits_refuse_what_hyp3_does_not_offer():
    with pytest.raises(BurstError):
        burst_credits(16)
    with pytest.raises(BurstError):
        burst_credits(3, "2x1")


def test_a_plan_of_burst_scenes_costs_a_tenth_of_a_plan_of_whole_scenes(monkeypatch):
    adapter = _adapter(monkeypatch, _catalogue(["2024-01-04", "2024-01-16", "2024-01-28", "2024-02-09"]))
    plan = InSAROrchestrator(adapter, processor=None).plan_pairs(PARAMS)
    assert len(plan.pairs) == 6 and plan.credits == 6                   # whole scenes would be 60
    assert credits_per_pair_for(plan.scenes, "5x1") == 10
    assert credits_per_pair_for([SimpleNamespace(burst_ids=None)], "10x2") == 15


# ── submission ───────────────────────────────────────────────────────────────

class _Client:
    def __init__(self):
        self.calls = []

    def _job(self, job_type, parameters):
        return [SimpleNamespace(job_id="job-1", job_type=job_type, job_parameters=parameters, status_code="PENDING",
                                credit_cost=1, logs=[], request_time=None)]

    def submit_insar_isce_burst_job(self, granule1, granule2, **kwargs):
        self.calls.append(("burst", granule1, granule2, kwargs))
        return self._job("INSAR_ISCE_BURST", {"granules": [granule1, granule2], **kwargs})

    def submit_insar_isce_multi_burst_job(self, reference, secondary, **kwargs):
        self.calls.append(("multi", reference, secondary, kwargs))
        # HyP3 may hand the lists back in another order
        return self._job("INSAR_ISCE_MULTI_BURST", {"reference": list(reversed(reference)), "secondary": list(reversed(secondary)), **kwargs})

    def submit_insar_job(self, granule1, granule2, **kwargs):
        self.calls.append(("scene", granule1, granule2, kwargs))
        return self._job("INSAR_GAMMA", {"granules": [granule1, granule2], **kwargs})


def _hyp3(monkeypatch):
    client = _Client()
    monkeypatch.setattr(hyp3_module.hyp3_sdk, "HyP3", lambda *args, **kwargs: client)
    return hyp3_module.HyP3Adapter(), client


def test_several_bursts_go_to_the_multi_burst_job_and_the_pair_keeps_its_name(monkeypatch):
    scenes = _adapter(monkeypatch, _catalogue(["2024-01-04", "2024-01-16"])).search(PARAMS)
    adapter, client = _hyp3(monkeypatch)
    # the parameters stored for a whole-scene project must not break a burst submission
    job = adapter.submit_pair(scenes[0].granule_name, scenes[1].granule_name, name="test", looks="10x2", include_dem=True)
    kind, reference, secondary, kwargs = client.calls[0]
    assert kind == "multi" and len(reference) == len(secondary) == 3
    assert [r.split("_")[1:3] for r in reference] == [s.split("_")[1:3] for s in secondary]       # burst by burst
    assert kwargs == {"name": "test", "looks": "10x2", "apply_water_mask": False}
    assert (job.reference_granule, job.secondary_granule) == (scenes[0].granule_name, scenes[1].granule_name)
    assert (job.reference_date, job.secondary_date) == (date(2024, 1, 4), date(2024, 1, 16))
    assert job.status == JobStatus.PENDING


def test_one_burst_goes_to_the_single_burst_job(monkeypatch):
    scenes = _adapter(monkeypatch, _catalogue(["2024-01-04", "2024-01-16"], bursts=BURSTS[:1])).search(PARAMS)
    adapter, client = _hyp3(monkeypatch)
    job = adapter.submit_pair(scenes[0].granule_name, scenes[1].granule_name)
    assert client.calls[0][0] == "burst" and client.calls[0][1] == scenes[0].granule_name
    assert job.reference_date == date(2024, 1, 4)


def test_whole_scenes_still_go_to_insar_gamma(monkeypatch):
    adapter, client = _hyp3(monkeypatch)
    reference = "S1A_IW_SLC__1SDV_20240104T061826_20240104T061854_051953_064700_B036"
    secondary = "S1A_IW_SLC__1SDV_20240116T061826_20240116T061854_052128_064D00_1F0B"
    job = adapter.submit_pair(reference, secondary)
    assert client.calls[0][0] == "scene" and client.calls[0][3]["include_look_vectors"] is True
    assert job.reference_date == date(2024, 1, 4)


def test_dates_with_a_different_number_of_bursts_are_refused(monkeypatch):
    adapter, _ = _hyp3(monkeypatch)
    one = "S1_172669_IW2_20240104T061840_VV_AB12-BURST"
    two = "S1_172669_IW2_20240116T061840_VV_AB12-BURST+S1_172670_IW2_20240116T061840_VV_AB12-BURST"
    with pytest.raises(ValueError, match="different number of bursts"):
        adapter.submit_pair(one, two)


# ── engine ───────────────────────────────────────────────────────────────────

def test_the_burst_engine_loads_like_hyp3_and_adds_the_connected_components_when_delivered(tmp_path):
    engine = get_engine("hyp3_burst")
    assert engine.info.mintpy_processor == "hyp3" and engine.info.costs_credits
    data = tmp_path / "data"
    (data / "S1_172669_IW2_20240104_20240116_VV_INT80_AB12").mkdir(parents=True)
    options = engine.load_options(data, tmp_path / "work")
    assert options["mintpy.load.unwFile"].endswith("*/*unw_phase.tif") and "mintpy.load.connCompFile" not in options
    (data / "S1_172669_IW2_20240104_20240116_VV_INT80_AB12" / "x_conncomp.tif").write_bytes(b"")
    assert engine.load_options(data, tmp_path / "work")["mintpy.load.connCompFile"].endswith("*/*conncomp.tif")
    assert "mintpy.load.connCompFile" not in get_engine("hyp3_gamma").load_options(data, tmp_path / "work")
