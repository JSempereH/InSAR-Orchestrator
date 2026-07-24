"""
EGMS (Copernicus European Ground Motion Service) product search + download.

Unlike /api/scenes, this doesn't feed into HyP3 processing - EGMS already
serves finished ground-motion products (velocity / displacement time series)
per AOI, so this is a standalone search-and-download flow.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import EGMSDownloadRequest, EGMSProductOut, EGMSSearchRequest
from app.services import egms_download_queue, egms_service

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
    egms_download_queue.start(db, products, destination)
    return egms_download_queue.get_state()


@router.get("/downloads/queue")
def get_download_queue():
    return egms_download_queue.get_state()


@router.delete("/downloads/queue")
def cancel_download_queue():
    egms_download_queue.cancel()
    return {"cancelled": True}
