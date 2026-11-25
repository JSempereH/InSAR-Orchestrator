from __future__ import annotations

import logging
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

import asf_search as asf
from shapely import wkt as swkt
from shapely.validation import make_valid

from insar_core.adapters.base import SceneSearchAdapter
from insar_core.models.scene import SearchParams, SARScene, TrackSummary
from insar_core.net import stream_download

log = logging.getLogger(__name__)

# NASA's catalogue answers a year of Sentinel-1 scenes slowly at busy hours, and the 30 seconds
# asf_search waits by default ends in a failed search that works a minute later.
_CMR_TIMEOUT_S = 120
if asf.constants.INTERNAL.CMR_TIMEOUT < _CMR_TIMEOUT_S:
    asf.constants.INTERNAL.CMR_TIMEOUT = _CMR_TIMEOUT_S

_FLIGHT_DIRECTION_MAP = {
    "ASCENDING": asf.FLIGHT_DIRECTION.ASCENDING,
    "DESCENDING": asf.FLIGHT_DIRECTION.DESCENDING,
}

# asf_search returns fileID with '-SLC' suffix (71 chars); HyP3 needs 67-char scene name
_SLC_SUFFIX = "-SLC"


def _sanitize_wkt(wkt_str: str) -> str:
    """Return a topologically valid WKT polygon for ASF search.

    Hand-drawn polygons often self-intersect when the user accidentally crosses
    a previous edge. ASF rejects these with ASFWKTError. We fix them with
    shapely (already a dependency of asf_search):
      1. If valid → return as-is.
      2. If invalid → make_valid(), which may produce a MultiPolygon.
      3. If still not a simple Polygon → use the convex hull (always valid,
         good enough for a catalog search over a small AOI).
    """
    try:
        geom = swkt.loads(wkt_str)
        if geom.is_valid:
            return wkt_str
        log.warning("AOI polygon is self-intersecting, attempting automatic repair.")
        geom = make_valid(geom)
        if geom.geom_type != "Polygon":
            log.warning("Repaired geometry is %s; falling back to convex hull.", geom.geom_type)
            geom = geom.convex_hull
        return geom.wkt
    except Exception:
        return wkt_str  # let ASF produce a clearer error if it still fails


def _to_granule_name(file_id: str) -> str:
    if file_id.endswith(_SLC_SUFFIX):
        return file_id[: -len(_SLC_SUFFIX)]
    return file_id


def _parse_date(ts: str) -> date:
    return datetime.fromisoformat(ts[:10]).date()


def _result_to_scene(r) -> SARScene:
    props = r.properties
    file_id = props.get("fileID", "")
    return SARScene(
        file_id=file_id,
        granule_name=_to_granule_name(file_id),
        acquisition_date=_parse_date(props.get("startTime", "")),
        orbit=props.get("orbit", 0),
        track_number=props.get("pathNumber", 0),
        flight_direction=props.get("flightDirection", ""),
        polarization=props.get("polarization", ""),
        size_mb=props.get("bytes", 0) / 1e6 if props.get("bytes") else None,
        download_url=props.get("url"),
        file_name=props.get("fileName"),
    )


# Sentinel-1 orbital period. Some catalog records store the ascending node crossing of the previous
# orbit, which puts the orbit state vectors far from where the baseline calculation expects them and
# makes it return absurd values. Moving the crossing forward by whole periods repairs those records
# (the same repair InSARHub, MIT licence, applies before its own baseline table).
_S1_ORBIT_PERIOD_S = 5929.0
_MAX_PRE_POSITION_S = 600.0


def _repair_ascending_node(result) -> None:
    from dateutil.parser import isoparse

    baseline = getattr(result, "baseline", None) or {}
    node = baseline.get("ascendingNodeTime")
    pre = ((baseline.get("stateVectors") or {}).get("positions") or {}).get("prePositionTime")
    if not (node and pre):
        return
    try:
        delta = isoparse(pre).timestamp() - isoparse(node).timestamp()
    except (TypeError, ValueError):
        return
    if delta > _MAX_PRE_POSITION_S:
        shift = int(round((delta - _MAX_PRE_POSITION_S / 2) / _S1_ORBIT_PERIOD_S))
        if shift > 0:
            fixed = datetime.fromtimestamp(isoparse(node).timestamp() + shift * _S1_ORBIT_PERIOD_S, tz=timezone.utc)
            baseline["ascendingNodeTime"] = fixed.strftime("%Y-%m-%dT%H:%M:%SZ")


def _attach_baselines(results: list, scenes: list) -> None:
    """Fill `perpendicular_baseline_m` on `scenes` (one per entry of `results`, same order).

    Baselines are relative, so each (track, direction) stack gets its own anchor, its first scene.
    This is local arithmetic on data the search already returned: no request is made."""
    try:
        from asf_search.baseline import calculate_perpendicular_baselines
    except ImportError:  # pragma: no cover - an asf_search without the baseline module
        return
    groups: dict[tuple, list[int]] = defaultdict(list)
    for index, scene in enumerate(scenes):
        groups[(scene.track_number, scene.flight_direction)].append(index)
    for indexes in groups.values():
        stack = [results[i] for i in indexes]
        for item in stack:
            _repair_ascending_node(item)
        try:
            computed = calculate_perpendicular_baselines(stack[0].properties["sceneName"], stack)
        except Exception:
            log.warning("Could not compute perpendicular baselines for track %s", scenes[indexes[0]].track_number, exc_info=True)
            continue
        by_name = {c.properties.get("sceneName"): c.properties.get("perpendicularBaseline") for c in computed}
        by_name.setdefault(stack[0].properties["sceneName"], 0.0)
        for i, item in zip(indexes, stack, strict=False):
            value = by_name.get(item.properties.get("sceneName"))
            # ASF reports a failed calculation as a missing value, and a wildly large one as an error.
            scenes[i].perpendicular_baseline_m = float(value) if value is not None and abs(value) < 100_000 else None


def _aoi_coverage(result, params: SearchParams) -> float:
    """The share of the search area (0 to 1) that a catalogue result's footprint covers, 0 when unknown."""
    try:
        from shapely.geometry import shape

        footprint = make_valid(shape(result.geometry))
        area = make_valid(swkt.loads(params.aoi.wkt))
        return float(footprint.intersection(area).area / area.area) if area.area > 0 else 0.0
    except Exception:
        return 0.0


def _dominant_polarization(scenes: list) -> str | None:
    """Return 'VV' or 'HH': whichever appears in most scenes' polarization field.

    Sentinel-1 occasionally acquires a track in HH mode instead of VV (e.g. S1C
    special campaigns). Mixing VV and HH scenes in one SBAS stack causes GAMMA to
    fail during interferogram formation.
    """
    counts: Counter = Counter()
    for s in scenes:
        pol = (s.polarization or "").upper()
        if "VV" in pol:
            counts["VV"] += 1
        elif "HH" in pol:
            counts["HH"] += 1
    if not counts:
        return None
    dominant, minority = counts.most_common()[:2] if len(counts) > 1 else (counts.most_common(1)[0], None)
    if minority and dominant[1] == minority[1]:
        return None  # tie, skip filter
    if minority and minority[1] > 0:
        log.warning(
            "Polarization mismatch detected: %d %s scenes vs %d %s scenes. "
            "Keeping only %s scenes to ensure interferometric compatibility.",
            dominant[1], dominant[0], minority[1], minority[0], dominant[0],
        )
    return dominant[0]


# How long a raw ASF catalog search result is reused for an identical query
# before hitting the network again. Sentinel-1 acquisitions in the queried
# date range are historical (already flown), so the catalog for a given AOI
# essentially doesn't change minute to minute - this just absorbs the
# duplicate queries a normal session naturally produces (reopening the track
# picker, re-planning a batch after tweaking an unrelated field), not meant
# to be a long-lived cache.
_SEARCH_CACHE_TTL_S = 300

# Safety cap so a long-running backend process reusing one ASFAdapter across
# many different AOIs/date-ranges doesn't grow this cache unbounded.
_SEARCH_CACHE_MAX_ENTRIES = 256


class ASFAdapter(SceneSearchAdapter):
    """Sentinel-1 scene discovery via the ASF metadata catalog (no auth required)."""

    def __init__(self) -> None:
        self._search_cache: dict[tuple, tuple[float, list]] = {}

    def download(
        self,
        download_url: str,
        file_name: str,
        destination: Path,
        username: str,
        password: str,
        progress_cb: Optional[Callable[..., None]] = None,
    ) -> Path:
        """Download a raw SLC product from ASF's data pool.

        `download_url` and `file_name` come from a SARScene returned by search()
        (its download_url / file_name fields). Unlike submit_pair(), this is a
        plain authenticated file transfer of the original Sentinel-1 product -
        no HyP3 processing credits involved. Requires Earthdata credentials.
        """
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        dest = destination / file_name

        session = asf.ASFSession()
        session.auth_with_creds(username, password)

        stream_download(download_url, dest, session=session, progress_cb=progress_cb)
        return dest

    def _raw_search(self, params: SearchParams) -> list:
        kwargs = dict(
            platform=asf.PLATFORM.SENTINEL1,
            processingLevel=asf.PRODUCT_TYPE.SLC,
            intersectsWith=_sanitize_wkt(params.aoi.wkt),
            start=datetime.combine(params.date_start, datetime.min.time()),
            # date_end is inclusive: run to the end of that day, not its midnight.
            end=datetime.combine(params.date_end, datetime.max.time().replace(microsecond=0)),
        )
        if params.track_number is not None:
            kwargs["relativeOrbit"] = params.track_number
        if params.flight_direction:
            direction = _FLIGHT_DIRECTION_MAP.get(params.flight_direction.upper())
            if direction:
                kwargs["flightDirection"] = direction

        cache_key = tuple(sorted(kwargs.items()))
        cached = self._search_cache.get(cache_key)
        now = time.monotonic()
        if cached is not None and now - cached[0] < _SEARCH_CACHE_TTL_S:
            return cached[1]

        results = list(asf.search(**kwargs))

        if len(self._search_cache) >= _SEARCH_CACHE_MAX_ENTRIES:
            self._search_cache.clear()
        self._search_cache[cache_key] = (now, results)
        return results

    def search(self, params: SearchParams) -> List[SARScene]:
        """Return scenes sorted by date, deduplicated to one per acquisition date.

        Only keeps scenes whose polarization matches the dominant polarization in
        the result set. This prevents pairing e.g. an S1A VV+VH scene with an
        S1C HH+HV scene acquired on the same track (polarization mismatch GAMMA will reject).
        """
        results = self._raw_search(params)
        pairs = sorted(
            ((_result_to_scene(r), r) for r in results),
            key=lambda item: item[0].acquisition_date,
        )

        # Find dominant polarization (VV or HH) by frequency
        dominant_pol = _dominant_polarization([scene for scene, _ in pairs])
        if dominant_pol:
            log.info("Filtering to dominant polarization: %s", dominant_pol)
            pairs = [(s, r) for s, r in pairs if dominant_pol in (s.polarization or "")]

        # One scene per acquisition date. Where the area lies across two consecutive frames a date has
        # two scenes: the one that covers more of the area goes first and is kept, so the choice does
        # not depend on the order the catalogue happened to answer in.
        coverage = {id(r): _aoi_coverage(r, params) for _, r in pairs}
        pairs.sort(key=lambda item: (item[0].acquisition_date, -coverage[id(item[1])]))
        seen: set[date] = set()
        deduped: List[tuple] = []
        for scene, result in pairs:
            if scene.acquisition_date not in seen:
                seen.add(scene.acquisition_date)
                deduped.append((scene, result))
        scenes = [scene for scene, _ in deduped]
        _attach_baselines([result for _, result in deduped], scenes)
        return scenes

    def available_tracks(self, params: SearchParams) -> List[TrackSummary]:
        """Search without track/direction filters; aggregate by (track, direction)."""
        broad_params = SearchParams(
            aoi=params.aoi,
            date_start=params.date_start,
            date_end=params.date_end,
        )
        results = self._raw_search(broad_params)
        scenes = [_result_to_scene(r) for r in results]

        groups: dict[tuple, list[date]] = defaultdict(list)
        for scene in scenes:
            key = (scene.track_number, scene.flight_direction)
            groups[key].append(scene.acquisition_date)

        summaries = []
        for (track, direction), dates in groups.items():
            summaries.append(TrackSummary(
                track_number=track,
                flight_direction=direction,
                scene_count=len(set(dates)),
                first_date=min(dates),
                last_date=max(dates),
            ))

        return sorted(summaries, key=lambda s: -s.scene_count)
