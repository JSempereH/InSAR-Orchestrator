"""
InSAR Orchestrator API

Run with: uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
"""

import asyncio
import json
import logging
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware

from sqlalchemy import text

from app.config import settings
from app.database import Base, engine, SessionLocal
from app.models import Batch, Job, JobStatus
from app.routers import batches, credentials, jobs, projects, scenes, storage
from app.services.polling_service import poll_active_jobs, force_poll
from app.services import download_queue

logging.basicConfig(level=logging.INFO)


def _migrate_schema() -> None:
    """Add columns introduced after the initial create_all, since there's no
    Alembic in this project and create_all only creates missing tables."""
    with engine.connect() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(projects)"))}
        if "storage_path" not in cols:
            conn.execute(text("ALTER TABLE projects ADD COLUMN storage_path VARCHAR"))
            conn.commit()


Base.metadata.create_all(bind=engine)
_migrate_schema()

app = FastAPI(title="InSAR Orchestrator API", version="0.2.0")

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


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(poll_active_jobs())


@app.get("/api/health")
def health_check():
    return {"status": "ok", "version": "0.2.0"}


@app.post("/api/admin/poll")
async def trigger_poll():
    """Force an immediate HyP3 status sync for all active jobs."""
    result = await force_poll()
    return result


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

    async def broadcast(self, batch_id: str, data: Any):
        for ws in list(self._connections.get(batch_id, [])):
            try:
                await ws.send_json(data)
            except Exception:
                self._connections[batch_id].remove(ws)


manager = _ConnectionManager()


@app.websocket("/ws/batches/{batch_id}")
async def batch_status_ws(websocket: WebSocket, batch_id: str):
    """Stream job status updates for a batch.

    On connect, sends the current snapshot. Then sends an update whenever
    the polling loop changes job statuses (poll interval ~60 s).
    """
    await manager.connect(batch_id, websocket)
    try:
        db = SessionLocal()
        try:
            batch = db.query(Batch).filter_by(id=batch_id).first()
            if not batch:
                await websocket.send_json({"error": "Batch not found"})
                return
            await _send_snapshot(websocket, batch)
        finally:
            db.close()

        while True:
            await asyncio.sleep(60)
            db = SessionLocal()
            try:
                batch = db.query(Batch).filter_by(id=batch_id).first()
                if batch:
                    await _send_snapshot(websocket, batch)
            finally:
                db.close()

    except WebSocketDisconnect:
        manager.disconnect(batch_id, websocket)


async def _send_snapshot(ws: WebSocket, batch: Batch):
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
    await ws.send_json({"batch_id": batch.id, "jobs": jobs_data})
