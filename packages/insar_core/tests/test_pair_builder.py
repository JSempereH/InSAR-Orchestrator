from datetime import date

from insar_core.models.scene import SARScene
from insar_core.pipeline.pair_builder import build_sbas_pairs


def _scene(day: int, name: str | None = None) -> SARScene:
    return SARScene(
        file_id=f"file-{day}",
        granule_name=name or f"S1_{day:02d}",
        acquisition_date=date(2023, 1, day),
        orbit=day,
        track_number=110,
        flight_direction="DESCENDING",
        polarization="VV",
    )


def test_empty_and_single_scene_produce_no_pairs():
    assert build_sbas_pairs([], max_temporal_neighbors=3) == []
    assert build_sbas_pairs([_scene(1)], max_temporal_neighbors=3) == []


def test_each_scene_pairs_with_next_n_neighbors():
    scenes = [_scene(d) for d in (1, 2, 3, 4, 5)]
    pairs = build_sbas_pairs(scenes, max_temporal_neighbors=2)

    names = [(ref.granule_name, sec.granule_name) for ref, sec in pairs]
    assert names == [
        ("S1_01", "S1_02"), ("S1_01", "S1_03"),
        ("S1_02", "S1_03"), ("S1_02", "S1_04"),
        ("S1_03", "S1_04"), ("S1_03", "S1_05"),
        ("S1_04", "S1_05"),
    ]


def test_reference_always_precedes_secondary():
    scenes = [_scene(d) for d in (1, 2, 3, 4)]
    pairs = build_sbas_pairs(scenes, max_temporal_neighbors=3)
    for ref, sec in pairs:
        assert ref.acquisition_date < sec.acquisition_date


def test_unsorted_input_is_sorted_by_acquisition_date():
    scenes = [_scene(3), _scene(1), _scene(2)]
    pairs = build_sbas_pairs(scenes, max_temporal_neighbors=1)
    assert [(r.granule_name, s.granule_name) for r, s in pairs] == [
        ("S1_01", "S1_02"), ("S1_02", "S1_03"),
    ]


def test_neighbors_covering_full_history_gives_complete_network():
    scenes = [_scene(d) for d in (1, 2, 3, 4)]
    pairs = build_sbas_pairs(scenes, max_temporal_neighbors=99)
    # complete network: n*(n-1)/2 unique pairs
    assert len(pairs) == 6
