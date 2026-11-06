"""Archiving a project to an rclone destination and restoring it (tests/fake_rclone.py, no network)."""
import time

import pytest

from app.config import settings
from app.database import SessionLocal
from app.models import Batch, Job, JobStatus, Project
from app.services import archive_service, backup_service, rclone_service, storage_move


@pytest.fixture()
def project(tmp_path, client):
    """A project with its own folder on disk: a cropped interferogram, MintPy output and an SLC."""
    folder = tmp_path / "bridge-site"
    (folder / "ifg_1").mkdir(parents=True)
    (folder / "ifg_1" / "unw.tif").write_bytes(b"interferogram")
    (folder / "mintpy").mkdir()
    (folder / "mintpy" / "velocity.h5").write_bytes(b"velocity")
    (folder / "slc").mkdir()
    (folder / "slc" / "raw.zip").write_bytes(b"raw-scene")
    db = SessionLocal()
    row = Project(name="Bridge Site", geometry={}, storage_path=str(folder))
    db.add(row)
    db.commit()
    pid = row.id
    db.close()
    yield {"id": pid, "folder": folder, "label": f"bridge-site-{pid[:8]}"}
    db = SessionLocal()
    db.query(Project).filter_by(id=pid).delete()
    db.commit()
    db.close()


def _project_row(pid):
    db = SessionLocal()
    try:
        row = db.query(Project).filter_by(id=pid).first()
        return row.archived_remote, row.archived_at
    finally:
        db.close()


def _wait(client):
    assert archive_service.wait_idle(15)
    return client.get("/api/archive/state").json()


def test_archive_copies_everything_verifies_then_removes_local_files(client, fake_rclone, project, tmp_path):
    dest = tmp_path / "cold-storage"
    resp = client.post(f"/api/projects/{project['id']}/archive", json={"destination": str(dest)})
    assert resp.status_code == 202
    state = _wait(client)
    assert state["status"] == "done", state

    archived = dest / "archive" / project["label"]
    assert (archived / "ifg_1" / "unw.tif").read_bytes() == b"interferogram"
    assert (archived / "mintpy" / "velocity.h5").read_bytes() == b"velocity"  # unlike backups: nothing is left out
    assert (archived / "slc" / "raw.zip").read_bytes() == b"raw-scene"
    assert not project["folder"].exists()

    remote, when = _project_row(project["id"])
    assert remote == str(archived) and when is not None
    assert client.get(f"/api/projects/{project['id']}").json()["archived_remote"] == str(archived)

    verbs = [c[0] for c in fake_rclone["calls"]()]
    assert verbs.index("check") > verbs.index("copy")


def test_failed_verification_keeps_local_files_and_does_not_flag_the_project(client, fake_rclone, project, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_RCLONE_CORRUPT", "1")
    client.post(f"/api/projects/{project['id']}/archive", json={"destination": str(tmp_path / "cold")})
    state = _wait(client)

    assert state["status"] == "error"
    assert "differences" in state["error"]
    assert (project["folder"] / "ifg_1" / "unw.tif").read_bytes() == b"interferogram"
    assert _project_row(project["id"])[0] is None


def test_failed_copy_keeps_local_files(client, fake_rclone, project, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_RCLONE_FAIL", str(project["folder"]))
    client.post(f"/api/projects/{project['id']}/archive", json={"destination": str(tmp_path / "cold")})
    assert _wait(client)["status"] == "error"
    assert project["folder"].exists()
    assert _project_row(project["id"])[0] is None


def test_files_changing_during_the_copy_abort_the_archive(client, fake_rclone, project, tmp_path, monkeypatch):
    real_check = rclone_service.check

    def check_then_a_download_lands(*args, **kwargs):
        real_check(*args, **kwargs)  # passes: the new file did not exist yet
        (project["folder"] / "ifg_2").mkdir()
        (project["folder"] / "ifg_2" / "unw.tif").write_bytes(b"arrived late")

    monkeypatch.setattr(rclone_service, "check", check_then_a_download_lands)
    client.post(f"/api/projects/{project['id']}/archive", json={"destination": str(tmp_path / "cold")})
    state = _wait(client)

    assert state["status"] == "error"
    assert "changed while" in state["error"]
    assert (project["folder"] / "ifg_2" / "unw.tif").exists()  # the late file was not deleted
    assert _project_row(project["id"])[0] is None


def test_restore_brings_files_back_and_keeps_the_archive_copy(client, fake_rclone, project, tmp_path):
    dest = tmp_path / "cold"
    client.post(f"/api/projects/{project['id']}/archive", json={"destination": str(dest)})
    assert _wait(client)["status"] == "done"

    resp = client.post(f"/api/projects/{project['id']}/restore")
    assert resp.status_code == 202
    state = _wait(client)
    assert state["status"] == "done", state
    assert state["kind"] == "restore"

    assert (project["folder"] / "ifg_1" / "unw.tif").read_bytes() == b"interferogram"
    assert (project["folder"] / "mintpy" / "velocity.h5").read_bytes() == b"velocity"
    assert _project_row(project["id"]) == (None, None)
    assert (dest / "archive" / project["label"] / "ifg_1" / "unw.tif").exists()  # nothing removed remotely


def test_failed_restore_stays_archived(client, fake_rclone, project, tmp_path, monkeypatch):
    client.post(f"/api/projects/{project['id']}/archive", json={"destination": str(tmp_path / "cold")})
    assert _wait(client)["status"] == "done"

    monkeypatch.setenv("FAKE_RCLONE_CORRUPT", "1")
    client.post(f"/api/projects/{project['id']}/restore")
    assert _wait(client)["status"] == "error"
    assert _project_row(project["id"])[0] is not None


def test_default_destination_is_the_backup_destination(client, fake_rclone, project, tmp_path):
    dest = tmp_path / "backup-disk"
    client.put("/api/backup/config", json={"destination": str(dest), "enabled": False})
    assert client.post(f"/api/projects/{project['id']}/archive", json={}).status_code == 202
    assert _wait(client)["status"] == "done"
    assert (dest / "archive" / project["label"]).is_dir()
    client.put("/api/backup/config", json={"destination": None, "enabled": False})


def test_archive_needs_a_destination(client, fake_rclone, project):
    client.put("/api/backup/config", json={"destination": None, "enabled": False})
    assert client.post(f"/api/projects/{project['id']}/archive", json={}).status_code == 422


def test_refused_while_hyp3_jobs_are_processing(client, fake_rclone, project, tmp_path):
    db = SessionLocal()
    batch = Batch(project_id=project["id"], status="done")
    db.add(batch)
    db.commit()
    db.add(Job(batch_id=batch.id, reference_granule="a", secondary_granule="b", status=JobStatus.RUNNING))
    db.commit()
    db.close()

    resp = client.post(f"/api/projects/{project['id']}/archive", json={"destination": str(tmp_path / "cold")})
    assert resp.status_code == 422
    assert "still processing" in resp.json()["detail"]
    assert project["folder"].exists()


def test_refused_without_a_folder_of_its_own(client, fake_rclone, tmp_path):
    db = SessionLocal()
    row = Project(name="Legacy", geometry={}, storage_path=None)
    db.add(row)
    db.commit()
    pid = row.id
    db.close()
    try:
        resp = client.post(f"/api/projects/{pid}/archive", json={"destination": str(tmp_path / "cold")})
        assert resp.status_code == 422
        assert "no folder of its own" in resp.json()["detail"]
    finally:
        db = SessionLocal()
        db.query(Project).filter_by(id=pid).delete()
        db.commit()
        db.close()


def test_refuses_to_delete_the_shared_downloads_folder(client, fake_rclone, tmp_path):
    db = SessionLocal()
    row = Project(name="Shared", geometry={}, storage_path=settings.downloads_dir)
    db.add(row)
    db.commit()
    pid = row.id
    db.close()
    try:
        resp = client.post(f"/api/projects/{pid}/archive", json={"destination": str(tmp_path / "cold")})
        assert resp.status_code == 422
        assert "not a project-specific folder" in resp.json()["detail"]
    finally:
        db = SessionLocal()
        db.query(Project).filter_by(id=pid).delete()
        db.commit()
        db.close()


def test_cannot_archive_twice_or_restore_an_active_project(client, fake_rclone, project, tmp_path):
    assert client.post(f"/api/projects/{project['id']}/restore").status_code == 422
    client.post(f"/api/projects/{project['id']}/archive", json={"destination": str(tmp_path / "cold")})
    assert _wait(client)["status"] == "done"
    again = client.post(f"/api/projects/{project['id']}/archive", json={"destination": str(tmp_path / "cold")})
    assert again.status_code == 422


def test_busy_when_a_backup_or_move_is_running(client, fake_rclone, project, tmp_path, monkeypatch):
    assert backup_service.acquire_exclusive()
    try:
        resp = client.post(f"/api/projects/{project['id']}/archive", json={"destination": str(tmp_path / "cold")})
        assert resp.status_code == 409
    finally:
        backup_service.release_exclusive()

    monkeypatch.setattr(storage_move, "is_busy", lambda: True)
    resp = client.post(f"/api/projects/{project['id']}/archive", json={"destination": str(tmp_path / "cold")})
    assert resp.status_code == 409
    assert project["folder"].exists()


def test_the_lock_is_released_after_a_failure(client, fake_rclone, project, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_RCLONE_CORRUPT", "1")
    client.post(f"/api/projects/{project['id']}/archive", json={"destination": str(tmp_path / "cold")})
    assert _wait(client)["status"] == "error"
    time.sleep(0.1)
    assert not backup_service.is_running()


def test_archived_projects_reject_operations_that_need_local_files(client, fake_rclone, project, tmp_path):
    client.post(f"/api/projects/{project['id']}/archive", json={"destination": str(tmp_path / "cold")})
    assert _wait(client)["status"] == "done"
    pid = project["id"]

    assert client.post(f"/api/projects/{pid}/batches", json={"dry_run": False}).status_code == 409
    assert client.post(f"/api/projects/{pid}/refresh", json={}).status_code == 409
    assert client.post(f"/api/projects/{pid}/mintpy/run").status_code == 409
    assert client.get(f"/api/projects/{pid}/mintpy/points").status_code == 409
    assert client.delete(f"/api/projects/{pid}/download-data").status_code == 409
    assert client.post(f"/api/projects/{pid}/storage/move", json={"mountpoint": None}).status_code == 409
    assert client.post(f"/api/projects/{pid}/slc/downloads/queue", json={"scenes": []}).status_code == 409


def test_fingerprint_changes_when_files_change(tmp_path):
    (tmp_path / "a").write_bytes(b"1")
    before = archive_service.fingerprint(tmp_path)
    assert archive_service.fingerprint(tmp_path) == before
    (tmp_path / "b").write_bytes(b"22")
    assert archive_service.fingerprint(tmp_path) != before
