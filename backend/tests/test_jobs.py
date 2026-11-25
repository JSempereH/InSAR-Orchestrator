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
from insar_core.pipeline.pair_planner import PairPlanConfig, plan_pairs


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

    def __init__(self, plan=None, pairs=None, scenes=None):
        self._plan = plan or BatchPlan(total_pairs=0, scene_count=0, pairs_preview=[])
        self._pairs = pairs if pairs is not None else []
        self._scenes = scenes if scenes is not None else []
        self.plan_batch_calls: list[set] = []
        self.build_pairs_calls: list[set] = []
        self.plan_pairs_calls: list[dict] = []

    def plan_pairs(self, params, config=None, submitted_pairs=None, excluded_pairs=None):
        """The real planner over the scenes this fake was given, as the router calls it."""
        self.plan_pairs_calls.append({"config": config, "submitted": submitted_pairs, "excluded": excluded_pairs})
        if self._pairs and not self._scenes:
            scenes = {s.granule_name: s for pair in self._pairs for s in pair}
            plan = plan_pairs(sorted(scenes.values(), key=lambda s: s.acquisition_date), PairPlanConfig(max_temporal_neighbors=10))
            wanted = {(a.granule_name, b.granule_name) for a, b in self._pairs}
            plan.pairs = [p for p in plan.pairs if (p.reference.granule_name, p.secondary.granule_name) in wanted]
            return plan.mark(submitted_pairs or (), excluded_pairs or ())
        return plan_pairs(self._scenes, config or PairPlanConfig()).mark(submitted_pairs or (), excluded_pairs or ())

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


def test_plan_batch_returns_the_pair_network(client, monkeypatch):
    project = _create_project(client)
    fake = FakeOrchestrator(scenes=[_scene(f"S{k}", day=1 + k) for k in range(5)])
    monkeypatch.setattr(jobs_router, "_get_orchestrator", lambda db: fake)

    resp = client.post(f"/api/projects/{project['id']}/batches/plan", json={"max_temporal_neighbors": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_pairs"] == 7 and body["scene_count"] == 5          # 5 scenes, 2 neighbours each, fewer at the end
    assert body["pairs_preview"] == [["S0", "S1"], ["S0", "S2"], ["S1", "S2"], ["S1", "S3"], ["S2", "S3"]]
    assert body["credits"] == 70 and body["connected"] is True and body["strategy"] == "neighbors"
    assert len(body["pairs"]) == 7 and body["pairs"][0]["reference"] == "S0" and body["pairs"][0]["state"] == "new"
    assert [s["granule"] for s in body["scenes"]] == ["S0", "S1", "S2", "S3", "S4"]
    assert body["estimated_size_gb"] is not None  # falls back to the flat per-job estimate


def test_plan_batch_passes_the_chosen_strategy_and_limits_on(client, monkeypatch):
    project = _create_project(client)
    fake = FakeOrchestrator(scenes=[_scene(f"S{k}", day=1 + k) for k in range(6)])
    monkeypatch.setattr(jobs_router, "_get_orchestrator", lambda db: fake)
    client.post(f"/api/projects/{project['id']}/batches/plan", json={
        "strategy": "baseline", "pb_max": 120, "min_degree": 2, "max_degree": 4, "include_annual": True, "dt_targets": [1, 2]})
    config = fake.plan_pairs_calls[0]["config"]
    assert (config.strategy, config.pb_max, config.max_degree, config.include_annual, list(config.dt_targets)) == ("baseline", 120, 4, True, [1, 2])


def test_pairs_left_out_by_hand_are_marked_and_cost_nothing(client, monkeypatch):
    project = _create_project(client)
    fake = FakeOrchestrator(scenes=[_scene(f"S{k}", day=1 + k) for k in range(4)])
    monkeypatch.setattr(jobs_router, "_get_orchestrator", lambda db: fake)
    body = client.post(f"/api/projects/{project['id']}/batches/plan", json={"exclude": [["S0", "S1"]]}).json()
    assert [p["state"] for p in body["pairs"] if p["reference"] == "S0" and p["secondary"] == "S1"] == ["excluded"]
    assert body["total_pairs"] == len(body["pairs"]) - 1 and body["credits"] == 10 * body["total_pairs"]


def test_impossible_settings_are_a_422_that_says_why(client, monkeypatch):
    project = _create_project(client)
    monkeypatch.setattr(jobs_router, "_get_orchestrator", lambda db: FakeOrchestrator())
    reply = client.post(f"/api/projects/{project['id']}/batches/plan", json={"strategy": "baseline", "min_degree": 5, "max_degree": 2})
    assert reply.status_code == 422 and "min_degree" in reply.json()["detail"]
    assert client.post(f"/api/projects/{project['id']}/batches/plan", json={"strategy": "other"}).status_code == 422


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

    exclude_pairs = fake.plan_pairs_calls[0]["submitted"]
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
    assert parameters["pairing"] == {"method": "temporal_neighbors", "max_temporal_neighbors": 2, "long_pairs_days": [], "calibration_pair": False}
    assert parameters["search"]["date_start"] == "2023-01-01"
    assert parameters["software"]["python"]
    assert adapter.parameters[0]["looks"] == "20x4"   # what was recorded is what was submitted

    db = SessionLocal()
    try:
        assert db.query(Batch).filter_by(id=created["id"]).first().pending_pairs is None
    finally:
        db.close()


def test_a_burst_project_is_planned_by_bursts_and_costs_one_credit_a_pair(client, monkeypatch):
    """The plan of a `hyp3_burst` project comes from the burst search and is priced by its bursts."""
    from app.services import asf_service
    from insar_core.adapters.asf_burst import BurstError

    project = client.post("/api/projects", json={
        "name": "Burst project", "engine": "hyp3_burst",
        "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 0.1], [0.1, 0.1], [0.1, 0], [0, 0]]]},
        "date_start": "2023-01-01", "date_end": "2023-06-01",
    }).json()
    assert project["engine"] == "hyp3_burst"

    bursts = ("081_172669_IW2", "081_172669_IW3", "081_172670_IW2")

    def scene(day):
        names = "+".join(f"S1_{b.split('_')[1]}_{b.split('_')[2]}_202301{day:02d}T061840_VV_AB12-BURST" for b in bursts)
        return SARScene(file_id=names, granule_name=names, acquisition_date=date(2023, 1, day), orbit=1, track_number=81,
                        flight_direction="DESCENDING", polarization="VV", burst_ids=bursts)

    monkeypatch.setattr(asf_service.get_burst_adapter(), "search", lambda params: [scene(d) for d in (1, 13, 25)])
    monkeypatch.setattr(jobs_router, "get_hyp3_adapter", lambda db: None)
    body = client.post(f"/api/projects/{project['id']}/batches/plan", json={"max_temporal_neighbors": 2}).json()
    assert body["total_pairs"] == 3 and body["credits"] == 3 and body["credits_per_pair"] == 1
    assert body["pairs"][0]["reference"].count("-BURST") == 3
    assert body["estimated_size_gb"] is None            # not measured for burst products, so not guessed

    def too_large(params):
        raise BurstError("The area needs 22 bursts and HyP3 takes at most 15 in one interferogram.")

    monkeypatch.setattr(asf_service.get_burst_adapter(), "search", too_large)
    reply = client.post(f"/api/projects/{project['id']}/batches/plan", json={})
    assert reply.status_code == 422 and "22 bursts" in reply.json()["detail"]
