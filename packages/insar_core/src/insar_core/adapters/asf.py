from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime
from typing import List

import asf_search as asf
from shapely import wkt as swkt
from shapely.validation import make_valid

from insar_core.adapters.base import SceneSearchAdapter
from insar_core.models.scene import SearchParams, SARScene, TrackSummary

log = logging.getLogger(__name__)

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
    )


def _dominant_polarization(scenes: list) -> str | None:
    """Return 'VV' or 'HH': whichever appears in most scenes' polarization field.

    Sentinel-1 occasionally acquires a track in HH mode instead of VV (e.g. S1C
    special campaigns). Mixing VV and HH scenes in one SBAS stack causes GAMMA to
    fail during interferogram formation.
    """
    from collections import Counter
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


class ASFAdapter(SceneSearchAdapter):
    """Sentinel-1 scene discovery via the ASF metadata catalog (no auth required)."""

    def _raw_search(self, params: SearchParams) -> list:
        kwargs = dict(
            platform=asf.PLATFORM.SENTINEL1,
            processingLevel=asf.PRODUCT_TYPE.SLC,
            intersectsWith=_sanitize_wkt(params.aoi.wkt),
            start=datetime.combine(params.date_start, datetime.min.time()),
            end=datetime.combine(params.date_end, datetime.min.time()),
        )
        if params.track_number is not None:
            kwargs["relativeOrbit"] = params.track_number
        if params.flight_direction:
            direction = _FLIGHT_DIRECTION_MAP.get(params.flight_direction.upper())
            if direction:
                kwargs["flightDirection"] = direction
        return list(asf.search(**kwargs))

    def search(self, params: SearchParams) -> List[SARScene]:
        """Return scenes sorted by date, deduplicated to one per acquisition date.

        Only keeps scenes whose polarization matches the dominant polarization in
        the result set. This prevents pairing e.g. an S1A VV+VH scene with an
        S1C HH+HV scene acquired on the same track (polarization mismatch GAMMA will reject).
        """
        results = self._raw_search(params)
        scenes = sorted(
            (_result_to_scene(r) for r in results),
            key=lambda s: s.acquisition_date,
        )

        # Find dominant polarization (VV or HH) by frequency
        dominant_pol = _dominant_polarization(scenes)
        if dominant_pol:
            log.info("Filtering to dominant polarization: %s", dominant_pol)
            scenes = [s for s in scenes if dominant_pol in (s.polarization or "")]

        # One scene per acquisition date
        seen: set[date] = set()
        deduped: List[SARScene] = []
        for scene in scenes:
            if scene.acquisition_date not in seen:
                seen.add(scene.acquisition_date)
                deduped.append(scene)
        return deduped

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
