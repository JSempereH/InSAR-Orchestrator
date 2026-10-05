"""Project CRUD not already covered by test_storage.py (create)."""
from pathlib import Path

from app.database import SessionLocal
from app.models import Batch, Job, JobStatus


def _project_payload(**overrides):
    payload = {
        "name": "Test Project",
        "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]},
        "date_start": "2023-01-01",
        "date_end": "2023-06-01",
    }
    payload.update(overrides)
    return payload


def test_list_projects_includes_created_project(client):
    created = client.post("/api/projects", json=_project_payload(name="Listed Project")).json()

    resp = client.get("/api/projects")
    assert resp.status_code == 200
    assert any(p["id"] == created["id"] for p in resp.json())


def test_get_project_by_id(client):
    created = client.post("/api/projects", json=_project_payload(name="Fetched Project")).json()

    resp = client.get(f"/api/projects/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Fetched Project"


def test_get_nonexistent_project_returns_404(client):
    resp = client.get("/api/projects/does-not-exist")
    assert resp.status_code == 404


def test_delete_project_removes_it(client):
    created = client.post("/api/projects", json=_project_payload(name="Doomed Project")).json()

    resp = client.delete(f"/api/projects/{created['id']}")
    assert resp.status_code == 200

    assert client.get(f"/api/projects/{created['id']}").status_code == 404


def test_delete_nonexistent_project_returns_404(client):
    resp = client.delete("/api/projects/does-not-exist")
    assert resp.status_code == 404


def test_download_summary_for_project_with_no_jobs(client):
    created = client.post("/api/projects", json=_project_payload(name="Summary Project")).json()

    resp = client.get(f"/api/projects/{created['id']}/download-summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["storage_path"] == created["storage_path"]
    assert body["total_jobs"] == 0
    assert body["downloaded_jobs"] == 0


def test_download_summary_for_nonexistent_project_returns_404(client):
    resp = client.get("/api/projects/does-not-exist/download-summary")
    assert resp.status_code == 404


def test_delete_download_data_frees_space_but_keeps_slc_and_history(client):
    project = client.post("/api/projects", json=_project_payload(name="Download Data Project")).json()
    storage = Path(project["storage_path"])
    storage.mkdir(parents=True, exist_ok=True)

    scene_dir = storage / "S1AA_scene"
    scene_dir.mkdir()
    (scene_dir / "unw_phase.tif").write_bytes(b"0" * 1000)

    slc_dir = storage / "slc"
    slc_dir.mkdir()
    (slc_dir / "raw.zip").write_bytes(b"0" * 500)

    db = SessionLocal()
    try:
        batch = Batch(project_id=project["id"], label="b1", total_pairs=1, status="done")
        db.add(batch)
        db.commit()
        db.refresh(batch)
        job = Job(
            batch_id=batch.id, reference_granule="ref", secondary_granule="sec",
            status=JobStatus.SUCCEEDED, downloaded=1, download_path=str(scene_dir),
        )
        db.add(job)
        db.commit()
        job_id = job.id
    finally:
        db.close()

    resp = client.delete(f"/api/projects/{project['id']}/download-data")
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted_jobs"] == 1
    assert body["freed_gb"] == round(1000 / 1e9, 2)  # only the scene dir, not slc/

    assert not scene_dir.exists()
    assert slc_dir.exists()  # left alone
    assert (slc_dir / "raw.zip").exists()

    db = SessionLocal()
    try:
        refreshed = db.query(Job).filter_by(id=job_id).first()
        assert refreshed.downloaded == 0
        assert refreshed.download_path is None
    finally:
        db.close()


def test_delete_download_data_for_nonexistent_project_returns_404(client):
    resp = client.delete("/api/projects/does-not-exist/download-data")
    assert resp.status_code == 404
