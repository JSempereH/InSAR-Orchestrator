"""
Global download queue. Persists across browser sessions as long as the backend runs.

Design: a single daemon thread processes one job at a time. POSTing a new queue
replaces the pending items (the item currently downloading is not interrupted).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from app.services import download_state
from app.services.sequential_queue import SequentialQueue

logger = logging.getLogger(__name__)

_queue = SequentialQueue(name="HyP3", item_key=lambda item: item["job_id"])


# ── Public API ─────────────────────────────────────────────────────────────

def start(jobs: list[dict]) -> None:
    """Replace pending queue with `jobs` and start worker if not already running.

    Each item: {"job_id": str, "hyp3_job_id": str}
    Currently-downloading job (if any) is not interrupted.
    """
    _queue.start(jobs, _download_one)


def enqueue(jobs: list[dict]) -> None:
    """Append `jobs` to the pending queue, unlike start() this never clobbers
    what's already queued (or being downloaded). Used by auto-download, which
    discovers newly-succeeded jobs a few at a time across poll cycles.

    Each item: {"job_id": str, "hyp3_job_id": str}
    """
    _queue.enqueue(jobs, _download_one)


def cancel() -> None:
    """Clear pending queue. The current download finishes, then the worker stops."""
    _queue.cancel()


def get_state() -> dict:
    """Return a snapshot of current queue state (thread-safe copy)."""
    raw = _queue.get_state()
    current = raw["current_item"]
    return {
        "active": raw["active"],
        "current_job_id": current["job_id"] if current else None,
        "current_progress": raw["current_progress"],
        "pending_count": raw["pending_count"],
        "pending_job_ids": raw["pending_items"],
        "total": raw["total"],
        "done": raw["done"],
        "cancelled": raw["cancelled"],
    }


# ── Item download ────────────────────────────────────────────────────────────

def _resolve_destination(db, job_id: str) -> Optional[Path]:
    """Per-project storage destination, falling back to the app default."""
    from app.config import settings
    from app.models import Batch, Job, Project

    storage_path = (
        db.query(Project.storage_path)
        .join(Batch, Batch.project_id == Project.id)
        .join(Job, Job.batch_id == Batch.id)
        .filter(Job.id == job_id)
        .scalar()
    )
    destination = Path(storage_path) if storage_path else Path(settings.downloads_dir)
    return destination if destination.exists() else None


def _download_one(item: dict) -> None:
    job_id = item["job_id"]
    hyp3_job_id = item["hyp3_job_id"]

    # Import here to avoid circular imports at module load time
    from app.database import SessionLocal
    from app.models import Job
    from app.services.hyp3_service import get_hyp3_adapter

    db = SessionLocal()
    try:
        adapter = get_hyp3_adapter(db)
        destination = _resolve_destination(db, job_id)
    finally:
        db.close()

    if destination is None:
        # Raise rather than setting status="error" here directly: the
        # queue's own try/except (see SequentialQueue._run) is what reports
        # a failed item's terminal state, for every queue uniformly - two
        # places setting it would risk them disagreeing.
        raise RuntimeError(f"Storage destination not available for job {job_id}")

    def on_progress(**kw):
        tb = kw.get("total_bytes", 0)
        dl = kw.get("downloaded_bytes", 0)
        pct = round(dl / tb * 100, 1) if tb else 0
        download_state.update(job_id, status="running", pct=pct, **kw)

    files = adapter.download(hyp3_job_id, destination, progress_cb=on_progress)

    db2 = SessionLocal()
    try:
        job = db2.query(Job).filter_by(id=job_id).first()
        if job:
            job.downloaded = 1
            job.download_path = str(files[0].parent) if files else None
            db2.commit()
    finally:
        db2.close()
