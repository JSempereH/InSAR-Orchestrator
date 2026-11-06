"""Backups of the app's data to any rclone destination, on demand or on a schedule.

What is copied, in this order:

1. Every project folder (except projects switched off for backup), to `<destination>/projects/<name>-<id>/`.
2. A consistent snapshot of the SQLite database, to
   `<destination>/database/insar_app-<timestamp>.db`.

Files go first and the database last on purpose. A database that is newer than
the files would, once restored, point at files that are not in the backup; files
that are newer than the database only leave harmless extra files behind. So if
any project fails to copy, the database snapshot is skipped for that run.

Only `rclone copy` is used, which never deletes at the destination: a problem on
this machine cannot erase the backup. The one exception is the pruning of old
*database snapshots*, which follows the retention settings.

Left out of the file backup, because they can be regenerated:
  - `mintpy/`   MintPy's working directory (rerun MintPy to rebuild it)
  - `slc/`      raw Sentinel-1 scenes: they cost no HyP3 credits (ASF serves them
                for free) but are several GB each, so they are opt-in
  - hidden folders (extraction staging)

`backend/.env` is never uploaded. It holds the key to the stored credentials,
and putting it next to the database would defeat that encryption.
"""
from __future__ import annotations

import logging
import re
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from app.services import download_state, rclone_service, storage_move

logger = logging.getLogger(__name__)

STATE_ID = "backup"

_BASE_EXCLUDES = ("mintpy/**", ".*/**")
_SLC_EXCLUDE = "slc/**"
_SNAPSHOT_RE = re.compile(r"^insar_app-(\d{8})-(\d{6})\.db$")

_run_lock = threading.Lock()


class BackupBusy(RuntimeError):
    """A backup (or a data move) is already running."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "project"


# ── Retention ────────────────────────────────────────────────────────────────

def snapshot_time(name: str) -> Optional[datetime]:
    match = _SNAPSHOT_RE.match(name)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def select_snapshots_to_delete(names: list[str], keep_daily: int, keep_weekly: int) -> list[str]:
    """Names to prune. Keeps the newest snapshot of each of the `keep_daily` most
    recent days and of each of the `keep_weekly` most recent ISO weeks, plus the
    newest snapshot overall. Names that are not ours are never selected."""
    parsed = [(snapshot_time(n), n) for n in names]
    dated = sorted(((when, n) for when, n in parsed if when is not None), reverse=True)
    keep: set[str] = set()
    days: set = set()
    weeks: set = set()
    for index, (when, name) in enumerate(dated):
        day = when.date()
        week = tuple(when.isocalendar())[:2]
        if index == 0:
            keep.add(name)
        if day not in days and len(days) < keep_daily:
            days.add(day)
            keep.add(name)
        if week not in weeks and len(weeks) < keep_weekly:
            weeks.add(week)
            keep.add(name)
    return [name for _when, name in dated if name not in keep]


# ── Config ───────────────────────────────────────────────────────────────────

def get_config(db):
    from app.models import BackupConfig

    config = db.query(BackupConfig).filter_by(id="default").first()
    if config is None:
        config = BackupConfig(id="default")
        db.add(config)
        db.commit()
        db.refresh(config)
    return config


def _record_result(status: str, message: str) -> None:
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        config = get_config(db)
        config.last_run_at = _utcnow()
        config.last_status = status
        config.last_message = message[:2000]
        db.commit()
    finally:
        db.close()


# ── Running a backup ─────────────────────────────────────────────────────────

def is_running() -> bool:
    return _run_lock.locked()


def acquire_exclusive() -> bool:
    """Take the lock shared by backups, archiving and restoring (one heavy
    transfer at a time). Returns False if it is already held. The caller may
    release it from another thread."""
    return _run_lock.acquire(blocking=False)


def release_exclusive() -> None:
    _run_lock.release()


def get_state() -> dict:
    state = download_state.get(STATE_ID)
    return {
        "running": is_running(),
        "status": state.get("status", "idle"),
        "step": state.get("step"),
        "pct": state.get("pct", 0),
        "error": state.get("error"),
    }


def _project_folders() -> list[tuple[str, Path]]:
    """(destination folder name, local folder) for every project whose folder exists."""
    from app.config import settings
    from app.database import SessionLocal
    from app.models import Project

    db = SessionLocal()
    try:
        rows = (
            db.query(Project.id, Project.name, Project.storage_path)
            .filter(Project.archived_remote.is_(None), Project.backup_enabled.is_(True))
            .all()
        )
    finally:
        db.close()
    folders = []
    for project_id, name, storage_path in rows:
        folder = Path(storage_path) if storage_path else Path(settings.downloads_dir) / _slug(name)
        if folder.is_dir():
            folders.append((f"{_slug(name)}-{project_id[:8]}", folder))
    return folders


def run_backup(destination: str, keep_daily: int = 14, keep_weekly: int = 8, include_slc: bool = False) -> dict:
    """Back everything up to `destination`. Blocks until done; raises on failure.

    Raises BackupBusy if another backup or a data move is running. Whatever the
    outcome, it is recorded in the backup config (last_status / last_message).
    """
    if not _run_lock.acquire(blocking=False):
        raise BackupBusy("A backup is already running.")
    try:
        if storage_move.is_busy():
            raise BackupBusy("A data move is in progress; the backup will retry later.")
        started = time.monotonic()
        download_state.update(STATE_ID, status="running", step="checking destination", pct=0, error=None)
        try:
            summary = _run_backup_locked(destination, keep_daily, keep_weekly, include_slc)
        except Exception as exc:
            download_state.update(STATE_ID, status="error", error=str(exc))
            _record_result("error", str(exc))
            logger.exception("Backup to %s failed", destination)
            raise
        summary["seconds"] = round(time.monotonic() - started, 1)
        download_state.update(STATE_ID, status="done", step=None, pct=100)
        _record_result("ok", _describe(summary))
        logger.info("Backup to %s complete: %s", destination, _describe(summary))
        return summary
    finally:
        _run_lock.release()


def _describe(summary: dict) -> str:
    return (
        f"{summary['projects']} project folder(s) copied, database snapshot "
        f"{summary['snapshot']}, {summary['pruned']} old snapshot(s) pruned"
    )


def _run_backup_locked(destination: str, keep_daily: int, keep_weekly: int, include_slc: bool) -> dict:
    from app.backup import backup_database

    dest = rclone_service.validate_destination(destination)
    folders = _project_folders()
    excludes = _BASE_EXCLUDES if include_slc else (*_BASE_EXCLUDES, _SLC_EXCLUDE)

    failures: list[str] = []
    for index, (label, folder) in enumerate(folders):
        base_pct = index / (len(folders) + 1) * 100
        span = 100 / (len(folders) + 1)
        download_state.update(STATE_ID, step=f"project {label}", pct=round(base_pct, 1))

        def on_progress(done: int, total: int, _speed: float, _eta, base=base_pct, span=span) -> None:
            frac = done / total if total else 0
            download_state.update(STATE_ID, pct=round(base + frac * span, 1))

        try:
            rclone_service.copy(
                str(folder), rclone_service.join(dest, "projects", label),
                excludes=excludes, on_progress=on_progress,
            )
        except rclone_service.RcloneError as exc:
            logger.error("Backup of project folder %s failed: %s", folder, exc)
            failures.append(f"{label}: {exc}")

    if failures:
        raise rclone_service.RcloneError(
            "Project files failed to copy, so the database snapshot was not uploaded "
            "(it would reference files missing from the backup):\n" + "\n".join(failures)
        )

    download_state.update(STATE_ID, step="database snapshot", pct=round(len(folders) / (len(folders) + 1) * 100, 1))
    db_dir = rclone_service.join(dest, "database")
    with tempfile.TemporaryDirectory(prefix="insar-backup-") as tmp:
        snapshot = backup_database(Path(tmp))
        rclone_service.copyto(str(snapshot), rclone_service.join(db_dir, snapshot.name))

    pruned = 0
    for name in select_snapshots_to_delete(rclone_service.list_files(db_dir), keep_daily, keep_weekly):
        try:
            rclone_service.delete_file(rclone_service.join(db_dir, name))
            pruned += 1
        except rclone_service.RcloneError:
            logger.warning("Could not prune old snapshot %s", name, exc_info=True)

    return {"projects": len(folders), "snapshot": snapshot.name, "pruned": pruned}


def start_in_background(destination: str, keep_daily: int, keep_weekly: int, include_slc: bool = False) -> None:
    """Run a backup in a daemon thread. Raises BackupBusy immediately if one is running."""
    if is_running() or storage_move.is_busy():
        raise BackupBusy("A backup or a data move is already running.")

    def _target() -> None:
        try:
            run_backup(destination, keep_daily, keep_weekly, include_slc)
        except BackupBusy:
            pass
        except Exception:
            pass  # already logged and recorded by run_backup

    threading.Thread(target=_target, daemon=True).start()


# ── Scheduler ────────────────────────────────────────────────────────────────

_CHECK_INTERVAL_SECONDS = 60


def is_due(enabled: bool, destination: Optional[str], interval_hours: int, last_run_at: Optional[datetime], now: datetime) -> bool:
    if not enabled or not destination:
        return False
    if last_run_at is None:
        return True
    return now - last_run_at >= timedelta(hours=interval_hours)


def run_if_due() -> bool:
    """One scheduler tick: run a backup if the configured interval has elapsed."""
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        config = get_config(db)
        due = is_due(config.enabled, config.destination, config.interval_hours, config.last_run_at, _utcnow())
        destination, keep_daily, keep_weekly = config.destination, config.keep_daily, config.keep_weekly
        include_slc = bool(config.include_slc)
    finally:
        db.close()
    if not due:
        return False
    try:
        run_backup(destination, keep_daily, keep_weekly, include_slc)
    except BackupBusy as exc:
        logger.info("Scheduled backup postponed: %s", exc)
        return False
    except Exception:
        return False  # run_backup logged it and recorded the failure
    return True


async def backup_scheduler() -> None:
    """Background task: check once a minute whether a scheduled backup is due.

    A failed run is recorded like a successful one, so it is retried after the
    normal interval rather than every minute.
    """
    import asyncio

    while True:
        await asyncio.sleep(_CHECK_INTERVAL_SECONDS)
        try:
            await asyncio.get_running_loop().run_in_executor(None, run_if_due)
        except Exception:
            logger.exception("Unhandled error in backup scheduler")
