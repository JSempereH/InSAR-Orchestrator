"""A safe snapshot of the app's SQLite database.

    cd backend && .venv/bin/python -m app.backup ../backups

To copy everything (project folders and a database snapshot) to an rclone
destination instead, for example from cron or a systemd timer:

    cd backend && .venv/bin/python -m app.backup --to s3backup:my-bucket/insar
    cd backend && .venv/bin/python -m app.backup --to /mnt/backup/insar

The database runs in WAL mode, where recent writes live in a separate `-wal`
file until SQLite folds them back in. Copying `insar_app.db` with `cp` can
therefore miss them (or be unreadable) while the app is running. SQLite's
online backup API takes a consistent snapshot instead, with the app running or
not, and that is what this does.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

_PREFIX = "sqlite:///"


def backup_database(dest_dir: Path, database_url: Optional[str] = None) -> Path:
    """Write a timestamped copy of the database into `dest_dir` and return its path."""
    if database_url is None:
        from app.config import settings

        database_url = settings.database_url
    if not database_url.startswith(_PREFIX):
        raise ValueError("Only SQLite databases can be backed up with this command.")

    source = Path(database_url[len(_PREFIX):])
    if not source.exists():
        raise FileNotFoundError(f"No database found at {source.resolve()}. Has the app run yet?")

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"insar_app-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"

    src_conn = sqlite3.connect(source)
    try:
        dst_conn = sqlite3.connect(dest)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()
    return dest


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.backup", description="Back up the database.")
    parser.add_argument("dest_dir", nargs="?", default="../backups", help="Local folder for a database-only snapshot")
    parser.add_argument("--to", metavar="DESTINATION", help="Back up projects and database to this rclone destination")
    parser.add_argument("--keep-daily", type=int, default=14)
    parser.add_argument("--keep-weekly", type=int, default=8)
    parser.add_argument("--include-slc", action="store_true", help="Also copy raw SLC scenes (large, free to re-download)")
    args = parser.parse_args(argv[1:])

    if args.to:
        from app.services import backup_service, rclone_service

        try:
            summary = backup_service.run_backup(args.to, args.keep_daily, args.keep_weekly, args.include_slc)
        except (backup_service.BackupBusy, rclone_service.RcloneError, FileNotFoundError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"Backed up {summary['projects']} project folder(s) and {summary['snapshot']} to {args.to}")
        print("The encryption key in backend/.env is NOT included: keep a copy of it somewhere else.")
        return 0

    try:
        dest = backup_database(Path(args.dest_dir))
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote {dest} ({dest.stat().st_size / 1e6:.1f} MB)")
    print("Also keep a copy of backend/.env: it holds the key that decrypts the stored credentials.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
