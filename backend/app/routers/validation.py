"""Check a project's MintPy result against an independent source (EGMS, and GNSS).

A time series always comes out of MintPy; this answers whether the numbers are right, by
comparing them with a source that did not use HyP3 or MintPy. See
insar_core.analysis.validation for how the comparison is made.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import EGMSDownload, Project
from app.services import archive_service, validation_service

router = APIRouter(prefix="/api/projects", tags=["validation"])


class EGMSValidationIn(BaseModel):
    download_id: str


def _project_or_404(db: Session, project_id: str) -> Project:
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(404, "Project not found")
    return project


def _downloads_dir(project: Project) -> Path:
    return Path(project.storage_path) if project.storage_path else Path(settings.downloads_dir)


@router.get("/{project_id}/validation")
def validation_status(project_id: str, db: Session = Depends(get_db)):
    """Where each validation stands: not run, running, failed, or finished with its numbers."""
    project = _project_or_404(db, project_id)
    archive_service.reject_if_archived(project)
    return validation_service.get_status(project_id, _downloads_dir(project))


@router.post("/{project_id}/validation/egms")
def validate_against_egms(project_id: str, body: EGMSValidationIn, db: Session = Depends(get_db)):
    """Compare the MintPy result with an EGMS L2a or L2b download, in the background."""
    project = _project_or_404(db, project_id)
    archive_service.reject_if_archived(project)
    row = db.query(EGMSDownload).filter_by(id=body.download_id).first()
    if row is None:
        raise HTTPException(404, "EGMS download not found")
    download = {
        "id": row.id, "level": row.level, "release": row.release, "direction": row.direction,
        "destination_path": row.destination_path, "filenames": list(row.filenames or []),
    }
    try:
        return validation_service.start_egms(project_id, _downloads_dir(project), download)
    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/{project_id}/validation/gnss")
def validate_against_gnss(project_id: str, db: Session = Depends(get_db)):
    """Compare the MintPy result with GNSS stations of the Nevada Geodetic Laboratory in the area.

    Needs internet access. Station series are downloaded from geodesy.unr.edu."""
    project = _project_or_404(db, project_id)
    archive_service.reject_if_archived(project)
    try:
        return validation_service.start_gnss(project_id, _downloads_dir(project))
    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc
