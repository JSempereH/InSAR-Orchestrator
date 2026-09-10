"""
CRUD endpoints for Project (area of interest).
"""

import re
import shutil
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app import models, schemas
from app.services import storage_move, storage_service

router = APIRouter(prefix="/api/projects", tags=["projects"])


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "project"


def _resolve_storage_path(mountpoint: Optional[str], project_name: str) -> str:
    """Every project gets its own subfolder, named after the project, so
    downloads from different projects never land mixed together in one
    flat directory - whether on an explicit disk or the app default."""
    base = Path(mountpoint) / "insar-orchestrator" if mountpoint else Path(settings.downloads_dir)
    resolved = base / _slugify(project_name)
    try:
        resolved.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Storage destination '{mountpoint}' is not available: {exc}",
        ) from exc
    return str(resolved)


@router.post("", response_model=schemas.ProjectOut)
def create_project(payload: schemas.ProjectCreate, db: Session = Depends(get_db)):
    data = payload.model_dump()
    mountpoint = data.pop("storage_mountpoint", None)
    data["storage_path"] = _resolve_storage_path(mountpoint, payload.name)

    project = models.Project(**data)
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


@router.get("", response_model=list[schemas.ProjectOut])
def list_projects(db: Session = Depends(get_db)):
    return db.query(models.Project).order_by(models.Project.created_at.desc()).all()


@router.get("/{project_id}", response_model=schemas.ProjectOut)
def get_project(project_id: str, db: Session = Depends(get_db)):
    project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.delete("/{project_id}")
def delete_project(project_id: str, db: Session = Depends(get_db)):
    project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    db.delete(project)
    db.commit()
    return {"deleted": True}


@router.get("/{project_id}/batches", response_model=list[schemas.BatchOut])
def list_project_batches(project_id: str, db: Session = Depends(get_db)):
    project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project.batches


@router.get("/{project_id}/download-summary", response_model=schemas.ProjectDownloadSummaryOut)
def project_download_summary(project_id: str, db: Session = Depends(get_db)):
    project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    jobs = (
        db.query(models.Job)
        .join(models.Batch, models.Batch.id == models.Job.batch_id)
        .filter(models.Batch.project_id == project_id)
        .all()
    )
    return schemas.ProjectDownloadSummaryOut(
        storage_path=project.storage_path,
        total_jobs=len(jobs),
        downloaded_jobs=sum(1 for j in jobs if j.downloaded),
    )


@router.post("/{project_id}/storage/move", response_model=schemas.MoveStateOut)
def move_project_storage(project_id: str, body: schemas.MoveRequest, db: Session = Depends(get_db)):
    """Move everything downloaded for this project (HyP3 outputs + the slc/
    subfolder) to a different disk. Runs in the background; poll GET
    /api/storage/move for progress."""
    project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if not project:
        raise HTTPException(404, "Project not found")
    if not project.storage_path or not Path(project.storage_path).exists():
        raise HTTPException(400, "Nothing to move: no storage path, or it doesn't exist on disk")

    src = Path(project.storage_path)
    base = Path(body.mountpoint) / "insar-orchestrator" if body.mountpoint else Path(settings.downloads_dir)
    dst = base / _slugify(project.name)

    if dst.resolve() == src.resolve():
        raise HTTPException(400, "Source and destination are the same")
    if dst.exists():
        raise HTTPException(400, f"Destination '{dst}' already exists")

    def on_complete(new_path: Path) -> None:
        from app.database import SessionLocal

        db2 = SessionLocal()
        try:
            proj = db2.query(models.Project).filter_by(id=project_id).first()
            if not proj:
                return
            old_prefix = proj.storage_path
            proj.storage_path = str(new_path)

            jobs = (
                db2.query(models.Job)
                .join(models.Batch, models.Batch.id == models.Job.batch_id)
                .filter(models.Batch.project_id == project_id, models.Job.download_path.isnot(None))
                .all()
            )
            for j in jobs:
                if j.download_path and j.download_path.startswith(old_prefix):
                    j.download_path = str(new_path) + j.download_path[len(old_prefix):]

            for rec in db2.query(models.SLCDownload).filter_by(project_id=project_id).all():
                if rec.destination_path.startswith(old_prefix):
                    rec.destination_path = str(new_path) + rec.destination_path[len(old_prefix):]

            db2.commit()
        finally:
            db2.close()

    try:
        storage_move.start(f"project:{project_id}", src, dst, on_complete)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))

    return storage_move.get_current_state()


@router.delete("/{project_id}/download-data")
def delete_project_download_data(project_id: str, db: Session = Depends(get_db)):
    """Delete the downloaded HyP3 output files for this project - the local
    file cache only. Leaves the slc/ subfolder (delete those separately, via
    the SLC download endpoint) and the Batch/Job processing history intact;
    just marks each Job as not-downloaded so it can be re-fetched from HyP3
    later without resubmitting."""
    project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if not project:
        raise HTTPException(404, "Project not found")

    freed_bytes = 0
    storage_path = Path(project.storage_path) if project.storage_path else None
    if storage_path and storage_path.exists():
        for entry in storage_path.iterdir():
            if entry.name == "slc":
                continue
            freed_bytes += entry.stat().st_size if entry.is_file() else storage_service.dir_size_bytes(entry)
            shutil.rmtree(entry) if entry.is_dir() else entry.unlink()

    jobs = (
        db.query(models.Job)
        .join(models.Batch, models.Batch.id == models.Job.batch_id)
        .filter(models.Batch.project_id == project_id, models.Job.downloaded == 1)
        .all()
    )
    for j in jobs:
        j.downloaded = 0
        j.download_path = None
    db.commit()

    return {"deleted_jobs": len(jobs), "freed_gb": round(freed_bytes / 1e9, 2)}
