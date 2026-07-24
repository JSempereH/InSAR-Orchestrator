"""
EGMS download queue: sequential background downloader for EGMS products.

Mirrors download_queue.py's single-worker design, but items are keyed by
product filename instead of a HyP3 Job id, since EGMS products aren't tied
to a Job row - they're downloaded directly into a chosen folder.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.services import download_state

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_pending: list[dict] = []        # [{query_id, filename, level}, ...]
_destination: Optional[Path] = None
_adapter = None
_current_filename: Optional[str] = None
_total_in_session: int = 0
_done_in_session: int = 0
_cancelled: bool = False
_worker: Optional[threading.Thread] = None


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
        )
    return destination


def start(db: Session, products: list[dict], destination: Path) -> None:
    """Replace pending queue with `products` and start the worker if idle.

    Each item: {"query_id": str, "filename": str, "level": str}
    """
    global _pending, _destination, _adapter, _cancelled, _worker
    global _total_in_session, _done_in_session

    from app.services.egms_service import get_egms_adapter

    with _lock:
        _adapter = get_egms_adapter(db)
        _destination = destination
        _cancelled = False
        already_done = _done_in_session if _current_filename else 0
        _pending = list(products)
        _total_in_session = already_done + (1 if _current_filename else 0) + len(products)
        _done_in_session = already_done

        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, daemon=True)
            _worker.start()


def cancel() -> None:
    global _cancelled
    with _lock:
        _cancelled = True
        _pending.clear()


def get_state() -> dict:
    with _lock:
        current = _current_filename
        pending = list(_pending)
        total = _total_in_session
        done = _done_in_session
        cancelled = _cancelled
        destination = _destination

    current_progress = download_state.get(current) if current else {}

    return {
        "active": current is not None or len(pending) > 0,
        "current_filename": current,
        "current_progress": current_progress or None,
        "pending_count": len(pending),
        "destination": str(destination) if destination else None,
        "total": total,
        "done": done,
        "cancelled": cancelled,
    }


# ── Worker ─────────────────────────────────────────────────────────────────

def _run() -> None:
    global _current_filename, _pending, _done_in_session

    while True:
        with _lock:
            if _cancelled or not _pending:
                _current_filename = None
                break
            item = _pending.pop(0)
            _current_filename = item["filename"]
            destination = _destination
            adapter = _adapter

        filename = item["filename"]
        logger.info("EGMS queue: starting download for %s", filename)
        download_state.update(filename, status="running", pct=0)

        try:
            _download_one(adapter, item, destination)
            download_state.update(filename, status="done", pct=100)
            logger.info("EGMS queue: completed download for %s", filename)
        except Exception:
            logger.exception("EGMS queue: download failed for %s", filename)
            download_state.update(filename, status="error")

        with _lock:
            _done_in_session += 1


def _download_one(adapter, item: dict, destination: Path) -> None:
    from insar_core.models.egms import EGMSProduct

    product = EGMSProduct(query_id=item["query_id"], filename=item["filename"], level=item.get("level", ""))

    def on_progress(**kw):
        tb = kw.get("total_bytes", 0)
        dl = kw.get("downloaded_bytes", 0)
        pct = round(dl / tb * 100, 1) if tb else 0
        download_state.update(item["filename"], status="running", pct=pct, **kw)

    adapter.download(product, destination, progress_cb=on_progress)
