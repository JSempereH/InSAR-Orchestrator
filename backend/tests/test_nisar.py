"""NISAR projects: choosing the source, finding and downloading granules, and running MintPy on them."""
import datetime as dt
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.models import Project
from app.routers import nisar as nisar_router
from app.services import mintpy_service, nisar_download_queue, provenance_service
from insar_core.adapters.nisar import NisarGunw, group_stacks
from insar_core.engines import Availability

GEOMETRY = {"type": "Polygon", "coordinates": [[[-1.8, 37.6], [-1.8, 37.7], [-1.6, 37.7], [-1.6, 37.6], [-1.8, 37.6]]]}


def _create(client, name, engine="nisar_gunw", **extra):
    body = {"name": name, "geometry": GEOMETRY, "date_start": "2026-06-01", "date_end": "2026-10-31", "engine": engine, **extra}
    return client.post("/api/projects", json=body)


def _granule(ref, sec, size=1000, processed="2026-10-18", frame=70, coverage="Full"):
    def d(text):
        return dt.datetime.strptime(text, "%Y%m%d").date()
    return NisarGunw(
        file_id=f"NISAR_L2_PR_GUNW_{ref}_{sec}", file_name=f"NISAR_L2_PR_GUNW_{ref}_{sec}.h5", reference_date=d(ref), secondary_date=d(sec),
        track_number=66, frame=frame, flight_direction="DESCENDING", polarization="HH", range_bandwidth="20", frame_coverage=coverage,
        size_bytes=size, download_url=f"https://datapool.invalid/{ref}_{sec}.h5", processing_date=processed)


@pytest.fixture()
def fake_search(monkeypatch):
    granules = [_granule("20260922", "20261004"), _granule("20261004", "20261016"), _granule("20261004", "20261016", processed="2026-10-30"),
                _granule("20260810", "20260822", frame=71, coverage="Partial")]
    monkeypatch.setattr(nisar_router._adapter, "stacks", lambda params: group_stacks(granules))
    return granules


# ── choosing the source ──────────────────────────────────────────────────────

def test_the_engines_are_listed_with_what_each_needs_and_whether_it_works_here(client):
    engines = {e["id"]: e for e in client.get("/api/engines").json()}
    assert set(engines) >= {"hyp3_gamma", "nisar_gunw"}
    assert engines["hyp3_gamma"]["costs_credits"] is True and engines["nisar_gunw"]["costs_credits"] is False
    assert engines["nisar_gunw"]["sensor"] == "nisar" and engines["nisar_gunw"]["available"] in (True, False)


def test_a_project_uses_hyp3_unless_it_says_otherwise(client):
    project = client.post("/api/projects", json={"name": "Default engine", "geometry": GEOMETRY, "date_start": "2023-01-01", "date_end": "2023-06-01"}).json()
    assert project["engine"] == "hyp3_gamma"


def test_a_nisar_project_is_created_with_its_engine(client):
    assert _create(client, "NISAR project").json()["engine"] == "nisar_gunw"


def test_an_unknown_engine_is_refused_and_the_known_ones_named(client):
    reply = _create(client, "Bad engine", engine="isce2")
    assert reply.status_code == 422 and "hyp3_gamma" in reply.json()["detail"]


def test_a_nisar_project_needs_its_dates(client):
    reply = client.post("/api/projects", json={"name": "No dates", "geometry": GEOMETRY, "engine": "nisar_gunw"})
    assert reply.status_code == 422 and "start and an end date" in reply.json()["detail"]


def test_hyp3_batches_are_refused_for_a_nisar_project_with_a_clear_reason(client):
    pid = _create(client, "NISAR no hyp3").json()["id"]
    for method, path, body in [("post", "batches/plan", {}), ("post", "batches", {"dry_run": False}), ("post", "refresh", {})]:
        reply = getattr(client, method)(f"/api/projects/{pid}/{path}", json=body)
        assert reply.status_code == 422 and "NISAR" in reply.json()["detail"], path


# ── finding and downloading granules ─────────────────────────────────────────

def test_the_search_groups_granules_into_stacks_and_keeps_the_newest_version_of_a_pair(client, fake_search):
    pid = _create(client, "NISAR search").json()["id"]
    stacks = client.get(f"/api/projects/{pid}/nisar/stacks").json()
    assert [(s["frame"], s["frame_coverage"], len(s["granules"])) for s in stacks] == [(70, "Full", 2), (71, "Partial", 1)]
    full = stacks[0]
    assert full["connected"] is True and full["dates"] == ["2026-09-22", "2026-10-04", "2026-10-16"]
    chosen = next(g for g in full["granules"] if g["reference_date"] == "2026-10-04")
    assert chosen["processing_date"] == "2026-10-30" and chosen["temporal_days"] == 12


def test_a_granule_counts_as_downloaded_only_when_the_file_is_complete(client, fake_search):
    created = _create(client, "NISAR on disk").json()
    folder = Path(created["storage_path"])
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "NISAR_L2_PR_GUNW_20260922_20261004.h5").write_bytes(b"x" * 1000)       # the size ASF reports
    (folder / "NISAR_L2_PR_GUNW_20261004_20261016.h5").write_bytes(b"x" * 400)        # an interrupted download
    granules = {g["reference_date"]: g for g in client.get(f"/api/projects/{created['id']}/nisar/stacks").json()[0]["granules"]}
    assert granules["2026-09-22"]["downloaded"] is True and granules["2026-10-04"]["downloaded"] is False


def test_an_unreachable_archive_is_a_502_that_says_so(client, monkeypatch):
    pid = _create(client, "NISAR down").json()["id"]

    def boom(params):
        raise ConnectionError("no route")

    monkeypatch.setattr(nisar_router._adapter, "stacks", boom)
    reply = client.get(f"/api/projects/{pid}/nisar/stacks")
    assert reply.status_code == 502 and "could not be searched" in reply.json()["detail"]


def test_the_stacks_of_a_hyp3_project_are_not_offered(client):
    pid = _create(client, "Plain hyp3", engine="hyp3_gamma").json()["id"]
    reply = client.get(f"/api/projects/{pid}/nisar/stacks")
    assert reply.status_code == 422 and "does not use NISAR" in reply.json()["detail"]
    assert client.get("/api/projects/nope/nisar/stacks").status_code == 404


def test_download_queues_only_what_is_missing_and_takes_urls_from_asf(client, fake_search, monkeypatch):
    created = _create(client, "NISAR download").json()
    folder = Path(created["storage_path"])
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "NISAR_L2_PR_GUNW_20260922_20261004.h5").write_bytes(b"x" * 1000)       # already complete
    started = []
    monkeypatch.setattr(nisar_router, "get_earthdata_credentials", lambda db: ("someone", "secret"))
    monkeypatch.setattr(nisar_download_queue, "start", lambda user, password, items, dest: started.append((user, items, dest)))
    reply = client.post(f"/api/projects/{created['id']}/nisar/downloads/queue", json={"file_names": [
        "NISAR_L2_PR_GUNW_20260922_20261004.h5", "NISAR_L2_PR_GUNW_20261004_20261016.h5"]})
    assert reply.status_code == 202 and reply.json()["queued"] == 1 and reply.json()["already_downloaded"] == 1
    assert started[0][1] == [{"file_name": "NISAR_L2_PR_GUNW_20261004_20261016.h5",
                              "download_url": "https://datapool.invalid/20261004_20261016.h5"}]
    assert started[0][0] == "someone" and started[0][2] == folder


def test_download_refuses_names_that_are_not_granules_of_the_project(client, fake_search, monkeypatch):
    pid = _create(client, "NISAR strange name").json()["id"]
    monkeypatch.setattr(nisar_router, "get_earthdata_credentials", lambda db: ("u", "p"))
    reply = client.post(f"/api/projects/{pid}/nisar/downloads/queue", json={"file_names": ["../../etc/passwd"]})
    assert reply.status_code == 422 and "passwd" in reply.json()["detail"]
    assert client.post(f"/api/projects/{pid}/nisar/downloads/queue", json={"file_names": []}).status_code == 422


def test_the_files_on_disk_are_listed_from_their_names(client):
    created = _create(client, "NISAR listing").json()
    folder = Path(created["storage_path"])
    folder.mkdir(parents=True, exist_ok=True)
    name = ("NISAR_L2_PR_GUNW_032_066_D_070_033_2000_SH_20261004T185352_20261004T185427_20261016T185352_20261016T185427_P05023_N_F_J_001.h5")
    (folder / name).write_bytes(b"x" * 10)
    (folder / "notes.txt").write_text("not a granule")
    listed = client.get(f"/api/projects/{created['id']}/nisar/granules").json()
    assert listed == [{"file_name": name, "reference_date": "2026-10-04", "secondary_date": "2026-10-16", "size_bytes": 10}]


def test_the_queue_state_reports_progress_of_the_current_file(client, monkeypatch):
    monkeypatch.setattr(nisar_download_queue, "get_state", lambda: {"active": True, "current_filename": "f.h5", "pending_count": 2})
    monkeypatch.setattr("app.routers.nisar.download_state.get", lambda name: {"pct": 42.0} if name == "f.h5" else {})
    state = client.get("/api/nisar/downloads/queue").json()
    assert state["active"] is True and state["progress"] == {"pct": 42.0}
    assert client.delete("/api/nisar/downloads/queue").json() == {"cancelled": True}


# ── MintPy on NISAR ──────────────────────────────────────────────────────────

def _project_folder(client, name, granule=True):
    created = _create(client, name).json()
    folder = Path(created["storage_path"])
    folder.mkdir(parents=True, exist_ok=True)
    if granule:
        (folder / "NISAR_L2_PR_GUNW_x.h5").write_bytes(b"x")
    return created, folder


def test_mintpy_needs_granules_on_disk_for_a_nisar_project(client, monkeypatch):
    created, folder = _project_folder(client, "NISAR no files", granule=False)
    monkeypatch.setattr(mintpy_service, "mintpy_available", lambda: True)
    monkeypatch.setattr(type(mintpy_service.get_engine("nisar_gunw")), "availability", lambda self: Availability(True))
    with pytest.raises(RuntimeError, match="No downloaded interferograms"):
        mintpy_service.start_run(created["id"], folder, engine_id="nisar_gunw")


def test_mintpy_says_why_the_nisar_engine_cannot_run_when_it_cannot(client, monkeypatch):
    created, folder = _project_folder(client, "NISAR old mintpy")
    monkeypatch.setattr(mintpy_service, "mintpy_available", lambda: True)
    monkeypatch.setattr(type(mintpy_service.get_engine("nisar_gunw")), "availability", lambda self: Availability(False, "MintPy is too old"))
    with pytest.raises(RuntimeError, match="too old"):
        mintpy_service.start_run(created["id"], folder, engine_id="nisar_gunw")


def test_the_run_endpoint_runs_a_nisar_project_through_the_nisar_engine(client, monkeypatch):
    created, folder = _project_folder(client, "NISAR run")
    got = {}
    monkeypatch.setattr(mintpy_service, "start_run", lambda *a, **k: got.update(k) or {"status": "RUNNING"})
    assert client.post(f"/api/projects/{created['id']}/mintpy/run", json={}).status_code == 200
    assert got["engine_id"] == "nisar_gunw"


def test_a_run_builds_its_adapter_with_the_projects_engine_and_remembers_it_for_a_restart(client, monkeypatch):
    created, folder = _project_folder(client, "NISAR engine kept")
    seen = {}

    class FakeProcess:
        pid = 1

        def wait(self):
            return 0

        def terminate(self):
            pass

    class Adapter:
        def __init__(self, downloads_dir, work_dir, engine=None):
            self.work_dir, self.engine = Path(work_dir), engine
            seen["engine"] = engine.info.id

        def prepare_inputs(self, geometry=None):
            return 1

        def write_config(self, config_path=None, geometry=None, extra_options=None):
            self.work_dir.mkdir(parents=True, exist_ok=True)
            (self.work_dir / "smallbaselineApp.cfg").write_text("mintpy.load.processor = nisar\n")
            return self.work_dir / "smallbaselineApp.cfg"

        def start_full_pipeline(self, config_path=None, start_step=None, end_step=None):
            seen.setdefault("marker", mintpy_service._read_marker(self.work_dir))
            return FakeProcess()

        def pin_reference_point(self):
            return None

    monkeypatch.setattr(mintpy_service, "MintPyAdapter", Adapter)
    work = folder / "mintpy"
    mintpy_service._runs[created["id"]] = mintpy_service.MintPyRunState(project_id=created["id"], work_dir=str(work), engine_id="nisar_gunw")
    mintpy_service._run(created["id"], folder, work, GEOMETRY, None)
    mintpy_service._runs.pop(created["id"], None)
    assert seen["engine"] == "nisar_gunw" and seen["marker"]["engine_id"] == "nisar_gunw"


def test_the_provenance_record_names_the_engine_and_lists_the_granules(client):
    created = _create(client, "NISAR provenance").json()
    folder = Path(created["storage_path"])
    folder.mkdir(parents=True, exist_ok=True)
    name = ("NISAR_L2_PR_GUNW_032_066_D_070_033_2000_SH_20261004T185352_20261004T185427_20261016T185352_20261016T185427_P05023_N_F_J_001.h5")
    (folder / name).write_bytes(b"x")
    db = SessionLocal()
    try:
        context = provenance_service.project_context(db, db.query(Project).filter_by(id=created["id"]).first())
    finally:
        db.close()
    assert context["engine"]["id"] == "nisar_gunw" and context["engine"]["band"] == "L"
    assert context["nisar_granules"][0]["secondary_date"] == "2026-10-16"


def test_the_options_offered_depend_on_the_projects_engine(client):
    nisar = client.get(f"/api/projects/{_create(client, 'NISAR options').json()['id']}/mintpy/options").json()
    hyp3 = client.get(f"/api/projects/{_create(client, 'HyP3 options', engine='hyp3_gamma').json()['id']}/mintpy/options").json()
    ion = next(o for o in nisar["options"] if o["key"] == "mintpy.ionosphericDelay.method")
    assert ion["default"] == "split_spectrum" and "engines" not in ion
    assert "mintpy.ionosphericDelay.method" not in {o["key"] for o in hyp3["options"]}


def test_a_hyp3_project_cannot_ask_for_the_ionosphere_option(client, monkeypatch):
    created = client.post("/api/projects", json={"name": "HyP3 ion", "geometry": GEOMETRY, "date_start": "2023-01-01", "date_end": "2023-06-01"}).json()
    reply = client.post(f"/api/projects/{created['id']}/mintpy/run", json={"options": {"mintpy.ionosphericDelay.method": "split_spectrum"}})
    assert reply.status_code == 422 and "not an option" in reply.json()["detail"]
