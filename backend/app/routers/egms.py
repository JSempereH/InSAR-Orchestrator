"""
EGMS (Copernicus European Ground Motion Service) product search + download.

Unlike /api/scenes, this doesn't feed into HyP3 processing - EGMS already
serves finished ground-motion products (velocity / displacement time series)
per AOI, so this is a standalone search-and-download flow.
"""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import EGMSDownload
from app.schemas import (
    EGMSDownloadOut, EGMSDownloadRequest, EGMSProductOut, EGMSSearchRequest,
    MoveRequest, MoveStateOut, TimeSeriesOut,
)
from app.services import egms_download_queue, egms_points, egms_service, storage_move

router = APIRouter(prefix="/api/egms", tags=["egms"])


@router.get("/options/{kind}")
def list_options(kind: str, db: Session = Depends(get_db)):
    """kind: levels | releases | swaths | relative_orbits | bursts | directions | tile_ids | product_types"""
    return egms_service.list_options(db, kind)


@router.post("/search", response_model=list[EGMSProductOut])
def search_products(body: EGMSSearchRequest, db: Session = Depends(get_db)):
    products = egms_service.search_products(
        db,
        geometry=body.geometry,
        level=body.level,
        release=body.release,
        direction=body.direction,
        product_type=body.product_type,
        tile_id=body.tile_id,
    )
    return [
        EGMSProductOut(query_id=p.query_id, filename=p.filename, level=p.level, size_mb=p.size_mb)
        for p in products
    ]


@router.post("/downloads/queue")
def start_download(body: EGMSDownloadRequest, db: Session = Depends(get_db)):
    destination = egms_download_queue.resolve_destination(body.storage_mountpoint, body.destination_name)
    products = [p.model_dump() for p in body.products]

    record = EGMSDownload(
        name=body.destination_name,
        geometry=body.geometry,
        level=body.level,
        release=body.release,
        direction=body.direction,
        product_type=body.product_type,
        tile_id=body.tile_id,
        destination_path=str(destination),
        filenames=[p["filename"] for p in products],
    )
    db.add(record)
    db.commit()

    egms_download_queue.start(db, products, destination)
    return egms_download_queue.get_state()


@router.get("/downloads/queue")
def get_download_queue():
    return egms_download_queue.get_state()


@router.delete("/downloads/queue")
def cancel_download_queue():
    egms_download_queue.cancel()
    return {"cancelled": True}


# ── Downloads inventory ──────────────────────────────────────────────────────

@router.get("/downloads", response_model=list[EGMSDownloadOut])
def list_downloads(db: Session = Depends(get_db)):
    return db.query(EGMSDownload).order_by(EGMSDownload.created_at.desc()).all()


@router.delete("/downloads/{download_id}")
def delete_download_record(download_id: str, delete_files: bool = False, db: Session = Depends(get_db)):
    """Remove the inventory record. Pass delete_files=true to also delete the
    downloaded product files themselves (the whole destination folder)."""
    row = db.query(EGMSDownload).filter_by(id=download_id).first()
    if not row:
        raise HTTPException(404, "Download not found")

    freed_bytes = 0
    if delete_files:
        import shutil
        from app.services import storage_service

        destination = Path(row.destination_path)
        if destination.exists():
            freed_bytes = storage_service.dir_size_bytes(destination)
            shutil.rmtree(destination)

    db.delete(row)
    db.commit()
    return {"deleted": True, "freed_gb": round(freed_bytes / 1e9, 2)}


@router.post("/downloads/{download_id}/move", response_model=MoveStateOut)
def move_download(download_id: str, body: MoveRequest, db: Session = Depends(get_db)):
    row = db.query(EGMSDownload).filter_by(id=download_id).first()
    if not row:
        raise HTTPException(404, "Download not found")

    src = Path(row.destination_path)
    if not src.exists():
        raise HTTPException(400, "Nothing to move: destination path doesn't exist")

    base = Path(body.mountpoint) / "egms" if body.mountpoint else Path(settings.downloads_dir) / "egms"
    dst = base / src.name

    if dst.resolve() == src.resolve():
        raise HTTPException(400, "Source and destination are the same")
    if dst.exists():
        raise HTTPException(400, f"Destination '{dst}' already exists")

    def on_complete(new_path: Path) -> None:
        from app.database import SessionLocal

        db2 = SessionLocal()
        try:
            rec = db2.query(EGMSDownload).filter_by(id=download_id).first()
            if rec:
                rec.destination_path = str(new_path)
                db2.commit()
        finally:
            db2.close()

    try:
        storage_move.start(f"egms:{download_id}", src, dst, on_complete)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc

    return storage_move.get_current_state()


@router.get("/downloads/{download_id}/points")
def get_download_points(download_id: str, db: Session = Depends(get_db)):
    """Parse the downloaded files into GeoJSON points (velocity per point).

    Works for L3 (velocity-only) as well as L2A/L2B, since their CSV rows
    carry latitude/longitude/mean_velocity too - the point map is how a user
    finds a point to then request its time series from the endpoint below."""
    row = db.query(EGMSDownload).filter_by(id=download_id).first()
    if not row:
        raise HTTPException(404, "Download not found")
    if row.level not in ("L2A", "L2B", "L3"):
        raise HTTPException(400, f"Unsupported level for point visualization: {row.level}")
    return egms_points.extract_points(Path(row.destination_path), row.filenames, geometry=row.geometry)


@router.get("/downloads/{download_id}/points/{point_id}/timeseries", response_model=TimeSeriesOut)
def get_point_time_series(download_id: str, point_id: str, db: Session = Depends(get_db)):
    """Displacement time series for one point, as plotted from GET .../points.
    Only L2A/L2B carry a time series; L3 is a velocity mosaic with no per-date
    columns to extract."""
    row = db.query(EGMSDownload).filter_by(id=download_id).first()
    if not row:
        raise HTTPException(404, "Download not found")
    if row.level not in ("L2A", "L2B"):
        raise HTTPException(400, "Time series is only available for L2A/L2B downloads")

    series = egms_points.extract_time_series(Path(row.destination_path), row.filenames, point_id, geometry=row.geometry)
    if series is None:
        raise HTTPException(404, "Point not found in downloaded files")
    return series
