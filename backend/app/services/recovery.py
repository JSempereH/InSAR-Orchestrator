"""Pick up where the app left off after it stopped and started again.

Download queues, submission threads and MintPy runs live in memory, so a restart
(an update, a crash, the machine rebooting) used to strand whatever was in
flight. Three things are put right on every start:

* Batches stuck in "submitting" are resumed from the pairs stored on the batch
  (see batch_submission: pairs HyP3 already accepted are adopted, not repeated).
* Jobs that succeeded on HyP3 but were never downloaded, in batches that
  auto-download, are queued again.
* MintPy runs that were cut short are restarted once (see mintpy_service).

Anything that cannot be recovered safely is left visible rather than guessed:
a batch with no stored pairs (created before this existed) is closed, and a
MintPy run interrupted twice stays INTERRUPTED until someone starts it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import or_

from app.database import SessionLocal
from app.models import Batch, Job, JobStatus, Project
from app.services import batch_submission, download_queue, mintpy_service

logger = logging.getLogger(__name__)

# HyP3 deletes a job's files after about two weeks. Past that, a download can only
# fail, so it is not retried on every start.
_RESULT_LIFETIME = timedelta(days=13)


def resume_submitting_batches() -> dict:
    """Resume or close every batch whose submission was cut short."""
    resumed: list[str] = []
    closed: list[str] = []
    db = SessionLocal()
    try:
        for batch in db.query(Batch).filter(Batch.status == "submitting").all():
            project = db.query(Project).filter_by(id=batch.project_id).first()
            pairs = [(ref, sec) for ref, sec in (batch.pending_pairs or [])]
            if project is None or not pairs or project.archived_remote:
                # Nothing to resume from (an older batch, or a project that is gone).
                # Closing it is honest: "Update" will offer whatever is still missing.
                batch.status = "done"
                batch.pending_pairs = None
                closed.append(batch.id)
                continue
            batch_submission.start(
                batch.id, pairs, job_name=batch_submission.job_name_for(project.name),
                parameters=batch.parameters, resume=True,
            )
            resumed.append(batch.id)
        db.commit()
    finally:
        db.close()
    return {"resumed": resumed, "closed": closed}


def requeue_auto_downloads(now: Optional[datetime] = None) -> int:
    """Queue succeeded, undownloaded jobs of auto-download batches. Returns how many."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    items: list[dict] = []
    db = SessionLocal()
    try:
        rows = (
            db.query(Job)
            .join(Batch, Batch.id == Job.batch_id)
            .join(Project, Project.id == Batch.project_id)
            .filter(
                Job.status == JobStatus.SUCCEEDED,
                or_(Job.downloaded == 0, Job.downloaded.is_(None)),
                Job.hyp3_job_id.isnot(None),
                Batch.auto_download.is_(True),
                Project.archived_remote.is_(None),
            )
            .all()
        )
        for job in rows:
            finished = job.completed_at or job.submitted_at
            if finished is not None and now - finished > _RESULT_LIFETIME:
                continue
            items.append({"job_id": job.id, "hyp3_job_id": job.hyp3_job_id})
    finally:
        db.close()
    if items:
        download_queue.enqueue(items)
    return len(items)


def recover_on_startup() -> dict:
    """Run every recovery step. A failure in one does not stop the others."""
    summary: dict = {}
    for name, step in (
        ("batches", resume_submitting_batches),
        ("downloads_queued", requeue_auto_downloads),
        ("mintpy", mintpy_service.recover_interrupted_runs),
    ):
        try:
            summary[name] = step()
        except Exception:
            logger.exception("Startup recovery step %r failed", name)
            summary[name] = "failed"
    logger.info("Startup recovery: %s", summary)
    return summary
