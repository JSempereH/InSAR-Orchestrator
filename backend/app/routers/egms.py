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
from app.schemas import EGMSDownloadOut, EGMSDownloadRequest, EGMSProductOut, EGMSSearchRequest, MoveRequest, MoveStateOut
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
    """Parse the downloaded L3 files into GeoJSON points (velocity per point)."""
    row = db.query(EGMSDownload).filter_by(id=download_id).first()
    if not row:
        raise HTTPException(404, "Download not found")
    if row.level != "L3":
        raise HTTPException(400, "Point visualization is only available for L3 downloads")
    return egms_points.extract_points(Path(row.destination_path), row.filenames)
