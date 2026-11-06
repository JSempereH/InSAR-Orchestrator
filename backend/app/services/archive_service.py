"""Archive a project's files to an rclone destination, and restore them later.

Archiving frees local disk without losing anything:

1. Copy the whole project folder (everything, MintPy and SLC included, because
   the local copy is about to be deleted) to `<base>/archive/<name>-<id>`.
2. Verify the copy with `rclone check` against the local files.
3. Check the local files did not change while that ran.
4. Only then record the project as archived in the database, and delete the
   local folder.

Any failure before step 4 leaves the local data exactly as it was. The database
is updated *before* the delete, so a crash in between leaves a project flagged
as archived whose files are still on disk, which is harmless, rather than files
deleted from a project that still claims to have them.

Restoring copies the folder back, verifies it, and clears the flag. The copy at
the destination is left in place, so a restore never removes anything remotely.

Archive, restore and backup share one lock: only one heavy transfer runs at a
time, and none while a data move is running.
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import HTTPException

from app.services import backup_service, download_state, rclone_service, storage_move

logger = logging.getLogger(__name__)

STATE_ID = "archive"


class ArchiveError(RuntimeError):
    """The operation was refused or failed. The message is safe to show the user."""


_last: dict = {"project_id": None, "kind": None}
_last_lock = threading.Lock()


def reject_if_archived(project) -> None:
    """Endpoints that need the project's files on local disk call this first."""
    if project.archived_remote:
        raise HTTPException(
            409,
            "This project is archived. Restore it from the Downloads page to use it again.",
        )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def fingerprint(folder: Path) -> tuple[int, int, int]:
    """(file count, total bytes, newest mtime in ns): changes if anything is
    added, removed, resized or rewritten."""
    count = total = newest = 0
    for root, _dirs, files in os.walk(folder):
        for name in files:
            try:
                st = os.stat(os.path.join(root, name))
            except OSError:
                continue
            count += 1
            total += st.st_size
            newest = max(newest, st.st_mtime_ns)
    return count, total, newest


def remote_path(base: str, project) -> str:
    from app.services.backup_service import _slug

    return rclone_service.join(base, "archive", f"{_slug(project.name)}-{project.id[:8]}")


def get_state() -> dict:
    with _last_lock:
        project_id, kind = _last["project_id"], _last["kind"]
    if project_id is None:
        return {"project_id": None, "kind": None, "active": False, "status": "idle", "step": None, "pct": 0, "error": None}
    state = download_state.get(STATE_ID)
    return {
        "project_id": project_id,
        "kind": kind,
        "active": state.get("status") == "running",
        "status": state.get("status", "idle"),
        "step": state.get("step"),
        "pct": state.get("pct", 0),
        "error": state.get("error"),
    }


def _safe_local_folder(project) -> Path:
    from app.config import settings

    if not project.storage_path:
        raise ArchiveError(
            "This project has no folder of its own (it uses the shared downloads folder). "
            "Move it to a disk from the Downloads page first, which gives it one."
        )
    folder = Path(project.storage_path)
    resolved = folder.resolve()
    if (
        resolved == Path(resolved.anchor)
        or resolved == Path(settings.downloads_dir).resolve()
        or os.path.ismount(resolved)
    ):
        raise ArchiveError(f"Refusing to archive {folder}: it is not a project-specific folder.")
    return folder


def _refuse_if_project_is_busy(db, project) -> None:
    from app.models import Batch, Job, JobStatus
    from app.services import download_queue, mintpy_service, slc_download_queue

    active_jobs = (
        db.query(Job.id)
        .join(Batch, Batch.id == Job.batch_id)
        .filter(Batch.project_id == project.id, Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]))
        .count()
    )
    if active_jobs:
        raise ArchiveError(f"{active_jobs} HyP3 job(s) are still processing; wait for them to finish first.")
    if download_queue.get_state()["active"] or slc_download_queue.get_state()["active"]:
        raise ArchiveError("A download is in progress; wait for it to finish first.")
    if mintpy_service.get_status(project.id).get("status") == "RUNNING":
        raise ArchiveError("MintPy is running for this project; wait for it to finish first.")


def start_archive(db, project, base_destination: str) -> None:
    """Validate, then archive in a background thread. Raises ArchiveError / BackupBusy."""
    if project.archived_remote:
        raise ArchiveError("This project is already archived.")
    folder = _safe_local_folder(project)
    if not folder.is_dir():
        raise ArchiveError(f"The project folder {folder} does not exist, so there is nothing to archive.")
    _refuse_if_project_is_busy(db, project)
    try:
        base = rclone_service.validate_destination(base_destination)
    except rclone_service.RcloneError as exc:
        raise ArchiveError(str(exc)) from exc
    _start(project.id, "archive", _archive, folder, remote_path(base, project))


def start_restore(project) -> None:
    if not project.archived_remote:
        raise ArchiveError("This project is not archived.")
    if not project.storage_path:
        raise ArchiveError("This project has no storage path to restore into.")
    if not rclone_service.is_available():
        raise ArchiveError("rclone is not installed on the backend machine.")
    _start(project.id, "restore", _restore, Path(project.storage_path), project.archived_remote)


def _start(project_id: str, kind: str, target, folder: Path, remote: str) -> None:
    if storage_move.is_busy() or not backup_service.acquire_exclusive():
        raise backup_service.BackupBusy("A backup, move, archive or restore is already running.")
    download_state.update(STATE_ID, status="running", step="starting", pct=0, error=None)
    with _last_lock:
        _last.update(project_id=project_id, kind=kind)

    def _run() -> None:
        try:
            target(project_id, folder, remote)
            download_state.update(STATE_ID, status="done", step=None, pct=100)
        except Exception as exc:
            logger.exception("%s of project %s failed", kind, project_id)
            download_state.update(STATE_ID, status="error", error=str(exc))
        finally:
            backup_service.release_exclusive()

    threading.Thread(target=_run, daemon=True).start()


def _progress(base: float, span: float):
    def cb(done: int, total: int, _speed: float, _eta) -> None:
        frac = done / total if total else 0
        download_state.update(STATE_ID, pct=round(base + frac * span, 1))
    return cb


def _set_flag(project_id: str, remote: Optional[str]) -> None:
    from app.database import SessionLocal
    from app.models import Project

    db = SessionLocal()
    try:
        project = db.query(Project).filter_by(id=project_id).first()
        if project is None:
            raise ArchiveError("The project no longer exists.")
        project.archived_remote = remote
        project.archived_at = _utcnow() if remote else None
        db.commit()
    finally:
        db.close()


def _archive(project_id: str, folder: Path, remote: str) -> None:
    before = fingerprint(folder)

    download_state.update(STATE_ID, step="copying", pct=0)
    # min_age="off": a file modified a moment ago must still be copied, because
    # the local copy is going to be deleted.
    rclone_service.copy(str(folder), remote, min_age="off", on_progress=_progress(0, 70))

    download_state.update(STATE_ID, step="verifying", pct=70)
    rclone_service.check(str(folder), remote)

    if fingerprint(folder) != before:
        raise ArchiveError(
            "Files in the project folder changed while it was being archived. "
            "Nothing was deleted; try again when nothing is writing to it."
        )

    download_state.update(STATE_ID, step="removing local files", pct=90)
    _set_flag(project_id, remote)
    shutil.rmtree(folder)
    logger.info("Archived project %s to %s", project_id, remote)


def _restore(project_id: str, folder: Path, remote: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)

    download_state.update(STATE_ID, step="copying", pct=0)
    rclone_service.copy(remote, str(folder), min_age="off", on_progress=_progress(0, 80))

    download_state.update(STATE_ID, step="verifying", pct=80)
    rclone_service.check(remote, str(folder))

    _set_flag(project_id, None)
    logger.info("Restored project %s from %s", project_id, remote)


def wait_idle(timeout: float = 30.0) -> bool:
    """Test helper: block until the archive/restore thread finishes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if download_state.get(STATE_ID).get("status") != "running":
            return True
        time.sleep(0.05)
    return False
