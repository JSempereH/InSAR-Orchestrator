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
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy.orm import Session
from fastapi import Depends

from app.config import settings
from app.database import get_db
from app.models import Project
from app.schemas import MintPyRunIn, TimeSeriesOut
from app.services import archive_service, mintpy_service, provenance_service
from app.services.json_response import json_response

router = APIRouter(prefix="/api/projects", tags=["mintpy"])


def _project_or_404(db: Session, project_id: str) -> Project:
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(404, "Project not found")
    return project


def _downloads_dir(project: Project) -> Path:
    return Path(project.storage_path) if project.storage_path else Path(settings.downloads_dir)


@router.get("/{project_id}/mintpy/options")
def mintpy_options(project_id: str, db: Session = Depends(get_db)):
    """The MintPy options a person can change, with the value a run uses when left alone."""
    project = _project_or_404(db, project_id)
    return mintpy_service.describe_options(_downloads_dir(project), project.engine or "hyp3_gamma")


@router.post("/{project_id}/mintpy/run")
def run_mintpy(project_id: str, body: Optional[MintPyRunIn] = None, db: Session = Depends(get_db)):
    project = _project_or_404(db, project_id)
    archive_service.reject_if_archived(project)
    downloads_dir = _downloads_dir(project)
    if not downloads_dir.exists():
        raise HTTPException(422, f"Storage destination does not exist: {downloads_dir}")
    try:
        return mintpy_service.start_run(
            project_id, downloads_dir, geometry=project.geometry,
            context=provenance_service.project_context(db, project),
            options=body.options if body else None,
            reference=body.reference.model_dump() if body and body.reference else None,
            engine_id=project.engine or "hyp3_gamma",
        )
    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/{project_id}/mintpy/status")
def mintpy_status(project_id: str, db: Session = Depends(get_db)):
    project = _project_or_404(db, project_id)
    return mintpy_service.get_status(project_id, _downloads_dir(project))


@router.delete("/{project_id}/mintpy")
def cancel_mintpy(project_id: str, db: Session = Depends(get_db)):
    _project_or_404(db, project_id)
    try:
        return mintpy_service.cancel_run(project_id)
    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/{project_id}/mintpy/timeseries", response_model=TimeSeriesOut)
def mintpy_time_series(project_id: str, lat: float, lon: float, db: Session = Depends(get_db)):
    project = _project_or_404(db, project_id)
    archive_service.reject_if_archived(project)
    try:
        return mintpy_service.get_time_series(_downloads_dir(project), lat, lon)
    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/{project_id}/mintpy/quality")
def mintpy_quality(project_id: str, db: Session = Depends(get_db)):
    """Quality checks on the finished MintPy run: network, coherence, residuals, reference pixel."""
    project = _project_or_404(db, project_id)
    archive_service.reject_if_archived(project)
    try:
        return mintpy_service.get_quality(_downloads_dir(project))
    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc


_MEDIA_TYPES = {
    "geotiff": "image/tiff", "netcdf": "application/x-netcdf", "csv": "text/csv",
    "gpkg": "application/geopackage+sqlite3",
}


@router.get("/{project_id}/mintpy/report")
def mintpy_report(project_id: str, download: bool = False, db: Session = Depends(get_db)):
    """The report of the results as one HTML page: data, processing, quality, validation, limits and what to cite.

    It opens in the browser and prints to PDF. `download` sends it as a file to save instead."""
    project = _project_or_404(db, project_id)
    archive_service.reject_if_archived(project)
    try:
        page = mintpy_service.build_report(_downloads_dir(project), provenance_service.get_provenance(db, project))
    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc
    headers = {"Content-Disposition": f'attachment; filename="insar_report_{project.id[:8]}.html"'} if download else None
    return HTMLResponse(page, headers=headers)


@router.get("/{project_id}/mintpy/export")
def mintpy_export(
    project_id: str, format: str = "geotiff", layer: str = "velocity",
    masked: bool = True, series: bool = True, db: Session = Depends(get_db),
):
    """Download the MintPy results as GeoTIFF (one layer), NetCDF, CSV or GeoPackage.

    `layer` only applies to GeoTIFF. `masked` blanks the pixels MintPy's temporal coherence
    threshold rejects. `series` adds one column per date to CSV and GeoPackage."""
    project = _project_or_404(db, project_id)
    archive_service.reject_if_archived(project)
    try:
        path = mintpy_service.export_results(_downloads_dir(project), format, layer, masked, series)
    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc
    return FileResponse(path, media_type=_MEDIA_TYPES[format], filename=path.name)


@router.get("/{project_id}/mintpy/points")
def mintpy_points(project_id: str, db: Session = Depends(get_db)):
    """Decimated velocity points as GeoJSON, for the map a user clicks to
    request a time series from the endpoint above - mirrors GET
    /api/egms/downloads/{id}/points, but for MintPy's own SBAS grid."""
    project = _project_or_404(db, project_id)
    archive_service.reject_if_archived(project)
    try:
        return json_response(mintpy_service.get_points(_downloads_dir(project)))
    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc
