"""A HyP3 that fails now and then: pairs are tried again, never twice at HyP3's expense."""
from datetime import date

import pytest
import requests
from hyp3_sdk.exceptions import HyP3Error, ServerError

from app.database import SessionLocal
from app.models import Batch, Job, Project
from app.services import batch_submission
from insar_core.models.job import JobStatus as CoreStatus, SubmittedJob

GEOMETRY = {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]}


def _job(ref, sec, job_id="job"):
    return SubmittedJob(hyp3_job_id=job_id, reference_granule=ref, secondary_granule=sec, reference_date=date(2025, 1, 1),
                        secondary_date=date(2025, 1, 13), status=CoreStatus.PENDING)


class Flaky:
    """Fails `failures` times with `error` for every pair, then accepts it."""
    def __init__(self, error, failures=1, accepted_despite=False, lookup_fails=False):
        self.error, self.failures, self.accepted_despite, self.lookup_fails = error, failures, accepted_despite, lookup_fails
        self.calls: dict[tuple, int] = {}
        self.server_side: list[SubmittedJob] = []

    def submit_pair(self, reference, secondary, name="x", **kwargs):
        key = (reference, secondary)
        self.calls[key] = self.calls.get(key, 0) + 1
        if self.calls[key] <= self.failures:
            if self.accepted_despite:
                self.server_side.append(_job(reference, secondary, f"server-{reference}"))
            raise self.error
        return _job(reference, secondary, f"new-{reference}")

    def find_jobs_by_name(self, name):
        if self.lookup_fails:
            raise RuntimeError("unreachable")
        return self.server_side


@pytest.fixture()
def run(monkeypatch):
    monkeypatch.setattr(batch_submission.time, "sleep", lambda s: None)

    def go(adapter, pairs=(("a", "b"),)):
        db = SessionLocal()
        project = Project(name="Retry Project", geometry=GEOMETRY, date_start="2025-01-01", date_end="2025-06-01")
        db.add(project)
        db.commit()
        batch = Batch(project_id=project.id, label="b", status="submitting", total_pairs=len(pairs))
        db.add(batch)
        db.commit()
        batch_id = batch.id
        db.close()
        monkeypatch.setattr("app.services.hyp3_service.get_hyp3_adapter", lambda db: adapter)
        batch_submission._run(batch_id, [tuple(p) for p in pairs], "insar-retry", {"hyp3": {}}, False)
        db = SessionLocal()
        try:
            return sorted((j.reference_granule, j.secondary_granule, j.hyp3_job_id) for j in db.query(Job).filter_by(batch_id=batch_id))
        finally:
            db.close()
    return go


def test_a_server_error_is_retried_and_the_pair_goes_through(client, run):
    adapter = Flaky(ServerError(), failures=1)
    assert run(adapter) == [("a", "b", "new-a")]
    assert adapter.calls[("a", "b")] == 2


def test_a_network_timeout_is_retried_too(client, run):
    adapter = Flaky(requests.Timeout("slow"), failures=2)
    assert run(adapter) == [("a", "b", "new-a")]
    assert adapter.calls[("a", "b")] == 3


def test_a_pair_that_keeps_failing_is_given_up_after_three_attempts(client, run):
    adapter = Flaky(ServerError(), failures=10)
    assert run(adapter, pairs=(("a", "b"), ("c", "d"))) == []
    assert adapter.calls == {("a", "b"): 3, ("c", "d"): 3}


def test_a_pair_hyp3_accepted_although_the_answer_was_lost_is_adopted_not_sent_again(client, run):
    adapter = Flaky(ServerError(), failures=1, accepted_despite=True)
    assert run(adapter) == [("a", "b", "server-a")]
    assert adapter.calls[("a", "b")] == 1        # never submitted a second time: that would charge twice


def test_when_hyp3_cannot_be_asked_the_pair_is_not_risked(client, run):
    adapter = Flaky(ServerError(), failures=1, lookup_fails=True)
    assert run(adapter) == []
    assert adapter.calls[("a", "b")] == 1


def test_an_error_about_the_pair_itself_is_not_retried(client, run):
    adapter = Flaky(HyP3Error("insufficient credits"), failures=5)
    assert run(adapter) == []
    assert adapter.calls[("a", "b")] == 1
