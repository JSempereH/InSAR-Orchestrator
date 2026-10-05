"""Runs MintPy's SBAS pipeline over a project's downloaded HyP3 interferograms.

Wires insar_core.pipeline.mintpy_adapter.MintPyAdapter (previously written but
never called from anywhere) into the backend: one background run per project,
tracked in memory (mirrors download_state.py - this is process-local runtime
state, not data that needs to survive a restart; the actual MintPy outputs on
disk do survive, in <project storage>/mintpy/).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from insar_core.pipeline.mintpy_adapter import MintPyAdapter
from insar_core.pipeline.mintpy_timeseries import read_point_time_series, read_velocity_points

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_runs: dict[str, "MintPyRunState"] = {}

_LOG_TAIL_LINES = 200


@dataclass
class MintPyRunState:
    project_id: str
    status: str = "RUNNING"  # RUNNING | SUCCEEDED | FAILED | CANCELLED
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: Optional[str] = None
    return_code: Optional[int] = None
    error: Optional[str] = None
    work_dir: str = ""
    process: Optional[subprocess.Popen] = None  # not serialized to API responses


def mintpy_available() -> bool:
    return shutil.which("smallbaselineApp.py") is not None


def _dir_size_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


# get_disk_usage does a real filesystem walk (stat() on every file under a
# project's downloads_dir), and get_status calls it on every request - the
# frontend polls that endpoint every 5s while a run is RUNNING, sometimes
# from more than one open tab. A short cache absorbs that polling without
# hiding genuinely new information for more than a few seconds.
_DISK_USAGE_CACHE_TTL_S = 3.0
_disk_usage_cache: dict[str, tuple[float, dict]] = {}


def get_disk_usage(downloads_dir: Path) -> dict:
    """Disk usage breakdown for a project's downloads directory.

    Surfaced by get_status so the UI can warn, before a run, how much space
    the downloaded HyP3 .zips currently take (unzip_all's crop-then-delete
    pipeline needs that headroom transiently, and hasn't reclaimed it yet),
    and show, after a run, how much of that was actually reclaimed - a real
    project in this repo went from 157 GB (77 GB zips + 80 GB extracted at
    full resolution) to a few hundred MB once cropped and the source zips
    were deleted.
    """
    cache_key = str(downloads_dir)
    cached = _disk_usage_cache.get(cache_key)
    now = time.monotonic()
    if cached is not None and now - cached[0] < _DISK_USAGE_CACHE_TTL_S:
        return cached[1]

    result = _compute_disk_usage(downloads_dir)
    _disk_usage_cache[cache_key] = (now, result)
    return result


def _compute_disk_usage(downloads_dir: Path) -> dict:
    if not downloads_dir.exists():
        return {"zips_bytes": 0, "extracted_bytes": 0, "mintpy_output_bytes": 0, "total_bytes": 0}

    zips_bytes = sum(p.stat().st_size for p in downloads_dir.glob("*.zip"))

    work_dir = downloads_dir / "mintpy"
    mintpy_output_bytes = _dir_size_bytes(work_dir) if work_dir.exists() else 0

    extracted_bytes = sum(
        _dir_size_bytes(entry)
        for entry in downloads_dir.iterdir()
        if entry.is_dir() and entry != work_dir
    )

    return {
        "zips_bytes": zips_bytes,
        "extracted_bytes": extracted_bytes,
        "mintpy_output_bytes": mintpy_output_bytes,
        "total_bytes": zips_bytes + extracted_bytes + mintpy_output_bytes,
    }


def get_status(project_id: str, downloads_dir: Optional[Path] = None) -> dict:
    disk_usage = get_disk_usage(downloads_dir) if downloads_dir is not None else None

    with _lock:
        state = _runs.get(project_id)
    if state is None:
        return {"status": "NOT_STARTED", "disk_usage": disk_usage}
    log_path = Path(state.work_dir) / "mintpy.log"
    log_tail = ""
    if log_path.exists():
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        log_tail = "\n".join(lines[-_LOG_TAIL_LINES:])
    return {
        "status": state.status,
        "started_at": state.started_at,
        "finished_at": state.finished_at,
        "return_code": state.return_code,
        "error": state.error,
        "work_dir": state.work_dir,
        "log_tail": log_tail,
        "disk_usage": disk_usage,
    }


def start_run(project_id: str, downloads_dir: Path, geometry: Optional[dict] = None) -> dict:
    with _lock:
        existing = _runs.get(project_id)
        if existing is not None and existing.status == "RUNNING":
            raise RuntimeError("A MintPy run is already in progress for this project")
        if not mintpy_available():
            raise RuntimeError(
                "smallbaselineApp.py was not found on PATH. Install MintPy on this "
                "machine (pip install mintpy) to run SBAS analysis."
            )
        if not any(downloads_dir.glob("*.zip")) and not any(
            p.is_dir() for p in downloads_dir.iterdir() if p.exists()
        ):
            raise RuntimeError(f"No downloaded interferograms found in {downloads_dir}")

        work_dir = downloads_dir / "mintpy"
        state = MintPyRunState(project_id=project_id, work_dir=str(work_dir))
        _runs[project_id] = state

    thread = threading.Thread(target=_run, args=(project_id, downloads_dir, work_dir, geometry), daemon=True)
    thread.start()
    return get_status(project_id, downloads_dir)


def get_time_series(downloads_dir: Path, lat: float, lon: float) -> dict:
    """Displacement time series for one point from this project's MintPy run.

    `work_dir` is computed the same way start_run does, so this works against
    whatever the most recent run for this project produced on disk - it does
    not depend on the in-memory `_runs` state, so it still works after a
    backend restart as long as the files are still there."""
    work_dir = downloads_dir / "mintpy"
    try:
        return read_point_time_series(work_dir, lat, lon)
    except (FileNotFoundError, ValueError) as exc:
        raise RuntimeError(str(exc)) from exc


def get_points(downloads_dir: Path) -> dict:
    """Decimated velocity points for map display, from this project's MintPy
    run - same work_dir/error-translation pattern as get_time_series."""
    work_dir = downloads_dir / "mintpy"
    try:
        return read_velocity_points(work_dir)
    except (FileNotFoundError, ValueError) as exc:
        raise RuntimeError(str(exc)) from exc


def cancel_run(project_id: str) -> dict:
    with _lock:
        state = _runs.get(project_id)
        if state is None or state.status != "RUNNING":
            raise RuntimeError("No running MintPy job for this project")
        process = state.process
    if process is not None:
        process.terminate()
    with _lock:
        state.status = "CANCELLED"
        state.finished_at = datetime.now(timezone.utc).isoformat()
    return get_status(project_id)


def _run(project_id: str, downloads_dir: Path, work_dir: Path, geometry: Optional[dict]) -> None:
    adapter = MintPyAdapter(downloads_dir=downloads_dir, work_dir=work_dir)
    try:
        adapter.unzip_all(geometry=geometry)
        config_path = adapter.write_config(geometry=geometry)
        process = adapter.start_full_pipeline(config_path=config_path)
        with _lock:
            _runs[project_id].process = process
        return_code = process.wait()
        with _lock:
            state = _runs[project_id]
            if state.status == "CANCELLED":
                return
            state.return_code = return_code
            state.status = "SUCCEEDED" if return_code == 0 else "FAILED"
            state.finished_at = datetime.now(timezone.utc).isoformat()
            if return_code != 0:
                state.error = f"smallbaselineApp.py exited with code {return_code}; see log_tail"
    except Exception as exc:
        logger.exception("MintPy run failed for project %s", project_id)
        with _lock:
            state = _runs.get(project_id)
            if state is not None:
                state.status = "FAILED"
                state.error = str(exc)
                state.finished_at = datetime.now(timezone.utc).isoformat()
