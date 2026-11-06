"""Attaches the statistical summary to a displacement series.

Computed here, in the same request that reads the series, so the browser gets
everything in one round trip and stays free for rendering. The analysis itself
lives in insar_core (numpy only), so it is also usable from scripts.
"""

from __future__ import annotations

import logging

from insar_core.analysis import analyze_time_series

logger = logging.getLogger(__name__)


def with_analysis(series: dict) -> dict:
    """Return a copy of `series` with an `analysis` key (None if it cannot be computed).

    A failure in the statistics must never hide the series itself, so errors
    are logged and the plain series is still returned."""
    try:
        analysis = analyze_time_series(series["dates"], series["values_mm"])
    except Exception:
        logger.warning("Time series analysis failed", exc_info=True)
        analysis = None
    return {**series, "analysis": analysis}
