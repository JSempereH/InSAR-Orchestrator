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

from insar_core.analysis import fading_correction, unwrap_correction
from insar_core.engines import get_engine
from insar_core.pipeline.mintpy_adapter import MintPyAdapter, era5_source, find_smallbaseline
from insar_core.pipeline import mintpy_export
from insar_core.pipeline.mintpy_qc import compute_qc
from insar_core.pipeline import mask_refinement
from insar_core.pipeline.mintpy_options import (
    FADING_KEY, MASK_CLOSURES_KEY, MASK_COHERENCE_KEY, options_for, reference_option, split_app_options, validate_options,
)
from insar_core.pipeline.mintpy_timeseries import (
    locate_timeseries_file, locate_velocity_file, read_point_time_series, read_reference_point,
    read_velocity_points,
)

from insar_core.pipeline.mintpy_adapter import _MIN_MARGIN_DEG, _SUBSET_MARGIN_FRACTION
from insar_core.pipeline.provenance import mintpy_effective_options, parse_mintpy_options, software_versions

from app.services import provenance_service, validation_service
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
    # Where the ERA5 weather files came from: "arco", "cds" or None (no correction).
    era5_source: Optional[str] = None
    # What the person asked for beyond the defaults: validated `mintpy.*` options and, when
    # they picked one, the reference point {"lat", "lon"}. Kept so a run restarted after an
    # interruption repeats the same request.
    options: dict = field(default_factory=dict)
    reference: Optional[dict] = None
    # Which source made the interferograms (insar_core.engines); decides how they are prepared and loaded.
    engine_id: str = "hyp3_gamma"


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
    options: Optional[dict] = None,
    reference: Optional[dict] = None,
    engine_id: str = "hyp3_gamma",
) -> dict:
    """Start a MintPy run in the background.

    `context` (the project, its batches and pairs; see provenance_service) is
    saved with the run in the project's provenance.json. `attempt` counts automatic
    restarts after an interruption and is 1 for a run someone asked for. `options`
    ({mintpy key: value}, see insar_core.pipeline.mintpy_options) and `reference`
    ({"lat", "lon"}) are what the person chose; a bad value is refused here, before
    anything starts."""
    try:
        engine = get_engine(engine_id)
        checked_options = validate_options(options, engine.info.id)
        reference_option(reference)
    except (ValueError, KeyError) as exc:
        raise RuntimeError(str(exc).strip("'\"")) from exc
    unavailable = engine.availability()
    if not unavailable.ok:
        raise RuntimeError(unavailable.reason)
    with _lock:
        existing = _runs.get(project_id)
        if existing is not None and existing.status == "RUNNING":
            raise RuntimeError("A MintPy run is already in progress for this project")
        if not mintpy_available():
            raise RuntimeError(
                "smallbaselineApp.py was not found on PATH. Install MintPy on this "
                "machine (pip install mintpy) to run SBAS analysis."
            )
        if not engine.has_inputs(downloads_dir):
            raise RuntimeError(f"No downloaded interferograms found in {downloads_dir}")

        work_dir = downloads_dir / "mintpy"
        state = MintPyRunState(
            project_id=project_id, work_dir=str(work_dir), attempt=attempt,
            options=checked_options, reference=dict(reference) if reference else None, engine_id=engine.info.id,
        )
        _runs[project_id] = state

    thread = threading.Thread(
        target=_run, args=(project_id, downloads_dir, work_dir, geometry, context), daemon=True,
    )
    thread.start()
    return get_status(project_id, downloads_dir)


def get_quality(downloads_dir: Path) -> dict:
    """Quality checks for this project's latest MintPy results, read from the files on disk."""
    work_dir = downloads_dir / "mintpy"
    if not (work_dir / "velocity.h5").exists():
        raise RuntimeError("There are no MintPy results to check yet")
    return compute_qc(work_dir)


def build_report(downloads_dir: Path, provenance: Optional[dict] = None) -> str:
    """The report of this project's latest MintPy results: what they are, how they were made, how far to trust them.

    One self-contained HTML document (see insar_core.pipeline.report). The validations are included
    only while they describe the current results: one made before MintPy ran again is left out."""
    from insar_core.pipeline import report

    work_dir = downloads_dir / "mintpy"
    if not (work_dir / "velocity.h5").exists():
        raise RuntimeError("There are no MintPy results to report on yet")
    validation = {}
    for kind in ("egms", "gnss"):
        result = validation_service._saved(downloads_dir, kind)
        newest = validation_service._results_mtime(downloads_dir)
        if result and not (newest is not None and newest > float(result.get("mintpy_results_mtime") or 0) + 1.0):
            validation[kind] = result
    try:
        return report.build_report(work_dir, provenance=provenance, validation=validation)
    except (FileNotFoundError, ValueError, OSError) as exc:
        raise RuntimeError(str(exc)) from exc


def export_results(
    downloads_dir: Path, fmt: str, layer: str = "velocity", masked: bool = True, series: bool = True,
) -> Path:
    """Write this project's MintPy results as `fmt` and return the file.

    Files go to `mintpy/exports/` and are reused until MintPy writes new results, so asking for
    the same export twice does not repeat the work (a GeoPackage of every pixel takes a while)."""
    work_dir = downloads_dir / "mintpy"
    if not (work_dir / "velocity.h5").exists():
        raise RuntimeError("There are no MintPy results to export yet")
    folder = work_dir / "exports"
    try:
        expected = mintpy_export.export_path(folder, fmt, layer, masked, series)
        calibrated = layer in ("velocity_calibrated", "velocity_std_calibrated")
        # A calibrated layer depends on the last validation too, so it is written again each time (it is one small file).
        # the mask can change after the velocity was written (the stricter mask of this app), and a masked export with it
        newest = max(p.stat().st_mtime for p in (work_dir / "velocity.h5", work_dir / "maskTempCoh.h5") if p.exists())
        if not calibrated and expected.exists() and expected.stat().st_mtime >= newest:
            return expected
        corrections = validation_service.corrections(downloads_dir) if calibrated else None
        return mintpy_export.export(work_dir, folder, fmt, layer=layer, masked=masked, include_series=series,
                                    corrections=corrections)
    except (mintpy_export.ExportError, FileNotFoundError, ValueError) as exc:
        raise RuntimeError(str(exc)) from exc


def describe_options(downloads_dir: Path, engine_id: str = "hyp3_gamma") -> dict:
    """The options the app lets a person change, with each default, and what the last run used."""
    engine = get_engine(engine_id)
    defaults = {**(mintpy_effective_options("") or {}), **engine.default_mintpy_options()}
    defaults[_UNWRAP_KEY] = "auto"            # the app's own choice: corrected when the network allows it
    available = options_for(engine.info.id)
    last = (provenance_service.read_run_record(downloads_dir) or {}).get("mintpy") or {}
    used = parse_mintpy_options(last.get("custom_config") or "")
    listed = [{k: v for k, v in option.items() if k != "engines"} | {"default": defaults.get(option["key"], "auto")} for option in available]
    reference = used.get("mintpy.reference.lalo")
    return {
        "options": listed,
        "last_run": {key: used[key] for key in (o["key"] for o in available) if key in used},
        "last_reference": reference if reference not in (None, "auto") else None,
    }


def get_time_series(downloads_dir: Path, lat: float, lon: float) -> dict:
    """Displacement time series for one point from this project's MintPy run.

    `work_dir` is computed the same way start_run does, so this works against
    whatever the most recent run for this project produced on disk - it does
    not depend on the in-memory `_runs` state, so it still works after a
    backend restart as long as the files are still there."""
    work_dir = downloads_dir / "mintpy"
    try:
        return with_analysis(read_point_time_series(work_dir, lat, lon), line_of_sight=True)
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


_NO_ERA5_WARNING = (
    "The ERA5 troposphere correction was left out because no weather data source works: "
    "the ARCO-ERA5 archive did not answer (no internet, or its packages are missing) and no "
    "Copernicus Climate Data Store login was found (~/.cdsapirc, or CDSAPI_KEY in the "
    "environment). MintPy would fail late with an unclear error. Run again once the "
    "archive is reachable to apply it."
)
_ERA5_SOURCES = {
    "arco": "ARCO-ERA5, the public copy of the Copernicus ERA5 reanalysis",
    "cds": "Copernicus Climate Data Store",
}


def _troposphere_options(user_options: Optional[dict] = None) -> tuple[dict[str, str], list[str], Optional[str]]:
    """Config overrides, warnings and the weather data source that follow from what this machine can do.

    MintPy's default troposphere correction is PyAPS with ERA5, which downloads one
    weather file per date. If neither source of those files works it finds none and
    stops with a division by zero (measured on a real run: it fails after the inversion,
    so the work done so far is wasted and the message does not say why). So the
    correction is turned off explicitly, and that is stated in the run's warnings,
    its provenance record and the status the UI shows, never silently.

    `user_options` are the person's own choices: with another method (or none) there is
    no weather data to fetch, so nothing is checked or changed."""
    chosen = "".join(f"{key} = {value}\n" for key, value in (user_options or {}).items())
    defaults = mintpy_effective_options(chosen)
    if not defaults or defaults.get("mintpy.troposphericDelay.method") != "pyaps":
        return {}, [], None
    source = era5_source()
    if source:
        return {}, [], source
    return {"mintpy.troposphericDelay.method": "no"}, [_NO_ERA5_WARNING], None


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
        weather_source = state.era5_source if state else None
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
            # Where the ERA5 weather data of the troposphere correction came from, if it ran.
            "weather_data": _ERA5_SOURCES.get(weather_source),
        },
        "crop": {
            "applied": bool(geometry_used and custom.get("mintpy.subset.lalo")),
            "subset_lalo": custom.get("mintpy.subset.lalo"),
            "margin_fraction": _SUBSET_MARGIN_FRACTION,
            "min_margin_deg": _MIN_MARGIN_DEG,
        },
        "interferograms": {"count": len(names), "names": names},
        "results": _result_files(work_dir),
        # Quality checks on the finished run, so the record says how far the result can be trusted.
        "quality": _quality(work_dir) if run["status"] == "SUCCEEDED" else None,
    }
    provenance_service.write_run_record(downloads_dir, record)


def _quality(work_dir: Path) -> Optional[dict]:
    try:
        report = compute_qc(work_dir)
    except Exception:
        logger.warning("Could not compute the quality checks for %s", work_dir, exc_info=True)
        return None
    return {"status": report["status"], "checks": report["checks"], "metrics": report["metrics"]}


def _run_stage(
    project_id: str, work_dir: Path, started_at, attempt, adapter, config_path: Path,
    start_step: Optional[str] = None, end_step: Optional[str] = None, on_started=None,
) -> int:
    """Run MintPy between two steps and wait for it; returns its exit code."""
    process = adapter.start_full_pipeline(config_path=config_path, start_step=start_step, end_step=end_step)
    _write_marker(work_dir, project_id=project_id, started_at=started_at, attempt=attempt, pid=process.pid,
                  **_requested(project_id))
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


_UNWRAP_KEY = "mintpy.unwrapError.method"


def _with_option(text: str, key: str, value: str) -> str:
    """The config `text` with `key` set to `value`, replacing its line or adding one."""
    import re

    line = f"{key.ljust(26)} = {value}"
    changed, count = re.subn(rf"^{re.escape(key)}\s*=[^#\n]*", line + "  ", text, flags=re.MULTILINE)
    return changed if count else text.rstrip("\n") + "\n" + line + "\n"


def _correct_unwrapping(work_dir: Path, config_path: Path, explicit: bool) -> Optional[str]:
    """Correct the unwrapping errors of the loaded stack and point MintPy's inversion at the corrected phases.

    Returns a sentence for the person when the correction they asked for could not be made. When it
    was left to the app (`explicit` False) and the network does not allow it, nothing is said: the
    run goes on with the phases as they are, as it did before this step existed."""
    try:
        summary = unwrap_correction.correct_stack(work_dir)
    except unwrap_correction.UnwrapCorrectionError as exc:
        return f"The unwrapping errors were not corrected. {exc}" if explicit else None
    except Exception:
        logger.exception("Unwrapping error correction failed in %s", work_dir)
        return "The unwrapping errors were not corrected: the correction stopped with an error (see the backend log)."
    config_path.write_text(_with_option(config_path.read_text(), _UNWRAP_KEY, "phase_closure"))
    logger.info("Unwrapping correction in %s: %s", work_dir, summary["pixels"])
    return None


def _correct_fading(work_dir: Path, config_path: Path) -> Optional[str]:
    """Remove the fading bias from the stack and point MintPy's inversion at the corrected phases.

    Returns a sentence for the person when it could not be done (the run goes on without it)."""
    source = fading_correction.OUTPUT_DATASET if (work_dir / unwrap_correction.SUMMARY_FILE).exists() else "unwrapPhase"
    try:
        summary = fading_correction.correct_stack(work_dir, source=source)
    except fading_correction.FadingCorrectionError as exc:
        return f"The fading bias was not corrected. {exc}"
    except Exception:
        logger.exception("Fading bias correction failed in %s", work_dir)
        return "The fading bias was not corrected: the correction stopped with an error (see the backend log)."
    config_path.write_text(_with_option(config_path.read_text(), _UNWRAP_KEY, "phase_closure"))
    logger.info("Fading bias correction in %s: a1 %.2f, a2 %.2f", work_dir, summary["a1"], summary["a2"])
    return None


def _refine_mask(work_dir: Path, app_options: dict[str, str]) -> Optional[str]:
    """Apply the stricter mask to a finished run. Returns a sentence when what was asked could not be done."""
    closures = app_options.get(MASK_CLOSURES_KEY, "auto")
    coherence = app_options.get(MASK_COHERENCE_KEY)
    if closures == "auto":
        # With long interferograms in the network, the pixels whose short triplets are noisy are constrained by them and
        # come out as good as the rest (measured against EGMS), so dropping them only loses pixels: left to the person.
        account = unwrap_correction.read_summary(work_dir) or {}
        if account.get("interferograms_in_the_stack", 0) > account.get("interferograms", 0):
            closures = "no"
    if closures == "no" and coherence is None:
        return None
    if not (work_dir / mask_refinement.MASK_FILE).exists():
        return None
    corrected = (work_dir / unwrap_correction.COUNT_FILE).exists()
    if coherence is None and not corrected:
        # nothing to mask by: the mask stays MintPy's, and no account of a refinement is written
        return ("Pixels whose triplets do not close were not dropped: that needs the unwrapping error correction, which "
                "did not run.") if closures == "yes" else None
    try:
        mask_refinement.refine(work_dir, drop_unreadable_closures=closures != "no" and corrected,
                               min_mean_coherence=float(coherence) if coherence is not None else None)
    except (mask_refinement.MaskError, OSError, ValueError) as exc:
        return f"The stricter mask was not applied. {exc}"
    return None


def _requested(project_id: str) -> dict:
    """What the person asked for, in the form the run marker keeps it."""
    with _lock:
        state = _runs.get(project_id)
        return {"options": dict(state.options), "reference": state.reference, "engine_id": state.engine_id} if state else {}


def _user_chose_reference(custom_text: str) -> bool:
    """True when the user's own options fix the reference pixel, which is then left alone."""
    options = parse_mintpy_options(custom_text)
    return any(options.get(key, "no") not in ("no", "auto") for key in ("mintpy.reference.yx", "mintpy.reference.lalo"))


def _run(
    project_id: str, downloads_dir: Path, work_dir: Path, geometry: Optional[dict],
    context: Optional[dict] = None,
) -> None:
    with _lock:
        engine_id = _runs[project_id].engine_id
    adapter = MintPyAdapter(downloads_dir=downloads_dir, work_dir=work_dir, engine=get_engine(engine_id))
    with _lock:
        attempt = _runs[project_id].attempt
        started_at = _runs[project_id].started_at
    _write_marker(work_dir, project_id=project_id, started_at=started_at, attempt=attempt, pid=None,
                  **_requested(project_id))
    # The config as this app wrote it. MintPy later overwrites that file with its own
    # full template (every option, most still "auto"), so it has to be read now.
    custom_text = ""
    try:
        adapter.prepare_inputs(geometry=geometry)
        if _cancel_requested(project_id):
            return
        with _lock:
            user_options = dict(_runs[project_id].options)
            user_reference = _runs[project_id].reference
        user_options, app_options = split_app_options(user_options)
        tropo_options, warnings, weather_source = _troposphere_options(user_options)
        # The checks on this machine come last so that they win over a person's choice
        # that cannot work here (ERA5 asked for with no weather data source).
        extra_options = {**adapter.engine.default_mintpy_options(), **user_options, **reference_option(user_reference), **tropo_options}
        # The unwrapping errors are corrected by this app between two MintPy stages (see _correct_unwrapping),
        # not by MintPy's own step, which needs layers the HyP3 products do not have. MintPy is told "no" until
        # the corrected phases exist.
        unwrap_method = extra_options.get(_UNWRAP_KEY, "auto")
        extra_options[_UNWRAP_KEY] = "no"
        for name in (unwrap_correction.SUMMARY_FILE, unwrap_correction.COUNT_FILE, fading_correction.SUMMARY_FILE):
            (work_dir / name).unlink(missing_ok=True)
        mask_refinement.restore(work_dir)
        adapter.era5_source = weather_source
        with _lock:
            _runs[project_id].warnings = warnings
            _runs[project_id].era5_source = weather_source
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
            return_code = stage(start_step="quick_overview", end_step="quick_overview")
            if return_code != 0 and not _cancel_requested(project_id) and _stopped_by_clean_triplets(work_dir):
                logger.info("MintPy for project %s: going on after the skipped triplet plot", project_id)
                with _lock:
                    _runs[project_id].warnings = [*_runs[project_id].warnings, _CLEAN_TRIPLETS_NOTE]
                return_code = 0
        if return_code == 0 and not _cancel_requested(project_id):
            if unwrap_method != "no":
                note = _correct_unwrapping(work_dir, Path(config_path), explicit=unwrap_method != "auto")
                if note:
                    with _lock:
                        _runs[project_id].warnings = [*_runs[project_id].warnings, note]
                elif (work_dir / unwrap_correction.SUMMARY_FILE).exists():
                    custom_text = _with_option(custom_text, _UNWRAP_KEY, "phase_closure")
            if app_options.get(FADING_KEY) == "yes" and not _cancel_requested(project_id):
                note = _correct_fading(work_dir, Path(config_path))
                if note:
                    with _lock:
                        _runs[project_id].warnings = [*_runs[project_id].warnings, note]
                elif (work_dir / fading_correction.SUMMARY_FILE).exists():
                    custom_text = _with_option(custom_text, _UNWRAP_KEY, "phase_closure")
            if not _cancel_requested(project_id):
                # MintPy's own correct_unwrap_error step is passed over: the correction is the one above
                return_code = stage(start_step="invert_network")
        if return_code == 0 and not _cancel_requested(project_id):
            note = _refine_mask(work_dir, app_options)
            if note:
                with _lock:
                    _runs[project_id].warnings = [*_runs[project_id].warnings, note]
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
                    options=marker.get("options"), reference=marker.get("reference"),
                    engine_id=marker.get("engine_id") or project.engine or "hyp3_gamma",
                )
                outcome["restarted"].append(project.id)
            except RuntimeError:
                logger.warning("Could not restart MintPy for project %s", project.id, exc_info=True)
                outcome["gave_up"].append(project.id)
    finally:
        db.close()
    return outcome
