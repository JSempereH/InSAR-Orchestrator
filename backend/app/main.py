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
from app.routers import archive, backup, batches, credentials, decomposition, egms, engines, jobs, mintpy, nisar, projects, scenes, slc, storage, validation
from app.models import Batch
from app.services import backup_service, credits_service
from app.services.polling_service import poll_active_jobs, force_poll
from app.services import download_queue

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


# (table, column, DDL of the column) added after the first release. There is no Alembic here and
# create_all only creates missing tables, so these are added on start when absent.
_ADDED_COLUMNS = [
    ("projects", "storage_path", "VARCHAR"),
    ("projects", "backup_enabled", "BOOLEAN DEFAULT TRUE NOT NULL"),
    ("projects", "archived_remote", "VARCHAR"),
    ("projects", "archived_at", "TIMESTAMP"),
    ("projects", "engine", "VARCHAR DEFAULT 'hyp3_gamma' NOT NULL"),
    ("backup_config", "include_slc", "BOOLEAN DEFAULT FALSE NOT NULL"),
    ("batches", "status", "VARCHAR DEFAULT 'done' NOT NULL"),      # existing batches finished submitting by definition
    ("batches", "auto_download", "BOOLEAN DEFAULT FALSE NOT NULL"),
    ("batches", "pending_pairs", "JSON"),
    ("batches", "parameters", "JSON"),
    ("batches", "run_mintpy_when_done", "BOOLEAN DEFAULT FALSE NOT NULL"),
]


def _migrate_schema() -> None:
    """Add the columns and indexes introduced after the initial create_all.

    Uses SQLAlchemy's inspector rather than SQLite's PRAGMA, so it also runs on the
    other databases DATABASE_URL can point to."""
    from sqlalchemy import inspect

    inspector = inspect(engine)
    with engine.begin() as conn:
        for table, column, ddl in _ADDED_COLUMNS:
            if not inspector.has_table(table):
                continue
            if column not in {c["name"] for c in inspector.get_columns(table)}:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
        # Column(index=True) only takes effect via create_all for a table that does not exist yet.
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_batches_project_id ON batches (project_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_jobs_batch_id ON jobs (batch_id)"))


Base.metadata.create_all(bind=engine)
_migrate_schema()

if not settings.app_password:
    logger.warning(
        "APP_PASSWORD is not set: anyone who can reach this port can use the app, including "
        "your stored HyP3 credentials. Fine on localhost or behind a VPN; set APP_PASSWORD "
        "(backend/.env) before exposing it any further."
    )

from app.services import crypto  # noqa: E402

crypto.validate_key()

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
app.include_router(validation.router)
app.include_router(decomposition.router)
app.include_router(nisar.router)
app.include_router(engines.router)
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


_background_tasks: set[asyncio.Task] = set()   # the event loop keeps only weak references to tasks


def _spawn(coroutine) -> None:
    task = asyncio.create_task(coroutine)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


@app.on_event("startup")
async def startup_event():
    if settings.background_tasks:
        _spawn(poll_active_jobs())
        _spawn(credits_service.poll_credits())
        _spawn(backup_service.backup_scheduler())
    if settings.recover_on_startup:
        _spawn(_recover_after_restart())


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
        if not conns:
            self._connections.pop(batch_id, None)
            _snapshot_cache.pop(batch_id, None)     # nobody is watching this batch any more


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
            await websocket.close()
            return
        await websocket.send_json(snapshot)

        while True:
            await asyncio.sleep(60)
            snapshot = _fetch_batch_snapshot(batch_id)
            if snapshot is not None:
                await websocket.send_json(snapshot)

    except (WebSocketDisconnect, RuntimeError):
        pass                                     # the client left, or the socket closed under us
    finally:
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
            _snapshot_cache.pop(batch_id, None)
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
