"""Raw Sentinel-1 SLC discovery + download for a project.

Separate from jobs.py/batches.py: this doesn't touch HyP3 at all. It exists
for external PS-InSAR pipelines (coregistration, persistent-scatterer
selection, etc.) that need the original SLC products rather than HyP3's
already-formed interferograms.
"""
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Project, SLCDownload
from app.schemas import SceneOut, SLCDownloadOut, SLCDownloadRequest
from app.services import slc_download_queue
from app.services.hyp3_service import get_earthdata_credentials
from insar_core.adapters.asf import ASFAdapter
from insar_core.models.scene import AOI, SearchParams

router = APIRouter(tags=["slc"])
_adapter = ASFAdapter()


def _project_search_params(project: Project) -> SearchParams:
    return SearchParams(
        aoi=AOI(geometry=project.geometry),
        date_start=date.fromisoformat(project.date_start),
        date_end=date.fromisoformat(project.date_end),
        track_number=project.track_number,
        flight_direction=project.flight_direction,
    )


def _get_project(project_id: str, db: Session) -> Project:
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(404, "Project not found")
    return project


@router.get("/api/projects/{project_id}/slc/scenes", response_model=list[SceneOut])
def list_slc_scenes(project_id: str, db: Session = Depends(get_db)):
    """Same scenes used to build SBAS pairs for this project, with download
    metadata (URL, filename) and whether each has already been downloaded."""
    project = _get_project(project_id, db)
    scenes = _adapter.search(_project_search_params(project))

    already: set[str] = set()
    for record in project.slc_downloads:
        already.update(record.filenames)

    return [
        SceneOut(
            file_id=s.file_id,
            granule_name=s.granule_name,
            acquisition_date=s.acquisition_date.isoformat(),
            orbit=s.orbit,
            track_number=s.track_number,
            flight_direction=s.flight_direction,
            polarization=s.polarization,
            size_mb=s.size_mb,
            download_url=s.download_url,
            file_name=s.file_name,
            already_downloaded=bool(s.file_name and s.file_name in already),
        )
        for s in scenes
    ]


@router.post("/api/projects/{project_id}/slc/downloads/queue", response_model=SLCDownloadOut, status_code=201)
def queue_slc_downloads(project_id: str, body: SLCDownloadRequest, db: Session = Depends(get_db)):
    project = _get_project(project_id, db)
    if not body.scenes:
        raise HTTPException(400, "No scenes selected")

    username, password = get_earthdata_credentials(db)

    destination = Path(project.storage_path or ".") / "slc"
    destination.mkdir(parents=True, exist_ok=True)

    items = [
        {"granule_name": s.granule_name, "file_name": s.file_name, "download_url": s.download_url}
        for s in body.scenes
    ]
    missing = [i["granule_name"] for i in items if not i["file_name"] or not i["download_url"]]
    if missing:
        raise HTTPException(400, f"Missing download URL for scene(s): {', '.join(missing[:3])}")

    record = SLCDownload(
        project_id=project_id,
        destination_path=str(destination),
        filenames=[i["file_name"] for i in items],
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    slc_download_queue.start(username, password, items, destination)
    return record


@router.get("/api/slc/downloads/queue")
def get_slc_download_queue():
    return slc_download_queue.get_state()


@router.delete("/api/slc/downloads/queue")
def cancel_slc_download_queue():
    slc_download_queue.cancel()
    return {"cancelled": True}


@router.get("/api/projects/{project_id}/slc/downloads", response_model=list[SLCDownloadOut])
def list_slc_downloads(project_id: str, db: Session = Depends(get_db)):
    _get_project(project_id, db)
    return (
        db.query(SLCDownload)
        .filter_by(project_id=project_id)
        .order_by(SLCDownload.created_at.desc())
        .all()
    )


@router.delete("/api/slc/downloads/{download_id}")
def delete_slc_download_record(download_id: str, delete_files: bool = False, db: Session = Depends(get_db)):
    """Remove the inventory record. Pass delete_files=true to also delete the
    actual SLC zips this record downloaded (other records sharing the same
    destination folder are untouched - only these filenames are removed)."""
    row = db.query(SLCDownload).filter_by(id=download_id).first()
    if not row:
        raise HTTPException(404, "Download not found")

    freed_bytes = 0
    if delete_files:
        destination = Path(row.destination_path)
        for filename in row.filenames:
            fpath = destination / filename
            if fpath.exists():
                freed_bytes += fpath.stat().st_size
                fpath.unlink()

    db.delete(row)
    db.commit()
    return {"deleted": True, "freed_gb": round(freed_bytes / 1e9, 2)}
