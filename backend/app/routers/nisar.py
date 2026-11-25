"""NISAR GUNW granules for a project: find them, download them, and see what is on disk.

A NISAR project does not use HyP3. NASA already produced the interferograms, so the work is to find
the granules that cover the area, choose one stack of them (one track, frame, polarization and
bandwidth) and download it. MintPy then reads the files directly, from the same MintPy card.
"""
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Project
from app.services import archive_service, download_state, nisar_download_queue
from app.services.hyp3_service import get_earthdata_credentials
from insar_core.adapters.nisar import NisarAdapter, granules_on_disk
from insar_core.models.scene import AOI, SearchParams

router = APIRouter(tags=["nisar"])
_adapter = NisarAdapter()


class NisarDownloadRequest(BaseModel):
    file_names: list[str] = Field(..., min_length=1, description="GUNW file names from the search to download")


def _nisar_project(project_id: str, db: Session) -> Project:
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(404, "Project not found")
    if project.engine != "nisar_gunw":
        raise HTTPException(422, "This project does not use NISAR products. Create a project with the NISAR GUNW source.")
    return project


def _destination(project: Project) -> Path:
    return Path(project.storage_path) if project.storage_path else Path(settings.downloads_dir)


def _search_params(project: Project) -> SearchParams:
    if not (project.date_start and project.date_end):
        raise HTTPException(422, "A NISAR project needs a start and an end date.")
    return SearchParams(
        aoi=AOI(geometry=project.geometry),
        date_start=date.fromisoformat(project.date_start), date_end=date.fromisoformat(project.date_end),
        track_number=project.track_number, flight_direction=project.flight_direction,
    )


def _complete(path: Path, size) -> bool:
    return path.is_file() and (size is None or path.stat().st_size == size)


@router.get("/api/projects/{project_id}/nisar/stacks")
def nisar_stacks(project_id: str, db: Session = Depends(get_db)):
    """The NISAR GUNW granules over the project's area, grouped into the stacks that can be one time series."""
    project = _nisar_project(project_id, db)
    archive_service.reject_if_archived(project)
    destination = _destination(project)
    try:
        stacks = _adapter.stacks(_search_params(project))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"The ASF archive could not be searched: {exc}") from exc
    return [
        {
            "track_number": s.track_number, "frame": s.frame, "flight_direction": s.flight_direction,
            "polarization": s.polarization, "range_bandwidth": s.range_bandwidth, "frame_coverage": s.frame_coverage,
            "dates": [d.isoformat() for d in s.dates], "connected": s.connected,
            "granules": [
                {
                    "file_name": g.file_name, "reference_date": g.reference_date.isoformat(),
                    "secondary_date": g.secondary_date.isoformat(), "temporal_days": g.temporal_days,
                    "size_bytes": g.size_bytes, "processing_date": g.processing_date[:10],
                    "downloaded": _complete(destination / g.file_name, g.size_bytes),
                }
                for g in s.granules
            ],
        }
        for s in stacks
    ]


@router.post("/api/projects/{project_id}/nisar/downloads/queue", status_code=202)
def queue_nisar_downloads(project_id: str, body: NisarDownloadRequest, db: Session = Depends(get_db)):
    """Download the named granules. The search is repeated here, so the URLs come from ASF and not from the client."""
    project = _nisar_project(project_id, db)
    archive_service.reject_if_archived(project)
    username, password = get_earthdata_credentials(db)
    try:
        available = {g.file_name: g for stack in _adapter.stacks(_search_params(project)) for g in stack.granules}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"The ASF archive could not be searched: {exc}") from exc
    unknown = [n for n in body.file_names if n not in available]
    if unknown:
        raise HTTPException(422, f"Not NISAR GUNW granules of this project: {', '.join(unknown[:3])}")
    destination = _destination(project)
    destination.mkdir(parents=True, exist_ok=True)
    items = [
        {"file_name": n, "download_url": available[n].download_url}
        for n in body.file_names if not _complete(destination / n, available[n].size_bytes)
    ]
    if items:
        nisar_download_queue.start(username, password, items, destination)
    return {"queued": len(items), "already_downloaded": len(body.file_names) - len(items), "destination": str(destination)}


@router.get("/api/nisar/downloads/queue")
def nisar_queue_state():
    state = nisar_download_queue.get_state()
    name = state.get("current_filename")
    return {**state, "progress": download_state.get(name) if name else None}


@router.delete("/api/nisar/downloads/queue")
def cancel_nisar_downloads():
    nisar_download_queue.cancel()
    return {"cancelled": True}


@router.get("/api/projects/{project_id}/nisar/granules")
def nisar_granules_on_disk(project_id: str, db: Session = Depends(get_db)):
    """The GUNW files already in the project folder."""
    project = _nisar_project(project_id, db)
    return granules_on_disk(_destination(project))
