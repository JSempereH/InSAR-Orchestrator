"""Picking up after a restart: interrupted batches, undownloaded results, MintPy runs."""
import subprocess
import sys
from datetime import date, datetime, timedelta

import pytest

from app.database import SessionLocal
from app.models import Batch, Job, JobStatus, Project
from app.services import batch_submission, download_queue, mintpy_service, recovery
from insar_core.models.job import JobStatus as CoreStatus, SubmittedJob

GEOMETRY = {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]}


def _submitted(ref, sec, job_id, status=CoreStatus.PENDING):
    return SubmittedJob(
        hyp3_job_id=job_id, reference_granule=ref, secondary_granule=sec,
        reference_date=date(2025, 1, 1), secondary_date=date(2025, 1, 13), status=status,
    )


class FakeAdapter:
    def __init__(self, on_hyp3=(), lookup_fails=False):
        self.on_hyp3 = list(on_hyp3)
        self.lookup_fails = lookup_fails
        self.submitted: list[tuple[str, str]] = []
        self.parameters: list[dict] = []
        self.lookups: list[str] = []

    def submit_pair(self, reference, secondary, name="insar-job", **kwargs):
        self.submitted.append((reference, secondary))
        self.parameters.append(kwargs)
        return _submitted(reference, secondary, f"new-{reference}-{secondary}")

    def find_jobs_by_name(self, name):
        self.lookups.append(name)
        if self.lookup_fails:
            raise RuntimeError("HyP3 unreachable")
        return self.on_hyp3


@pytest.fixture()
def adapter(monkeypatch):
    # The test database is shared: settle batches other tests left "submitting".
    db = SessionLocal()
    db.query(Batch).filter(Batch.status == "submitting").update({"status": "done"})
    db.commit()
    db.close()
    fake = FakeAdapter()
    monkeypatch.setattr("app.services.hyp3_service.get_hyp3_adapter", lambda db: fake)
    # Run the submission inline instead of in a thread so tests can assert right after.
    real_run = batch_submission._run
    monkeypatch.setattr(
        batch_submission, "start",
        lambda batch_id, pairs, job_name, parameters=None, resume=False: real_run(batch_id, pairs, job_name, parameters, resume),
    )
    return fake


def _project(db, name="Recovery Project", **fields):
    project = Project(name=name, geometry=GEOMETRY, date_start="2025-01-01", date_end="2025-06-01", **fields)
    db.add(project)
    db.commit()
    return project


def _batch(db, project, **fields):
    defaults = dict(project_id=project.id, label="b", status="submitting", total_pairs=3)
    defaults.update(fields)
    batch = Batch(**defaults)
    db.add(batch)
    db.commit()
    return batch


def _job(db, batch, ref="x", sec="y", hyp3_id=None, status=JobStatus.PENDING, **fields):
    job = Job(batch_id=batch.id, reference_granule=ref, secondary_granule=sec, hyp3_job_id=hyp3_id, status=status, **fields)
    db.add(job)
    db.commit()
    return job


def _reload(batch_id):
    db = SessionLocal()
    try:
        batch = db.query(Batch).filter_by(id=batch_id).first()
        jobs = db.query(Job).filter_by(batch_id=batch_id).all()
        return batch.status, batch.pending_pairs, sorted((j.reference_granule, j.secondary_granule, j.hyp3_job_id) for j in jobs)
    finally:
        db.close()


# ── interrupted batches ──────────────────────────────────────────────────────

def test_resume_submits_only_the_pairs_not_recorded_yet(client, adapter):
    db = SessionLocal()
    project = _project(db)
    batch = _batch(db, project, pending_pairs=[["a", "b"], ["b", "c"], ["c", "d"]])
    _job(db, batch, "a", "b", hyp3_id="already")
    batch_id = batch.id
    db.close()

    result = recovery.resume_submitting_batches()

    assert result["resumed"] == [batch_id]
    assert adapter.submitted == [("b", "c"), ("c", "d")]
    status, pending, jobs = _reload(batch_id)
    assert status == "done" and pending is None
    assert [(r, s) for r, s, _ in jobs] == [("a", "b"), ("b", "c"), ("c", "d")]


def test_a_pair_hyp3_already_accepted_is_adopted_not_submitted_twice(client, adapter):
    adapter.on_hyp3 = [_submitted("b", "c", "hyp3-had-it")]
    db = SessionLocal()
    batch = _batch(db, _project(db), pending_pairs=[["b", "c"], ["c", "d"]])
    batch_id = batch.id
    db.close()

    recovery.resume_submitting_batches()

    assert adapter.submitted == [("c", "d")]                       # (b, c) was not paid for again
    assert ("b", "c", "hyp3-had-it") in _reload(batch_id)[2]
    assert adapter.lookups == ["insar-Recovery Project"]


def test_jobs_the_database_already_knows_or_that_failed_are_not_adopted(client, adapter):
    adapter.on_hyp3 = [
        _submitted("b", "c", "known-elsewhere"),
        _submitted("c", "d", "failed-one", status=CoreStatus.FAILED),
    ]
    db = SessionLocal()
    project = _project(db)
    other = _batch(db, project, status="done")
    _job(db, other, "b", "c", hyp3_id="known-elsewhere")
    batch = _batch(db, project, pending_pairs=[["b", "c"], ["c", "d"]])
    batch_id = batch.id
    db.close()

    recovery.resume_submitting_batches()

    assert adapter.submitted == [("b", "c"), ("c", "d")]   # neither was adoptable
    assert {j for _r, _s, j in _reload(batch_id)[2]} == {"new-b-c", "new-c-d"}


def test_when_hyp3_cannot_be_asked_nothing_is_resubmitted(client, adapter):
    adapter.lookup_fails = True
    db = SessionLocal()
    batch = _batch(db, _project(db), pending_pairs=[["a", "b"]])
    batch_id = batch.id
    db.close()

    recovery.resume_submitting_batches()

    assert adapter.submitted == []          # resubmitting blind could spend credits twice
    status, pending, jobs = _reload(batch_id)
    assert status == "done" and pending is None and jobs == []


def test_a_batch_with_no_stored_pairs_is_closed_not_resumed(client, adapter):
    db = SessionLocal()
    batch = _batch(db, _project(db), pending_pairs=None)
    batch_id = batch.id
    db.close()

    result = recovery.resume_submitting_batches()

    assert result["closed"] == [batch_id] and result["resumed"] == []
    assert adapter.submitted == [] and adapter.lookups == []
    assert _reload(batch_id)[0] == "done"


def test_resumed_pairs_use_the_parameters_stored_with_the_batch(client, adapter):
    db = SessionLocal()
    _batch(
        db, _project(db), pending_pairs=[["a", "b"]],
        parameters={"hyp3": {"looks": "10x2", "include_dem": False}},
    )
    db.close()
    recovery.resume_submitting_batches()
    assert adapter.parameters == [{"looks": "10x2", "include_dem": False}]


def test_batches_that_are_not_submitting_are_left_alone(client, adapter):
    db = SessionLocal()
    batch = _batch(db, _project(db), status="done", pending_pairs=[["a", "b"]])
    batch_id = batch.id
    db.close()
    assert recovery.resume_submitting_batches() == {"resumed": [], "closed": []}
    assert _reload(batch_id)[1] == [["a", "b"]]


# ── downloads that never ran ─────────────────────────────────────────────────

def test_undownloaded_results_of_auto_download_batches_are_queued_again(client, monkeypatch):
    queued = []
    monkeypatch.setattr(download_queue, "enqueue", lambda items: queued.extend(items))
    now = datetime(2025, 3, 1)
    db = SessionLocal()
    project = _project(db, name="Requeue")
    archived = _project(db, name="Requeue archived", archived_remote="x:y")
    auto = _batch(db, project, status="done", auto_download=True)
    manual = _batch(db, project, status="done", auto_download=False)
    in_archive = _batch(db, archived, status="done", auto_download=True)
    wanted = _job(db, auto, "w", "w", hyp3_id="h-wanted", status=JobStatus.SUCCEEDED, downloaded=0, completed_at=now - timedelta(days=1))
    _job(db, auto, "d", "d", hyp3_id="h-done", status=JobStatus.SUCCEEDED, downloaded=1, completed_at=now)
    _job(db, auto, "o", "o", hyp3_id="h-old", status=JobStatus.SUCCEEDED, downloaded=0, completed_at=now - timedelta(days=20))
    _job(db, auto, "f", "f", hyp3_id="h-failed", status=JobStatus.FAILED, downloaded=0)
    _job(db, auto, "r", "r", hyp3_id="h-running", status=JobStatus.RUNNING, downloaded=0)
    _job(db, manual, "m", "m", hyp3_id="h-manual", status=JobStatus.SUCCEEDED, downloaded=0, completed_at=now)
    _job(db, in_archive, "a", "a", hyp3_id="h-archived", status=JobStatus.SUCCEEDED, downloaded=0, completed_at=now)
    wanted_id = wanted.id
    db.close()

    # The test database is shared with other test files, so check these jobs by
    # their ids rather than the total.
    count = recovery.requeue_auto_downloads(now=now)
    queued_hyp3_ids = {item["hyp3_job_id"] for item in queued}
    assert count == len(queued) >= 1
    assert {"job_id": wanted_id, "hyp3_job_id": "h-wanted"} in queued
    assert queued_hyp3_ids.isdisjoint(
        {"h-done", "h-old", "h-failed", "h-running", "h-manual", "h-archived"}
    )


def test_a_download_that_returns_no_files_is_an_error_not_a_success(client, monkeypatch):
    class Empty:
        def download(self, *a, **k):
            return []

    db = SessionLocal()
    batch = _batch(db, _project(db, name="Expired"), status="done")
    job = _job(db, batch, hyp3_id="gone", status=JobStatus.SUCCEEDED, downloaded=0)
    job_id = job.id
    db.close()
    monkeypatch.setattr("app.services.hyp3_service.get_hyp3_adapter", lambda db: Empty())

    with pytest.raises(RuntimeError, match="expired"):
        download_queue._download_one({"job_id": job_id, "hyp3_job_id": "gone"})

    db = SessionLocal()
    try:
        assert not db.query(Job).filter_by(id=job_id).first().downloaded
    finally:
        db.close()


# ── MintPy runs cut short ────────────────────────────────────────────────────

@pytest.fixture()
def mintpy_project(tmp_path, client):
    folder = tmp_path / "proj"
    (folder / "ifg_1").mkdir(parents=True)
    db = SessionLocal()
    project = _project(db, name="MintPy Recovery", storage_path=str(folder))
    pid = project.id
    db.close()
    yield pid, folder
    db = SessionLocal()
    db.query(Project).filter_by(id=pid).delete()
    db.commit()
    db.close()
    mintpy_service._runs.pop(pid, None)


def _write_marker(folder, project_id, attempt, pid=None):
    mintpy_service._write_marker(folder / "mintpy", project_id=project_id, started_at="t", attempt=attempt, pid=pid)


def test_first_interruption_restarts_the_run_once(client, mintpy_project, monkeypatch):
    pid, folder = mintpy_project
    _write_marker(folder, pid, attempt=1)
    started = []
    monkeypatch.setattr(mintpy_service, "mintpy_available", lambda: True)
    monkeypatch.setattr(mintpy_service, "start_run", lambda *a, **k: started.append((a, k)))

    outcome = mintpy_service.recover_interrupted_runs()

    assert pid in outcome["restarted"]
    args, kwargs = started[0]
    assert args[0] == pid and kwargs["attempt"] == 2
    assert kwargs["context"]["project"]["id"] == pid    # provenance keeps being recorded


def test_a_run_interrupted_twice_is_not_restarted_again(client, mintpy_project, monkeypatch):
    pid, folder = mintpy_project
    _write_marker(folder, pid, attempt=mintpy_service.MAX_AUTO_ATTEMPTS)
    started = []
    monkeypatch.setattr(mintpy_service, "mintpy_available", lambda: True)
    monkeypatch.setattr(mintpy_service, "start_run", lambda *a, **k: started.append(a))

    outcome = mintpy_service.recover_interrupted_runs()

    assert pid in outcome["gave_up"] and started == []
    status = mintpy_service.get_status(pid, folder)
    assert status["status"] == "INTERRUPTED"
    assert "Run it again" in status["error"]


def test_markers_of_other_projects_and_runs_in_progress_are_ignored(client, mintpy_project, monkeypatch):
    pid, folder = mintpy_project
    monkeypatch.setattr(mintpy_service, "mintpy_available", lambda: True)
    started = []
    monkeypatch.setattr(mintpy_service, "start_run", lambda *a, **k: started.append(a))

    _write_marker(folder, "someone-else", attempt=1)
    assert mintpy_service.recover_interrupted_runs() == {"restarted": [], "gave_up": []}

    _write_marker(folder, pid, attempt=1)
    mintpy_service._runs[pid] = mintpy_service.MintPyRunState(project_id=pid)
    assert mintpy_service.recover_interrupted_runs() == {"restarted": [], "gave_up": []}
    assert started == []


def test_a_clean_project_reports_not_started(client, mintpy_project):
    pid, folder = mintpy_project
    assert mintpy_service.get_status(pid, folder)["status"] == "NOT_STARTED"


def test_a_leftover_mintpy_process_is_stopped_before_restarting(tmp_path):
    work_dir = tmp_path / "mintpy"
    work_dir.mkdir()
    code = "import time; time.sleep(60)"
    mine = subprocess.Popen([sys.executable, "-c", code, "smallbaselineApp.py", str(work_dir)])
    unrelated = subprocess.Popen([sys.executable, "-c", code])
    try:
        mintpy_service._stop_orphan({"pid": mine.pid}, work_dir)
        assert mine.wait(timeout=15) is not None           # stopped
        mintpy_service._stop_orphan({"pid": unrelated.pid}, work_dir)
        assert unrelated.poll() is None                    # a process that is not ours is never touched
    finally:
        for process in (mine, unrelated):
            process.kill()
            process.wait()
