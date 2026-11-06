"""Archive a project to an rclone destination and restore it. Logic in services/archive_service.py."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Project
from app.schemas import ArchiveRequest, ArchiveStateOut
from app.services import archive_service, backup_service, rclone_service

router = APIRouter(tags=["archive"])


def _project(db: Session, project_id: str) -> Project:
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(404, "Project not found")
    return project


@router.get("/api/archive/state", response_model=ArchiveStateOut)
def archive_state():
    """Progress of the most recent archive or restore (idle if none ran yet)."""
    return archive_service.get_state()


@router.post("/api/projects/{project_id}/archive", response_model=ArchiveStateOut, status_code=202)
def archive_project(project_id: str, body: ArchiveRequest, db: Session = Depends(get_db)):
    """Copy the project's files to the destination, verify them, then delete the
    local copy. Runs in the background; poll GET /api/archive/state."""
    project = _project(db, project_id)
    base = (body.destination or "").strip() or backup_service.get_config(db).destination
    if not base:
        raise HTTPException(422, "Give a destination, or set one in Settings, Backup.")
    if not rclone_service.is_available():
        raise HTTPException(422, "rclone is not installed on the backend machine.")
    try:
        archive_service.start_archive(db, project, base)
    except archive_service.ArchiveError as exc:
        raise HTTPException(422, str(exc)) from exc
    except backup_service.BackupBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    return archive_service.get_state()


@router.post("/api/projects/{project_id}/restore", response_model=ArchiveStateOut, status_code=202)
def restore_project(project_id: str, db: Session = Depends(get_db)):
    """Copy an archived project's files back and verify them. The archive copy is kept."""
    project = _project(db, project_id)
    try:
        archive_service.start_restore(project)
    except archive_service.ArchiveError as exc:
        raise HTTPException(422, str(exc)) from exc
    except backup_service.BackupBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    return archive_service.get_state()
