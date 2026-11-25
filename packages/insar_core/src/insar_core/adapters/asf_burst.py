"""Sentinel-1 bursts for an area: the pieces HyP3 can process instead of whole scenes.

A Sentinel-1 scene is about 250 km wide and 170 km long. It is made of bursts, strips of about
20 km by 85 km that are always acquired over the same ground, each with an identifier that does
not change from pass to pass. HyP3 can make an interferogram from the bursts that cover an area
instead of from the whole scene (its INSAR_ISCE_BURST and INSAR_ISCE_MULTI_BURST jobs), which

* costs 1 credit for up to four bursts where a whole scene costs 10 (see `burst_credits`),
* comes back as a file of tens of megabytes instead of several hundred, and
* can be made at a finer resolution (10x2 or 5x1 looks, 40 m or 20 m pixels) at a price that a
  whole scene never offers.

The price is that the area must fit in at most 15 bursts, all in one pass of the satellite.

Each acquisition date becomes one `SARScene` whose `granule_name` is the names of its bursts
joined with "+", always in the same order, so that everything built on scene names (the pair
plan, the record of what was already submitted) works unchanged. `split_granules` takes it apart.
"""
from __future__ import annotations

import logging
import time
from collections import Counter, defaultdict
from datetime import date, datetime
from typing import List, Optional

from insar_core.adapters.asf import _SEARCH_CACHE_MAX_ENTRIES, _SEARCH_CACHE_TTL_S, _attach_baselines, _parse_date, _sanitize_wkt
from insar_core.adapters.base import SceneSearchAdapter
from insar_core.models.scene import SARScene, SearchParams, TrackSummary

log = logging.getLogger(__name__)

SEPARATOR = "+"
BURST_SUFFIX = "-BURST"
MAX_BURSTS = 15              # the most HyP3 takes in one multi-burst job
# A burst that the area only touches at a corner can be missing from the catalogue on some dates.
# A burst is part of the stack when at least this share of the dates has it; a date that lacks one
# of the stack's bursts is left out, because every interferogram must cover the same ground.
_MIN_BURST_PRESENCE = 0.8

# Credits per job, read from https://hyp3-api.asf.alaska.edu/costs on 2026-11-16. One burst is the
# INSAR_ISCE_BURST job; more are the INSAR_ISCE_MULTI_BURST job, priced by looks and number of bursts.
_MULTI_BURST_CREDITS = {
    "20x4": (1, 1, 1, 1, 5, 5, 5, 5, 5, 5, 5, 5, 10, 10, 10),
    "10x2": (1, 1, 1, 5, 5, 5, 5, 5, 5, 10, 10, 10, 10, 10, 10),
    "5x1": (1, 5, 10, 15, 20, 25, 30, 35, 40, 45, 90, 95, 100, 105, 110),
}
LOOKS = tuple(_MULTI_BURST_CREDITS)
PIXEL_SPACING_M = {"20x4": 80, "10x2": 40, "5x1": 20}


class BurstError(ValueError):
    """The area cannot be processed by bursts, and the message says why."""


def burst_credits(bursts: int, looks: str = "20x4") -> int:
    """What HyP3 charges for one interferogram of `bursts` bursts at `looks`."""
    if looks not in _MULTI_BURST_CREDITS:
        raise BurstError(f"looks must be one of {', '.join(LOOKS)}")
    if not 1 <= bursts <= MAX_BURSTS:
        raise BurstError(f"A burst interferogram takes 1 to {MAX_BURSTS} bursts, not {bursts}")
    return 1 if bursts == 1 else _MULTI_BURST_CREDITS[looks][bursts - 1]


def is_burst_granule(name: str) -> bool:
    return name.split(SEPARATOR)[0].endswith(BURST_SUFFIX)


def _order(name: str) -> tuple:
    """Sorting key that puts the bursts of one date in a fixed order: by burst number, then swath.

    The name is S1_<burst number>_<swath>_<time>_<polarization>_<id>-BURST."""
    parts = name.split("_")
    return (parts[1], parts[2]) if len(parts) > 2 else (name, "")


def join_granules(names) -> str:
    return SEPARATOR.join(sorted(names, key=_order))


def split_granules(granule_name: str) -> list[str]:
    """The burst names of one date, in the order HyP3 is given them."""
    return sorted(granule_name.split(SEPARATOR), key=_order)


def burst_date(name: str) -> Optional[date]:
    """The acquisition date in a burst name, or None when it is not one."""
    parts = name.split(SEPARATOR)[0].split("_")
    try:
        return datetime.strptime(parts[3][:8], "%Y%m%d").date()
    except (IndexError, ValueError):
        return None


class ASFBurstAdapter(SceneSearchAdapter):
    """Sentinel-1 burst discovery through the ASF catalogue (no login needed)."""

    def __init__(self) -> None:
        self._search_cache: dict[tuple, tuple[float, list]] = {}

    def _raw_search(self, params: SearchParams) -> list:
        import asf_search as asf

        kwargs = dict(
            dataset=asf.DATASET.SLC_BURST,
            intersectsWith=_sanitize_wkt(params.aoi.wkt),
            start=datetime.combine(params.date_start, datetime.min.time()),
            end=datetime.combine(params.date_end, datetime.max.time().replace(microsecond=0)),
        )
        if params.track_number is not None:
            kwargs["relativeOrbit"] = params.track_number
        if params.flight_direction and params.flight_direction.upper() in ("ASCENDING", "DESCENDING"):
            kwargs["flightDirection"] = params.flight_direction.upper()
        key = tuple(sorted((k, str(v)) for k, v in kwargs.items()))
        cached = self._search_cache.get(key)
        now = time.monotonic()
        if cached is not None and now - cached[0] < _SEARCH_CACHE_TTL_S:
            return cached[1]
        results = list(asf.search(**kwargs))
        if len(self._search_cache) >= _SEARCH_CACHE_MAX_ENTRIES:
            self._search_cache.clear()
        self._search_cache[key] = (now, results)
        return results

    @staticmethod
    def _co_polarized(results: list) -> list:
        """The bursts of the co-polarized channel (VV, or HH where that is what was acquired): the one
        interferometry uses. Each burst is in the catalogue once per channel."""
        counts = Counter(r.properties.get("polarization") for r in results if r.properties.get("polarization") in ("VV", "HH"))
        if not counts:
            return []
        wanted = counts.most_common(1)[0][0]
        return [r for r in results if r.properties.get("polarization") == wanted]

    def search(self, params: SearchParams) -> List[SARScene]:
        """One scene per acquisition date, made of the bursts that cover the area on that date.

        Raises BurstError when the bursts are of more than one track (name the track), or when the
        area needs more than MAX_BURSTS bursts."""
        results = self._co_polarized(self._raw_search(params))
        if not results:
            return []
        tracks = {(r.properties.get("pathNumber"), r.properties.get("flightDirection")) for r in results}
        if len(tracks) > 1:
            raise BurstError("The area is seen from more than one track. Choose one track and direction: bursts of "
                             "different tracks cannot be combined.")
        by_date: dict[date, dict[str, object]] = defaultdict(dict)
        for r in results:
            burst_id = (r.properties.get("burst") or {}).get("fullBurstID")
            if burst_id:
                # a burst can be in the catalogue twice for one date (reprocessed): the last one wins
                by_date[_parse_date(r.properties.get("startTime", ""))][burst_id] = r
        presence = Counter(burst_id for bursts in by_date.values() for burst_id in bursts)
        stack = sorted(b for b, n in presence.items() if n >= _MIN_BURST_PRESENCE * len(by_date))
        if not stack:
            return []
        if len(stack) > MAX_BURSTS:
            raise BurstError(f"The area needs {len(stack)} bursts and HyP3 takes at most {MAX_BURSTS} in one interferogram. "
                             "Draw a smaller area, or use the whole-scene engine.")
        dropped = [d for d, bursts in by_date.items() if any(b not in bursts for b in stack)]
        if dropped:
            log.warning("%d date(s) lack one of the %d bursts of the stack and are left out: %s", len(dropped), len(stack),
                        ", ".join(d.isoformat() for d in sorted(dropped)[:5]))
        scenes, anchors = [], []
        for day in sorted(d for d in by_date if d not in dropped):
            chosen = [by_date[day][b] for b in stack]
            first = chosen[0].properties
            names = [r.properties.get("sceneName") or r.properties.get("fileID", "") for r in chosen]
            sizes = [float(r.properties["bytes"]) for r in chosen if r.properties.get("bytes")]
            scenes.append(SARScene(
                file_id=join_granules(names), granule_name=join_granules(names), acquisition_date=day,
                orbit=first.get("orbit", 0), track_number=first.get("pathNumber", 0),
                flight_direction=first.get("flightDirection", ""), polarization=first.get("polarization", ""),
                size_mb=sum(sizes) / 1e6 if sizes else None, burst_ids=tuple(stack),
            ))
            anchors.append(chosen[0])
        # The perpendicular baseline is that of the first burst of the stack: within one pass it changes
        # by a few metres from burst to burst, far less than the limits a pair plan works with.
        _attach_baselines(anchors, scenes)
        return scenes

    def available_tracks(self, params: SearchParams) -> List[TrackSummary]:
        broad = SearchParams(aoi=params.aoi, date_start=params.date_start, date_end=params.date_end)
        groups: dict[tuple, set[date]] = defaultdict(set)
        for r in self._co_polarized(self._raw_search(broad)):
            groups[(r.properties.get("pathNumber", 0), r.properties.get("flightDirection", ""))].add(
                _parse_date(r.properties.get("startTime", "")))
        summaries = [TrackSummary(track_number=track, flight_direction=direction, scene_count=len(dates),
                                  first_date=min(dates), last_date=max(dates)) for (track, direction), dates in groups.items()]
        return sorted(summaries, key=lambda s: -s.scene_count)
