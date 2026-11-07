"""Runs MintPy's SBAS pipeline over a project's downloaded HyP3 interferograms.

Wires insar_core.pipeline.mintpy_adapter.MintPyAdapter (previously written but
never called from anywhere) into the backend: one background run per project,
tracked in memory (mirrors download_state.py - this is process-local runtime
state, not data that needs to survive a restart; the actual MintPy outputs on
disk do survive, in <project storage>/mintpy/).
"""

from __future__ import annotations

import functools
import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from insar_core.pipeline.mintpy_adapter import MintPyAdapter, cds_credentials_available, find_smallbaseline
from insar_core.pipeline.mintpy_timeseries import (
    locate_timeseries_file, locate_velocity_file, read_point_time_series, read_reference_point,
    read_velocity_points,
)

from insar_core.pipeline.mintpy_adapter import _MIN_MARGIN_DEG, _SUBSET_MARGIN_FRACTION
from insar_core.pipeline.provenance import mintpy_effective_options, parse_mintpy_options, software_versions

from app.services import provenance_service
from app.services.series_analysis import with_analysis

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_runs: dict[str, "MintPyRunState"] = {}

_LOG_TAIL_LINES = 200

# A run writes this file when it starts and removes it when it ends. One found at
# startup with no live run behind it means the app stopped mid-run.
_MARKER_NAME = ".run-state.json"
# Original run plus one automatic retry. A run that kills the backend (running out
# of memory, say) must not be restarted forever.
MAX_AUTO_ATTEMPTS = 2


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
    cancel_requested: bool = False
    attempt: int = 1
    # Things the person should know about how this run was configured, such as a
    # correction that had to be left out. Shown in the UI and saved in provenance.json.
    warnings: list[str] = field(default_factory=list)


def mintpy_available() -> bool:
    return find_smallbaseline() is not None


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
        # slc/ holds raw SLC downloads, which MintPy never reads; they are sized
        # (and deleted) separately, so they must not inflate this figure.
        if entry.is_dir() and entry != work_dir and entry.name != "slc"
    )

    return {
        "zips_bytes": zips_bytes,
        "extracted_bytes": extracted_bytes,
        "mintpy_output_bytes": mintpy_output_bytes,
        "total_bytes": zips_bytes + extracted_bytes + mintpy_output_bytes,
    }


def _restored_status(downloads_dir: Path, disk_usage: Optional[dict]) -> Optional[dict]:
    """A SUCCEEDED status rebuilt from files, for a run this process never saw.

    Run state lives in memory, so a backend restart (or a dev auto-reload)
    forgets every finished run even though its outputs are still on disk. A
    project whose work directory holds both the time series and the velocity
    map is viewable, so it is reported as SUCCEEDED instead of NOT_STARTED."""
    work_dir = downloads_dir / "mintpy"
    try:
        locate_timeseries_file(work_dir)
        velocity = locate_velocity_file(work_dir)
    except FileNotFoundError:
        return None
    log_path = work_dir / "mintpy.log"
    log_tail = ""
    if log_path.exists():
        log_tail = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-_LOG_TAIL_LINES:])
    return {
        "status": "SUCCEEDED",
        "started_at": None,
        "finished_at": datetime.fromtimestamp(velocity.stat().st_mtime, tz=timezone.utc).isoformat(),
        "return_code": None,
        "error": None,
        "work_dir": str(work_dir),
        "log_tail": log_tail,
        "disk_usage": disk_usage,
        "warnings": _recorded_warnings(downloads_dir),
    }


def _recorded_warnings(downloads_dir: Path) -> list[str]:
    record = provenance_service.read_run_record(downloads_dir) or {}
    return list((record.get("mintpy_run") or {}).get("warnings") or [])


def _marker_path(work_dir: Path) -> Path:
    return Path(work_dir) / _MARKER_NAME


def _write_marker(work_dir: Path, **fields) -> None:
    path = _marker_path(work_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(fields), encoding="utf-8")
    os.replace(tmp, path)


def _read_marker(work_dir: Path) -> Optional[dict]:
    try:
        return json.loads(_marker_path(work_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _clear_marker(work_dir: Path) -> None:
    _marker_path(work_dir).unlink(missing_ok=True)


def _interrupted_status(downloads_dir: Path, disk_usage: Optional[dict]) -> Optional[dict]:
    work_dir = downloads_dir / "mintpy"
    marker = _read_marker(work_dir)
    if marker is None:
        return None
    return {
        "status": "INTERRUPTED",
        "started_at": marker.get("started_at"),
        "finished_at": None,
        "return_code": None,
        "error": "MintPy was interrupted when the app stopped. Run it again to finish.",
        "work_dir": str(work_dir),
        "log_tail": "",
        "disk_usage": disk_usage,
    }


def forget_project(project_id: str, downloads_dir: Optional[Path] = None) -> None:
    """Drop what is remembered about a project's MintPy run, once its files are deleted.

    Without this, a run finished earlier in this session would keep reporting
    SUCCEEDED (and a stale disk usage) for results that no longer exist. Raises
    RuntimeError while a run is in progress, since its files must not disappear.
    """
    with _lock:
        state = _runs.get(project_id)
        if state is not None and state.status == "RUNNING":
            raise RuntimeError("A MintPy run is in progress for this project. Stop it first.")
        _runs.pop(project_id, None)
    if downloads_dir is not None:
        _disk_usage_cache.pop(str(downloads_dir), None)


def get_status(project_id: str, downloads_dir: Optional[Path] = None) -> dict:
    disk_usage = get_disk_usage(downloads_dir) if downloads_dir is not None else None

    with _lock:
        state = _runs.get(project_id)
    if state is None:
        interrupted = _interrupted_status(downloads_dir, disk_usage) if downloads_dir is not None else None
        if interrupted:
            return interrupted
        restored = _restored_status(downloads_dir, disk_usage) if downloads_dir is not None else None
        return restored or {"status": "NOT_STARTED", "disk_usage": disk_usage}
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
        "warnings": list(state.warnings),
    }


def start_run(
    project_id: str,
    downloads_dir: Path,
    geometry: Optional[dict] = None,
    context: Optional[dict] = None,
    attempt: int = 1,
) -> dict:
    """Start a MintPy run in the background.

    `context` (the project, its batches and pairs; see provenance_service) is
    saved with the run in the project's provenance.json. `attempt` counts automatic
    restarts after an interruption and is 1 for a run someone asked for."""
    with _lock:
        existing = _runs.get(project_id)
        if existing is not None and existing.status == "RUNNING":
            raise RuntimeError("A MintPy run is already in progress for this project")
        if not mintpy_available():
            raise RuntimeError(
                "smallbaselineApp.py was not found on PATH. Install MintPy on this "
                "machine (pip install mintpy) to run SBAS analysis."
            )
        if not downloads_dir.is_dir() or (
            not any(downloads_dir.glob("*.zip")) and not any(p.is_dir() for p in downloads_dir.iterdir())
        ):
            raise RuntimeError(f"No downloaded interferograms found in {downloads_dir}")

        work_dir = downloads_dir / "mintpy"
        state = MintPyRunState(project_id=project_id, work_dir=str(work_dir), attempt=attempt)
        _runs[project_id] = state

    thread = threading.Thread(
        target=_run, args=(project_id, downloads_dir, work_dir, geometry, context), daemon=True,
    )
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
        return with_analysis(read_point_time_series(work_dir, lat, lon))
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
        # Seen by _run if the MintPy process has not been started yet.
        state.cancel_requested = True
    if process is not None:
        process.terminate()
    with _lock:
        state.status = "CANCELLED"
        state.finished_at = datetime.now(timezone.utc).isoformat()
    return get_status(project_id)


def _cancel_requested(project_id: str) -> bool:
    with _lock:
        state = _runs.get(project_id)
        return state is not None and state.cancel_requested


_NO_CDS_WARNING = (
    "The ERA5 troposphere correction was left out because no Copernicus Climate Data "
    "Store login was found (~/.cdsapirc, or CDSAPI_KEY in the environment). MintPy "
    "cannot download the weather data without one, and the whole run would fail late "
    "with an unclear error. Add a free CDS account's key and run again to apply it."
)


def _troposphere_options() -> tuple[dict[str, str], list[str]]:
    """Config overrides and warnings that follow from what this machine can do.

    MintPy's default troposphere correction is PyAPS with ERA5, which needs a CDS
    login. Without one it downloads nothing, finds no weather files and stops with
    a division by zero (measured on a real run: it fails after the inversion, so
    the work done so far is wasted and the message does not say why). So the
    correction is turned off explicitly, and that is stated in the run's warnings,
    its provenance record and the status the UI shows, never silently."""
    defaults = mintpy_effective_options("")
    if not defaults or defaults.get("mintpy.troposphericDelay.method") != "pyaps":
        return {}, []
    if cds_credentials_available():
        return {}, []
    return {"mintpy.troposphericDelay.method": "no"}, [_NO_CDS_WARNING]


# MintPy's quick-overview step plots how many interferogram triplets have a non-zero
# integer ambiguity. When every triplet closes (a clean, coherent area) the histogram
# gets zero bins and MintPy stops with this error, although the numbers it needed are
# already saved. The plot is only a diagnostic, so the run continues after it.
_CLEAN_TRIPLETS_SIGNATURE = ("plot_num_triplet_with_nonzero_integer_ambiguity", "`bins` must be positive")
_CLEAN_TRIPLETS_NOTE = (
    "Every interferogram triplet closed without unwrapping inconsistencies, which makes "
    "MintPy fail while drawing a diagnostic plot. The plot was skipped and the run continued."
)


def _stopped_by_clean_triplets(work_dir: Path) -> bool:
    log = Path(work_dir) / "mintpy.log"
    try:
        tail = log.read_text(encoding="utf-8", errors="replace")[-6000:]
    except OSError:
        return False
    return all(marker in tail for marker in _CLEAN_TRIPLETS_SIGNATURE)


def _interferogram_names(downloads_dir: Path) -> list[str]:
    """Folders of cropped interferograms: every project subfolder except our own."""
    return sorted(
        entry.name for entry in downloads_dir.iterdir()
        if entry.is_dir() and not entry.name.startswith(".") and entry.name not in ("mintpy", "slc")
    )


def _result_files(work_dir: Path) -> dict:
    """Which files the viewer reads, so a record says exactly what the numbers come from."""
    out: dict = {"timeseries": None, "velocity": None, "reference": None}
    try:
        out["timeseries"] = locate_timeseries_file(work_dir).name
    except FileNotFoundError:
        pass
    try:
        velocity = locate_velocity_file(work_dir)
        out["velocity"] = velocity.name
        out["reference"] = read_reference_point(velocity)
    except (FileNotFoundError, OSError):
        pass
    return out


def _record_run(
    project_id: str, downloads_dir: Path, work_dir: Path, context: Optional[dict],
    custom_text: str, geometry_used: bool,
) -> None:
    """Write or refresh the project's provenance.json from the run's current state."""
    with _lock:
        state = _runs.get(project_id)
        run = {
            "status": state.status if state else None,
            "started_at": state.started_at if state else None,
            "finished_at": state.finished_at if state else None,
            "return_code": state.return_code if state else None,
            "attempt": state.attempt if state else None,
            "error": state.error if state else None,
            "warnings": list(state.warnings) if state else [],
        }
    custom = parse_mintpy_options(custom_text)
    names = _interferogram_names(downloads_dir)
    record = {
        **(context or {}),
        "mintpy_run": run,
        "software": software_versions(),
        "mintpy": {
            "custom_config": custom_text,
            # Everything not in custom_config comes from MintPy's own defaults.
            "options": mintpy_effective_options(custom_text),
        },
        "crop": {
            "applied": bool(geometry_used and custom.get("mintpy.subset.lalo")),
            "subset_lalo": custom.get("mintpy.subset.lalo"),
            "margin_fraction": _SUBSET_MARGIN_FRACTION,
            "min_margin_deg": _MIN_MARGIN_DEG,
        },
        "interferograms": {"count": len(names), "names": names},
        "results": _result_files(work_dir),
    }
    provenance_service.write_run_record(downloads_dir, record)


def _run_stage(
    project_id: str, work_dir: Path, started_at, attempt, adapter, config_path: Path,
    start_step: Optional[str] = None, end_step: Optional[str] = None, on_started=None,
) -> int:
    """Run MintPy between two steps and wait for it; returns its exit code."""
    process = adapter.start_full_pipeline(config_path=config_path, start_step=start_step, end_step=end_step)
    _write_marker(work_dir, project_id=project_id, started_at=started_at, attempt=attempt, pid=process.pid)
    with _lock:
        state = _runs[project_id]
        state.process = process
        cancelled = state.cancel_requested
    if on_started:
        on_started()
    if cancelled:
        # Cancelled while the process was being launched: stop it now.
        process.terminate()
    return process.wait()


def _user_chose_reference(custom_text: str) -> bool:
    """True when the user's own options fix the reference pixel, which is then left alone."""
    options = parse_mintpy_options(custom_text)
    return any(options.get(key, "no") not in ("no", "auto") for key in ("mintpy.reference.yx", "mintpy.reference.lalo"))


def _run(
    project_id: str, downloads_dir: Path, work_dir: Path, geometry: Optional[dict],
    context: Optional[dict] = None,
) -> None:
    adapter = MintPyAdapter(downloads_dir=downloads_dir, work_dir=work_dir)
    with _lock:
        attempt = _runs[project_id].attempt
        started_at = _runs[project_id].started_at
    _write_marker(work_dir, project_id=project_id, started_at=started_at, attempt=attempt, pid=None)
    # The config as this app wrote it. MintPy later overwrites that file with its own
    # full template (every option, most still "auto"), so it has to be read now.
    custom_text = ""
    try:
        adapter.unzip_all(geometry=geometry)
        if _cancel_requested(project_id):
            return
        extra_options, warnings = _troposphere_options()
        with _lock:
            _runs[project_id].warnings = warnings
        if warnings:
            logger.warning("MintPy for project %s: %s", project_id, warnings[0])
        config_path = adapter.write_config(geometry=geometry, extra_options=extra_options)
        custom_text = Path(config_path).read_text()
        # Two stages, so the reference pixel can be fixed in between (see
        # pin_reference_point). A stage that fails ends the run with its exit code.
        stage = functools.partial(_run_stage, project_id, work_dir, started_at, attempt, adapter, config_path)
        return_code = stage(
            end_step="reference_point",
            on_started=lambda: _safe_record(project_id, downloads_dir, work_dir, context, custom_text, geometry),
        )
        if return_code == 0 and not _cancel_requested(project_id):
            if not _user_chose_reference(custom_text):
                pinned = adapter.pin_reference_point()
                if pinned:
                    logger.info("MintPy for project %s: reference pixel moved %s -> %s",
                                project_id, pinned["previous"], pinned["chosen"])
            return_code = stage(start_step="quick_overview")
        if return_code != 0 and not _cancel_requested(project_id) and _stopped_by_clean_triplets(work_dir):
            logger.info("MintPy for project %s: resuming after the skipped triplet plot", project_id)
            with _lock:
                _runs[project_id].warnings = [*_runs[project_id].warnings, _CLEAN_TRIPLETS_NOTE]
            return_code = stage(start_step="correct_unwrap_error")
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
    finally:
        _clear_marker(work_dir)
        _safe_record(project_id, downloads_dir, work_dir, context, custom_text, geometry)


def _safe_record(*args) -> None:
    """A problem writing the record must never fail or hide the run itself."""
    try:
        _record_run(*args)
    except Exception:
        logger.exception("Could not write provenance.json for project %s", args[0])


# ── Recovery after the app stopped ───────────────────────────────────────────

def _stop_orphan(marker: dict, work_dir: Path) -> None:
    """End a MintPy process left over from before the restart, if one is still alive.

    Two MintPy processes in the same work directory would corrupt each other, so a
    survivor is stopped before the run is restarted. Only a process whose command
    line names this work directory is touched."""
    import psutil

    pid = marker.get("pid")
    if not pid or not psutil.pid_exists(pid):
        return
    try:
        process = psutil.Process(pid)
        command = " ".join(process.cmdline())
        if "smallbaselineApp" not in command or str(work_dir) not in command:
            return
        process.terminate()
        try:
            process.wait(timeout=10)
        except psutil.TimeoutExpired:
            process.kill()
    except psutil.Error:
        logger.warning("Could not stop leftover MintPy process %s", pid, exc_info=True)


def recover_interrupted_runs() -> dict:
    """Restart MintPy runs that were cut short, once, and report the rest.

    A project's work directory holding a run marker with no live run behind it
    means the app stopped mid-run. The first interruption is retried automatically
    (the run is safe to repeat: finished crops are skipped); a run interrupted
    again stays INTERRUPTED until someone starts it, so a run that brings the
    backend down cannot loop."""
    from app.config import settings
    from app.database import SessionLocal
    from app.models import Project

    outcome: dict = {"restarted": [], "gave_up": []}
    db = SessionLocal()
    try:
        for project in db.query(Project).filter(Project.archived_remote.is_(None)).all():
            folder = Path(project.storage_path) if project.storage_path else Path(settings.downloads_dir)
            work_dir = folder / "mintpy"
            marker = _read_marker(work_dir)
            if marker is None or marker.get("project_id") != project.id:
                continue
            with _lock:
                if project.id in _runs:
                    continue
            _stop_orphan(marker, work_dir)
            attempt = int(marker.get("attempt") or 1)
            if attempt >= MAX_AUTO_ATTEMPTS or not mintpy_available():
                outcome["gave_up"].append(project.id)
                continue
            try:
                start_run(
                    project.id, folder, geometry=project.geometry,
                    context=provenance_service.project_context(db, project), attempt=attempt + 1,
                )
                outcome["restarted"].append(project.id)
            except RuntimeError:
                logger.warning("Could not restart MintPy for project %s", project.id, exc_info=True)
                outcome["gave_up"].append(project.id)
    finally:
        db.close()
    return outcome
