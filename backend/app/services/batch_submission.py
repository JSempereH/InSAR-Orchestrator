"""Background submission of a batch's pairs to HyP3.

Design: the router creates the Batch row (status "submitting") as soon as the
pair list is known, so the frontend can show it right away. This module then
submits each pair one at a time in a daemon thread - each submission is a
network round trip to HyP3, so a large batch can take a while - and commits a
Job row after every successful submission, so the UI fills in progressively
instead of only appearing once everything is done.
"""
from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from insar_core.models.scene import SARScene

logger = logging.getLogger(__name__)


def start(batch_id: str, pairs: list[tuple["SARScene", "SARScene"]], job_name: str) -> None:
    thread = threading.Thread(target=_run, args=(batch_id, pairs, job_name), daemon=True)
    thread.start()


def _run(batch_id: str, pairs: list[tuple["SARScene", "SARScene"]], job_name: str) -> None:
    from app.database import SessionLocal
    from app.models import Batch, Job, JobStatus
    from app.services.hyp3_service import get_hyp3_adapter

    db = SessionLocal()
    try:
        adapter = get_hyp3_adapter(db)

        for ref, sec in pairs:
            try:
                submitted = adapter.submit_pair(ref.granule_name, sec.granule_name, name=job_name)
            except Exception:
                logger.exception(
                    "Batch %s: failed to submit pair (%s, %s)",
                    batch_id, ref.granule_name, sec.granule_name,
                )
                continue

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
    except Exception:
        logger.exception("Batch %s: submission run aborted", batch_id)
    finally:
        batch = db.query(Batch).filter_by(id=batch_id).first()
        if batch:
            batch.status = "done"
            db.commit()
        db.close()
