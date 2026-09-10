"""
Disk discovery and generic path-usage/move-status endpoints. Move actions
that touch a specific resource (Project, EGMSDownload, SLCDownload) live in
their own routers, since only they know which DB rows to update afterward.
"""
from pathlib import Path

from fastapi import APIRouter, HTTPException

from app.schemas import MoveStateOut, PathUsageOut, StorageTargetOut
from app.services import storage_move
from app.services.storage_service import list_storage_targets, path_usage

router = APIRouter(prefix="/api/storage", tags=["storage"])


@router.get("/targets", response_model=list[StorageTargetOut])
def get_storage_targets():
    return list_storage_targets()


@router.get("/usage", response_model=PathUsageOut)
def get_path_usage(path: str):
    """Size on disk for `path`, plus free/total space on its filesystem."""
    usage = path_usage(Path(path))
    if usage is None:
        raise HTTPException(404, "Path not found")
    return usage


@router.get("/move", response_model=MoveStateOut)
def get_move_state():
    """State of the most recently started (or currently running) move."""
    return storage_move.get_current_state()
