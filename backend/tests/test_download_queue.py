"""The HyP3 download queue (backend/app/services/download_queue.py),
exercised through its /api/downloads/queue endpoints - it had no coverage
at all before being rebuilt on top of the shared SequentialQueue engine."""
import time
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.models import Batch, Job, JobStatus
from app.services import download_queue, hyp3_service


class _FakeHyp3Adapter:
    def __init__(self, fail=False):
        self.fail = fail
        self.downloaded_job_ids = []

    def download(self, hyp3_job_id, destination, progress_cb=None):
        if self.fail:
            raise RuntimeError("boom")
        self.downloaded_job_ids.append(hyp3_job_id)
        dest_file = Path(destination) / f"{hyp3_job_id}.tif"
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        dest_file.write_bytes(b"0")
        if progress_cb:
            progress_cb(total_bytes=1, downloaded_bytes=1)
        return [dest_file]


@pytest.fixture(autouse=True)
def _reset_queue():
    yield
    download_queue.cancel()
    for _ in range(50):
        if not download_queue.get_state()["active"]:
            break
        time.sleep(0.02)


def _create_project_batch_and_job(client, name="Download Queue Project"):
    project = client.post("/api/projects", json={
        "name": name,
        "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]},
        "date_start": "2023-01-01",
        "date_end": "2023-06-01",
    }).json()
    storage = Path(project["storage_path"])
    storage.mkdir(parents=True, exist_ok=True)

    db = SessionLocal()
    try:
        batch = Batch(project_id=project["id"], label="b1", total_pairs=1, status="done")
        db.add(batch)
        db.commit()
        db.refresh(batch)
        job = Job(
            batch_id=batch.id, hyp3_job_id="hyp3-abc",
            reference_granule="ref", secondary_granule="sec",
            status=JobStatus.SUCCEEDED,
        )
        db.add(job)
        db.commit()
        job_id = job.id
    finally:
        db.close()
    return job_id


def test_queue_downloads_a_job_and_marks_it_downloaded(client, monkeypatch):
    fake = _FakeHyp3Adapter()
    monkeypatch.setattr(hyp3_service, "get_hyp3_adapter", lambda db: fake)
    job_id = _create_project_batch_and_job(client)

    resp = client.post("/api/downloads/queue", json={"jobs": [{"job_id": job_id, "hyp3_job_id": "hyp3-abc"}]})
    assert resp.status_code == 200

    state = client.get("/api/downloads/queue").json()
    for _ in range(50):
        if not state["active"]:
            break
        time.sleep(0.02)
        state = client.get("/api/downloads/queue").json()

    assert state["done"] == 1
    assert fake.downloaded_job_ids == ["hyp3-abc"]

    db = SessionLocal()
    try:
        refreshed = db.query(Job).filter_by(id=job_id).first()
        assert refreshed.downloaded == 1
        assert refreshed.download_path is not None
    finally:
        db.close()


def test_queue_reports_error_state_when_download_fails(monkeypatch, client):
    fake = _FakeHyp3Adapter(fail=True)
    monkeypatch.setattr(hyp3_service, "get_hyp3_adapter", lambda db: fake)
    job_id = _create_project_batch_and_job(client, name="Failing Download Project")

    client.post("/api/downloads/queue", json={"jobs": [{"job_id": job_id, "hyp3_job_id": "hyp3-abc"}]})

    state = client.get("/api/downloads/queue").json()
    for _ in range(50):
        if not state["active"]:
            break
        time.sleep(0.02)
        state = client.get("/api/downloads/queue").json()

    assert state["done"] == 1

    db = SessionLocal()
    try:
        refreshed = db.query(Job).filter_by(id=job_id).first()
        assert refreshed.downloaded == 0  # never marked - the download raised
    finally:
        db.close()


def test_cancel_clears_pending_jobs(client, monkeypatch):
    fake = _FakeHyp3Adapter()
    monkeypatch.setattr(hyp3_service, "get_hyp3_adapter", lambda db: fake)
    job_id = _create_project_batch_and_job(client, name="Cancel Queue Project")

    client.post("/api/downloads/queue", json={
        "jobs": [
            {"job_id": job_id, "hyp3_job_id": "hyp3-abc"},
            {"job_id": "job-2", "hyp3_job_id": "hyp3-def"},
        ],
    })
    resp = client.delete("/api/downloads/queue")
    assert resp.status_code == 200

    state = client.get("/api/downloads/queue").json()
    assert state["pending_count"] == 0
