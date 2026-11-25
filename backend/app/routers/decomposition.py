"""Vertical and east-west motion from an ascending and a descending project of the same area.

One track measures motion along its line of sight, which mixes vertical and horizontal movement. Two tracks looking
from opposite sides can be separated. See insar_core.analysis.decomposition for how, and for what it assumes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Decomposition, EGMSDownload, Project
from app.services import archive_service, decomposition_service

router = APIRouter(prefix="/api/decompositions", tags=["decomposition"])


class ReferencePointIn(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)


class DecompositionIn(BaseModel):
    first_project_id: str
    second_project_id: str
    name: Optional[str] = None
    reference: Optional[ReferencePointIn] = Field(
        None, description="Where both maps are made to read zero. Defaults to the ascending project's MintPy reference point, or the descending one's when that has no valid pixels in both.")


class L3ValidationIn(BaseModel):
    up_download_id: str = Field(..., description="EGMS L3 ORTHO-UP download")
    east_download_id: str = Field(..., description="EGMS L3 ORTHO-EAST download")


def _project_dir(project: Project) -> Path:
    return Path(project.storage_path) if project.storage_path else Path(settings.downloads_dir)


def _project_or_404(db: Session, project_id: str) -> Project:
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(404, f"Project {project_id} not found")
    archive_service.reject_if_archived(project)
    return project


def _row_or_404(db: Session, decomposition_id: str) -> Decomposition:
    row = db.query(Decomposition).filter_by(id=decomposition_id).first()
    if not row:
        raise HTTPException(404, "Decomposition not found")
    return row


def _project_ref(db: Session, project_id: str) -> dict:
    project = db.query(Project).filter_by(id=project_id).first()
    return {"id": project_id, "name": project.name if project else None, "exists": project is not None}


def _brief(db: Session, row: Decomposition) -> dict:
    summary = row.summary or {}
    return {
        "id": row.id, "name": row.name, "created_at": row.created_at.isoformat() if row.created_at else None,
        "ascending": _project_ref(db, row.ascending_project_id), "descending": _project_ref(db, row.descending_project_id),
        "pixels": summary.get("pixels"), "reference": row.reference or summary.get("reference"),
    }


def _detail(db: Session, row: Decomposition) -> dict:
    folder = decomposition_service.folder_for(row.id)
    asc = db.query(Project).filter_by(id=row.ascending_project_id).first()
    desc = db.query(Project).filter_by(id=row.descending_project_id).first()
    summary = decomposition_service.read_summary(folder) if folder.exists() else (row.summary or {})
    return {
        **_brief(db, row), "summary": summary,
        "stale": decomposition_service.stale(
            summary, _project_dir(asc) if asc else None, _project_dir(desc) if desc else None) if folder.exists() else False,
        "checks": decomposition_service.status(row.id, folder), "products_available": folder.exists(),
    }


@router.get("")
def list_decompositions(db: Session = Depends(get_db)):
    rows = db.query(Decomposition).order_by(Decomposition.created_at.desc()).all()
    return [_brief(db, r) for r in rows]


@router.post("", status_code=201)
def create_decomposition(body: DecompositionIn, db: Session = Depends(get_db)):
    """Separate the MintPy results of two projects into vertical and east-west velocity.

    The two projects must cover the same area from opposite directions and both have finished MintPy."""
    if body.first_project_id == body.second_project_id:
        raise HTTPException(422, "Pick two different projects: one ascending and one descending")
    first, second = _project_or_404(db, body.first_project_id), _project_or_404(db, body.second_project_id)
    row = Decomposition(name=body.name or "", ascending_project_id="", descending_project_id="", folder="", summary={})
    db.add(row)
    db.flush()
    folder = decomposition_service.folder_for(row.id)
    try:
        # Each project's id is its label, so the summary says which one turned out to be ascending.
        summary = decomposition_service.compute(
            folder, _project_dir(first), _project_dir(second), first.id, second.id,
            reference=body.reference.model_dump() if body.reference else None)
    except decomposition_service.DecompositionServiceError as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc
    except Exception:
        db.rollback()
        raise
    by_id = {first.id: first, second.id: second}
    asc, desc = by_id[summary["ascending"]["label"]], by_id[summary["descending"]["label"]]
    row.ascending_project_id, row.descending_project_id = asc.id, desc.id
    row.name = body.name or f"{asc.name} + {desc.name}"
    row.reference = summary["reference"] and {"lat": summary["reference"]["lat"], "lon": summary["reference"]["lon"]}
    row.folder, row.summary = str(folder), summary
    db.commit()
    return _detail(db, row)


@router.get("/{decomposition_id}")
def get_decomposition(decomposition_id: str, db: Session = Depends(get_db)):
    return _detail(db, _row_or_404(db, decomposition_id))


@router.delete("/{decomposition_id}", status_code=204)
def delete_decomposition(decomposition_id: str, db: Session = Depends(get_db)):
    row = _row_or_404(db, decomposition_id)
    decomposition_service.remove(decomposition_service.folder_for(row.id))
    decomposition_service.forget(row.id)
    db.delete(row)
    db.commit()


@router.get("/{decomposition_id}/preview.png")
def preview(decomposition_id: str, db: Session = Depends(get_db)):
    row = _row_or_404(db, decomposition_id)
    folder = decomposition_service.folder_for(row.id)
    if not folder.exists():
        raise HTTPException(404, "The products of this decomposition are missing")
    return FileResponse(decomposition_service.preview(folder), media_type="image/png")


@router.get("/{decomposition_id}/export")
def export(decomposition_id: str, layer: str = "vertical", db: Session = Depends(get_db)):
    """One layer as a cloud optimised GeoTIFF: vertical, east, vertical_std, east_std, or the calibrated ones."""
    row = _row_or_404(db, decomposition_id)
    try:
        path = decomposition_service.export(decomposition_service.folder_for(row.id), layer)
    except decomposition_service.DecompositionServiceError as exc:
        raise HTTPException(422, str(exc)) from exc
    return FileResponse(path, media_type="image/tiff", filename=f"decomposition_{layer}.tif")


@router.post("/{decomposition_id}/validation/l3")
def validate_l3(decomposition_id: str, body: L3ValidationIn, db: Session = Depends(get_db)):
    """Compare the vertical and east-west maps with EGMS L3, which publishes both components."""
    row = _row_or_404(db, decomposition_id)
    downloads = {}
    for key, download_id in (("up", body.up_download_id), ("east", body.east_download_id)):
        found = db.query(EGMSDownload).filter_by(id=download_id).first()
        if found is None:
            raise HTTPException(404, f"EGMS download {download_id} not found")
        downloads[key] = {"id": found.id, "level": found.level, "release": found.release, "product_type": found.product_type,
                          "destination_path": found.destination_path, "filenames": list(found.filenames or [])}
    try:
        decomposition_service.validate_l3(decomposition_service.folder_for(row.id), downloads["up"], downloads["east"])
    except decomposition_service.DecompositionServiceError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _detail(db, row)


@router.post("/{decomposition_id}/validation/gnss")
def validate_gnss(decomposition_id: str, db: Session = Depends(get_db)):
    """Check the maps against GNSS stations of the Nevada Geodetic Laboratory in the area, in the background.

    Needs internet access. Station series are downloaded from geodesy.unr.edu."""
    row = _row_or_404(db, decomposition_id)
    try:
        decomposition_service.start_gnss(row.id, decomposition_service.folder_for(row.id))
    except decomposition_service.DecompositionServiceError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _detail(db, row)
