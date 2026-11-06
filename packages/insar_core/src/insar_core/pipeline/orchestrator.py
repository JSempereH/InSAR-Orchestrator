from __future__ import annotations

import logging
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from insar_core.adapters.base import ProcessorAdapter, SceneSearchAdapter
from insar_core.models.job import JobStatus, SubmittedJob
from insar_core.models.scene import SearchParams
from insar_core.pipeline.pair_builder import build_sbas_pairs

log = logging.getLogger(__name__)

# Pair submission and job-status polling are both network-bound (one HTTP
# call per pair/job to HyP3), so a modest amount of concurrency shortens
# wall-clock time for a large batch without needing HyP3 to support a true
# bulk-submit API for this job type (it doesn't - submit_insar_job takes
# exactly one pair per call).
_SUBMIT_CONCURRENCY = 4


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

    def build_pairs(
        self,
        params: SearchParams,
        max_temporal_neighbors: int = 3,
        exclude_pairs: Optional[set[tuple[str, str]]] = None,
    ):
        """Search scenes and build the SBAS pairs that submit_batch() would submit.

        Exposed separately so callers can know the pair count (and thus create
        a Batch record) before running the slower, network-bound submission loop.
        """
        scenes = self._scenes.search(params)
        pairs = build_sbas_pairs(scenes, max_temporal_neighbors)

        if exclude_pairs:
            pairs = [
                (ref, sec) for ref, sec in pairs
                if (ref.granule_name, sec.granule_name) not in exclude_pairs
            ]
        return pairs

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
        pairs = self.build_pairs(params, max_temporal_neighbors, exclude_pairs)
        if not pairs:
            return []

        def _submit_one(ref, sec) -> tuple:
            try:
                return ("ok", self._processor.submit_pair(
                    ref.granule_name, sec.granule_name, name=job_name, **processor_kwargs,
                ))
            except Exception as exc:
                return ("failed", (ref.granule_name, sec.granule_name, str(exc)))

        jobs: List[SubmittedJob] = []
        failed: List[tuple] = []
        with ThreadPoolExecutor(max_workers=min(_SUBMIT_CONCURRENCY, len(pairs))) as executor:
            futures = [executor.submit(_submit_one, ref, sec) for ref, sec in pairs]
            for future in as_completed(futures):
                kind, value = future.result()
                (jobs if kind == "ok" else failed).append(value)

        if failed:
            # Don't abort: return what succeeded; caller can inspect failures
            warnings.warn(f"{len(failed)} pair(s) failed to submit: {failed[:3]}", stacklevel=2)

        return jobs

    def poll_jobs(self, job_ids: List[str]) -> List[SubmittedJob]:
        """Refresh status for a list of HyP3 job IDs.

        Uses the processor's bulk lookup (one paginated API call) when it
        supports one, instead of one get_status() call per job - HyP3Adapter
        does via get_jobs_bulk(). Jobs the bulk call does not cover (and every
        job, for a ProcessorAdapter that only implements get_status()) fall
        back to one get_status() call each.
        """
        by_id: Dict[str, SubmittedJob] = {}
        get_jobs_bulk = getattr(self._processor, "get_jobs_bulk", None)
        if callable(get_jobs_bulk):
            by_id = get_jobs_bulk()  # type: ignore[assignment]

        # The bulk lookup only covers a recent window (HyP3Adapter: the last
        # 30 days), so any job it did not return is fetched individually
        # instead of silently disappearing from the result.
        refreshed = []
        for jid in job_ids:
            if jid in by_id:
                refreshed.append(by_id[jid])
                continue
            try:
                refreshed.append(self._processor.get_status(jid))
            except Exception:
                log.warning("poll_jobs: get_status failed for job %s", jid, exc_info=True)
        return refreshed

    def download_succeeded(
        self,
        jobs: List[SubmittedJob],
        destination: Path,
    ) -> Dict[str, List[Path]]:
        """Download every SUCCEEDED job in `jobs`.

        Does not track which jobs were downloaded in a previous call - it has
        no persistent state of its own, so "already downloaded" is whatever
        the caller decides before building `jobs` (e.g. the backend only
        passes jobs whose DB row has downloaded=0).
        """
        results: Dict[str, List[Path]] = {}
        for job in jobs:
            if job.status == JobStatus.SUCCEEDED:
                files = self._processor.download(job.hyp3_job_id, destination)
                results[job.hyp3_job_id] = files
        return results
