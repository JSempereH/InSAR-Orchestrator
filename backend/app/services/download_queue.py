"""
Global download queue. Persists across browser sessions as long as the backend runs.

Design: a single daemon thread processes one job at a time. POSTing a new queue
replaces the pending items (the item currently downloading is not interrupted).
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

from app.services import download_state

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_pending: list[dict] = []        # [{job_id, hyp3_job_id}, ...]
_current_job_id: Optional[str] = None
_total_in_session: int = 0
_done_in_session: int = 0
_cancelled: bool = False
_worker: Optional[threading.Thread] = None


# ── Public API ─────────────────────────────────────────────────────────────

def start(jobs: list[dict]) -> None:
    """Replace pending queue with `jobs` and start worker if not already running.

    Each item: {"job_id": str, "hyp3_job_id": str}
    Currently-downloading job (if any) is not interrupted.
    """
    global _pending, _cancelled, _worker, _total_in_session, _done_in_session

    with _lock:
        _cancelled = False
        already_done = _done_in_session if _current_job_id else 0
        _pending = list(jobs)
        _total_in_session = already_done + (1 if _current_job_id else 0) + len(jobs)
        _done_in_session = already_done

        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, daemon=True)
            _worker.start()


def cancel() -> None:
    """Clear pending queue. The current download finishes, then the worker stops."""
    global _cancelled
    with _lock:
        _cancelled = True
        _pending.clear()


def get_state() -> dict:
    """Return a snapshot of current queue state (thread-safe copy)."""
    with _lock:
        current = _current_job_id
        pending = list(_pending)
        total = _total_in_session
        done = _done_in_session
        cancelled = _cancelled

    current_progress = download_state.get(current) if current else {}

    return {
        "active": current is not None or len(pending) > 0,
        "current_job_id": current,
        "current_progress": current_progress or None,
        "pending_count": len(pending),
        "pending_job_ids": pending,
        "total": total,
        "done": done,
        "cancelled": cancelled,
    }


# ── Worker ─────────────────────────────────────────────────────────────────

def _run() -> None:
    global _current_job_id, _pending, _done_in_session

    while True:
        with _lock:
            if _cancelled or not _pending:
                _current_job_id = None
                break
            item = _pending.pop(0)
            _current_job_id = item["job_id"]

        job_id = item["job_id"]
        hyp3_job_id = item["hyp3_job_id"]

        logger.info("Queue: starting download for job %s", job_id)
        download_state.update(job_id, status="running", pct=0, filename=None)

        try:
            _download_one(job_id, hyp3_job_id)
        except Exception:
            logger.exception("Queue: download failed for job %s", job_id)
            download_state.update(job_id, status="error")

        with _lock:
            _done_in_session += 1


def _download_one(job_id: str, hyp3_job_id: str) -> None:
    # Import here to avoid circular imports at module load time
    from app.config import settings
    from app.database import SessionLocal
    from app.models import Job
    from app.services.hyp3_service import get_hyp3_adapter

    db = SessionLocal()
    try:
        adapter = get_hyp3_adapter(db)
    finally:
        db.close()

    def on_progress(**kw):
        tb = kw.get("total_bytes", 0)
        dl = kw.get("downloaded_bytes", 0)
        pct = round(dl / tb * 100, 1) if tb else 0
        download_state.update(job_id, status="running", pct=pct, **kw)

    files = adapter.download(hyp3_job_id, Path(settings.downloads_dir), progress_cb=on_progress)

    db2 = SessionLocal()
    try:
        job = db2.query(Job).filter_by(id=job_id).first()
        if job:
            job.downloaded = 1
            job.download_path = str(files[0].parent) if files else None
            db2.commit()
    finally:
        db2.close()

    download_state.update(job_id, status="done", pct=100)
    logger.info("Queue: completed download for job %s", job_id)
