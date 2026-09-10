"""SLC download queue: sequential background downloader for raw Sentinel-1 SLCs.

Mirrors download_queue.py / egms_download_queue.py's single-worker design.
Items are keyed by filename (there's no Job row for a raw SLC download - it's
not tied to HyP3 processing at all, just a direct ASF data-pool transfer).
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

from app.services import download_state

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_pending: list[dict] = []        # [{granule_name, file_name, download_url}, ...]
_destination: Optional[Path] = None
_username: Optional[str] = None
_password: Optional[str] = None
_current_filename: Optional[str] = None
_total_in_session: int = 0
_done_in_session: int = 0
_cancelled: bool = False
_worker: Optional[threading.Thread] = None


# ── Public API ─────────────────────────────────────────────────────────────

def start(username: str, password: str, scenes: list[dict], destination: Path) -> None:
    """Replace pending queue with `scenes` and start the worker if idle.

    Each item: {"granule_name": str, "file_name": str, "download_url": str}
    """
    global _pending, _destination, _cancelled, _worker
    global _total_in_session, _done_in_session, _username, _password

    with _lock:
        _username = username
        _password = password
        _destination = destination
        _cancelled = False
        already_done = _done_in_session if _current_filename else 0
        _pending = list(scenes)
        _total_in_session = already_done + (1 if _current_filename else 0) + len(scenes)
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
            _current_filename = item["file_name"]
            destination = _destination
            username = _username
            password = _password

        filename = item["file_name"]
        logger.info("SLC queue: starting download for %s", filename)
        download_state.update(filename, status="running", pct=0)

        try:
            _download_one(item, destination, username, password)
            download_state.update(filename, status="done", pct=100)
            logger.info("SLC queue: completed download for %s", filename)
        except Exception:
            logger.exception("SLC queue: download failed for %s", filename)
            download_state.update(filename, status="error")

        with _lock:
            _done_in_session += 1


def _download_one(item: dict, destination: Path, username: str, password: str) -> None:
    from insar_core.adapters.asf import ASFAdapter

    def on_progress(**kw):
        tb = kw.get("total_bytes", 0)
        dl = kw.get("downloaded_bytes", 0)
        pct = round(dl / tb * 100, 1) if tb else 0
        download_state.update(item["file_name"], status="running", pct=pct, **kw)

    ASFAdapter().download(
        item["download_url"], item["file_name"], destination, username, password,
        progress_cb=on_progress,
    )
