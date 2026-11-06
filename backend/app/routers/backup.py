"""Backup configuration and control. The work itself is in services/backup_service.py."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import (
    BackupConfigIn, BackupConfigOut, BackupOverviewOut, BackupStateOut, BackupTestRequest, RcloneInfoOut,
)
from app.services import backup_service, rclone_service

router = APIRouter(prefix="/api/backup", tags=["backup"])


def _rclone_info() -> RcloneInfoOut:
    if not rclone_service.is_available():
        return RcloneInfoOut(available=False)
    try:
        remotes = rclone_service.list_remotes()
    except rclone_service.RcloneError:
        remotes = []
    return RcloneInfoOut(available=True, version=rclone_service.version(), remotes=remotes)


@router.get("", response_model=BackupOverviewOut)
def get_overview(db: Session = Depends(get_db)):
    return BackupOverviewOut(
        rclone=_rclone_info(),
        config=BackupConfigOut.model_validate(backup_service.get_config(db)),
        state=BackupStateOut(**backup_service.get_state()),
    )


@router.put("/config", response_model=BackupConfigOut)
def update_config(body: BackupConfigIn, db: Session = Depends(get_db)):
    destination = (body.destination or "").strip() or None
    if destination:
        try:
            destination = rclone_service.validate_destination(destination)
        except rclone_service.RcloneError as exc:
            raise HTTPException(422, str(exc)) from exc
    if body.enabled and not destination:
        raise HTTPException(422, "Set a destination before enabling scheduled backups.")

    config = backup_service.get_config(db)
    config.destination = destination
    config.enabled = body.enabled
    config.interval_hours = body.interval_hours
    config.keep_daily = body.keep_daily
    config.keep_weekly = body.keep_weekly
    config.include_slc = body.include_slc
    db.commit()
    db.refresh(config)
    return config


@router.post("/test")
def test_destination(body: BackupTestRequest):
    """Write, list and delete a small marker file to prove the destination works."""
    try:
        destination = rclone_service.test_destination(body.destination)
    except rclone_service.RcloneError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"ok": True, "destination": destination}


@router.post("/run", response_model=BackupStateOut, status_code=202)
def run_now(db: Session = Depends(get_db)):
    config = backup_service.get_config(db)
    if not config.destination:
        raise HTTPException(422, "Set a backup destination first.")
    if not rclone_service.is_available():
        raise HTTPException(422, "rclone is not installed on the backend machine.")
    try:
        backup_service.start_in_background(
            config.destination, config.keep_daily, config.keep_weekly, bool(config.include_slc),
        )
    except backup_service.BackupBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    return BackupStateOut(**{**backup_service.get_state(), "running": True, "status": "running"})
