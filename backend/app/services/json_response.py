"""Serialising large GeoJSON responses without FastAPI's generic encoder.

Returning a plain dict makes FastAPI walk every nested value through
jsonable_encoder first, which took over a second for the ~15 000 points of an
EGMS tile. These payloads are already JSON-compatible, so they are serialised
directly.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import Response


def json_response(payload: Any) -> Response:
    return Response(content=json.dumps(payload, separators=(",", ":")), media_type="application/json")
