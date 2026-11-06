from types import SimpleNamespace

from app.database import SessionLocal
from app.models import Batch, Job, JobStatus, Project
from app.services import download_queue, polling_service
from insar_core.models.job import JobStatus as CoreStatus


def _seed(db):
    project = Project(name="p", geometry={}, date_start="2020-01-01", date_end="2020-02-01")
    db.add(project)
    db.flush()
    batch = Batch(project_id=project.id, auto_download=True, total_pairs=3)
    db.add(batch)
    db.flush()
    jobs = []
    for i in range(3):
        job = Job(batch_id=batch.id, hyp3_job_id=f"h{i}", reference_granule="a", secondary_granule="b", status=JobStatus.RUNNING)
        db.add(job)
        jobs.append(job)
    db.commit()
    return [j.id for j in jobs]


class _FakeAdapter:
    def __init__(self, poison):
        self._poison = poison

    def get_jobs_bulk(self, since=None):
        out = {}
        for i in range(3):
            cost = object() if f"h{i}" == self._poison else 5.0
            out[f"h{i}"] = SimpleNamespace(status=CoreStatus.SUCCEEDED, completed_at=None, credit_cost=cost, error_message=None)
        return out

    def get_status(self, job_id):
        raise AssertionError("bulk result should cover every job")


def test_one_bad_row_does_not_roll_back_earlier_updates(monkeypatch):
    db = SessionLocal()
    ids = _seed(db)
    db.close()

    queued = []
    monkeypatch.setattr(polling_service, "get_hyp3_adapter", lambda _db: _FakeAdapter(poison="h1"))
    monkeypatch.setattr(download_queue, "enqueue", lambda items: queued.extend(items))

    result = polling_service._refresh_jobs_sync()
    assert result["updated"] == 2

    db = SessionLocal()
    try:
        by_id = {j.id: j for j in db.query(Job).filter(Job.id.in_(ids))}
        statuses = [by_id[i].status for i in ids]
        assert statuses == [JobStatus.SUCCEEDED, JobStatus.RUNNING, JobStatus.SUCCEEDED]
        assert by_id[ids[0]].completed_at is not None
        assert by_id[ids[1]].completed_at is None
    finally:
        db.close()

    # Only rows whose update was stored get queued for auto-download.
    assert {q["hyp3_job_id"] for q in queued} == {"h0", "h2"}


# ---------- MintPy after an update batch ----------

def _seed_update_batch(db, statuses_downloaded):
    project = Project(name="u", geometry={}, date_start="2020-01-01", date_end="2020-02-01")
    db.add(project)
    db.flush()
    batch = Batch(project_id=project.id, status="done", run_mintpy_when_done=True, auto_download=True)
    db.add(batch)
    db.flush()
    for i, (status, downloaded) in enumerate(statuses_downloaded):
        db.add(Job(
            batch_id=batch.id, hyp3_job_id=f"u{i}", reference_granule="a", secondary_granule="b",
            status=status, downloaded=int(downloaded),
        ))
    db.commit()
    return project.id, batch.id


def _flag(batch_id):
    db = SessionLocal()
    try:
        return db.query(Batch).filter_by(id=batch_id).first().run_mintpy_when_done
    finally:
        db.close()


def test_mintpy_waits_until_every_job_is_downloaded(monkeypatch):
    from app.services import mintpy_service

    calls = []
    monkeypatch.setattr(mintpy_service, "start_run", lambda *a, **k: calls.append(a))

    db = SessionLocal()
    _, waiting = _seed_update_batch(db, [(JobStatus.SUCCEEDED, True), (JobStatus.SUCCEEDED, False)])
    _, running = _seed_update_batch(db, [(JobStatus.SUCCEEDED, True), (JobStatus.RUNNING, False)])
    _, ready = _seed_update_batch(db, [(JobStatus.SUCCEEDED, True), (JobStatus.FAILED, False)])
    db.close()

    polling_service._launch_pending_mintpy()

    assert _flag(waiting) is True
    assert _flag(running) is True
    assert _flag(ready) is False
    assert len(calls) == 1


def test_mintpy_retries_while_another_run_is_in_progress(monkeypatch):
    from app.services import mintpy_service

    def busy(*a, **k):
        raise RuntimeError("A MintPy run is already in progress for this project")

    monkeypatch.setattr(mintpy_service, "start_run", busy)
    db = SessionLocal()
    _, batch_id = _seed_update_batch(db, [(JobStatus.SUCCEEDED, True)])
    db.close()

    polling_service._launch_pending_mintpy()
    assert _flag(batch_id) is True


def test_mintpy_flag_is_dropped_when_mintpy_is_missing(monkeypatch):
    from app.services import mintpy_service

    def missing(*a, **k):
        raise RuntimeError("smallbaselineApp.py was not found on PATH")

    monkeypatch.setattr(mintpy_service, "start_run", missing)
    db = SessionLocal()
    _, batch_id = _seed_update_batch(db, [(JobStatus.SUCCEEDED, True)])
    db.close()

    polling_service._launch_pending_mintpy()
    assert _flag(batch_id) is False
