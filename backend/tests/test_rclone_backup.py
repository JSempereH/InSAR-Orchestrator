"""Backups through rclone, run against tests/fake_rclone.py (no network, no real rclone)."""
from datetime import datetime

import pytest

from app.config import settings
from app.database import SessionLocal
from app.models import Project
from app.services import backup_service, rclone_service, storage_move


@pytest.fixture()
def project_folder(tmp_path, client):
    """A real project row whose folder holds a cropped result plus files that must be skipped."""
    folder = tmp_path / "proj-files"
    (folder / "ifg_1").mkdir(parents=True)
    (folder / "ifg_1" / "unw.tif").write_bytes(b"data")
    (folder / "mintpy").mkdir()
    (folder / "mintpy" / "big.h5").write_bytes(b"regenerable")
    (folder / "slc").mkdir()
    (folder / "slc" / "raw.zip").write_bytes(b"raw")
    db = SessionLocal()
    project = Project(name="Bridge Site", geometry={}, storage_path=str(folder))
    db.add(project)
    db.commit()
    prefix = project.id[:8]
    db.close()
    yield folder, f"bridge-site-{prefix}"
    db = SessionLocal()
    db.query(Project).filter(Project.id.like(f"{prefix}%")).delete(synchronize_session=False)
    db.commit()
    db.close()


# ── retention ────────────────────────────────────────────────────────────────

def _names(*stamps):
    return [f"insar_app-{s}.db" for s in stamps]


def test_retention_keeps_newest_per_day_and_per_week():
    names = _names(
        "20260105-020000", "20260105-180000",  # same day: only the later one counts
        "20260104-020000", "20260103-020000",
        "20251201-020000",                      # old, outside both windows
    )
    deleted = backup_service.select_snapshots_to_delete(names, keep_daily=2, keep_weekly=1)
    assert sorted(deleted) == sorted(_names("20260105-020000", "20260103-020000", "20251201-020000"))


def test_retention_always_keeps_the_newest_and_ignores_foreign_files():
    names = _names("20260105-020000") + ["notes.txt", "insar_app-bad.db"]
    assert backup_service.select_snapshots_to_delete(names, keep_daily=1, keep_weekly=0) == []
    assert backup_service.select_snapshots_to_delete(names, keep_daily=0, keep_weekly=0) == []


def test_retention_weekly_window_keeps_older_weeks():
    names = _names("20260105-020000", "20251229-020000", "20251222-020000", "20251215-020000")
    deleted = backup_service.select_snapshots_to_delete(names, keep_daily=1, keep_weekly=3)
    assert deleted == _names("20251215-020000")


def test_is_due():
    now = datetime(2026, 1, 2, 12, 0)
    assert not backup_service.is_due(False, "/x", 24, None, now)
    assert not backup_service.is_due(True, None, 24, None, now)
    assert backup_service.is_due(True, "/x", 24, None, now)
    assert not backup_service.is_due(True, "/x", 24, datetime(2026, 1, 2, 0, 0), now)
    assert backup_service.is_due(True, "/x", 24, datetime(2026, 1, 1, 12, 0), now)


# ── destinations ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["", "  ", "-flag", ":s3,env_auth:bucket", "relative/path", "unknownremote:bucket"])
def test_bad_destinations_are_refused(fake_rclone, bad):
    with pytest.raises(rclone_service.RcloneError):
        rclone_service.validate_destination(bad)


def test_good_destinations_are_accepted(fake_rclone):
    assert rclone_service.validate_destination("/mnt/backup/insar/") == "/mnt/backup/insar"
    assert rclone_service.validate_destination("fakeremote:bucket/insar") == "fakeremote:bucket/insar"


def test_join_handles_local_and_remote():
    assert rclone_service.join("/a/b", "x", "y") == "/a/b/x/y"
    assert rclone_service.join("remote:", "x") == "remote:x"
    assert rclone_service.join("remote:bucket", "x/") == "remote:bucket/x"


def test_missing_rclone_gives_a_clear_error(monkeypatch):
    monkeypatch.setattr(settings, "rclone_path", "definitely-not-installed-rclone")
    assert not rclone_service.is_available()
    with pytest.raises(rclone_service.RcloneError, match="not found"):
        rclone_service.list_remotes()


# ── backups ──────────────────────────────────────────────────────────────────

def test_backup_copies_projects_then_database_and_skips_regenerable_data(fake_rclone, project_folder, tmp_path):
    folder, label = project_folder
    dest = tmp_path / "backup-disk"

    summary = backup_service.run_backup(str(dest))

    assert (dest / "projects" / label / "ifg_1" / "unw.tif").read_bytes() == b"data"
    assert not (dest / "projects" / label / "mintpy").exists()
    assert not (dest / "projects" / label / "slc").exists()
    snapshots = [p.name for p in (dest / "database").iterdir()]
    assert snapshots == [summary["snapshot"]]

    verbs = [c[0] for c in fake_rclone["calls"]()]
    assert verbs.index("copyto") > max(i for i, v in enumerate(verbs) if v == "copy")  # database last
    assert "sync" not in verbs and "move" not in verbs  # nothing is ever deleted by the copy


def test_backup_never_uploads_the_env_file(fake_rclone, project_folder, tmp_path):
    dest = tmp_path / "backup-disk"
    backup_service.run_backup(str(dest))
    uploaded = [p.name for p in dest.rglob("*") if p.is_file()]
    assert ".env" not in uploaded


def test_backup_works_against_a_named_remote(fake_rclone, project_folder):
    backup_service.run_backup("fakeremote:bucket/insar")
    assert any((fake_rclone["root"] / "bucket" / "insar" / "database").iterdir())


def test_failed_project_copy_skips_the_database_snapshot(fake_rclone, project_folder, tmp_path, monkeypatch):
    folder, _ = project_folder
    monkeypatch.setenv("FAKE_RCLONE_FAIL", str(folder))
    dest = tmp_path / "backup-disk"

    with pytest.raises(rclone_service.RcloneError, match="database snapshot was not uploaded"):
        backup_service.run_backup(str(dest))

    assert not (dest / "database").exists()
    db = SessionLocal()
    try:
        config = backup_service.get_config(db)
        assert config.last_status == "error"
        assert "simulated copy failure" in config.last_message
    finally:
        db.close()


def test_old_snapshots_are_pruned_by_retention(fake_rclone, project_folder, tmp_path):
    dest = tmp_path / "backup-disk"
    old_dir = dest / "database"
    old_dir.mkdir(parents=True)
    for stamp in ("20200101-020000", "20200102-020000", "20200103-020000"):
        (old_dir / f"insar_app-{stamp}.db").write_bytes(b"old")
    (old_dir / "README.txt").write_text("not ours")

    summary = backup_service.run_backup(str(dest), keep_daily=1, keep_weekly=0)

    remaining = sorted(p.name for p in old_dir.iterdir())
    assert "README.txt" in remaining
    assert summary["snapshot"] in remaining
    assert len([n for n in remaining if n.startswith("insar_app-")]) == 1
    assert summary["pruned"] == 3


def test_second_run_while_busy_is_refused(fake_rclone, project_folder, tmp_path):
    assert backup_service._run_lock.acquire(blocking=False)
    try:
        with pytest.raises(backup_service.BackupBusy):
            backup_service.run_backup(str(tmp_path / "d"))
    finally:
        backup_service._run_lock.release()


def test_backup_waits_while_a_move_is_running(fake_rclone, project_folder, tmp_path, monkeypatch):
    monkeypatch.setattr(storage_move, "is_busy", lambda: True)
    with pytest.raises(backup_service.BackupBusy):
        backup_service.run_backup(str(tmp_path / "d"))


# ── API ──────────────────────────────────────────────────────────────────────

def test_overview_reports_rclone_and_remotes(client, fake_rclone):
    body = client.get("/api/backup").json()
    assert body["rclone"] == {"available": True, "version": "v1.99.0", "remotes": ["fakeremote"]}
    assert body["config"]["enabled"] is False
    assert body["state"]["running"] is False


def test_overview_without_rclone(client, monkeypatch):
    monkeypatch.setattr(settings, "rclone_path", "definitely-not-installed-rclone")
    assert client.get("/api/backup").json()["rclone"]["available"] is False


def test_config_roundtrip_and_validation(client, fake_rclone, tmp_path):
    ok = client.put("/api/backup/config", json={
        "destination": str(tmp_path / "disk"), "enabled": True, "interval_hours": 12, "keep_daily": 7, "keep_weekly": 4,
    })
    assert ok.status_code == 200
    assert ok.json()["enabled"] is True and ok.json()["interval_hours"] == 12

    assert client.put("/api/backup/config", json={"destination": "-bad", "enabled": False}).status_code == 422
    assert client.put("/api/backup/config", json={"destination": None, "enabled": True}).status_code == 422
    assert client.put("/api/backup/config", json={"destination": "/x", "interval_hours": 0}).status_code == 422


def test_test_endpoint_writes_and_removes_a_marker(client, fake_rclone, tmp_path):
    dest = tmp_path / "disk"
    resp = client.post("/api/backup/test", json={"destination": str(dest)})
    assert resp.status_code == 200
    assert list(dest.iterdir()) == []  # marker was cleaned up
    assert client.post("/api/backup/test", json={"destination": "nope:x"}).status_code == 422


def test_run_requires_a_destination(client, fake_rclone):
    client.put("/api/backup/config", json={"destination": None, "enabled": False})
    assert client.post("/api/backup/run").status_code == 422


def test_run_now_starts_a_backup_in_the_background(client, fake_rclone, project_folder, tmp_path):
    import time

    dest = tmp_path / "disk"
    client.put("/api/backup/config", json={"destination": str(dest), "enabled": False})
    assert client.post("/api/backup/run").status_code == 202

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        state = client.get("/api/backup").json()
        if not state["state"]["running"] and state["config"]["last_status"]:
            break
        time.sleep(0.05)
    assert state["config"]["last_status"] == "ok"
    assert (dest / "database").is_dir()


def test_scheduler_tick_runs_only_when_due(client, fake_rclone, project_folder, tmp_path):
    dest = tmp_path / "disk"
    db = SessionLocal()
    backup_service.get_config(db).last_run_at = None  # the config row is shared with earlier tests
    db.commit()
    db.close()
    client.put("/api/backup/config", json={"destination": str(dest), "enabled": False})
    assert backup_service.run_if_due() is False  # disabled

    client.put("/api/backup/config", json={"destination": str(dest), "enabled": True, "interval_hours": 24})
    assert backup_service.run_if_due() is True   # never ran
    assert backup_service.run_if_due() is False  # ran just now
    client.put("/api/backup/config", json={"destination": None, "enabled": False})


def test_slc_is_skipped_unless_asked_for(fake_rclone, project_folder, tmp_path):
    folder, label = project_folder
    skipped = tmp_path / "without"
    included = tmp_path / "with"

    backup_service.run_backup(str(skipped))
    backup_service.run_backup(str(included), include_slc=True)

    assert not (skipped / "projects" / label / "slc").exists()
    assert (included / "projects" / label / "slc" / "raw.zip").read_bytes() == b"raw"
    assert not (included / "projects" / label / "mintpy").exists()  # still regenerable


def test_archived_projects_are_not_backed_up(fake_rclone, project_folder, tmp_path):
    folder, label = project_folder
    db = SessionLocal()
    db.query(Project).filter(Project.id.like(label.rsplit("-", 1)[1] + "%")).update({"archived_remote": "x:y"}, synchronize_session=False)
    db.commit()
    db.close()

    backup_service.run_backup(str(tmp_path / "dest"))
    assert not (tmp_path / "dest" / "projects" / label).exists()


def test_projects_switched_off_are_not_backed_up(client, fake_rclone, project_folder, tmp_path):
    folder, label = project_folder
    project_id = label.rsplit("-", 1)[1]
    db = SessionLocal()
    pid = db.query(Project).filter(Project.id.like(project_id + "%")).first().id
    db.close()

    assert client.patch(f"/api/projects/{pid}", json={"backup_enabled": False}).json()["backup_enabled"] is False
    backup_service.run_backup(str(tmp_path / "off"))
    assert not (tmp_path / "off" / "projects" / label).exists()
    assert any((tmp_path / "off" / "database").iterdir())  # the database snapshot still happens

    assert client.patch(f"/api/projects/{pid}", json={"backup_enabled": True}).json()["backup_enabled"] is True
    backup_service.run_backup(str(tmp_path / "on"))
    assert (tmp_path / "on" / "projects" / label / "ifg_1" / "unw.tif").exists()


def test_new_projects_default_to_being_backed_up(client):
    created = client.post("/api/projects", json={
        "name": "Default Backup", "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]},
    }).json()
    assert created["backup_enabled"] is True
