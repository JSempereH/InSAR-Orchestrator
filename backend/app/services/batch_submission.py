"""Background submission of a batch's pairs to HyP3.

Design: the router creates the Batch row (status "submitting") as soon as the
pair list is known, so the frontend can show it right away. This module then
submits each pair one at a time in a daemon thread - each submission is a
network round trip to HyP3, so a large batch can take a while - and commits a
Job row after every successful submission, so the UI fills in progressively
instead of only appearing once everything is done.

The pair list is stored on the batch (`pending_pairs`) before the thread starts,
so if the app stops half way, `start(..., resume=True)` can finish the job on the
next start. When resuming, HyP3 is asked which jobs already exist under this
project's job name: a pair HyP3 accepted but the app never recorded is adopted
rather than submitted again, because a second submission would spend credits twice.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from insar_core.adapters.hyp3 import DEFAULT_INSAR_PARAMETERS

logger = logging.getLogger(__name__)

Pair = tuple[str, str]   # (reference granule, secondary granule)


def job_name_for(project_name: str) -> str:
    return f"insar-{project_name[:20]}"


def start(
    batch_id: str,
    pairs: list[Pair],
    job_name: str,
    parameters: Optional[dict] = None,
    resume: bool = False,
) -> None:
    thread = threading.Thread(
        target=_run, args=(batch_id, pairs, job_name, parameters, resume), daemon=True,
    )
    thread.start()


def _add_job(db, batch_id: str, submitted) -> None:
    from app.models import Job, JobStatus

    db.add(Job(
        batch_id=batch_id,
        hyp3_job_id=submitted.hyp3_job_id,
        reference_granule=submitted.reference_granule,
        secondary_granule=submitted.secondary_granule,
        reference_date=submitted.reference_date.isoformat() if submitted.reference_date else None,
        secondary_date=submitted.secondary_date.isoformat() if submitted.secondary_date else None,
        status=JobStatus(submitted.status.value),
        submitted_at=submitted.submitted_at,
    ))
    db.commit()


def _adoptable_jobs(db, adapter, job_name: str) -> dict[Pair, object]:
    """HyP3 jobs under `job_name` that the database does not know yet, by pair."""
    from app.models import Job

    known = {hyp3_id for (hyp3_id,) in db.query(Job.hyp3_job_id).filter(Job.hyp3_job_id.isnot(None))}
    found: dict[Pair, object] = {}
    for job in adapter.find_jobs_by_name(job_name):
        if job.status.value == "FAILED" or job.hyp3_job_id in known:
            continue
        found[(job.reference_granule, job.secondary_granule)] = job
    return found


def _run(
    batch_id: str,
    pairs: list[Pair],
    job_name: str,
    parameters: Optional[dict],
    resume: bool,
) -> None:
    from app.database import SessionLocal
    from app.models import Batch, Job
    from app.services.hyp3_service import get_hyp3_adapter

    hyp3_parameters = (parameters or {}).get("hyp3") or dict(DEFAULT_INSAR_PARAMETERS)

    db = SessionLocal()
    try:
        adapter = get_hyp3_adapter(db)
        done = {
            (ref, sec)
            for ref, sec in db.query(Job.reference_granule, Job.secondary_granule).filter(Job.batch_id == batch_id)
        }
        adoptable: dict[Pair, object] = {}
        if resume:
            try:
                adoptable = _adoptable_jobs(db, adapter, job_name)
            except Exception:
                # Without this check a resumed submission could double-spend, so give up
                # on the resume rather than guess; the batch is closed and "Update"
                # offers the missing pairs again.
                logger.exception("Batch %s: could not list HyP3 jobs, not resuming", batch_id)
                return

        for ref, sec in pairs:
            if (ref, sec) in done:
                continue
            submitted = adoptable.pop((ref, sec), None)
            if submitted is not None:
                logger.info("Batch %s: adopting existing HyP3 job %s", batch_id, submitted.hyp3_job_id)
            else:
                try:
                    submitted = adapter.submit_pair(ref, sec, name=job_name, **hyp3_parameters)
                except Exception:
                    logger.exception("Batch %s: failed to submit pair (%s, %s)", batch_id, ref, sec)
                    continue
            _add_job(db, batch_id, submitted)
    except Exception:
        logger.exception("Batch %s: submission run aborted", batch_id)
    finally:
        batch = db.query(Batch).filter_by(id=batch_id).first()
        if batch:
            batch.status = "done"
            batch.pending_pairs = None
            db.commit()
        db.close()
