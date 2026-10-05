"""
EGMS download queue: sequential background downloader for EGMS products.

Items are keyed by product filename instead of a HyP3 Job id, since EGMS
products aren't tied to a Job row - they're downloaded directly into a
chosen folder.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.services import download_state
from app.services.sequential_queue import SequentialQueue

_queue = SequentialQueue(name="EGMS", item_key=lambda item: item["filename"])
_destination: Optional[Path] = None
_adapter = None


# ── Public API ─────────────────────────────────────────────────────────────

def resolve_destination(mountpoint: Optional[str], name: str) -> Path:
    from app.config import settings
    from app.routers.projects import _slugify

    base = Path(mountpoint) if mountpoint else Path(settings.downloads_dir)
    destination = base / "egms" / _slugify(name)
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Storage destination '{mountpoint}' is not available: {exc}",
        ) from exc
    return destination


def start(db: Session, products: list[dict], destination: Path) -> None:
    """Replace pending queue with `products` and start the worker if idle.

    Each item: {"query_id": str, "filename": str, "level": str}
    """
    global _destination, _adapter

    from app.services.egms_service import get_egms_adapter

    _destination = destination
    _adapter = get_egms_adapter(db)
    _queue.start(products, _download_one)


def cancel() -> None:
    _queue.cancel()


def get_state() -> dict:
    raw = _queue.get_state()
    current = raw["current_item"]
    return {
        "active": raw["active"],
        "current_filename": current["filename"] if current else None,
        "current_progress": raw["current_progress"],
        "pending_count": raw["pending_count"],
        "destination": str(_destination) if _destination else None,
        "total": raw["total"],
        "done": raw["done"],
        "cancelled": raw["cancelled"],
    }


# ── Item download ────────────────────────────────────────────────────────────

def _download_one(item: dict) -> None:
    from insar_core.models.egms import EGMSProduct

    product = EGMSProduct(query_id=item["query_id"], filename=item["filename"], level=item.get("level", ""))

    def on_progress(**kw):
        tb = kw.get("total_bytes", 0)
        dl = kw.get("downloaded_bytes", 0)
        pct = round(dl / tb * 100, 1) if tb else 0
        download_state.update(item["filename"], status="running", pct=pct, **kw)

    _adapter.download(product, _destination, progress_cb=on_progress)
