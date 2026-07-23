from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.database import SessionLocal
from app.models import Job, JobStatus
from app.services.hyp3_service import get_hyp3_adapter

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 30
_poll_lock = asyncio.Lock()


async def poll_active_jobs() -> None:
    """Background task: refresh HyP3 status for all non-terminal jobs every 30 s."""
    while True:
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        try:
            await _refresh_jobs()
        except Exception:
            logger.exception("Unhandled error in polling loop")


async def force_poll() -> dict:
    """Trigger an immediate poll outside the regular schedule. Returns stats."""
    if _poll_lock.locked():
        return {"status": "already_running"}
    return await _refresh_jobs()


async def _refresh_jobs() -> dict:
    async with _poll_lock:
        return await asyncio.get_running_loop().run_in_executor(None, _refresh_jobs_sync)


def _refresh_jobs_sync() -> dict:
    db = SessionLocal()
    try:
        active = (
            db.query(Job)
            .filter(Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]))
            .filter(Job.hyp3_job_id.isnot(None))
            .all()
        )
        if not active:
            logger.info("Polling: no active jobs to check")
            return {"active": 0, "updated": 0}

        # Find earliest submission to bound the HyP3 query
        submitted_dates = [
            j.submitted_at for j in active if j.submitted_at is not None
        ]
        since = min(submitted_dates) if submitted_dates else None

        adapter = get_hyp3_adapter(db)

        # ONE bulk API call instead of N individual calls
        try:
            hyp3_statuses = adapter.get_jobs_bulk(since=since)
            logger.info(
                "Bulk HyP3 query returned %d jobs (checking %d active in DB)",
                len(hyp3_statuses), len(active),
            )
        except Exception:
            logger.exception("Bulk HyP3 fetch failed, skipping poll cycle")
            return {"active": len(active), "updated": 0, "error": "bulk_fetch_failed"}

        updated = 0
        for job in active:
            hyp3_job = hyp3_statuses.get(job.hyp3_job_id)
            if hyp3_job is None:
                try:
                    hyp3_job = adapter.get_status(job.hyp3_job_id)
                except Exception:
                    logger.warning("Could not refresh job %s", job.hyp3_job_id)
                    continue

            try:
                new_status = JobStatus(hyp3_job.status.value)
                if new_status != job.status:
                    logger.info(
                        "Job %s: %s → %s", job.hyp3_job_id, job.status.value, new_status.value
                    )
                    job.status = new_status
                    updated += 1

                if hyp3_job.completed_at:
                    job.completed_at = hyp3_job.completed_at
                if hyp3_job.credit_cost is not None:
                    job.credit_cost = hyp3_job.credit_cost
                if hyp3_job.error_message:
                    job.error_message = str(hyp3_job.error_message)

                db.flush()  # validate each row before moving on
            except Exception:
                logger.warning("Could not update job %s, skipping", job.hyp3_job_id, exc_info=True)
                db.rollback()

        db.commit()
        logger.info("Poll complete: %d/%d jobs updated", updated, len(active))
        return {"active": len(active), "updated": updated}
    finally:
        db.close()
