import logging
from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Batch, Job, JobStatus, Project
from app.schemas import BatchOut, BatchPlanOut, JobOut, SubmitBatchRequest
from app.services import batch_submission, download_state
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


# Rough fallback when a project has no downloaded jobs yet to measure from -
# observed average size of a HyP3 INSAR_GAMMA product bundle.
_DEFAULT_JOB_SIZE_GB = 0.28


def _estimate_batch_size_gb(db: Session, project: Project, pair_count: int) -> Optional[float]:
    """Estimate total download size for `pair_count` new jobs, based on the
    average size of this project's already-downloaded jobs (or a flat
    fallback if it doesn't have any yet).

    HyP3Adapter.download() has no per-job subfolder - every job in a project
    lands flat in the same directory (project.storage_path). So the only way
    to get a per-job average is: size of that shared folder's *top-level
    files* (excluding the slc/ subfolder, which is unrelated raw SLC data),
    divided by how many jobs share it - not the folder's size on its own.
    """
    if pair_count <= 0:
        return None

    downloaded = (
        db.query(Job)
        .join(Batch, Batch.id == Job.batch_id)
        .filter(Batch.project_id == project.id, Job.downloaded == 1, Job.download_path.isnot(None))
        .all()
    )
    if not downloaded:
        return round(_DEFAULT_JOB_SIZE_GB * pair_count, 1)

    jobs_per_folder: dict[str, int] = {}
    for job in downloaded:
        jobs_per_folder[job.download_path] = jobs_per_folder.get(job.download_path, 0) + 1

    total_bytes = 0
    total_jobs = 0
    for folder, job_count in jobs_per_folder.items():
        path = Path(folder)
        if not path.is_dir():
            continue
        total_bytes += sum(f.stat().st_size for f in path.iterdir() if f.is_file())
        total_jobs += job_count

    if total_jobs == 0:
        return round(_DEFAULT_JOB_SIZE_GB * pair_count, 1)

    avg_gb = (total_bytes / 1e9) / total_jobs
    return round(avg_gb * pair_count, 1)


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
        estimated_size_gb=_estimate_batch_size_gb(db, project, plan.total_pairs),
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
    pairs = _get_orchestrator(db).build_pairs(
        params=_project_search_params(project),
        max_temporal_neighbors=body.max_temporal_neighbors,
        exclude_pairs=exclude or None,
    )

    if not pairs:
        raise HTTPException(400, "All pairs for this project have already been submitted.")

    # Create the batch now, before submitting a single pair, so it's visible
    # in the UI immediately. Submission to HyP3 (one HTTP call per pair) runs
    # in the background and fills in Job rows as it goes; see batch_submission.
    batch = Batch(
        project_id=project_id,
        label=body.label or f"Batch {len(project.batches) + 1}",
        total_pairs=len(pairs),
        status="submitting",
        auto_download=body.auto_download,
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)

    batch_submission.start(batch.id, pairs, job_name=f"insar-{project.name[:20]}")

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
