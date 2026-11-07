"""
InSAR Orchestrator API

Run with: uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
"""

import asyncio
import logging
import time
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware

from sqlalchemy import text

from app.config import settings
from app.database import Base, engine, SessionLocal
from app import auth
from app.routers import archive, backup, batches, credentials, egms, jobs, mintpy, projects, scenes, slc, storage
from app.models import Batch
from app.services import backup_service, credits_service
from app.services.polling_service import poll_active_jobs, force_poll
from app.services import download_queue

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


def _migrate_schema() -> None:
    """Add columns introduced after the initial create_all, since there's no
    Alembic in this project and create_all only creates missing tables."""
    with engine.connect() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(projects)"))}
        if "storage_path" not in cols:
            conn.execute(text("ALTER TABLE projects ADD COLUMN storage_path VARCHAR"))
            conn.commit()
        if "backup_enabled" not in cols:
            conn.execute(text("ALTER TABLE projects ADD COLUMN backup_enabled BOOLEAN DEFAULT 1 NOT NULL"))
            conn.commit()
        if "archived_remote" not in cols:
            conn.execute(text("ALTER TABLE projects ADD COLUMN archived_remote VARCHAR"))
            conn.commit()
        if "archived_at" not in cols:
            conn.execute(text("ALTER TABLE projects ADD COLUMN archived_at DATETIME"))
            conn.commit()

        backup_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(backup_config)"))}
        if backup_cols and "include_slc" not in backup_cols:
            conn.execute(text("ALTER TABLE backup_config ADD COLUMN include_slc BOOLEAN DEFAULT 0 NOT NULL"))
            conn.commit()

        batch_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(batches)"))}
        if "status" not in batch_cols:
            # Existing batches already finished submitting by definition.
            conn.execute(text("ALTER TABLE batches ADD COLUMN status VARCHAR DEFAULT 'done' NOT NULL"))
            conn.commit()
        if "auto_download" not in batch_cols:
            conn.execute(text("ALTER TABLE batches ADD COLUMN auto_download BOOLEAN DEFAULT 0 NOT NULL"))
            conn.commit()
        if "pending_pairs" not in batch_cols:
            conn.execute(text("ALTER TABLE batches ADD COLUMN pending_pairs JSON"))
            conn.commit()
        if "parameters" not in batch_cols:
            conn.execute(text("ALTER TABLE batches ADD COLUMN parameters JSON"))
            conn.commit()
        if "run_mintpy_when_done" not in batch_cols:
            conn.execute(text("ALTER TABLE batches ADD COLUMN run_mintpy_when_done BOOLEAN DEFAULT 0 NOT NULL"))
            conn.commit()

        # Column(index=True) only takes effect via create_all for a table
        # that doesn't exist yet - these two were added to already-live
        # tables, so the index needs creating explicitly here too.
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_batches_project_id ON batches (project_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_jobs_batch_id ON jobs (batch_id)"))
        conn.commit()


Base.metadata.create_all(bind=engine)
_migrate_schema()

if not settings.app_password:
    logger.warning(
        "APP_PASSWORD is not set: anyone who can reach this port can use the app, including "
        "your stored HyP3 credentials. Fine on localhost or behind a VPN; set APP_PASSWORD "
        "(backend/.env) before exposing it any further."
    )

if not settings.secret_key:
    logger.warning(
        "SECRET_KEY is not set (backend/.env). A key is generated in memory for this "
        "process only, so any stored credentials (Earthdata, EGMS) "
        "will fail to decrypt after a restart. Run ../setup.sh or set SECRET_KEY "
        "yourself before relying on this outside local development."
    )

app = FastAPI(title="InSAR Orchestrator API", version="0.2.0")

# Added before CORS so CORS is the outermost layer: a 401 from the auth check
# still carries the CORS headers the browser needs to read it.
app.add_middleware(auth.AuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(projects.router)
app.include_router(batches.router)
app.include_router(scenes.router)
app.include_router(credentials.router)
app.include_router(jobs.router)
app.include_router(storage.router)
app.include_router(egms.router)
app.include_router(slc.router)
app.include_router(mintpy.router)
app.include_router(backup.router)
app.include_router(archive.router)
app.include_router(auth.router)


async def _recover_after_restart() -> None:
    """Resume what the previous run left half done, without delaying startup."""
    from app.services import recovery

    try:
        await asyncio.get_running_loop().run_in_executor(None, recovery.recover_on_startup)
    except Exception:
        logger.exception("Startup recovery failed")


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(poll_active_jobs())
    asyncio.create_task(credits_service.poll_credits())
    asyncio.create_task(backup_service.backup_scheduler())
    if settings.recover_on_startup:
        asyncio.create_task(_recover_after_restart())


@app.get("/api/health")
def health_check():
    return {"status": "ok", "version": "0.2.0"}


@app.post("/api/admin/poll")
async def trigger_poll():
    """Force an immediate HyP3 status sync for all active jobs."""
    result = await force_poll()
    return result


@app.get("/api/credits")
def get_credits():
    """Last known HyP3 credit balance, refreshed every 5 minutes in the
    background. `updated_at` and `refresh_interval_seconds` let the frontend
    show "updated Ns ago" / "next update in ~Ns" without polling HyP3 itself."""
    return credits_service.get_snapshot()


@app.post("/api/admin/poll-credits")
async def trigger_credits_poll():
    """Force an immediate HyP3 credit-balance refresh."""
    return await credits_service.force_poll()


# ── Download queue ────────────────────────────────────────────────────────────

class _QueueStartRequest(BaseModel):
    jobs: list[dict]  # [{job_id, hyp3_job_id}, ...]


@app.post("/api/downloads/queue")
def start_download_queue(body: _QueueStartRequest):
    download_queue.start(body.jobs)
    return download_queue.get_state()


@app.get("/api/downloads/queue")
def get_download_queue():
    return download_queue.get_state()


@app.delete("/api/downloads/queue")
def cancel_download_queue():
    download_queue.cancel()
    return {"cancelled": True}



# ── WebSocket: real-time job status for a batch ──────────────────────────────

class _ConnectionManager:
    def __init__(self):
        self._connections: dict[str, list[WebSocket]] = {}

    async def connect(self, batch_id: str, ws: WebSocket):
        await ws.accept()
        self._connections.setdefault(batch_id, []).append(ws)

    def disconnect(self, batch_id: str, ws: WebSocket):
        conns = self._connections.get(batch_id, [])
        if ws in conns:
            conns.remove(ws)


manager = _ConnectionManager()


@app.websocket("/ws/batches/{batch_id}")
async def batch_status_ws(websocket: WebSocket, batch_id: str):
    """Stream job status updates for a batch.

    On connect, sends the current snapshot. Then sends an update whenever
    the polling loop changes job statuses (poll interval ~60 s).
    """
    await manager.connect(batch_id, websocket)
    try:
        snapshot = _fetch_batch_snapshot(batch_id)
        if snapshot is None:
            await websocket.send_json({"error": "Batch not found"})
            return
        await websocket.send_json(snapshot)

        while True:
            await asyncio.sleep(60)
            snapshot = _fetch_batch_snapshot(batch_id)
            if snapshot is not None:
                await websocket.send_json(snapshot)

    except WebSocketDisconnect:
        manager.disconnect(batch_id, websocket)


# Every open socket queries on its own independent 60s timer, so N clients
# watching the same batch_id used to mean N identical DB queries roughly
# every 60s. Caching the query result for just under that interval means
# whichever socket asks first (per batch) does the real query, and every
# other socket watching the same batch within that window reuses it.
_SNAPSHOT_CACHE_TTL_S = 55.0
_snapshot_cache: dict[str, tuple[float, dict]] = {}


def _fetch_batch_snapshot(batch_id: str) -> Optional[dict]:
    cached = _snapshot_cache.get(batch_id)
    now = time.monotonic()
    if cached is not None and now - cached[0] < _SNAPSHOT_CACHE_TTL_S:
        return cached[1]

    db = SessionLocal()
    try:
        batch = db.query(Batch).filter_by(id=batch_id).first()
        if not batch:
            return None
        snapshot = _batch_snapshot_dict(batch)
    finally:
        db.close()

    _snapshot_cache[batch_id] = (now, snapshot)
    return snapshot


def _batch_snapshot_dict(batch: Batch) -> dict:
    jobs_data = [
        {
            "id": j.id,
            "hyp3_job_id": j.hyp3_job_id,
            "reference_date": j.reference_date,
            "secondary_date": j.secondary_date,
            "status": j.status.value if j.status else None,
            "downloaded": bool(j.downloaded),
        }
        for j in batch.jobs
    ]
    return {"batch_id": batch.id, "jobs": jobs_data}


# ── Built frontend ───────────────────────────────────────────────────────────
# Must stay the last route registered: the catch-all below would shadow any
# route declared after it.

def mount_frontend(target: FastAPI, directory: str) -> None:
    """Serve a built single-page app from `directory` on `target`."""
    from pathlib import Path

    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    if not directory:
        return
    root = Path(directory).resolve()
    if not (root / "index.html").is_file():
        logger.warning("FRONTEND_DIR=%s has no index.html; not serving the UI", root)
        return

    @target.get("/{full_path:path}", include_in_schema=False)
    def serve_frontend(full_path: str):
        if full_path in ("api", "ws") or full_path.startswith(("api/", "ws/")):
            raise HTTPException(404, "Not found")
        candidate = (root / full_path).resolve()
        if full_path and candidate.is_file() and root in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(root / "index.html")   # single-page app: unknown paths show the UI


mount_frontend(app, settings.frontend_dir)
