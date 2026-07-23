import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Batch, Job, JobStatus, Project
from app.schemas import BatchOut, BatchPlanOut, JobOut, SubmitBatchRequest
from app.services import download_state
from app.services.hyp3_service import get_hyp3_adapter
from insar_core.adapters.asf import ASFAdapter
from insar_core.models.scene import AOI, SearchParams
from insar_core.pipeline.orchestrator import InSAROrchestrator

router = APIRouter(prefix="/api/projects", tags=["jobs"])
logger = logging.getLogger(__name__)


def _get_orchestrator(db: Session) -> InSAROrchestrator:
    return InSAROrchestrator(
        scene_adapter=ASFAdapter(),
        processor=get_hyp3_adapter(db),
    )


def _project_search_params(project: Project) -> SearchParams:
    return SearchParams(
        aoi=AOI(geometry=project.geometry),
        date_start=date.fromisoformat(project.date_start),
        date_end=date.fromisoformat(project.date_end),
        track_number=project.track_number,
        flight_direction=project.flight_direction,
    )


def _existing_pairs(project: Project) -> set[tuple[str, str]]:
    """Granule pairs already in the DB for this project with a non-FAILED status."""
    return {
        (j.reference_granule, j.secondary_granule)
        for b in project.batches
        for j in b.jobs
        if j.status != JobStatus.FAILED
    }


@router.post("/{project_id}/batches/plan", response_model=BatchPlanOut)
def plan_batch(
    project_id: str,
    body: SubmitBatchRequest,
    db: Session = Depends(get_db),
):
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(404, "Project not found")

    exclude = _existing_pairs(project)
    plan = _get_orchestrator(db).plan_batch(
        params=_project_search_params(project),
        max_temporal_neighbors=body.max_temporal_neighbors,
        exclude_pairs=exclude or None,
    )
    return BatchPlanOut(
        total_pairs=plan.total_pairs,
        scene_count=plan.scene_count,
        pairs_preview=[[r, s] for r, s in plan.pairs_preview],
    )


@router.post("/{project_id}/batches", response_model=BatchOut, status_code=201)
def submit_batch(
    project_id: str,
    body: SubmitBatchRequest,
    db: Session = Depends(get_db),
):
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(404, "Project not found")

    if body.dry_run:
        raise HTTPException(400, "Set dry_run=false to actually submit")

    exclude = _existing_pairs(project)
    submitted = _get_orchestrator(db).submit_batch(
        params=_project_search_params(project),
        max_temporal_neighbors=body.max_temporal_neighbors,
        job_name=f"insar-{project.name[:20]}",
        exclude_pairs=exclude or None,
    )

    if not submitted:
        raise HTTPException(400, "All pairs for this project have already been submitted.")

    batch = Batch(
        project_id=project_id,
        label=body.label or f"Batch {len(project.batches) + 1}",
        total_pairs=len(submitted),
    )
    db.add(batch)
    db.flush()

    for s in submitted:
        db.add(Job(
            batch_id=batch.id,
            hyp3_job_id=s.hyp3_job_id,
            reference_granule=s.reference_granule,
            secondary_granule=s.secondary_granule,
            reference_date=s.reference_date.isoformat() if s.reference_date else None,
            secondary_date=s.secondary_date.isoformat() if s.secondary_date else None,
            status=JobStatus(s.status.value),
            submitted_at=s.submitted_at,
        ))

    db.commit()
    db.refresh(batch)
    return batch


@router.get("/{project_id}/batches/{batch_id}/jobs", response_model=list[JobOut])
def list_jobs(
    project_id: str,
    batch_id: str,
    db: Session = Depends(get_db),
):
    batch = db.query(Batch).filter_by(id=batch_id, project_id=project_id).first()
    if not batch:
        raise HTTPException(404, "Batch not found")

    result = []
    for job in batch.jobs:
        out = JobOut.model_validate(job)
        out.is_downloading = download_state.get(job.id).get("status") == "running"
        result.append(out)
    return result
