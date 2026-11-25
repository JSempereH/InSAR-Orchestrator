"""The pair planner: what it chooses, what it reports, and what it never does silently."""
import datetime as dt

import pytest

from insar_core.models.scene import SARScene
from insar_core.pipeline.pair_builder import build_sbas_pairs
from insar_core.pipeline.pair_planner import CREDITS_PER_PAIR, PairPlanConfig, plan_pairs


def _scenes(n=24, step=12, start=dt.date(2023, 1, 3), bperp=None, skip=()):
    out = []
    for k in range(n):
        if k in skip:
            continue
        day = start + dt.timedelta(days=step * k)
        value = bperp(k) if bperp else float((k * 37) % 200 - 100)           # spreads between -100 and +99 m
        out.append(SARScene(
            file_id=f"S1_{k}-SLC", granule_name=f"S1_{k}", acquisition_date=day, orbit=k, track_number=8,
            flight_direction="DESCENDING", polarization="VV+VH", perpendicular_baseline_m=value))
    return out


def test_without_long_pairs_the_neighbors_strategy_is_exactly_what_the_app_always_did():
    scenes = _scenes()
    plan = plan_pairs(scenes, PairPlanConfig(strategy="neighbors", max_temporal_neighbors=3, long_pairs_days=()))
    assert [(a.granule_name, b.granule_name) for a, b in plan.as_pairs()] == [
        (a.granule_name, b.granule_name) for a, b in build_sbas_pairs(scenes, 3)]
    assert plan.credits == len(plan.pairs) * CREDITS_PER_PAIR and plan.connected


def test_long_pairs_are_off_by_default_because_they_lose_fast_motion():
    scenes = _scenes(n=40)
    assert len(plan_pairs(scenes).pairs) == len(plan_pairs(scenes, PairPlanConfig(long_pairs_days=())).pairs)
    assert not any("fading bias" in p.reason for p in plan_pairs(scenes).pairs)


def test_when_asked_each_date_also_gets_a_pair_of_about_three_months_and_one_of_about_a_year():
    """Against the fading bias of a network made only of short pairs."""
    scenes = _scenes(n=40)                                             # 468 days of 12-day revisits
    short = plan_pairs(scenes, PairPlanConfig(long_pairs_days=()))
    plan = plan_pairs(scenes, PairPlanConfig(long_pairs_days=(90, 365)))
    added = [p for p in plan.pairs if "fading bias" in p.reason]
    assert len(plan.pairs) == len(short.pairs) + len(added) and plan.connected
    quarter = [p for p in added if p.temporal_days < 200]
    year = [p for p in added if p.temporal_days >= 200]
    assert all(78 <= p.temporal_days <= 102 for p in quarter) and all(353 <= p.temporal_days <= 377 for p in year)
    # every date that has a date about that much later gets one, and only one, per span
    assert len(quarter) == sum(1 for s in scenes if (scenes[-1].acquisition_date - s.acquisition_date).days >= 78)
    assert len(year) == sum(1 for s in scenes if (scenes[-1].acquisition_date - s.acquisition_date).days >= 353)
    assert len({(p.reference.granule_name, p.secondary.granule_name) for p in plan.pairs}) == len(plan.pairs)
    assert plan.credits == len(plan.pairs) * CREDITS_PER_PAIR


def test_a_long_pair_is_the_one_with_the_smallest_baseline_among_the_dates_near_its_span():
    # the dates 84 and 96 days after the first are both within a revisit of 90: the nearer one has the larger baseline
    scenes = _scenes(n=10, bperp=lambda k: {7: 150.0, 8: 60.0}.get(k, 0.0))
    plan = plan_pairs(scenes, PairPlanConfig(long_pairs_days=(90,)))
    first = [p for p in plan.pairs if p.reference.granule_name == "S1_0" and "fading bias" in p.reason]
    assert [(p.secondary.granule_name, p.temporal_days) for p in first] == [("S1_8", 96)]


def test_long_pairs_that_the_period_does_not_reach_are_simply_not_there():
    plan = plan_pairs(_scenes(n=6))                                    # 60 days in all
    assert not [p for p in plan.pairs if "fading bias" in p.reason] and not plan.warnings
    with pytest.raises(ValueError, match="long_pairs_days"):
        plan_pairs(_scenes(n=6), PairPlanConfig(long_pairs_days=(0,)))


def test_the_neighbors_strategy_reports_pairs_over_the_baseline_limit_but_keeps_them():
    scenes = _scenes()
    plan = plan_pairs(scenes, PairPlanConfig(strategy="neighbors", pb_max=50.0, long_pairs_days=()))
    assert plan.over_limit > 0 and len(plan.pairs) == len(build_sbas_pairs(scenes, 3))
    assert all(p.over_limit == (p.perpendicular_m > 50.0) for p in plan.pairs)


def test_the_baseline_strategy_keeps_every_pair_inside_the_limits():
    plan = plan_pairs(_scenes(), PairPlanConfig(strategy="baseline", pb_max=120.0))
    assert plan.pairs and plan.connected and plan.over_limit == 0
    assert all(p.perpendicular_m <= 120.0 for p in plan.pairs)
    assert all(p.temporal_days in (12, 24, 36, 48) or p.reason.startswith("added") for p in plan.pairs)


def test_every_date_ends_up_with_between_the_minimum_and_maximum_interferograms():
    plan = plan_pairs(_scenes(n=30), PairPlanConfig(strategy="baseline", min_degree=3, max_degree=5, pb_max=None))
    degrees = list(plan.degrees.values())
    assert min(degrees) >= 3 and max(degrees) <= 5


def test_the_plan_is_the_same_every_time():
    config = PairPlanConfig(strategy="baseline")
    first = [(a.granule_name, b.granule_name) for a, b in plan_pairs(_scenes(), config).as_pairs()]
    again = [(a.granule_name, b.granule_name) for a, b in plan_pairs(_scenes(), config).as_pairs()]
    assert first == again


def test_a_stricter_baseline_limit_means_fewer_credits():
    loose = plan_pairs(_scenes(), PairPlanConfig(strategy="baseline", pb_max=300.0, max_degree=8))
    strict = plan_pairs(_scenes(), PairPlanConfig(strategy="baseline", pb_max=60.0, max_degree=8))
    assert strict.credits < loose.credits


def test_the_spacing_follows_the_revisit_interval_of_the_data():
    plan = plan_pairs(_scenes(n=30, step=6), PairPlanConfig(strategy="baseline", pb_max=None, max_degree=9))
    assert {p.temporal_days for p in plan.pairs if not p.reason.startswith("added")} <= {6, 12, 18, 24}
    assert 6 in {p.temporal_days for p in plan.pairs}


def test_the_targets_can_be_given_explicitly():
    plan = plan_pairs(_scenes(), PairPlanConfig(strategy="baseline", dt_targets=[12, 60], dt_max=60, pb_max=None, max_degree=9))
    assert {p.temporal_days for p in plan.pairs if not p.reason.startswith("added")} <= {12, 60}


def test_dates_about_a_year_apart_can_be_added():
    base = plan_pairs(_scenes(n=40), PairPlanConfig(strategy="baseline", pb_max=None))
    yearly = plan_pairs(_scenes(n=40), PairPlanConfig(strategy="baseline", pb_max=None, include_annual=True))
    assert not any(360 <= p.temporal_days <= 370 for p in base.pairs)
    assert any(360 <= p.temporal_days <= 370 and p.reason == "about a year apart" for p in yearly.pairs)


def test_a_gap_in_the_acquisitions_is_bridged_and_the_plan_says_so():
    scenes = _scenes(n=20, skip=range(8, 14))      # no acquisitions for 6 revisits
    plan = plan_pairs(scenes, PairPlanConfig(strategy="baseline", pb_max=None))
    assert plan.connected
    assert any(p.reason.startswith("added to connect") for p in plan.pairs)
    assert any("connect the network" in w for w in plan.warnings)


def test_dates_with_no_baseline_are_still_paired_and_the_plan_warns():
    scenes = _scenes()
    scenes[5].perpendicular_baseline_m = None
    plan = plan_pairs(scenes, PairPlanConfig(strategy="baseline", pb_max=100.0))
    assert plan.degrees[scenes[5].acquisition_date] >= 2
    assert any("no perpendicular baseline" in w for w in plan.warnings)
    assert all(p.perpendicular_m is None for p in plan.pairs if scenes[5] in (p.reference, p.secondary))


def test_fewer_than_two_scenes_is_an_empty_plan_with_a_reason():
    plan = plan_pairs(_scenes(n=1), PairPlanConfig())
    assert plan.pairs == [] and "two acquisitions" in plan.warnings[0]
    assert plan_pairs([], PairPlanConfig()).pairs == []


def test_a_thin_network_is_called_out():
    plan = plan_pairs(_scenes(n=8), PairPlanConfig(strategy="neighbors", max_temporal_neighbors=1, long_pairs_days=()))
    assert plan.connected and any("single interferogram" in w for w in plan.warnings)


@pytest.mark.parametrize("config", [
    PairPlanConfig(strategy="other"), PairPlanConfig(min_degree=0), PairPlanConfig(min_degree=4, max_degree=2),
    PairPlanConfig(pb_max=-1.0), PairPlanConfig(max_temporal_neighbors=0), PairPlanConfig(dt_max=0),
])
def test_impossible_settings_are_refused(config):
    with pytest.raises(ValueError):
        plan_pairs(_scenes(), config)


def test_the_record_saved_with_a_batch_holds_what_matters_for_the_strategy():
    assert PairPlanConfig(strategy="neighbors", max_temporal_neighbors=4).as_record() == {
        "strategy": "neighbors", "max_temporal_neighbors": 4, "long_pairs_days": [], "calibration_pair": False}
    record = PairPlanConfig(strategy="baseline", pb_max=120.0).as_record()
    assert record["strategy"] == "baseline" and record["pb_max"] == 120.0 and record["max_degree"] == 5


def test_pairs_already_submitted_or_left_out_cost_nothing_but_still_count_for_connectivity():
    scenes = _scenes(n=6)
    plan = plan_pairs(scenes, PairPlanConfig(strategy="neighbors", max_temporal_neighbors=1))
    first, second = plan.pairs[0], plan.pairs[1]
    plan.mark(
        submitted=[(first.reference.granule_name, first.secondary.granule_name)],
        excluded=[(second.reference.granule_name, second.secondary.granule_name)],
    )
    assert [p.state for p in plan.pairs[:3]] == ["submitted", "excluded", "new"]
    assert plan.credits == (len(plan.pairs) - 2) * CREDITS_PER_PAIR
    assert len(plan.as_pairs()) == len(plan.pairs) - 2
    assert plan.connected                       # the network is judged with every pair, sent or not


def test_a_target_beyond_four_revisits_is_used_when_no_limit_is_given():
    """A spacing target the person gave must not be dropped because of the default limit."""
    from datetime import date, timedelta

    from insar_core.models.scene import SARScene
    from insar_core.pipeline.pair_planner import PairPlanConfig, plan_pairs

    scenes = [SARScene(file_id=f"s{i}", granule_name=f"s{i}", acquisition_date=date(2024, 1, 1) + timedelta(days=12 * i),
                       orbit=i, track_number=1, flight_direction="ASCENDING", polarization="VV", perpendicular_baseline_m=0.0)
              for i in range(12)]
    plan = plan_pairs(scenes, PairPlanConfig(strategy="baseline", dt_targets=[12, 96], dt_tolerance=1, max_degree=10))
    assert any(p.temporal_days == 96 for p in plan.pairs)


def test_a_calibration_pair_joins_the_first_date_to_one_a_whole_number_of_six_step_chains_away():
    scenes = _scenes(n=31)
    plain = plan_pairs(scenes)
    plan = plan_pairs(scenes, PairPlanConfig(calibration_pair=True))
    added = [p for p in plan.pairs if "constants" in p.reason]
    assert len(plan.pairs) == len(plain.pairs) + 1 and len(added) == 1
    pair = added[0]
    assert (pair.reference.acquisition_date, pair.secondary.acquisition_date) == (scenes[0].acquisition_date, scenes[30].acquisition_date)
    shorter = plan_pairs(_scenes(n=28), PairPlanConfig(calibration_pair=True))
    assert [p for p in shorter.pairs if "constants" in p.reason][0].secondary.acquisition_date == _scenes(n=28)[24].acquisition_date
    assert not [p for p in plan_pairs(_scenes(n=5), PairPlanConfig(calibration_pair=True)).pairs if "constants" in p.reason]
