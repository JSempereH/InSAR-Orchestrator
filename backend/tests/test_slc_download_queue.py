"""The SLC download queue (backend/app/services/slc_download_queue.py),
exercised through its endpoints - it had no coverage at all before being
rebuilt on top of the shared SequentialQueue engine."""
import time
from pathlib import Path

import pytest

from app.services import slc_download_queue
from app.routers import slc as slc_router
from insar_core.adapters.asf import ASFAdapter


class _FakeASFAdapter:
    def __init__(self, fail=False):
        self.fail = fail
        self.downloaded = []

    def download(self, download_url, file_name, destination, username, password, progress_cb=None):
        if self.fail:
            raise RuntimeError("boom")
        self.downloaded.append(file_name)
        dest_file = Path(destination) / file_name
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        dest_file.write_bytes(b"0")
        if progress_cb:
            progress_cb(total_bytes=1, downloaded_bytes=1)
        return dest_file


@pytest.fixture(autouse=True)
def _reset_queue():
    yield
    slc_download_queue.cancel()
    for _ in range(50):
        if not slc_download_queue.get_state()["active"]:
            break
        time.sleep(0.02)


def _create_project(client, name="SLC Queue Project"):
    project = client.post("/api/projects", json={
        "name": name,
        "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]},
        "date_start": "2023-01-01",
        "date_end": "2023-06-01",
    }).json()
    Path(project["storage_path"]).mkdir(parents=True, exist_ok=True)
    return project


def _scene_payload(**overrides):
    scene = {
        "file_id": "file-1", "granule_name": "S1_01", "acquisition_date": "2023-01-01",
        "orbit": 1, "track_number": 100, "flight_direction": "DESCENDING", "polarization": "VV",
        "size_mb": 1.0, "download_url": "https://datapool.asf.alaska.edu/SLC/SA/a.zip", "file_name": "S1_01.zip",
    }
    scene.update(overrides)
    return scene


def test_queue_downloads_scenes_and_creates_a_download_record(client, monkeypatch):
    fake = _FakeASFAdapter()
    monkeypatch.setattr(ASFAdapter, "download", fake.download)
    monkeypatch.setattr(slc_router, "get_earthdata_credentials", lambda db: ("user", "pass"))
    project = _create_project(client)

    resp = client.post(f"/api/projects/{project['id']}/slc/downloads/queue", json={
        "scenes": [_scene_payload()],
    })
    assert resp.status_code == 201

    state = client.get("/api/slc/downloads/queue").json()
    for _ in range(50):
        if not state["active"]:
            break
        time.sleep(0.02)
        state = client.get("/api/slc/downloads/queue").json()

    assert state["done"] == 1
    assert fake.downloaded == ["S1_01.zip"]
    assert state["destination"] and state["destination"].endswith("slc")


def test_queue_rejects_scenes_missing_a_download_url(client, monkeypatch):
    monkeypatch.setattr(slc_router, "get_earthdata_credentials", lambda db: ("user", "pass"))
    project = _create_project(client)

    resp = client.post(f"/api/projects/{project['id']}/slc/downloads/queue", json={
        "scenes": [_scene_payload(download_url=None)],
    })
    assert resp.status_code == 400


def test_cancel_clears_pending_scenes(client, monkeypatch):
    fake = _FakeASFAdapter()
    monkeypatch.setattr(ASFAdapter, "download", fake.download)
    monkeypatch.setattr(slc_router, "get_earthdata_credentials", lambda db: ("user", "pass"))
    project = _create_project(client)

    client.post(f"/api/projects/{project['id']}/slc/downloads/queue", json={
        "scenes": [
            _scene_payload(),
            _scene_payload(granule_name="S1_02", file_name="b.zip", download_url="https://datapool.asf.alaska.edu/SLC/SA/b.zip"),
        ],
    })
    resp = client.delete("/api/slc/downloads/queue")
    assert resp.status_code == 200

    state = client.get("/api/slc/downloads/queue").json()
    assert state["pending_count"] == 0


@pytest.mark.parametrize("overrides", [
    {"file_name": "../../outside.zip"},
    {"file_name": "/etc/passwd"},
    {"file_name": ".hidden"},
    {"download_url": "https://attacker.example.net/a.zip"},        # the Earthdata login must not be sent there
    {"download_url": "http://datapool.asf.alaska.edu/a.zip"},      # nor over plain http
    {"download_url": "https://asf.alaska.edu.attacker.net/a.zip"},
])
def test_queue_refuses_names_and_hosts_the_client_should_not_choose(client, monkeypatch, overrides):
    monkeypatch.setattr(slc_router, "get_earthdata_credentials", lambda db: ("user", "pass"))
    project = _create_project(client)
    resp = client.post(f"/api/projects/{project['id']}/slc/downloads/queue", json={"scenes": [_scene_payload(**overrides)]})
    assert resp.status_code == 400


def test_deleting_a_record_never_removes_files_outside_its_folder(client, monkeypatch):
    from app.database import SessionLocal
    from app.models import SLCDownload

    project = _create_project(client)
    outside = Path(project["storage_path"]).parent / "must-survive.txt"
    outside.write_text("keep")
    db = SessionLocal()
    row = SLCDownload(project_id=project["id"], destination_path=str(Path(project["storage_path"]) / "slc"), filenames=["../must-survive.txt"])
    db.add(row)
    db.commit()
    row_id = row.id
    db.close()
    assert client.delete(f"/api/slc/downloads/{row_id}?delete_files=true").status_code == 200
    assert outside.read_text() == "keep"


def test_each_item_keeps_its_own_destination_and_credentials(tmp_path):
    seen = []
    monkey = ASFAdapter.download
    ASFAdapter.download = lambda self, url, name, dest, user, pw, progress_cb=None: seen.append((name, str(dest), user))
    try:
        slc_download_queue.start("alice", "pw", [{"granule_name": "a", "file_name": "a.zip", "download_url": "u"}], tmp_path / "A")
        for _ in range(100):
            if not slc_download_queue.get_state()["active"]:
                break
            time.sleep(0.02)
        slc_download_queue.start("bob", "pw", [{"granule_name": "b", "file_name": "b.zip", "download_url": "u"}], tmp_path / "B")
        for _ in range(100):
            if not slc_download_queue.get_state()["active"]:
                break
            time.sleep(0.02)
    finally:
        ASFAdapter.download = monkey
    assert seen == [("a.zip", str(tmp_path / "A"), "alice"), ("b.zip", str(tmp_path / "B"), "bob")]
