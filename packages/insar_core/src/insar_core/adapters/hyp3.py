from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

import hyp3_sdk

from insar_core.adapters.base import ProcessorAdapter
from insar_core.models.job import JobStatus, SubmittedJob
from insar_core.net import stream_download


def _sdk_job_to_submitted(job) -> SubmittedJob:
    status_map = {
        "PENDING": JobStatus.PENDING,
        "RUNNING": JobStatus.RUNNING,
        "SUCCEEDED": JobStatus.SUCCEEDED,
        "FAILED": JobStatus.FAILED,
    }
    files = job.files or []
    granules = job.job_parameters.get("granules", ["", ""])
    ref = granules[0] if len(granules) > 0 else ""
    sec = granules[1] if len(granules) > 1 else ""

    def _extract_date(granule: str) -> Optional[datetime]:
        # Sentinel-1 granule date is at chars 17-25: YYYYMMDD
        try:
            return datetime.strptime(granule[17:25], "%Y%m%d").date()
        except (ValueError, IndexError):
            return None

    return SubmittedJob(
        hyp3_job_id=job.job_id,
        reference_granule=ref,
        secondary_granule=sec,
        reference_date=_extract_date(ref),
        secondary_date=_extract_date(sec),
        status=status_map.get(job.status_code, JobStatus.PENDING),
        credit_cost=getattr(job, "credit_cost", None),
        error_message="\n".join(getattr(job, "logs", None) or []) or None,
        submitted_at=getattr(job, "request_time", None),
        completed_at=getattr(job, "expiration_time", None),
    )


class HyP3Adapter(ProcessorAdapter):
    """InSAR processor adapter for ASF's HyP3 cloud service."""

    def __init__(self, username: Optional[str] = None, password: Optional[str] = None):
        """If username/password are None, hyp3_sdk reads from ~/.netrc."""
        if username and password:
            self._client = hyp3_sdk.HyP3(username=username, password=password)
        else:
            self._client = hyp3_sdk.HyP3()

    def submit_pair(
        self,
        reference: str,
        secondary: str,
        name: str = "insar-job",
        include_dem: bool = True,
        include_inc_map: bool = True,
        include_look_vectors: bool = True,
        include_displacement_maps: bool = True,
        looks: str = "20x4",
        **kwargs,
    ) -> SubmittedJob:
        batch = self._client.submit_insar_job(
            reference,
            secondary,
            name=name,
            include_dem=include_dem,
            include_inc_map=include_inc_map,
            include_look_vectors=include_look_vectors,
            include_displacement_maps=include_displacement_maps,
            looks=looks,
        )
        jobs = list(batch)
        return _sdk_job_to_submitted(jobs[0])

    def get_status(self, job_id: str) -> SubmittedJob:
        job = self._client.get_job_by_id(job_id)
        return _sdk_job_to_submitted(job)

    def download(
        self,
        job_id: str,
        destination: Path,
        progress_cb: Callable[..., None] | None = None,
    ) -> List[Path]:
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        job = self._client.get_job_by_id(job_id)
        files = job.files or []
        paths: List[Path] = []
        for idx, file_info in enumerate(files):
            url = file_info["url"]
            filename = file_info.get("filename") or url.split("/")[-1].split("?")[0]
            dest = destination / filename
            stream_download(url, dest, file_index=idx + 1, file_count=len(files), progress_cb=progress_cb)
            paths.append(dest)
        return paths

    def get_jobs_bulk(self, since: datetime | None = None) -> dict[str, "SubmittedJob"]:
        """Fetch all jobs submitted after `since` in one paginated API call.

        Returns a dict keyed by hyp3_job_id. Used by the polling service to
        avoid N individual get_status() calls for large batches.
        """
        from datetime import timedelta, timezone as tz
        if since is None:
            since = datetime.now(tz.utc) - timedelta(days=30)
        result: dict[str, SubmittedJob] = {}
        batch = self._client.find_jobs(start=since)
        for job in batch:
            submitted = _sdk_job_to_submitted(job)
            result[submitted.hyp3_job_id] = submitted
        return result

    def check_credits(self) -> float:
        return self._client.check_credits()
