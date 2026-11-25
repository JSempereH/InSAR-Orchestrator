import logging
from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Batch, Job, JobStatus, Project
from app.schemas import (
    BatchOut, BatchPlanOut, JobOut, PlannedPairOut, PlannedSceneOut, ProjectRefreshOut, ProjectRefreshRequest,
    SubmitBatchRequest,
)
from app.services import archive_service, batch_submission, download_state, provenance_service
from app.services import asf_service
from app.services.hyp3_service import get_hyp3_adapter
from insar_core.adapters.asf_burst import BurstError
from insar_core.models.scene import AOI, SearchParams
from insar_core.pipeline.orchestrator import InSAROrchestrator
from insar_core.pipeline.pair_planner import PairPlan, PairPlanConfig

router = APIRouter(prefix="/api/projects", tags=["jobs"])
logger = logging.getLogger(__name__)


HYP3_ENGINES = ("hyp3_gamma", "hyp3_burst")


def _get_orchestrator(db: Session) -> InSAROrchestrator:
    # Reuses the same ASFAdapter instance asf_service uses (rather than a
    # fresh one per request) - it caches raw catalog searches internally, so
    # sharing it lets that cache actually do something across requests.
    return InSAROrchestrator(
        scene_adapter=asf_service.get_adapter(),
        processor=get_hyp3_adapter(db),
    )


def _orchestrator_for(db: Session, project: Project) -> InSAROrchestrator:
    """The orchestrator of this project's engine: a burst project searches by bursts.

    The HyP3 adapter is the same for both, it submits whole scenes or bursts by their names."""
    if project.engine == "hyp3_burst":
        return InSAROrchestrator(scene_adapter=asf_service.get_burst_adapter(), processor=get_hyp3_adapter(db))
    return _get_orchestrator(db)


def _project_search_params(project: Project, date_end: Optional[str] = None) -> SearchParams:
    return SearchParams(
        aoi=AOI(geometry=project.geometry),
        date_start=date.fromisoformat(project.date_start),
        date_end=date.fromisoformat(date_end or project.date_end),
        track_number=project.track_number,
        flight_direction=project.flight_direction,
    )


def _require_hyp3(project: Project) -> None:
    if project.engine not in HYP3_ENGINES:
        raise HTTPException(
            422, "This is a NISAR project: its interferograms are ready-made and are not processed through HyP3. Download its files instead.")


def _pair_config(body: SubmitBatchRequest) -> PairPlanConfig:
    config = PairPlanConfig(
        strategy=body.strategy, max_temporal_neighbors=body.max_temporal_neighbors,
        dt_targets=body.dt_targets or None, dt_tolerance=body.dt_tolerance, dt_max=body.dt_max,
        pb_max=body.pb_max, min_degree=body.min_degree, max_degree=body.max_degree,
        include_annual=body.include_annual,
    )
    if body.long_pairs_days is not None:
        config.long_pairs_days = tuple(body.long_pairs_days)
    config.calibration_pair = body.calibration_pair
    try:
        config.validate()
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return config


def _plan_for(db: Session, project: Project, body: SubmitBatchRequest) -> PairPlan:
    try:
        return _orchestrator_for(db, project).plan_pairs(
            _project_search_params(project), _pair_config(body),
            submitted_pairs=_existing_pairs(db, project),
            excluded_pairs={(pair[0], pair[1]) for pair in body.exclude if len(pair) == 2},
        )
    except BurstError as exc:
        # the area does not fit in one burst interferogram, or spans two tracks: the message says which
        raise HTTPException(422, str(exc)) from exc


def _existing_pairs(db: Session, project: Project) -> set[tuple[str, str]]:
    """Granule pairs already in the DB for this project with a non-FAILED status.

    One query joined on Batch.project_id, rather than iterating
    project.batches then each batch's .jobs (lazy relationships - that was
    1 query per batch instead of 1 total)."""
    rows = (
        db.query(Job.reference_granule, Job.secondary_granule)
        .join(Batch, Batch.id == Job.batch_id)
        .filter(Batch.project_id == project.id, Job.status != JobStatus.FAILED)
        .all()
    )
    return {(ref, sec) for ref, sec in rows}


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
        # The fallback was measured on whole-scene products. A burst product is far smaller and has not
        # been measured here, so no figure is given for it rather than one that is ten times too large.
        if project.engine == "hyp3_burst":
            return None
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
    _require_hyp3(project)

    plan = _plan_for(db, project, body)
    new_pairs = plan.new_pairs
    return BatchPlanOut(
        total_pairs=len(new_pairs),
        scene_count=len(plan.scenes),
        pairs_preview=[[p.reference.granule_name, p.secondary.granule_name] for p in new_pairs[:5]],
        estimated_size_gb=_estimate_batch_size_gb(db, project, len(new_pairs)),
        strategy=plan.config.strategy,
        credits=plan.credits,
        credits_per_pair=plan.credits_per_pair,
        connected=plan.connected,
        components=plan.components,
        over_limit=plan.over_limit,
        warnings=plan.warnings,
        pairs=[
            PlannedPairOut(
                reference=p.reference.granule_name, secondary=p.secondary.granule_name,
                reference_date=p.reference.acquisition_date.isoformat(), secondary_date=p.secondary.acquisition_date.isoformat(),
                temporal_days=p.temporal_days, perpendicular_m=p.perpendicular_m, over_limit=p.over_limit,
                reason=p.reason, state=p.state,
            )
            for p in plan.pairs
        ],
        scenes=[
            PlannedSceneOut(granule=s.granule_name, date=s.acquisition_date.isoformat(), perpendicular_m=s.perpendicular_baseline_m)
            for s in plan.scenes
        ],
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

    archive_service.reject_if_archived(project)
    _require_hyp3(project)
    if body.dry_run:
        raise HTTPException(400, "Set dry_run=false to actually submit")

    plan = _plan_for(db, project, body)
    pairs = plan.as_pairs()
    if not pairs:
        raise HTTPException(400, "All pairs for this project have already been submitted or left out.")

    return _start_batch(
        db, project, pairs,
        label=body.label, auto_download=body.auto_download,
        max_temporal_neighbors=body.max_temporal_neighbors,
        pair_config=plan.config,
    )


def _start_batch(
    db: Session,
    project: Project,
    pairs: list,
    label: Optional[str],
    auto_download: bool,
    max_temporal_neighbors: int,
    run_mintpy_when_done: bool = False,
    pair_config: Optional[PairPlanConfig] = None,
) -> Batch:
    """Create the batch row and hand the pairs to the background submitter.

    The batch is created now, before submitting a single pair, so it's
    visible in the UI immediately. Submission to HyP3 (one HTTP call per pair)
    runs in the background and fills in Job rows as it goes; see
    batch_submission."""
    granule_pairs = [(ref.granule_name, sec.granule_name) for ref, sec in pairs]
    parameters = provenance_service.batch_parameters(project, max_temporal_neighbors, pair_config)
    batch = Batch(
        project_id=project.id,
        label=label or f"Batch {len(project.batches) + 1}",
        total_pairs=len(pairs),
        status="submitting",
        auto_download=auto_download,
        run_mintpy_when_done=run_mintpy_when_done,
        # Saved before submitting, so a restart can resume the batch (see recovery.py).
        pending_pairs=[list(p) for p in granule_pairs],
        parameters=parameters,
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)

    batch_submission.start(
        batch.id, granule_pairs, job_name=batch_submission.job_name_for(project.name), parameters=parameters,
    )
    return batch


@router.post("/{project_id}/refresh", response_model=ProjectRefreshOut)
def refresh_project(
    project_id: str,
    body: ProjectRefreshRequest,
    db: Session = Depends(get_db),
):
    """Bring a project up to date with newer Sentinel-1 acquisitions.

    Moves the project's end date forward (never back), then processes only the
    pairs that are not already in the database, so earlier interferograms are
    neither re-submitted nor re-downloaded. A dry run (the default) reports
    what would be submitted without changing anything.
    """
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(404, "Project not found")

    archive_service.reject_if_archived(project)
    _require_hyp3(project)
    requested_end = body.date_end or date.today().isoformat()
    new_end = max(project.date_end or requested_end, requested_end)
    neighbors = body.max_temporal_neighbors or project.max_temporal_neighbors or 3

    params = _project_search_params(project, date_end=new_end)
    exclude = _existing_pairs(db, project) or None
    orchestrator = _orchestrator_for(db, project)

    if body.dry_run:
        plan = orchestrator.plan_batch(
            params=params, max_temporal_neighbors=neighbors, exclude_pairs=exclude,
        )
        return ProjectRefreshOut(
            date_end=new_end,
            total_pairs=plan.total_pairs,
            scene_count=plan.scene_count,
            estimated_size_gb=_estimate_batch_size_gb(db, project, plan.total_pairs),
        )

    pairs = orchestrator.build_pairs(
        params=params, max_temporal_neighbors=neighbors, exclude_pairs=exclude,
    )
    project.date_end = new_end
    project.max_temporal_neighbors = neighbors
    db.commit()

    if not pairs:
        return ProjectRefreshOut(date_end=new_end, total_pairs=0)

    batch = _start_batch(
        db, project, pairs,
        label=f"Update {date.today().isoformat()}",
        auto_download=True,
        max_temporal_neighbors=neighbors,
        run_mintpy_when_done=body.run_mintpy,
    )
    return ProjectRefreshOut(
        date_end=new_end,
        total_pairs=len(pairs),
        estimated_size_gb=_estimate_batch_size_gb(db, project, len(pairs)),
        batch=batch,
    )


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
