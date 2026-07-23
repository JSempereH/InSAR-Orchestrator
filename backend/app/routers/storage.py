"""
Read-only endpoint listing disks/mount points available on the backend
host, so the frontend can offer a project a specific storage destination.
"""

from fastapi import APIRouter

from app.schemas import StorageTargetOut
from app.services.storage_service import list_storage_targets

router = APIRouter(prefix="/api/storage", tags=["storage"])


@router.get("/targets", response_model=list[StorageTargetOut])
def get_storage_targets():
    return list_storage_targets()
