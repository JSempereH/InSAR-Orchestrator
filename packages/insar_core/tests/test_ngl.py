"""The Nevada Geodetic Laboratory adapter, against text in their formats and a fake network."""
import datetime as dt

import pytest

from insar_core.adapters import ngl

HOLDINGS = """Sta  Lat(deg)   Long(deg) Hgt(m)  X(m)           Y(m)         Z(m)          Dtbeg      Dtend      Dtmod      NumSol StaOrigName
LOR1  37.6539   358.3132  364.999  5054086.6804  -148833.6167  3875323.8940 2009-09-12 2026-10-17 2026-11-04   3763 LORC
LORC  37.6566   358.2863  409.894  5053868.0027  -151203.1303  3875589.5419 2007-05-24 2012-01-09 2025-10-04   1011
FAR1  40.0000   358.3000  100.000  1.0  1.0  1.0 2009-01-01 2026-10-17 2026-11-04   100
WEST  37.6000   -3.0000   100.000  1.0  1.0  1.0 2009-01-01 2026-10-17 2026-11-04   100
LATE  37.6500   358.3000  100.000  1.0  1.0  1.0 2024-01-01 2026-10-17 2026-11-04   100
broken line
"""

TENV3 = """site YYMMMDD yyyy.yyyy __MJD week d reflon _e0(m) __east(m) ____n0(m) _north(m) u0(m) ____up(m) _ant(m)
LOR1 23JAN04 2023.0082 59948 2243 3  -1.7   1167  0.448716   4169082  0.480237   365  0.614635  0.0000 0.000757
LOR1 23JAN05 2023.0110 59949 2243 4  -1.7   1167  0.451767   4169082  0.482657   365  0.614584  0.0000 0.000780
not a data line
"""


def test_the_holdings_give_names_positions_in_wgs84_and_the_days_with_data():
    stations = {s["name"]: s for s in ngl.parse_holdings(HOLDINGS)}
    assert set(stations) == {"LOR1", "LORC", "FAR1", "WEST", "LATE"}                  # the broken line is skipped
    assert stations["LOR1"]["lon"] == pytest.approx(-1.6868, abs=1e-4)                  # 358.3132 east of Greenwich
    assert stations["LOR1"]["first"] == dt.date(2009, 9, 12) and stations["LOR1"]["last"] == dt.date(2026, 10, 17)


def test_only_stations_in_the_area_that_recorded_over_the_whole_window_are_listed():
    area = (-1.8, 37.5, -1.6, 37.8)
    found = ngl.stations_in_area(area, dt.date(2023, 1, 4), dt.date(2023, 12, 30), get=lambda url: HOLDINGS)
    assert [s["name"] for s in found] == ["LOR1"]       # LORC stopped in 2012, LATE began in 2024, FAR1 and WEST are elsewhere


def test_a_series_is_in_millimetres_with_the_origin_added_back_and_dated_from_the_mjd():
    series = ngl.parse_tenv3(TENV3)
    assert list(series) == ["20230104", "20230105"]
    east, north, up = series["20230104"]
    assert east == pytest.approx((1167 + 0.448716) * 1000)
    assert north == pytest.approx((4169082 + 0.480237) * 1000)
    assert up == pytest.approx((365 + 0.614635) * 1000)


def test_the_newest_frame_is_tried_first_and_the_older_one_used_when_it_is_all_there_is():
    asked = []

    def only_igs14(url):
        asked.append(url)
        if "IGS20" in url:
            raise ngl.NGLError("Not found")
        return TENV3

    frame, series = ngl.fetch_series("LOR1", get=only_igs14)
    assert frame == "IGS14" and len(series) == 2 and "IGS20" in asked[0] and "IGS14" in asked[1]
    assert ngl.fetch_series("LOR1", get=lambda url: TENV3)[0] == "IGS20"


def test_a_station_with_no_series_in_any_frame_is_an_error():
    def nothing(url):
        raise ngl.NGLError("Not found")

    with pytest.raises(ngl.NGLError, match="LOR1"):
        ngl.fetch_series("LOR1", get=nothing)


def test_an_unreachable_server_is_reported_as_such(monkeypatch):
    import requests

    def down(*a, **k):
        raise requests.ConnectionError("no route")

    monkeypatch.setattr(requests, "get", down)
    with pytest.raises(ngl.NGLError, match="Could not reach"):
        ngl._http_get("https://geodesy.unr.edu/x")
