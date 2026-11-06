from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone as tz
from pathlib import Path
from typing import Callable, List, Optional

import hyp3_sdk

from insar_core.adapters.base import ProcessorAdapter
from insar_core.models.job import JobStatus, SubmittedJob
from insar_core.net import stream_download

# How many of a job's output files (HyP3 InSAR products typically ship
# ~8-15: interferogram, coherence, DEM, look vectors, browse images, etc.)
# to fetch at once. Downloads are network-bound, not CPU-bound, so a modest
# amount of concurrency shortens wall-clock time without competing for CPU.
_DOWNLOAD_CONCURRENCY = 4


def _sdk_job_to_submitted(job) -> SubmittedJob:
    status_map = {
        "PENDING": JobStatus.PENDING,
        "RUNNING": JobStatus.RUNNING,
        "SUCCEEDED": JobStatus.SUCCEEDED,
        "FAILED": JobStatus.FAILED,
    }
    granules = job.job_parameters.get("granules", ["", ""])
    ref = granules[0] if len(granules) > 0 else ""
    sec = granules[1] if len(granules) > 1 else ""

    def _extract_date(granule: str) -> Optional[date]:
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
        # HyP3 exposes no completion timestamp (expiration_time is when the
        # output files are deleted, not when the job finished), so leave it
        # unset; callers stamp it when they first observe a terminal status.
        completed_at=None,
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
        """Download every output file for `job_id`, up to `_DOWNLOAD_CONCURRENCY`
        at a time (network-bound, not CPU-bound - a HyP3 InSAR product ships
        as many separate files, and fetching them one at a time left most of
        the available bandwidth unused).

        `progress_cb` receives combined totals across every file currently in
        flight (not one file's own totals), so the caller sees one coherent
        overall progress figure instead of it resetting/jumping each time a
        file finishes and the next one starts.
        """
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        job = self._client.get_job_by_id(job_id)
        files = job.files or []
        if not files:
            return []

        lock = threading.Lock()
        downloaded_by_file: dict[int, int] = {}
        total_by_file: dict[int, int] = {}
        t0 = time.monotonic()

        def make_file_cb(idx: int) -> Callable[..., None]:
            def _cb(*, filename: str, total_bytes: int, downloaded_bytes: int, **_kw) -> None:
                if progress_cb is None:
                    return
                with lock:
                    downloaded_by_file[idx] = downloaded_bytes
                    total_by_file[idx] = total_bytes
                    total = sum(total_by_file.values())
                    done = sum(downloaded_by_file.values())
                    in_flight = len(downloaded_by_file)
                elapsed = max(time.monotonic() - t0, 0.001)
                speed = done / elapsed
                eta = int((total - done) / speed) if speed and total else None
                progress_cb(
                    file_index=in_flight,
                    file_count=len(files),
                    filename=filename,
                    total_bytes=total,
                    downloaded_bytes=done,
                    speed_bps=speed,
                    eta_s=eta,
                )
            return _cb

        def _download_one(idx: int, file_info: dict) -> tuple[int, Path]:
            url = file_info["url"]
            filename = file_info.get("filename") or url.split("/")[-1].split("?")[0]
            dest = destination / filename
            stream_download(url, dest, file_index=idx + 1, file_count=len(files), progress_cb=make_file_cb(idx))
            return idx, dest

        paths: List[Optional[Path]] = [None] * len(files)
        with ThreadPoolExecutor(max_workers=min(_DOWNLOAD_CONCURRENCY, len(files))) as executor:
            futures = [executor.submit(_download_one, idx, f) for idx, f in enumerate(files)]
            for future in as_completed(futures):
                idx, dest = future.result()
                paths[idx] = dest

        return paths  # type: ignore[return-value]  # every slot is filled by the loop above

    def get_jobs_bulk(self, since: datetime | None = None) -> dict[str, "SubmittedJob"]:
        """Fetch all jobs submitted after `since` in one paginated API call.

        Returns a dict keyed by hyp3_job_id. Used by the polling service to
        avoid N individual get_status() calls for large batches.
        """
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
