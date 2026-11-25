"""The sources of interferograms the app can use, and whether each works on this machine."""
from fastapi import APIRouter

from insar_core.engines import list_engines

router = APIRouter(prefix="/api/engines", tags=["engines"])


@router.get("")
def get_engines():
    """Every engine with what it is, what it needs and whether it can run here."""
    return [engine.describe() for engine in list_engines()]
