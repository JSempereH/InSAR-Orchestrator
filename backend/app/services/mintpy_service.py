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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from insar_core.pipeline.mintpy_adapter import MintPyAdapter

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


def get_status(project_id: str) -> dict:
    with _lock:
        state = _runs.get(project_id)
    if state is None:
        return {"status": "NOT_STARTED"}
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
    }


def start_run(project_id: str, downloads_dir: Path) -> dict:
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

    thread = threading.Thread(target=_run, args=(project_id, downloads_dir, work_dir), daemon=True)
    thread.start()
    return get_status(project_id)


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


def _run(project_id: str, downloads_dir: Path, work_dir: Path) -> None:
    adapter = MintPyAdapter(downloads_dir=downloads_dir, work_dir=work_dir)
    try:
        adapter.unzip_all()
        config_path = adapter.write_config()
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
