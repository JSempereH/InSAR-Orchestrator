"""The /ws/batches/{id} websocket and its snapshot cache (main.py)."""
from app.database import SessionLocal
from app.models import Batch, Job, JobStatus
from app import main as main_module


def _create_project_and_batch(client):
    project = client.post("/api/projects", json={
        "name": "WS Test Project",
        "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]},
        "date_start": "2023-01-01",
        "date_end": "2023-06-01",
    }).json()

    db = SessionLocal()
    try:
        batch = Batch(project_id=project["id"], label="b1", total_pairs=1, status="done")
        db.add(batch)
        db.commit()
        db.refresh(batch)
        job = Job(
            batch_id=batch.id, hyp3_job_id="hyp3-1",
            reference_granule="ref", secondary_granule="sec",
            status=JobStatus.SUCCEEDED,
        )
        db.add(job)
        db.commit()
        return batch.id
    finally:
        db.close()


def test_websocket_sends_error_for_unknown_batch(client):
    main_module._snapshot_cache.clear()
    with client.websocket_connect("/ws/batches/does-not-exist") as ws:
        data = ws.receive_json()
    assert data == {"error": "Batch not found"}


def test_websocket_sends_initial_snapshot(client):
    main_module._snapshot_cache.clear()
    batch_id = _create_project_and_batch(client)

    with client.websocket_connect(f"/ws/batches/{batch_id}") as ws:
        data = ws.receive_json()

    assert data["batch_id"] == batch_id
    assert len(data["jobs"]) == 1
    assert data["jobs"][0]["hyp3_job_id"] == "hyp3-1"
    assert data["jobs"][0]["status"] == "SUCCEEDED"
    assert data["jobs"][0]["downloaded"] is False


def test_fetch_batch_snapshot_is_cached_within_ttl(client):
    main_module._snapshot_cache.clear()
    batch_id = _create_project_and_batch(client)

    first = main_module._fetch_batch_snapshot(batch_id)
    assert len(first["jobs"]) == 1

    db = SessionLocal()
    try:
        job2 = Job(
            batch_id=batch_id, hyp3_job_id="hyp3-2",
            reference_granule="ref2", secondary_granule="sec2",
            status=JobStatus.PENDING,
        )
        db.add(job2)
        db.commit()
    finally:
        db.close()

    second = main_module._fetch_batch_snapshot(batch_id)
    assert len(second["jobs"]) == 1  # still cached, new job not yet reflected

    main_module._snapshot_cache.pop(batch_id, None)
    third = main_module._fetch_batch_snapshot(batch_id)
    assert len(third["jobs"]) == 2  # recomputed once the cache entry is gone


def test_fetch_batch_snapshot_returns_none_for_unknown_batch():
    main_module._snapshot_cache.clear()
    assert main_module._fetch_batch_snapshot("does-not-exist") is None
