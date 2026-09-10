"""Background poller for the HyP3 processing-credit balance.

HyP3 credits change only when a job is submitted (debited) or ASF grants
more (rare), so this polls far less often than job status
(polling_service.py, every 30s). It mirrors that module's shape: a
background asyncio loop plus a force_poll() for an on-demand refresh,
so the frontend can show "updated Ns ago" / "next update in Ns" instead of
guessing.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from app.database import SessionLocal
from app.services.hyp3_service import get_hyp3_adapter

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 300  # 5 minutes: credits rarely change faster than jobs are submitted
_poll_lock = asyncio.Lock()


@dataclass
class CreditsSnapshot:
    credits: Optional[float] = None
    updated_at: Optional[str] = None  # ISO 8601, UTC
    error: Optional[str] = None


_state = CreditsSnapshot()


def get_snapshot() -> dict:
    return {
        "credits": _state.credits,
        "updated_at": _state.updated_at,
        "error": _state.error,
        "refresh_interval_seconds": _POLL_INTERVAL_SECONDS,
    }


async def poll_credits() -> None:
    """Background task: refresh the HyP3 credit balance every 5 minutes."""
    while True:
        try:
            await _refresh()
        except Exception:
            logger.exception("Unhandled error in credits polling loop")
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


async def force_poll() -> dict:
    """Trigger an immediate credit refresh outside the regular schedule."""
    if _poll_lock.locked():
        return get_snapshot()
    await _refresh()
    return get_snapshot()


async def _refresh() -> None:
    async with _poll_lock:
        await asyncio.get_running_loop().run_in_executor(None, _refresh_sync)


def _refresh_sync() -> None:
    db = SessionLocal()
    try:
        adapter = get_hyp3_adapter(db)
        credits = adapter.check_credits()
        _state.credits = credits
        _state.updated_at = datetime.now(timezone.utc).isoformat()
        _state.error = None
        logger.info("HyP3 credits refreshed: %s", credits)
    except Exception as exc:
        _state.error = str(exc)
        logger.warning("Could not refresh HyP3 credits: %s", exc)
    finally:
        db.close()
