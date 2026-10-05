"""jobs.py: plan/submit a batch of SBAS pairs, list a batch's jobs.

Had zero endpoint-level coverage before this - only the orchestrator's own
logic (build_pairs, submit_batch) was unit-tested, not this router's own
behavior (existing-pair exclusion via a real DB query, batch creation,
response shaping, error codes). Notably: _existing_pairs was rewritten this
session (N+1 fix, one joined query instead of one per batch) and had no
test proving the real query is actually correct end-to-end.
"""
import time
from datetime import date

from app.database import SessionLocal
from app.models import Batch, Job, JobStatus
from app.routers import jobs as jobs_router
from insar_core.models.job import JobStatus as CoreJobStatus, SubmittedJob
from insar_core.models.scene import SARScene
from insar_core.pipeline.orchestrator import BatchPlan


def _scene(name: str, day: int = 1) -> SARScene:
    return SARScene(
        file_id=f"file-{name}",
        granule_name=name,
        acquisition_date=date(2023, 1, day),
        orbit=day,
        track_number=100,
        flight_direction="DESCENDING",
        polarization="VV",
    )


class FakeOrchestrator:
    """Stands in for InSAROrchestrator - jobs.py's own logic (existing-pair
    exclusion, response shaping, batch/job persistence) is what's under
    test here, not the orchestrator's own pair-building/submission, which
    test_orchestrator.py already covers directly."""

    def __init__(self, plan=None, pairs=None):
        self._plan = plan or BatchPlan(total_pairs=0, scene_count=0, pairs_preview=[])
        self._pairs = pairs if pairs is not None else []
        self.plan_batch_calls: list[set] = []
        self.build_pairs_calls: list[set] = []

    def plan_batch(self, params, max_temporal_neighbors, exclude_pairs=None, preview_count=5):
        self.plan_batch_calls.append(exclude_pairs)
        return self._plan

    def build_pairs(self, params, max_temporal_neighbors, exclude_pairs=None):
        self.build_pairs_calls.append(exclude_pairs)
        return self._pairs


class FakeHyp3Adapter:
    def __init__(self):
        self.submitted: list[tuple[str, str]] = []

    def submit_pair(self, reference, secondary, name="insar-job", **kwargs):
        self.submitted.append((reference, secondary))
        return SubmittedJob(
            hyp3_job_id=f"job-{reference}-{secondary}",
            reference_granule=reference,
            secondary_granule=secondary,
            reference_date=date(2023, 1, 1),
            secondary_date=date(2023, 1, 2),
            status=CoreJobStatus.PENDING,
        )


def _create_project(client, name="Jobs Test Project"):
    return client.post("/api/projects", json={
        "name": name,
        "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]},
        "date_start": "2023-01-01",
        "date_end": "2023-06-01",
    }).json()


def _wait_for_jobs(client, project_id, batch_id, count, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jobs = client.get(f"/api/projects/{project_id}/batches/{batch_id}/jobs").json()
        if len(jobs) >= count:
            return jobs
        time.sleep(0.05)
    raise AssertionError(f"jobs never reached count {count}")


def test_plan_batch_for_unknown_project_returns_404(client):
    resp = client.post("/api/projects/does-not-exist/batches/plan", json={})
    assert resp.status_code == 404


def test_plan_batch_returns_orchestrator_plan(client, monkeypatch):
    project = _create_project(client)
    fake = FakeOrchestrator(plan=BatchPlan(total_pairs=3, scene_count=4, pairs_preview=[("a", "b")]))
    monkeypatch.setattr(jobs_router, "_get_orchestrator", lambda db: fake)

    resp = client.post(f"/api/projects/{project['id']}/batches/plan", json={"max_temporal_neighbors": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_pairs"] == 3
    assert body["scene_count"] == 4
    assert body["pairs_preview"] == [["a", "b"]]
    assert body["estimated_size_gb"] is not None  # falls back to the flat per-job estimate


def test_plan_batch_excludes_pairs_already_in_the_db(client, monkeypatch):
    """The real, integration-level check for _existing_pairs's rewritten
    query: a SUCCEEDED job for this project must actually reach the
    orchestrator as an excluded pair, and a FAILED one must not."""
    project = _create_project(client)
    fake = FakeOrchestrator()
    monkeypatch.setattr(jobs_router, "_get_orchestrator", lambda db: fake)

    db = SessionLocal()
    try:
        batch = Batch(project_id=project["id"], label="b1", total_pairs=2, status="done")
        db.add(batch)
        db.commit()
        db.refresh(batch)
        db.add(Job(
            batch_id=batch.id, reference_granule="ref-ok", secondary_granule="sec-ok",
            status=JobStatus.SUCCEEDED,
        ))
        db.add(Job(
            batch_id=batch.id, reference_granule="ref-failed", secondary_granule="sec-failed",
            status=JobStatus.FAILED,
        ))
        db.commit()
    finally:
        db.close()

    client.post(f"/api/projects/{project['id']}/batches/plan", json={})

    exclude_pairs = fake.plan_batch_calls[0]
    assert ("ref-ok", "sec-ok") in exclude_pairs
    assert ("ref-failed", "sec-failed") not in exclude_pairs  # FAILED jobs are re-offered, not excluded


def test_submit_batch_rejects_dry_run(client):
    project = _create_project(client)
    resp = client.post(f"/api/projects/{project['id']}/batches", json={"dry_run": True})
    assert resp.status_code == 400


def test_submit_batch_for_unknown_project_returns_404(client):
    resp = client.post("/api/projects/does-not-exist/batches", json={"dry_run": False})
    assert resp.status_code == 404


def test_submit_batch_with_no_new_pairs_returns_400(client, monkeypatch):
    project = _create_project(client)
    monkeypatch.setattr(jobs_router, "_get_orchestrator", lambda db: FakeOrchestrator(pairs=[]))

    resp = client.post(f"/api/projects/{project['id']}/batches", json={"dry_run": False})
    assert resp.status_code == 400


def test_submit_batch_creates_batch_and_jobs(client, monkeypatch):
    project = _create_project(client)
    pairs = [(_scene("S1_01"), _scene("S1_02", day=13))]
    monkeypatch.setattr(jobs_router, "_get_orchestrator", lambda db: FakeOrchestrator(pairs=pairs))
    monkeypatch.setattr("app.services.hyp3_service.get_hyp3_adapter", lambda db: FakeHyp3Adapter())

    resp = client.post(f"/api/projects/{project['id']}/batches", json={"label": "my batch", "dry_run": False})
    assert resp.status_code == 201
    body = resp.json()
    assert body["project_id"] == project["id"]
    assert body["label"] == "my batch"
    assert body["total_pairs"] == 1

    jobs = _wait_for_jobs(client, project["id"], body["id"], count=1)
    assert jobs[0]["reference_granule"] == "S1_01"
    assert jobs[0]["secondary_granule"] == "S1_02"
    assert jobs[0]["status"] == "PENDING"


def test_list_jobs_for_unknown_batch_returns_404(client):
    project = _create_project(client)
    resp = client.get(f"/api/projects/{project['id']}/batches/does-not-exist/jobs")
    assert resp.status_code == 404


def test_list_jobs_reports_is_downloading_from_download_state(client, monkeypatch):
    from app.services import download_state

    project = _create_project(client)
    monkeypatch.setattr(jobs_router, "_get_orchestrator", lambda db: FakeOrchestrator(pairs=[]))

    db = SessionLocal()
    try:
        batch = Batch(project_id=project["id"], label="b1", total_pairs=1, status="done")
        db.add(batch)
        db.commit()
        db.refresh(batch)
        job = Job(batch_id=batch.id, reference_granule="ref", secondary_granule="sec", status=JobStatus.SUCCEEDED)
        db.add(job)
        db.commit()
        job_id = job.id
        batch_id = batch.id
    finally:
        db.close()

    download_state.update(job_id, status="running", pct=42)
    try:
        jobs = client.get(f"/api/projects/{project['id']}/batches/{batch_id}/jobs").json()
        assert len(jobs) == 1
        assert jobs[0]["is_downloading"] is True
    finally:
        download_state.clear(job_id)
