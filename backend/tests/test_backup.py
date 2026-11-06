import shutil
import sqlite3

import pytest

from app.backup import backup_database


def _wal_database(path, rows: int):
    """A database in WAL mode whose recent writes are still in the -wal file."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.execute("PRAGMA wal_autocheckpoint=0")  # as on a busy server: nothing is folded back yet
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
    conn.commit()
    return conn  # kept open on purpose: the app is "running"


def test_backup_contains_writes_that_are_still_in_the_wal(tmp_path):
    db = tmp_path / "app.db"
    live = _wal_database(db, rows=100)
    try:
        backup = backup_database(tmp_path / "backups", f"sqlite:///{db}")
        restored = sqlite3.connect(backup)
        assert restored.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 100
        restored.close()
    finally:
        live.close()


def test_a_plain_copy_would_have_missed_them(tmp_path):
    """Why backup_database exists: copying only the main file loses the WAL."""
    db = tmp_path / "app.db"
    live = _wal_database(db, rows=100)
    try:
        shutil.copy(db, tmp_path / "plain.db")
        plain = sqlite3.connect(tmp_path / "plain.db")
        with pytest.raises(sqlite3.OperationalError):
            plain.execute("SELECT COUNT(*) FROM t").fetchone()
        plain.close()
    finally:
        live.close()


def test_backup_creates_the_destination_and_a_timestamped_name(tmp_path):
    db = tmp_path / "app.db"
    _wal_database(db, rows=1).close()
    backup = backup_database(tmp_path / "nested" / "backups", f"sqlite:///{db}")
    assert backup.parent == tmp_path / "nested" / "backups"
    assert backup.name.startswith("insar_app-") and backup.suffix == ".db"


def test_backup_refuses_a_non_sqlite_database(tmp_path):
    with pytest.raises(ValueError, match="SQLite"):
        backup_database(tmp_path, "postgresql://user@host/db")


def test_backup_reports_a_missing_database(tmp_path):
    with pytest.raises(FileNotFoundError, match="No database found"):
        backup_database(tmp_path, f"sqlite:///{tmp_path / 'missing.db'}")
