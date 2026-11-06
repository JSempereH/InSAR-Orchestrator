from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from app.database import SessionLocal
from app.models import Job, JobStatus
from app.services import download_queue
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
    result = _refresh_statuses_sync()
    try:
        _launch_pending_mintpy()
    except Exception:
        logger.exception("Could not check batches waiting for MintPy")
    return result


def _launch_pending_mintpy() -> None:
    """Start MintPy for projects whose "update" batch has fully landed on disk.

    A batch is ready once it finished submitting, none of its jobs is still
    pending or running, and every job that succeeded has been downloaded. This
    runs on every poll cycle, including those with no active jobs, because the
    last downloads usually finish after the last status change.
    """
    from app.config import settings
    from app.models import Batch, Project
    from app.services import mintpy_service

    db = SessionLocal()
    try:
        waiting = (
            db.query(Batch)
            .filter(Batch.run_mintpy_when_done.is_(True), Batch.status == "done")
            .all()
        )
        for batch in waiting:
            if not batch.jobs:
                # Every submission failed: nothing will ever arrive.
                batch.run_mintpy_when_done = False
                continue
            if any(j.status in (JobStatus.PENDING, JobStatus.RUNNING) for j in batch.jobs):
                continue
            if any(j.status == JobStatus.SUCCEEDED and not j.downloaded for j in batch.jobs):
                continue

            project = db.query(Project).filter_by(id=batch.project_id).first()
            if project is None:
                batch.run_mintpy_when_done = False
                continue
            downloads_dir = Path(project.storage_path) if project.storage_path else Path(settings.downloads_dir)
            try:
                mintpy_service.start_run(project.id, downloads_dir, geometry=project.geometry)
            except RuntimeError as exc:
                if "already in progress" in str(exc):
                    continue  # try again next cycle
                logger.warning("Not running MintPy for project %s: %s", project.id, exc)
            else:
                logger.info("Started MintPy for project %s after batch %s", project.id, batch.id)
            batch.run_mintpy_when_done = False
        db.commit()
    finally:
        db.close()


def _refresh_statuses_sync() -> dict:
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
        to_auto_download: list[dict] = []
        for job in active:
            hyp3_job = hyp3_statuses.get(job.hyp3_job_id)
            if hyp3_job is None:
                try:
                    hyp3_job = adapter.get_status(job.hyp3_job_id)
                except Exception:
                    logger.warning("Could not refresh job %s", job.hyp3_job_id)
                    continue

            # Savepoint per job: a bad row rolls back only itself, not every
            # update already made earlier in this cycle.
            try:
                with db.begin_nested():
                    new_status = JobStatus(hyp3_job.status.value)
                    changed = new_status != job.status
                    if changed:
                        logger.info(
                            "Job %s: %s → %s", job.hyp3_job_id, job.status.value, new_status.value
                        )
                        job.status = new_status
                        if new_status in (JobStatus.SUCCEEDED, JobStatus.FAILED) and job.completed_at is None:
                            job.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
                    if hyp3_job.credit_cost is not None:
                        job.credit_cost = hyp3_job.credit_cost
                    if hyp3_job.error_message:
                        job.error_message = str(hyp3_job.error_message)
                    db.flush()  # validate the row before moving on
            except Exception:
                logger.warning("Could not update job %s, skipping", job.hyp3_job_id, exc_info=True)
                continue

            # Only queue the download once the row's update is safely stored.
            if changed:
                updated += 1
                if new_status == JobStatus.SUCCEEDED and job.batch and job.batch.auto_download:
                    to_auto_download.append({"job_id": job.id, "hyp3_job_id": job.hyp3_job_id})

        db.commit()

        if to_auto_download:
            logger.info("Auto-download: queueing %d newly succeeded job(s)", len(to_auto_download))
            download_queue.enqueue(to_auto_download)

        logger.info("Poll complete: %d/%d jobs updated", updated, len(active))
        return {"active": len(active), "updated": updated}
    finally:
        db.close()
