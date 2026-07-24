from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from insar_core.models.scene import AOI


@dataclass
class EGMSSearchParams:
    """Parameters for an EGMS ground-motion product search.

    EGMS publishes yearly "releases" (e.g. "2019-2023") instead of arbitrary
    date ranges - each release is a fixed multi-year processing vintage.
    Valid values for level/release/direction/product_type come from
    EGMSAdapter.list_options(), since the API is the source of truth.
    """
    aoi: AOI
    level: str                          # "L2A", "L2B", or "L3"
    release: str                        # e.g. "2019-2023"
    direction: Optional[str] = None     # "ascending"/"descending" - required for L2A/L2B
    product_type: Optional[str] = None  # e.g. "ORTHO-UP" - required for L3
    tile_id: Optional[str] = None       # optional L3 grid tile filter


@dataclass
class EGMSProduct:
    """A single downloadable EGMS product file returned by a search.

    `query_id` is the search-session id EGMS returns alongside the hits;
    it must be replayed as the `id` query param when downloading.
    """
    query_id: str
    filename: str
    level: str
    size_mb: Optional[float] = None
    properties: dict[str, Any] = field(default_factory=dict)
