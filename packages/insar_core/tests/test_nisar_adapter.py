"""The NISAR GUNW adapter: reading search results, choosing the newest version, grouping stacks."""
import datetime as dt


from insar_core.adapters.nisar import NisarAdapter, group_stacks, newest_version_of_each_pair, parse_granule
from insar_core.models.scene import AOI, SearchParams


def _file_id(track=66, frame=70, ref="20261004", sec="20261016", level="GUNW", version="P05023"):
    return (f"NISAR_L2_PR_{level}_032_{track:03d}_D_{frame:03d}_033_2000_SH_{ref}T185352_{ref}T185427_{sec}T185352_{sec}T185427_{version}_N_F_J_001")


class _Result:
    def __init__(self, ref="20261004", sec="20261016", track=66, frame=70, processed="2026-10-18", coverage="Full",
                 bandwidth=("20",), pol=("HH",), size=2_390_000_000, level="GUNW"):
        file_id = _file_id(track, frame, ref, sec, level)
        self.properties = {
            "fileID": file_id, "fileName": f"{file_id}.h5", "pathNumber": track, "frameNumber": frame,
            "flightDirection": "DESCENDING", "mainBandPolarization": list(pol), "rangeBandwidth": list(bandwidth),
            "frameCoverage": coverage, "processingDate": f"{processed}T10:00:00Z", "pgeVersion": "R05.02.3",
            "url": f"https://datapool.invalid/{file_id}.h5",
            "bytes": {f"{file_id}.h5": {"bytes": size, "format": "HDF5"}, f"{file_id}_QA_REPORT.pdf": {"bytes": 1000, "format": "PDF"}},
        }


def test_a_result_gives_its_pair_dates_stack_and_size():
    granule = parse_granule(_Result())
    assert (granule.reference_date, granule.secondary_date) == (dt.date(2026, 10, 4), dt.date(2026, 10, 16))
    assert granule.temporal_days == 12 and granule.size_bytes == 2_390_000_000
    assert granule.stack_key == (66, 70, "DESCENDING", "HH", "20", "Full")
    assert granule.file_name.endswith(".h5") and granule.download_url.endswith(".h5")


def test_things_that_are_not_a_gunw_h5_are_skipped():
    assert parse_granule(_Result(level="GSLC")) is None
    odd = _Result()
    odd.properties["fileID"] = "not a nisar name"
    assert parse_granule(odd) is None
    no_file = _Result()
    no_file.properties["fileName"] = "something.png"
    assert parse_granule(no_file) is None


def test_the_newest_processing_of_a_pair_wins():
    old, new = parse_granule(_Result(processed="2026-09-07")), parse_granule(_Result(processed="2026-09-13"))
    other = parse_granule(_Result(ref="20261016", sec="20261028"))
    kept = newest_version_of_each_pair([old, other, new])
    assert [(g.reference_date, g.processing_date[:10]) for g in kept] == [(dt.date(2026, 10, 4), "2026-09-13"), (dt.date(2026, 10, 16), "2026-10-18")]


def test_granules_group_into_stacks_with_full_frames_and_longer_series_first():
    full = [parse_granule(_Result(ref=f"2026{m:02d}01", sec=f"2026{m:02d}13")) for m in (6, 7, 8)]
    partial = [parse_granule(_Result(ref=f"2026{m:02d}01", sec=f"2026{m:02d}13", coverage="Partial", bandwidth=("40",))) for m in (6, 7, 8, 9)]
    other_frame = [parse_granule(_Result(frame=71))]
    stacks = group_stacks(partial + other_frame + full)
    assert [(s.frame, s.frame_coverage, len(s.granules)) for s in stacks] == [(70, "Full", 3), (71, "Full", 1), (70, "Partial", 4)]


def test_a_stack_knows_its_dates_and_whether_the_pairs_chain_together():
    chained = group_stacks([parse_granule(_Result(ref="20261004", sec="20261016")), parse_granule(_Result(ref="20261016", sec="20261028"))])[0]
    assert [d.isoformat() for d in chained.dates] == ["2026-10-04", "2026-10-16", "2026-10-28"] and chained.connected
    split = group_stacks([parse_granule(_Result(ref="20261004", sec="20261016")), parse_granule(_Result(ref="20261101", sec="20261113"))])[0]
    assert not split.connected


def test_search_asks_asf_for_nisar_gunw_in_the_area_and_keeps_only_usable_granules(monkeypatch):
    import asf_search

    seen = {}

    def fake(**kwargs):
        seen.update(kwargs)
        return [_Result(), _Result(level="GSLC")]

    monkeypatch.setattr(asf_search, "search", fake)
    params = SearchParams(aoi=AOI.from_bbox(-1.8, 37.6, -1.6, 37.7), date_start=dt.date(2026, 6, 1), date_end=dt.date(2026, 10, 31),
                          track_number=66, flight_direction="descending")
    found = NisarAdapter().search(params)
    assert len(found) == 1
    assert (seen["dataset"], seen["processingLevel"], seen["relativeOrbit"], seen["flightDirection"]) == ("NISAR", "GUNW", 66, "DESCENDING")
    assert "POLYGON" in seen["intersectsWith"] and seen["end"].date() == dt.date(2026, 10, 31)


def test_search_without_a_track_does_not_filter_on_one(monkeypatch):
    import asf_search

    seen = {}
    monkeypatch.setattr(asf_search, "search", lambda **kw: seen.update(kw) or [])
    NisarAdapter().search(SearchParams(aoi=AOI.from_bbox(0, 0, 1, 1), date_start=dt.date(2026, 6, 1), date_end=dt.date(2026, 7, 1)))
    assert "relativeOrbit" not in seen and "flightDirection" not in seen


def test_download_hands_the_granule_to_the_archive_downloader(monkeypatch, tmp_path):
    from insar_core.adapters import asf as asf_module

    calls = []
    monkeypatch.setattr(asf_module.ASFAdapter, "download", lambda self, url, name, dest, user, password, progress_cb=None:
                        calls.append((url, name, dest, user, password)) or dest / name)
    granule = parse_granule(_Result())
    path = NisarAdapter().download(granule, tmp_path, "someone", "secret")
    assert calls == [(granule.download_url, granule.file_name, tmp_path, "someone", "secret")] and path == tmp_path / granule.file_name


def test_a_search_that_hits_the_catalog_limit_is_split_until_nothing_is_cut_off():
    from datetime import datetime, timedelta
    from types import SimpleNamespace

    from insar_core.adapters.nisar import _search_in_windows

    everything = [SimpleNamespace(day=datetime(2025, 1, 1) + timedelta(hours=6 * i), properties={"fileID": f"g{i}"}) for i in range(50)]

    def fetch(start, end):                                            # a catalog that returns at most 10 per request
        return [g for g in everything if start <= g.day <= end][:10]

    found = _search_in_windows(fetch, datetime(2025, 1, 1), datetime(2025, 2, 1), 10)
    assert sorted(g.properties["fileID"] for g in found) == sorted(g.properties["fileID"] for g in everything)


def test_a_granule_already_on_disk_in_full_is_not_downloaded_again(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import pytest

    from insar_core.adapters import asf as asf_module
    from insar_core.adapters.nisar import NisarAdapter

    (tmp_path / "x.h5").write_bytes(b"12345678")
    granule = SimpleNamespace(file_name="x.h5", size_bytes=8, download_url="https://datapool.asf.alaska.edu/x.h5")
    monkeypatch.setattr(asf_module.ASFAdapter, "download", lambda *a, **k: pytest.fail("downloaded again"))
    assert NisarAdapter().download(granule, tmp_path, "u", "p") == tmp_path / "x.h5"
