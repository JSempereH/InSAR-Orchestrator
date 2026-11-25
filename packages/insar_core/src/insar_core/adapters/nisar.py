"""NISAR products from the ASF archive: search and download of the L2 GUNW interferograms.

GUNW is a geocoded, unwrapped interferogram that NASA's own processor has already made for a pair of
acquisitions, one HDF5 file of a few GB with the unwrapped phase, the coherence, the connected
components and the ionosphere, troposphere and solid earth tide corrections. It is free, so unlike
HyP3 it costs no credits, and MintPy reads it directly. The pairs are the ones NASA produced, not
ones we choose, and several processing versions of one pair can exist: the newest is used.

Searching needs no login. Downloading needs a NASA Earthdata login, like every ASF archive file.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from insar_core.adapters.asf import _sanitize_wkt
from insar_core.models.scene import SearchParams

log = logging.getLogger(__name__)

# ..._<reference start>_<reference end>_<secondary start>_<secondary end>_...
_ACQUISITIONS = re.compile(r"_(\d{8})T\d{6}_(\d{8})T\d{6}_(\d{8})T\d{6}_(\d{8})T\d{6}_")
_FLIGHT_DIRECTIONS = {"ASCENDING", "DESCENDING"}


@dataclass
class NisarGunw:
    """One NISAR interferogram granule."""
    file_id: str
    file_name: str
    reference_date: date
    secondary_date: date
    track_number: int
    frame: int
    flight_direction: str
    polarization: str
    range_bandwidth: str
    frame_coverage: str
    size_bytes: Optional[int]
    download_url: str
    processing_date: str = ""
    product_version: str = ""

    @property
    def temporal_days(self) -> int:
        return (self.secondary_date - self.reference_date).days

    @property
    def stack_key(self) -> tuple:
        """What must agree for two granules to belong to one time series."""
        return (self.track_number, self.frame, self.flight_direction, self.polarization, self.range_bandwidth, self.frame_coverage)


def _first(value) -> str:
    """ASF returns some NISAR properties as one-element lists."""
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    return "" if value is None else str(value)


def parse_granule(result) -> Optional[NisarGunw]:
    """A search result as a NisarGunw, or None when it is not a usable GUNW granule."""
    props = result.properties
    file_id = props.get("fileID") or ""
    found = _ACQUISITIONS.search(file_id)
    file_name = props.get("fileName") or ""
    if not found or "GUNW" not in file_id or not file_name.endswith(".h5"):
        return None
    sizes = props.get("bytes") or {}
    size = sizes.get(file_name, {}).get("bytes") if isinstance(sizes, dict) else None
    return NisarGunw(
        file_id=file_id, file_name=file_name,
        reference_date=datetime.strptime(found.group(1), "%Y%m%d").date(),
        secondary_date=datetime.strptime(found.group(3), "%Y%m%d").date(),
        track_number=int(props.get("pathNumber") or 0), frame=int(props.get("frameNumber") or 0),
        flight_direction=_first(props.get("flightDirection")).upper(),
        polarization=_first(props.get("mainBandPolarization")), range_bandwidth=_first(props.get("rangeBandwidth")),
        frame_coverage=_first(props.get("frameCoverage")), size_bytes=int(size) if size else None,
        download_url=props.get("url") or "", processing_date=_first(props.get("processingDate")),
        product_version=_first(props.get("pgeVersion")),
    )


def newest_version_of_each_pair(granules: list[NisarGunw]) -> list[NisarGunw]:
    """One granule per (stack, reference date, secondary date): the most recently processed."""
    best: dict[tuple, NisarGunw] = {}
    for g in granules:
        key = (g.stack_key, g.reference_date, g.secondary_date)
        if key not in best or (g.processing_date, g.file_id) > (best[key].processing_date, best[key].file_id):
            best[key] = g
    return sorted(best.values(), key=lambda g: (g.stack_key, g.reference_date, g.secondary_date))


@dataclass
class NisarStack:
    """The granules that can be one time series, with what a person needs to choose between stacks."""
    key: tuple
    track_number: int
    frame: int
    flight_direction: str
    polarization: str
    range_bandwidth: str
    frame_coverage: str
    granules: list[NisarGunw]

    @property
    def dates(self) -> list[date]:
        return sorted({d for g in self.granules for d in (g.reference_date, g.secondary_date)})

    @property
    def connected(self) -> bool:
        parent = {d: d for d in self.dates}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for g in self.granules:
            parent[find(g.reference_date)] = find(g.secondary_date)
        return len({find(d) for d in parent}) <= 1


def group_stacks(granules: list[NisarGunw]) -> list[NisarStack]:
    groups: dict[tuple, list[NisarGunw]] = defaultdict(list)
    for g in newest_version_of_each_pair(granules):
        groups[g.stack_key].append(g)
    stacks = [
        NisarStack(key=key, track_number=key[0], frame=key[1], flight_direction=key[2], polarization=key[3],
                   range_bandwidth=key[4], frame_coverage=key[5], granules=items)
        for key, items in groups.items()
    ]
    # Full frames first (partial ones cover only part of the area), then the longest series.
    return sorted(stacks, key=lambda s: (s.frame_coverage.upper() != "FULL", -len(s.granules), s.track_number, s.frame))


def granules_on_disk(directory: Path) -> list[dict]:
    """The GUNW files in `directory`, read from their names (no network): pair dates, size and name."""
    out = []
    for path in sorted(Path(directory).glob("*GUNW*.h5")):
        found = _ACQUISITIONS.search(path.name)
        if not found:
            continue
        out.append({
            "file_name": path.name,
            "reference_date": datetime.strptime(found.group(1), "%Y%m%d").date().isoformat(),
            "secondary_date": datetime.strptime(found.group(3), "%Y%m%d").date().isoformat(),
            "size_bytes": path.stat().st_size,
        })
    return out


def _search_in_windows(fetch, start: datetime, end: datetime, limit: int) -> list:
    """Everything `fetch(start, end)` finds. A window that comes back full may have been cut off by
    the catalog's limit, so it is split in two and each half searched again, until no window is full."""
    results = fetch(start, end)
    if len(results) < limit or (end - start).total_seconds() < 2:
        return results
    middle = start + (end - start) / 2
    merged = {}
    for half in (_search_in_windows(fetch, start, middle, limit), _search_in_windows(fetch, middle + timedelta(seconds=1), end, limit)):
        for r in half:
            merged[_result_key(r)] = r
    return list(merged.values())


def _result_key(result) -> str:
    properties = getattr(result, "properties", None) or {}
    return str(properties.get("fileID") or properties.get("sceneName") or id(result))


class NisarAdapter:
    """Search the NISAR GUNW catalog and download granules."""

    MAX_RESULTS = 2000   # most granules the catalog returns for one request

    def search(self, params: SearchParams) -> list[NisarGunw]:
        import asf_search as asf

        kwargs = dict(
            dataset="NISAR", processingLevel="GUNW", intersectsWith=_sanitize_wkt(params.aoi.wkt),
            maxResults=self.MAX_RESULTS,
        )
        if params.track_number is not None:
            kwargs["relativeOrbit"] = params.track_number
        if params.flight_direction and params.flight_direction.upper() in _FLIGHT_DIRECTIONS:
            kwargs["flightDirection"] = params.flight_direction.upper()
        found = _search_in_windows(
            lambda start, end: list(asf.search(start=start, end=end, **kwargs)),
            datetime.combine(params.date_start, datetime.min.time()),
            datetime.combine(params.date_end, datetime.max.time().replace(microsecond=0)),
            self.MAX_RESULTS,
        )
        return [g for g in (parse_granule(r) for r in found) if g is not None]

    def stacks(self, params: SearchParams) -> list[NisarStack]:
        return group_stacks(self.search(params))

    def download(
        self, granule: NisarGunw, destination: Path, username: str, password: str,
        progress_cb: Optional[Callable[..., None]] = None,
    ) -> Path:
        """Download one granule into `destination` (an existing complete file is kept)."""
        from insar_core.adapters.asf import ASFAdapter

        existing = Path(destination) / granule.file_name
        if existing.is_file() and (granule.size_bytes is None or existing.stat().st_size == granule.size_bytes):
            return existing
        return ASFAdapter().download(granule.download_url, granule.file_name, destination, username, password, progress_cb=progress_cb)
