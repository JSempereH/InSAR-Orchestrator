"""Runs MintPy's SBAS time-series inversion over a project's downloaded HyP3
interferograms, finishing the InSAR pipeline in the UI instead of requiring a
manual `smallbaselineApp.py` invocation after downloading.

Wraps insar_core.pipeline.mintpy_adapter.MintPyAdapter via
app.services.mintpy_service. Requires MintPy installed on the backend host
(`pip install mintpy`, `smallbaselineApp.py` on PATH) - this endpoint runs a
real, potentially long, subprocess on the backend machine.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from sqlalchemy.orm import Session
from fastapi import Depends

from app.config import settings
from app.database import get_db
from app.models import Project
from app.services import mintpy_service

router = APIRouter(prefix="/api/projects", tags=["mintpy"])


def _project_or_404(db: Session, project_id: str) -> Project:
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(404, "Project not found")
    return project


def _downloads_dir(project: Project) -> Path:
    return Path(project.storage_path) if project.storage_path else Path(settings.downloads_dir)


@router.post("/{project_id}/mintpy/run")
def run_mintpy(project_id: str, db: Session = Depends(get_db)):
    project = _project_or_404(db, project_id)
    downloads_dir = _downloads_dir(project)
    if not downloads_dir.exists():
        raise HTTPException(422, f"Storage destination does not exist: {downloads_dir}")
    try:
        return mintpy_service.start_run(project_id, downloads_dir)
    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/{project_id}/mintpy/status")
def mintpy_status(project_id: str, db: Session = Depends(get_db)):
    _project_or_404(db, project_id)
    return mintpy_service.get_status(project_id)


@router.delete("/{project_id}/mintpy")
def cancel_mintpy(project_id: str, db: Session = Depends(get_db)):
    _project_or_404(db, project_id)
    try:
        return mintpy_service.cancel_run(project_id)
    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc
