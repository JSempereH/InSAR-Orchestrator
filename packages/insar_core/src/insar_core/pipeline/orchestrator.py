from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from insar_core.adapters.base import ProcessorAdapter, SceneSearchAdapter
from insar_core.models.job import JobStatus, SubmittedJob
from insar_core.models.scene import SearchParams
from insar_core.pipeline.pair_builder import build_sbas_pairs


@dataclass
class BatchPlan:
    """Dry-run result: what would be submitted without hitting the API."""
    total_pairs: int
    scene_count: int
    pairs_preview: list[tuple[str, str]] = field(default_factory=list)


class InSAROrchestrator:
    """Combines scene search + SBAS pair building + cloud submission."""

    def __init__(
        self,
        scene_adapter: SceneSearchAdapter,
        processor: ProcessorAdapter,
    ):
        self._scenes = scene_adapter
        self._processor = processor

    def plan_batch(
        self,
        params: SearchParams,
        max_temporal_neighbors: int = 3,
        preview_count: int = 5,
        exclude_pairs: Optional[set[tuple[str, str]]] = None,
    ) -> BatchPlan:
        """Return what would be submitted, without submitting. Safe to call freely."""
        scenes = self._scenes.search(params)
        pairs = build_sbas_pairs(scenes, max_temporal_neighbors)
        if exclude_pairs:
            pairs = [
                (ref, sec) for ref, sec in pairs
                if (ref.granule_name, sec.granule_name) not in exclude_pairs
            ]
        preview = [
            (ref.granule_name, sec.granule_name)
            for ref, sec in pairs[:preview_count]
        ]
        return BatchPlan(
            total_pairs=len(pairs),
            scene_count=len(scenes),
            pairs_preview=preview,
        )

    def submit_batch(
        self,
        params: SearchParams,
        max_temporal_neighbors: int = 3,
        job_name: str = "insar-job",
        exclude_pairs: Optional[set[tuple[str, str]]] = None,
        **processor_kwargs,
    ) -> List[SubmittedJob]:
        """Search scenes, build pairs, and submit all to the processor.

        exclude_pairs: set of (reference_granule, secondary_granule) tuples to skip.
        Use this to avoid re-submitting pairs that already exist in the DB.
        """
        scenes = self._scenes.search(params)
        pairs = build_sbas_pairs(scenes, max_temporal_neighbors)

        if exclude_pairs:
            pairs = [
                (ref, sec) for ref, sec in pairs
                if (ref.granule_name, sec.granule_name) not in exclude_pairs
            ]

        jobs: List[SubmittedJob] = []
        failed: List[tuple] = []
        for ref, sec in pairs:
            try:
                job = self._processor.submit_pair(
                    ref.granule_name,
                    sec.granule_name,
                    name=job_name,
                    **processor_kwargs,
                )
                jobs.append(job)
            except Exception as exc:
                failed.append((ref.granule_name, sec.granule_name, str(exc)))

        if failed:
            # Don't abort: return what succeeded; caller can inspect failures
            import warnings
            warnings.warn(f"{len(failed)} pair(s) failed to submit: {failed[:3]}")

        return jobs

    def poll_jobs(self, job_ids: List[str]) -> List[SubmittedJob]:
        """Refresh status for a list of HyP3 job IDs."""
        refreshed = []
        for jid in job_ids:
            try:
                refreshed.append(self._processor.get_status(jid))
            except Exception:
                pass
        return refreshed

    def download_succeeded(
        self,
        jobs: List[SubmittedJob],
        destination: Path,
    ) -> Dict[str, List[Path]]:
        """Download all SUCCEEDED jobs that haven't been downloaded yet."""
        results: Dict[str, List[Path]] = {}
        for job in jobs:
            if job.status == JobStatus.SUCCEEDED:
                files = self._processor.download(job.hyp3_job_id, destination)
                results[job.hyp3_job_id] = files
        return results
