"""
CRUD endpoints for Project (area of interest).
"""

import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app import models, schemas

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
        )
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
