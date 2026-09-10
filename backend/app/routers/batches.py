"""
Endpoints for Batch and Job: mostly read-only, plus small in-place updates
(e.g. toggling auto_download) that don't belong to the submission flow.

Batch submission lives in jobs.py to keep routing concerns separate.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.services import download_queue

router = APIRouter(prefix="/api/batches", tags=["batches"])


@router.get("/{batch_id}", response_model=schemas.BatchOut)
def get_batch(batch_id: str, db: Session = Depends(get_db)):
    batch = db.query(models.Batch).filter(models.Batch.id == batch_id).first()
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    return batch


@router.patch("/{batch_id}", response_model=schemas.BatchOut)
def update_batch(batch_id: str, body: schemas.BatchUpdate, db: Session = Depends(get_db)):
    batch = db.query(models.Batch).filter(models.Batch.id == batch_id).first()
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    turning_on = body.auto_download and not batch.auto_download
    batch.auto_download = body.auto_download
    db.commit()
    db.refresh(batch)

    if turning_on:
        # Jobs that already succeeded before auto_download was enabled won't
        # trigger another status transition, so queue them here directly.
        already_succeeded = [
            {"job_id": j.id, "hyp3_job_id": j.hyp3_job_id}
            for j in batch.jobs
            if j.status == models.JobStatus.SUCCEEDED and not j.downloaded and j.hyp3_job_id
        ]
        download_queue.enqueue(already_succeeded)

    return batch


@router.get("/{batch_id}/jobs", response_model=list[schemas.JobOut])
def list_batch_jobs(batch_id: str, db: Session = Depends(get_db)):
    batch = db.query(models.Batch).filter(models.Batch.id == batch_id).first()
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    return batch.jobs
