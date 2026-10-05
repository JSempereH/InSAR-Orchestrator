"""batches.py: read-only batch/job lookups plus the auto_download toggle.
Had zero endpoint-level coverage before this - notably the toggle's side
effect (queueing already-succeeded, not-yet-downloaded jobs the moment
auto_download turns on) was entirely untested."""
from app.database import SessionLocal
from app.models import Batch, Job, JobStatus
from app.routers import batches as batches_router


def _create_project(client, name="Batches Test Project"):
    return client.post("/api/projects", json={
        "name": name,
        "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]},
        "date_start": "2023-01-01",
        "date_end": "2023-06-01",
    }).json()


def _create_batch_with_jobs(project_id, jobs_spec):
    """jobs_spec: list of (status, downloaded, hyp3_job_id)."""
    db = SessionLocal()
    try:
        batch = Batch(project_id=project_id, label="b1", total_pairs=len(jobs_spec), status="done")
        db.add(batch)
        db.commit()
        db.refresh(batch)
        for status, downloaded, hyp3_job_id in jobs_spec:
            db.add(Job(
                batch_id=batch.id, reference_granule="ref", secondary_granule="sec",
                status=status, downloaded=downloaded, hyp3_job_id=hyp3_job_id,
            ))
        db.commit()
        return batch.id
    finally:
        db.close()


def test_get_batch(client):
    project = _create_project(client)
    batch_id = _create_batch_with_jobs(project["id"], [])
    resp = client.get(f"/api/batches/{batch_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == batch_id


def test_get_batch_for_unknown_id_returns_404(client):
    resp = client.get("/api/batches/does-not-exist")
    assert resp.status_code == 404


def test_list_batch_jobs(client):
    project = _create_project(client)
    batch_id = _create_batch_with_jobs(project["id"], [
        (JobStatus.SUCCEEDED, 0, "hyp3-1"),
        (JobStatus.PENDING, 0, None),
    ])
    resp = client.get(f"/api/batches/{batch_id}/jobs")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_list_batch_jobs_for_unknown_id_returns_404(client):
    resp = client.get("/api/batches/does-not-exist/jobs")
    assert resp.status_code == 404


def test_update_batch_toggles_auto_download(client):
    project = _create_project(client)
    batch_id = _create_batch_with_jobs(project["id"], [])
    resp = client.patch(f"/api/batches/{batch_id}", json={"auto_download": True})
    assert resp.status_code == 200
    assert resp.json()["auto_download"] is True


def test_update_batch_for_unknown_id_returns_404(client):
    resp = client.patch("/api/batches/does-not-exist", json={"auto_download": True})
    assert resp.status_code == 404


def test_turning_on_auto_download_queues_already_succeeded_undownloaded_jobs(client, monkeypatch):
    project = _create_project(client)
    batch_id = _create_batch_with_jobs(project["id"], [
        (JobStatus.SUCCEEDED, 0, "hyp3-succeeded-not-downloaded"),
        (JobStatus.SUCCEEDED, 1, "hyp3-succeeded-already-downloaded"),
        (JobStatus.PENDING, 0, None),
        (JobStatus.SUCCEEDED, 0, None),  # succeeded but no hyp3_job_id yet - can't be queued
    ])

    enqueued = []
    monkeypatch.setattr(batches_router.download_queue, "enqueue", lambda items: enqueued.extend(items))

    client.patch(f"/api/batches/{batch_id}", json={"auto_download": True})

    assert len(enqueued) == 1
    assert enqueued[0]["hyp3_job_id"] == "hyp3-succeeded-not-downloaded"


def test_turning_on_auto_download_twice_does_not_requeue(client, monkeypatch):
    """The router only queues on the on/off transition (turning_on), not on
    every PATCH - re-enabling an already-on flag must be a no-op here."""
    project = _create_project(client)
    batch_id = _create_batch_with_jobs(project["id"], [
        (JobStatus.SUCCEEDED, 0, "hyp3-1"),
    ])

    enqueued = []
    monkeypatch.setattr(batches_router.download_queue, "enqueue", lambda items: enqueued.extend(items))

    client.patch(f"/api/batches/{batch_id}", json={"auto_download": True})
    client.patch(f"/api/batches/{batch_id}", json={"auto_download": True})

    assert len(enqueued) == 1  # only the first PATCH was a real on/off transition
