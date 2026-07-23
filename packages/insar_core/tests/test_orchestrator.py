from datetime import date
from pathlib import Path
from typing import List

import pytest

from insar_core.adapters.base import ProcessorAdapter, SceneSearchAdapter
from insar_core.models.job import JobStatus, SubmittedJob
from insar_core.models.scene import AOI, SARScene, SearchParams, TrackSummary
from insar_core.pipeline.orchestrator import InSAROrchestrator


def _scene(day: int) -> SARScene:
    return SARScene(
        file_id=f"file-{day}",
        granule_name=f"S1_{day:02d}",
        acquisition_date=date(2023, 1, day),
        orbit=day,
        track_number=110,
        flight_direction="DESCENDING",
        polarization="VV",
    )


class FakeSceneAdapter(SceneSearchAdapter):
    def __init__(self, scenes: List[SARScene]):
        self._scenes = scenes

    def search(self, params: SearchParams) -> List[SARScene]:
        return self._scenes

    def available_tracks(self, params: SearchParams) -> List[TrackSummary]:
        return []


class FakeProcessor(ProcessorAdapter):
    def __init__(self, fail_granules=frozenset()):
        self.submitted: list[tuple[str, str]] = []
        self._fail_granules = fail_granules

    def submit_pair(self, reference: str, secondary: str, name: str = "insar-job", **kwargs) -> SubmittedJob:
        if reference in self._fail_granules:
            raise RuntimeError(f"submission failed for {reference}")
        self.submitted.append((reference, secondary))
        return SubmittedJob(
            hyp3_job_id=f"job-{reference}-{secondary}",
            reference_granule=reference,
            secondary_granule=secondary,
            reference_date=date(2023, 1, 1),
            secondary_date=date(2023, 1, 2),
            status=JobStatus.PENDING,
        )

    def get_status(self, job_id: str) -> SubmittedJob:
        raise NotImplementedError

    def download(self, job_id: str, destination: Path) -> List[Path]:
        raise NotImplementedError


def _params() -> SearchParams:
    return SearchParams(
        aoi=AOI.from_bbox(-1, 37, -0.9, 37.1),
        date_start=date(2023, 1, 1),
        date_end=date(2023, 1, 5),
    )


def test_plan_batch_reports_counts_without_submitting():
    scenes = [_scene(d) for d in (1, 2, 3, 4)]
    processor = FakeProcessor()
    orchestrator = InSAROrchestrator(FakeSceneAdapter(scenes), processor)

    plan = orchestrator.plan_batch(_params(), max_temporal_neighbors=1, preview_count=2)

    assert plan.scene_count == 4
    assert plan.total_pairs == 3  # (1,2) (2,3) (3,4)
    assert len(plan.pairs_preview) == 2
    assert processor.submitted == []  # dry run: nothing actually submitted


def test_plan_batch_respects_exclude_pairs():
    scenes = [_scene(d) for d in (1, 2, 3)]
    orchestrator = InSAROrchestrator(FakeSceneAdapter(scenes), FakeProcessor())

    plan = orchestrator.plan_batch(
        _params(), max_temporal_neighbors=2, exclude_pairs={("S1_01", "S1_02")}
    )
    assert plan.total_pairs == 2  # (1,3) and (2,3) remain


def test_submit_batch_submits_all_built_pairs():
    scenes = [_scene(d) for d in (1, 2, 3)]
    processor = FakeProcessor()
    orchestrator = InSAROrchestrator(FakeSceneAdapter(scenes), processor)

    jobs = orchestrator.submit_batch(_params(), max_temporal_neighbors=1, job_name="test")

    assert len(jobs) == 2
    assert processor.submitted == [("S1_01", "S1_02"), ("S1_02", "S1_03")]
    assert all(j.status == JobStatus.PENDING for j in jobs)


def test_submit_batch_skips_failed_pairs_but_returns_the_rest():
    scenes = [_scene(d) for d in (1, 2, 3)]
    processor = FakeProcessor(fail_granules={"S1_01"})
    orchestrator = InSAROrchestrator(FakeSceneAdapter(scenes), processor)

    with pytest.warns(UserWarning):
        jobs = orchestrator.submit_batch(_params(), max_temporal_neighbors=1)

    assert len(jobs) == 1
    assert jobs[0].reference_granule == "S1_02"
