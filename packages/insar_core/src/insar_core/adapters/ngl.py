"""GNSS station time series from the Nevada Geodetic Laboratory (NGL), for validating InSAR.

NGL publishes daily positions of more than 17,000 GNSS stations, free to use with this
acknowledgement: Blewitt, G., Hammond, W. C., and Kreemer, C. (2018), Harnessing the GPS
data explosion for interdisciplinary science, Eos, 99. The files are the `tenv3` series in
a global frame (IGS20, or IGS14 for stations NGL has not reprocessed): east, north and up
in metres relative to a station-specific origin, so the motion includes the movement of the
plate. That is fine for a comparison that removes a common offset, and it is why the
comparison does so.

The network is reached through an injectable `get`, so tests never leave the machine.
"""
from __future__ import annotations

import datetime as dt
from typing import Callable, Optional

import requests

CITATION = "Blewitt, G., Hammond, W. C., and Kreemer, C. (2018), Eos, 99 (Nevada Geodetic Laboratory)"
BASE = "https://geodesy.unr.edu"
HOLDINGS_URL = f"{BASE}/NGLStationPages/DataHoldings.txt"
SERIES_URLS = (
    ("IGS20", f"{BASE}/gps_timeseries/IGS20/tenv3/IGS20/{{name}}.tenv3"),
    ("IGS14", f"{BASE}/gps_timeseries/IGS14/tenv3/IGS14/{{name}}.tenv3"),
)
_MJD_EPOCH = dt.date(1858, 11, 17)


class NGLError(RuntimeError):
    """The Nevada Geodetic Laboratory could not be reached or does not have what was asked."""


def _http_get(url: str, timeout: float = 60.0) -> str:
    try:
        reply = requests.get(url, timeout=timeout)
    except requests.RequestException as exc:
        raise NGLError(f"Could not reach the Nevada Geodetic Laboratory ({exc.__class__.__name__}).") from exc
    if reply.status_code == 404:
        raise NGLError(f"Not found: {url}")
    if reply.status_code != 200:
        raise NGLError(f"The Nevada Geodetic Laboratory answered {reply.status_code} for {url}")
    return reply.text


def parse_holdings(text: str) -> list[dict]:
    """The station list: name, WGS84 position and the first and last day of data."""
    stations = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 9:
            continue
        try:
            lon = float(parts[2])
            stations.append({
                "name": parts[0], "lat": float(parts[1]), "lon": lon - 360.0 if lon > 180.0 else lon,
                "first": dt.date.fromisoformat(parts[7]), "last": dt.date.fromisoformat(parts[8]),
            })
        except ValueError:
            continue
    return stations


def stations_in_area(
    bbox: tuple[float, float, float, float], start: dt.date, end: dt.date, get: Callable[[str], str] = _http_get,
) -> list[dict]:
    """Stations inside (min_lon, min_lat, max_lon, max_lat) that recorded over the whole of start..end."""
    min_lon, min_lat, max_lon, max_lat = bbox
    return [
        s for s in parse_holdings(get(HOLDINGS_URL))
        if min_lat <= s["lat"] <= max_lat and min_lon <= s["lon"] <= max_lon and s["first"] <= start and s["last"] >= end
    ]


def parse_tenv3(text: str) -> dict[str, tuple[float, float, float]]:
    """{YYYYMMDD: (east, north, up)} in millimetres, from a `tenv3` file."""
    series: dict[str, tuple[float, float, float]] = {}
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 13:
            continue
        try:
            day = _MJD_EPOCH + dt.timedelta(days=int(parts[3]))
            east = (float(parts[7]) + float(parts[8])) * 1000.0
            north = (float(parts[9]) + float(parts[10])) * 1000.0
            up = (float(parts[11]) + float(parts[12])) * 1000.0
        except ValueError:
            continue
        series[day.strftime("%Y%m%d")] = (east, north, up)
    return series


def fetch_series(name: str, get: Callable[[str], str] = _http_get) -> tuple[str, dict[str, tuple[float, float, float]]]:
    """The series of one station and the frame it came in (IGS20 first, IGS14 if that is all there is)."""
    last: Optional[NGLError] = None
    for frame, template in SERIES_URLS:
        try:
            series = parse_tenv3(get(template.format(name=name)))
        except NGLError as exc:
            last = exc
            continue
        if series:
            return frame, series
    raise NGLError(f"No series for station {name}") from last


# ── steps and MIDAS velocities ───────────────────────────────────────────────
#
# Two more files NGL publishes, both needed to trust a station velocity:
#
# * The step table lists the days on which a station's series may jump: a change of antenna or
#   receiver (an artefact of the instrument) or an earthquake nearby (real motion of the ground).
#   A straight line fitted across a jump of a few millimetres is off by that jump divided by the
#   length of the record, which over three years is the size of the signal InSAR is checked for.
# * The MIDAS table holds NGL's own velocity of every station over its whole record (Blewitt et
#   al., 2016), robust to jumps and to the seasons. It is a second opinion on the velocity fitted
#   here over the InSAR window, not a replacement: the two cover different periods.

STEPS_URL = f"{BASE}/NGLStationPages/steps.txt"
MIDAS_URLS = (
    ("IGS20", f"{BASE}/velocities/midas.IGS20.txt"),
    ("IGS14", f"{BASE}/velocities/midas.IGS14.txt"),
)
MIDAS_CITATION = ("Blewitt, G., Kreemer, C., Hammond, W. C., and Gazeaux, J. (2016), MIDAS robust trend estimator for accurate "
                  "GPS station velocities without step detection, J. Geophys. Res. Solid Earth, 121")
_MONTHS = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), start=1)}


def _step_date(text: str) -> str:
    """'17MAY04' as '20170504'. Two-digit years from 80 up are in the 1900s (GPS began in 1980)."""
    year = int(text[:2])
    return f"{(1900 if year >= 80 else 2000) + year:04d}{_MONTHS[text[2:5].upper()]:02d}{int(text[5:7]):02d}"


def parse_steps(text: str, stations: Optional[set] = None) -> dict[str, list[dict]]:
    """{station: [{date, kind, detail}]} from NGL's step table, in date order.

    `kind` is "equipment" (the instrument changed: the jump is not ground motion) or "earthquake"
    (the ground may have moved: `detail` gives the magnitude and the distance in km). `stations`
    keeps only those names, since the table covers every station in the world."""
    steps: dict[str, list[dict]] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3 or (stations is not None and parts[0] not in stations):
            continue
        try:
            date = _step_date(parts[1])
        except (KeyError, ValueError):
            continue
        if parts[2] == "1":
            entry = {"date": date, "kind": "equipment", "detail": " ".join(parts[3:]).replace("_", " ")}
        elif parts[2] == "2":
            detail = f"magnitude {parts[5]}, {float(parts[4]):.0f} km away" if len(parts) >= 6 else ""
            entry = {"date": date, "kind": "earthquake", "detail": detail}
        else:
            continue
        steps.setdefault(parts[0], []).append(entry)
    return {name: sorted(items, key=lambda s: s["date"]) for name, items in steps.items()}


def fetch_steps(stations: Optional[set] = None, get: Callable[[str], str] = _http_get) -> dict[str, list[dict]]:
    """The known steps of `stations` (all of them when None)."""
    return parse_steps(get(STEPS_URL), stations)


def parse_midas(text: str, stations: Optional[set] = None) -> dict[str, dict]:
    """{station: velocity} from a MIDAS table: east, north and up in mm/year with their errors,
    the first and last decimal year of the record and the number of steps NGL assumed."""
    out: dict[str, dict] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 24 or (stations is not None and parts[0] not in stations):
            continue
        try:
            out[parts[0]] = {
                "first_year": float(parts[2]), "last_year": float(parts[3]), "years": float(parts[4]),
                "east": float(parts[8]) * 1000.0, "north": float(parts[9]) * 1000.0, "up": float(parts[10]) * 1000.0,
                "sigma_east": float(parts[11]) * 1000.0, "sigma_north": float(parts[12]) * 1000.0,
                "sigma_up": float(parts[13]) * 1000.0, "steps": int(parts[23]),
            }
        except ValueError:
            continue
    return out


def fetch_midas(stations: Optional[set] = None, get: Callable[[str], str] = _http_get) -> tuple[str, dict[str, dict]]:
    """NGL's MIDAS velocities of `stations` and the frame of the table (IGS20 when published, else IGS14)."""
    last: Optional[NGLError] = None
    for frame, url in MIDAS_URLS:
        try:
            table = parse_midas(get(url), stations)
        except NGLError as exc:
            last = exc
            continue
        if table:
            return frame, table
    raise NGLError("No MIDAS velocity table could be read") from last
