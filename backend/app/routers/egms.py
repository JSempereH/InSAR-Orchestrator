"""
EGMS (Copernicus European Ground Motion Service) product search + download.

Unlike /api/scenes, this doesn't feed into HyP3 processing - EGMS already
serves finished ground-motion products (velocity / displacement time series)
per AOI, so this is a standalone search-and-download flow.
"""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import EGMSDownload
from app.schemas import EGMSDownloadOut, EGMSDownloadRequest, EGMSProductOut, EGMSSearchRequest
from app.services import egms_download_queue, egms_points, egms_service

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
def delete_download_record(download_id: str, db: Session = Depends(get_db)):
    """Remove the inventory record only - does not delete files on disk."""
    row = db.query(EGMSDownload).filter_by(id=download_id).first()
    if not row:
        raise HTTPException(404, "Download not found")
    db.delete(row)
    db.commit()
    return {"deleted": True}


@router.get("/downloads/{download_id}/points")
def get_download_points(download_id: str, db: Session = Depends(get_db)):
    """Parse the downloaded L3 files into GeoJSON points (velocity per point)."""
    row = db.query(EGMSDownload).filter_by(id=download_id).first()
    if not row:
        raise HTTPException(404, "Download not found")
    if row.level != "L3":
        raise HTTPException(400, "Point visualization is only available for L3 downloads")
    return egms_points.extract_points(Path(row.destination_path), row.filenames)
