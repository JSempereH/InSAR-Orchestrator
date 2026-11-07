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
        self.parameters: list[dict] = []

    def submit_pair(self, reference, secondary, name="insar-job", **kwargs):
        self.submitted.append((reference, secondary))
        self.parameters.append(kwargs)
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


# ---------- refresh (incremental update) ----------

def test_refresh_dry_run_changes_nothing(client, monkeypatch):
    project = _create_project(client)
    fake = FakeOrchestrator(plan=BatchPlan(total_pairs=3, scene_count=5, pairs_preview=[]))
    monkeypatch.setattr(jobs_router, "_get_orchestrator", lambda db: fake)

    resp = client.post(f"/api/projects/{project['id']}/refresh", json={"date_end": "2023-09-01"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_pairs"] == 3
    assert body["date_end"] == "2023-09-01"
    assert body["batch"] is None
    assert client.get(f"/api/projects/{project['id']}").json()["date_end"] == "2023-06-01"
    assert client.get(f"/api/projects/{project['id']}/batches").json() == []


def test_refresh_never_moves_date_end_back(client, monkeypatch):
    project = _create_project(client)
    monkeypatch.setattr(jobs_router, "_get_orchestrator", lambda db: FakeOrchestrator())

    resp = client.post(f"/api/projects/{project['id']}/refresh", json={"date_end": "2023-01-15"})
    assert resp.json()["date_end"] == "2023-06-01"


def test_refresh_submits_only_new_pairs_and_flags_the_batch(client, monkeypatch):
    project = _create_project(client)
    fake = FakeOrchestrator(pairs=[(_scene("S1_03", day=25), _scene("S1_04", day=30))])
    monkeypatch.setattr(jobs_router, "_get_orchestrator", lambda db: fake)
    monkeypatch.setattr("app.services.hyp3_service.get_hyp3_adapter", lambda db: FakeHyp3Adapter())

    db = SessionLocal()
    try:
        old = Batch(project_id=project["id"], label="first", total_pairs=1, status="done")
        db.add(old)
        db.commit()
        db.add(Job(batch_id=old.id, reference_granule="S1_01", secondary_granule="S1_02", status=JobStatus.SUCCEEDED))
        db.commit()
    finally:
        db.close()

    resp = client.post(
        f"/api/projects/{project['id']}/refresh",
        json={"dry_run": False, "date_end": "2023-09-01"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_pairs"] == 1
    assert body["batch"]["auto_download"] is True
    assert body["batch"]["run_mintpy_when_done"] is True
    assert ("S1_01", "S1_02") in fake.build_pairs_calls[0]
    assert client.get(f"/api/projects/{project['id']}").json()["date_end"] == "2023-09-01"
    _wait_for_jobs(client, project["id"], body["batch"]["id"], count=1)


def test_refresh_with_nothing_new_creates_no_batch(client, monkeypatch):
    project = _create_project(client)
    monkeypatch.setattr(jobs_router, "_get_orchestrator", lambda db: FakeOrchestrator(pairs=[]))

    resp = client.post(f"/api/projects/{project['id']}/refresh", json={"dry_run": False})
    assert resp.status_code == 200
    assert resp.json()["batch"] is None
    assert client.get(f"/api/projects/{project['id']}/batches").json() == []


def test_refresh_unknown_project_returns_404(client):
    assert client.post("/api/projects/nope/refresh", json={}).status_code == 404


def _wait_until_done(client, project_id, batch_id, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        batch = next(b for b in client.get(f"/api/projects/{project_id}/batches").json() if b["id"] == batch_id)
        if batch["status"] == "done":
            return batch
        time.sleep(0.05)
    raise AssertionError("batch never finished submitting")


def test_a_batch_records_how_it_was_produced_and_clears_its_pending_pairs(client, monkeypatch):
    project = _create_project(client)
    adapter = FakeHyp3Adapter()
    pairs = [(_scene("S1_01"), _scene("S1_02", day=13))]
    monkeypatch.setattr(jobs_router, "_get_orchestrator", lambda db: FakeOrchestrator(pairs=pairs))
    monkeypatch.setattr("app.services.hyp3_service.get_hyp3_adapter", lambda db: adapter)

    created = client.post(
        f"/api/projects/{project['id']}/batches", json={"dry_run": False, "max_temporal_neighbors": 2},
    ).json()
    done = _wait_until_done(client, project["id"], created["id"])

    parameters = done["parameters"]
    assert parameters["processor"] == "HyP3 INSAR_GAMMA"
    assert parameters["hyp3"]["looks"] == "20x4"
    assert parameters["pairing"] == {"method": "temporal_neighbors", "max_temporal_neighbors": 2}
    assert parameters["search"]["date_start"] == "2023-01-01"
    assert parameters["software"]["python"]
    assert adapter.parameters[0]["looks"] == "20x4"   # what was recorded is what was submitted

    db = SessionLocal()
    try:
        assert db.query(Batch).filter_by(id=created["id"]).first().pending_pairs is None
    finally:
        db.close()
