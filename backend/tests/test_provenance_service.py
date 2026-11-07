"""provenance.json and the provenance endpoint: how a result was produced."""
import json
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.models import Batch, Job, JobStatus, Project
from app.services import mintpy_service, provenance_service

GEOMETRY = {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]}


@pytest.fixture()
def project(tmp_path, client):
    folder = tmp_path / "madrid"
    for name in ("ifg_a", "ifg_b", "mintpy", "slc", ".staging"):
        (folder / name).mkdir(parents=True)
    db = SessionLocal()
    row = Project(
        name="Madrid", geometry=GEOMETRY, track_number=1, flight_direction="ASCENDING",
        date_start="2025-01-01", date_end="2025-06-01", max_temporal_neighbors=3, storage_path=str(folder),
    )
    db.add(row)
    db.commit()
    batch = Batch(
        project_id=row.id, label="Batch 1", status="done", total_pairs=1,
        parameters=provenance_service.batch_parameters(row, 3),
    )
    db.add(batch)
    db.commit()
    db.add(Job(
        batch_id=batch.id, reference_granule="S1_A", secondary_granule="S1_B", hyp3_job_id="job-1",
        reference_date="2025-01-04", secondary_date="2025-01-16", status=JobStatus.SUCCEEDED, downloaded=1,
    ))
    db.commit()
    pid = row.id
    db.close()
    yield {"id": pid, "folder": folder}
    db = SessionLocal()
    db.query(Project).filter_by(id=pid).delete()
    db.commit()
    db.close()
    mintpy_service._runs.pop(pid, None)


def _context(project_id):
    db = SessionLocal()
    try:
        return provenance_service.project_context(db, db.query(Project).filter_by(id=project_id).first())
    finally:
        db.close()


def test_batch_parameters_say_what_was_submitted_and_with_which_tools():
    project = Project(name="p", track_number=7, flight_direction="DESCENDING", date_start="2025-01-01", date_end="2025-02-01")
    parameters = provenance_service.batch_parameters(project, 4)
    assert parameters["hyp3"]["looks"] == "20x4" and parameters["hyp3"]["include_dem"] is True
    assert parameters["pairing"] == {"method": "temporal_neighbors", "max_temporal_neighbors": 4}
    assert parameters["search"] == {
        "track_number": 7, "flight_direction": "DESCENDING", "date_start": "2025-01-01", "date_end": "2025-02-01",
    }
    assert parameters["software"]["python"] and "insar-core" in parameters["software"]


def test_context_lists_the_project_its_batches_and_every_pair(project):
    context = _context(project["id"])
    assert context["project"]["name"] == "Madrid" and context["project"]["track_number"] == 1
    assert [b["label"] for b in context["batches"]] == ["Batch 1"]
    assert context["batches"][0]["parameters"]["hyp3"]["looks"] == "20x4"
    assert context["pairs"] == [{
        "batch_id": context["batches"][0]["id"], "reference_granule": "S1_A", "secondary_granule": "S1_B",
        "reference_date": "2025-01-04", "secondary_date": "2025-01-16", "hyp3_job_id": "job-1",
        "status": "SUCCEEDED", "downloaded": True, "credit_cost": None,
    }]


def test_the_endpoint_returns_database_context_before_any_run(client, project):
    body = client.get(f"/api/projects/{project['id']}/provenance").json()
    assert body["project"]["id"] == project["id"]
    assert body["archived"] is False and body["last_mintpy_run"] is None
    assert body["pairs"][0]["hyp3_job_id"] == "job-1"


def test_the_endpoint_includes_the_last_runs_record(client, project):
    provenance_service.write_run_record(project["folder"], {"mintpy_run": {"status": "SUCCEEDED"}})
    body = client.get(f"/api/projects/{project['id']}/provenance").json()
    assert body["last_mintpy_run"]["mintpy_run"]["status"] == "SUCCEEDED"
    assert body["last_mintpy_run"]["schema"] == provenance_service.SCHEMA


def test_an_archived_project_still_answers_from_the_database(client, project):
    db = SessionLocal()
    db.query(Project).filter_by(id=project["id"]).update({"archived_remote": "x:y"})
    db.commit()
    db.close()
    provenance_service.write_run_record(project["folder"], {"mintpy_run": {}})
    body = client.get(f"/api/projects/{project['id']}/provenance").json()
    assert body["archived"] is True and body["last_mintpy_run"] is None   # files are not here
    assert body["pairs"]


def test_unknown_project_is_404(client):
    assert client.get("/api/projects/nope/provenance").status_code == 404


def test_the_record_is_written_atomically_and_reads_back(tmp_path):
    provenance_service.write_run_record(tmp_path, {"a": 1})
    assert provenance_service.read_run_record(tmp_path)["a"] == 1
    assert not list(tmp_path.glob("*.tmp"))
    assert provenance_service.read_run_record(tmp_path / "missing") is None
    (tmp_path / provenance_service.FILE_NAME).write_text("{ not json")
    assert provenance_service.read_run_record(tmp_path) is None


# ── a full run, with MintPy itself faked ─────────────────────────────────────

class FakeProcess:
    def __init__(self, folder: Path, code: int):
        self.pid = 4242
        self._folder = folder
        self._code = code

    def terminate(self):
        pass

    def wait(self):
        marker = mintpy_service._read_marker(self._folder / "mintpy")
        assert marker is not None and marker["pid"] == 4242   # the run is marked while it is alive
        return self._code


def _fake_adapter(code=0, unzip_error=None, calls=None):
    calls = [] if calls is None else calls

    class FakeAdapter:
        def __init__(self, downloads_dir, work_dir):
            self.downloads_dir, self.work_dir = Path(downloads_dir), Path(work_dir)

        def unzip_all(self, geometry=None):
            if unzip_error:
                raise RuntimeError(unzip_error)
            return 2

        def write_config(self, config_path=None, geometry=None, extra_options=None):
            self.work_dir.mkdir(parents=True, exist_ok=True)
            path = self.work_dir / "smallbaselineApp.cfg"
            path.write_text("mintpy.load.processor = hyp3\nmintpy.subset.lalo = 40.1:40.9,-3.9:-3.4\n")
            return path

        def start_full_pipeline(self, config_path=None, start_step=None, end_step=None):
            calls.append(("start", start_step, end_step))
            return FakeProcess(self.downloads_dir, code)

        def pin_reference_point(self):
            calls.append(("pin",))
            return None

    return FakeAdapter


def _run_inline(project, monkeypatch, adapter, attempt=1):
    monkeypatch.setattr(mintpy_service, "MintPyAdapter", adapter)
    work_dir = project["folder"] / "mintpy"
    mintpy_service._runs[project["id"]] = mintpy_service.MintPyRunState(
        project_id=project["id"], work_dir=str(work_dir), attempt=attempt,
    )
    mintpy_service._run(project["id"], project["folder"], work_dir, GEOMETRY, _context(project["id"]))
    return provenance_service.read_run_record(project["folder"]), work_dir


def test_a_successful_run_records_everything_and_clears_its_marker(project, monkeypatch):
    record, work_dir = _run_inline(project, monkeypatch, _fake_adapter(code=0))

    assert record["mintpy_run"]["status"] == "SUCCEEDED" and record["mintpy_run"]["return_code"] == 0
    assert record["mintpy_run"]["attempt"] == 1 and record["mintpy_run"]["finished_at"]
    assert record["project"]["name"] == "Madrid"
    assert record["batches"][0]["parameters"]["hyp3"]["looks"] == "20x4"
    assert record["pairs"][0]["hyp3_job_id"] == "job-1"
    assert record["crop"]["applied"] is True and record["crop"]["subset_lalo"] == "40.1:40.9,-3.9:-3.4"
    assert record["crop"]["margin_fraction"] == 0.25
    # Interferogram folders only: not MintPy's own, not SLC, not hidden staging.
    assert record["interferograms"] == {"count": 2, "names": ["ifg_a", "ifg_b"]}
    assert "mintpy.load.processor = hyp3" in record["mintpy"]["custom_config"]
    assert record["software"]["python"]
    assert mintpy_service._read_marker(work_dir) is None


def test_the_record_carries_mintpys_effective_options(project, monkeypatch):
    pytest.importorskip("mintpy")
    record, _ = _run_inline(project, monkeypatch, _fake_adapter())
    options = record["mintpy"]["options"]
    assert options["mintpy.load.processor"] == "hyp3"
    assert options["mintpy.troposphericDelay.method"] != "auto"   # the default is resolved, not hidden


def test_a_failed_run_is_recorded_as_failed(project, monkeypatch):
    record, work_dir = _run_inline(project, monkeypatch, _fake_adapter(code=1))
    assert record["mintpy_run"]["status"] == "FAILED" and record["mintpy_run"]["return_code"] == 1
    assert mintpy_service._read_marker(work_dir) is None


def test_a_crash_before_mintpy_starts_is_recorded_and_clears_the_marker(project, monkeypatch):
    record, work_dir = _run_inline(project, monkeypatch, _fake_adapter(unzip_error="disk full"))
    assert record["mintpy_run"]["status"] == "FAILED" and "disk full" in record["mintpy_run"]["error"]
    assert record["crop"]["applied"] is False
    assert mintpy_service._read_marker(work_dir) is None


def test_the_record_stays_at_the_project_root_so_backups_include_it(project, monkeypatch):
    _run_inline(project, monkeypatch, _fake_adapter())
    assert (project["folder"] / "provenance.json").is_file()
    assert not (project["folder"] / "mintpy" / "provenance.json").exists()
    assert json.loads((project["folder"] / "provenance.json").read_text())["schema"] == 1


def test_a_failure_writing_the_record_never_breaks_the_run(project, monkeypatch):
    def boom(*a, **k):
        raise OSError("read-only")

    monkeypatch.setattr(provenance_service, "write_run_record", boom)
    monkeypatch.setattr(mintpy_service, "MintPyAdapter", _fake_adapter(code=0))
    work_dir = project["folder"] / "mintpy"
    mintpy_service._runs[project["id"]] = mintpy_service.MintPyRunState(project_id=project["id"], work_dir=str(work_dir))
    mintpy_service._run(project["id"], project["folder"], work_dir, GEOMETRY, None)
    assert mintpy_service._runs[project["id"]].status == "SUCCEEDED"


# ── troposphere correction depends on a Copernicus login ─────────────────────

def _patch_defaults(monkeypatch, method="pyaps"):
    def effective(text):
        if not method:
            return None
        # Like the real function: the custom config wins over MintPy's default.
        override = "no" if "troposphericDelay.method = no" in text else method
        return {"mintpy.troposphericDelay.method": override}

    monkeypatch.setattr(mintpy_service, "mintpy_effective_options", effective)


def test_without_a_login_the_era5_correction_is_turned_off_and_says_so(monkeypatch):
    _patch_defaults(monkeypatch)
    monkeypatch.setattr(mintpy_service, "cds_credentials_available", lambda: False)
    options, warnings = mintpy_service._troposphere_options()
    assert options == {"mintpy.troposphericDelay.method": "no"}
    assert len(warnings) == 1 and "ERA5" in warnings[0] and "cdsapirc" in warnings[0]


def test_with_a_login_mintpys_default_is_left_alone(monkeypatch):
    _patch_defaults(monkeypatch)
    monkeypatch.setattr(mintpy_service, "cds_credentials_available", lambda: True)
    assert mintpy_service._troposphere_options() == ({}, [])


def test_nothing_is_overridden_when_the_default_is_not_pyaps_or_unknown(monkeypatch):
    monkeypatch.setattr(mintpy_service, "cds_credentials_available", lambda: False)
    _patch_defaults(monkeypatch, method="gacos")
    assert mintpy_service._troposphere_options() == ({}, [])
    _patch_defaults(monkeypatch, method=None)
    assert mintpy_service._troposphere_options() == ({}, [])


def test_a_run_without_a_login_passes_the_override_and_records_the_warning(project, monkeypatch):
    seen = {}

    class Adapter:
        def __init__(self, downloads_dir, work_dir):
            self.work_dir = Path(work_dir)

        def unzip_all(self, geometry=None):
            return 0

        def write_config(self, config_path=None, geometry=None, extra_options=None):
            seen["extra_options"] = extra_options
            self.work_dir.mkdir(parents=True, exist_ok=True)
            path = self.work_dir / "smallbaselineApp.cfg"
            path.write_text("mintpy.load.processor = hyp3\n" + "".join(f"{k} = {v}\n" for k, v in (extra_options or {}).items()))
            return path

        def start_full_pipeline(self, config_path=None, start_step=None, end_step=None):
            return FakeProcess(project["folder"], 0)

        def pin_reference_point(self):
            return None

    _patch_defaults(monkeypatch)
    monkeypatch.setattr(mintpy_service, "cds_credentials_available", lambda: False)
    record, _ = _run_inline(project, monkeypatch, Adapter)

    assert seen["extra_options"] == {"mintpy.troposphericDelay.method": "no"}
    assert record["mintpy_run"]["status"] == "SUCCEEDED"
    assert record["mintpy_run"]["warnings"] and "ERA5" in record["mintpy_run"]["warnings"][0]
    assert record["mintpy"]["options"]["mintpy.troposphericDelay.method"] == "no"
    assert "troposphericDelay.method = no" in record["mintpy"]["custom_config"]
    assert mintpy_service.get_status(project["id"], project["folder"])["warnings"] == record["mintpy_run"]["warnings"]


def test_warnings_survive_a_restart_through_the_provenance_record(project, tmp_path):
    work = project["folder"] / "mintpy"
    import h5py
    import numpy as np

    for name in ("timeseries.h5", "velocity.h5"):
        with h5py.File(work / name, "w") as f:
            f.attrs.update({"X_FIRST": 0.0, "Y_FIRST": 1.0, "X_STEP": 0.1, "Y_STEP": -0.1, "WIDTH": 2, "LENGTH": 2})
            f.create_dataset("timeseries" if "series" in name else "velocity", data=np.zeros((2, 2), dtype="float32"))
    provenance_service.write_run_record(project["folder"], {"mintpy_run": {"warnings": ["left out on purpose"]}})
    mintpy_service._runs.pop(project["id"], None)

    status = mintpy_service.get_status(project["id"], project["folder"])
    assert status["status"] == "SUCCEEDED" and status["warnings"] == ["left out on purpose"]


def test_the_record_names_the_files_the_results_come_from(project, monkeypatch):
    work = project["folder"] / "mintpy"
    import h5py
    import numpy as np

    def series(name, value):
        with h5py.File(work / name, "w") as f:
            f.attrs.update({"X_FIRST": 0.0, "Y_FIRST": 1.0, "X_STEP": 0.1, "Y_STEP": -0.1, "WIDTH": 2, "LENGTH": 2})
            f.create_dataset("timeseries", data=np.full((1, 2, 2), value, dtype="float32"))

    series("timeseries.h5", 0.001)
    series("timeseries_ERA5_demErr.h5", 0.002)
    with h5py.File(work / "velocity.h5", "w") as f:
        f.attrs.update({"X_FIRST": 0.0, "Y_FIRST": 1.0, "X_STEP": 0.1, "Y_STEP": -0.1, "WIDTH": 2, "LENGTH": 2,
                        "FILE_PATH": "/elsewhere/timeseries_ERA5_demErr.h5"})
        f.create_dataset("velocity", data=np.zeros((2, 2), dtype="float32"))

    record, _ = _run_inline(project, monkeypatch, _fake_adapter(code=0))
    assert record["results"] == {"timeseries": "timeseries_ERA5_demErr.h5", "velocity": "velocity.h5", "reference": None}


def test_mintpys_rewrite_of_the_config_file_does_not_change_what_is_recorded(project, monkeypatch):
    """The real MintPy overwrites smallbaselineApp.cfg with its own full template, where
    most options read `auto`. The record must keep the config this app wrote, and the
    effective options must still resolve MintPy's `auto` values."""
    pytest.importorskip("mintpy")

    class RewritingProcess(FakeProcess):
        def wait(self):
            (self._folder / "mintpy" / "smallbaselineApp.cfg").write_text(
                "mintpy.troposphericDelay.method = auto\nmintpy.unwrapError.method = auto\n"
            )
            return super().wait()

    class Adapter(_fake_adapter(code=0)):
        def start_full_pipeline(self, config_path=None, start_step=None, end_step=None):
            return RewritingProcess(self.downloads_dir, 0)

    record, _ = _run_inline(project, monkeypatch, Adapter)
    assert "auto" not in record["mintpy"]["custom_config"]
    assert record["mintpy"]["custom_config"].startswith("mintpy.load.processor = hyp3")
    assert record["mintpy"]["options"]["mintpy.troposphericDelay.method"] != "auto"
    assert record["mintpy"]["options"]["mintpy.unwrapError.method"] != "auto"


def test_an_older_record_gets_its_reference_point_from_the_results(project):
    """provenance.json files written before the reference point was recorded."""
    import h5py
    import numpy as np

    folder = project["folder"]
    with h5py.File(folder / "mintpy" / "velocity.h5", "w") as f:
        for key, value in {"X_FIRST": 10.0, "Y_FIRST": 50.0, "X_STEP": 0.01, "Y_STEP": -0.01, "WIDTH": 3,
                           "LENGTH": 3, "REF_Y": 1, "REF_X": 2, "REF_DATE": "20200101"}.items():
            f.attrs[key] = value
        f.create_dataset("velocity", data=np.zeros((3, 3), dtype="float32"))
    (folder / "provenance.json").write_text('{"mintpy_run": {"status": "SUCCEEDED"}, "results": {"velocity": "velocity.h5"}}')
    db = SessionLocal()
    try:
        data = provenance_service.get_provenance(db, db.get(Project, project["id"]))
    finally:
        db.close()
    ref = data["last_mintpy_run"]["results"]["reference"]
    assert (ref["row"], ref["column"], ref["date"]) == (1, 2, "2020-01-01")


def test_a_run_stopped_by_clean_triplets_resumes_and_says_so(project, monkeypatch):
    """MintPy fails while plotting a histogram when every triplet closes. The run goes on."""
    starts = []
    work_dir = project["folder"] / "mintpy"

    class Adapter(_fake_adapter(code=0)):
        def start_full_pipeline(self, config_path=None, start_step=None, end_step=None):
            starts.append(start_step)
            if start_step == "quick_overview":
                (work_dir / "mintpy.log").write_text(
                    "File plot.py, in plot_num_triplet_with_nonzero_integer_ambiguity\n"
                    "ValueError: `bins` must be positive, when an integer\n")
                return FakeProcess(self.downloads_dir, 1)
            return FakeProcess(self.downloads_dir, 0)

    record, _ = _run_inline(project, monkeypatch, Adapter)
    assert starts == [None, "quick_overview", "correct_unwrap_error"]
    assert record["mintpy_run"]["status"] == "SUCCEEDED"
    assert any("triplet" in w for w in record["mintpy_run"]["warnings"])


def test_other_failures_are_not_retried(project, monkeypatch):
    calls = []
    record, _ = _run_inline(project, monkeypatch, _fake_adapter(code=1, calls=calls))
    assert calls == [("start", None, "reference_point")]   # the first stage failed, nothing else ran
    assert record["mintpy_run"]["status"] == "FAILED"


def test_the_reference_pixel_is_fixed_between_the_two_stages(project, monkeypatch):
    calls = []
    record, _ = _run_inline(project, monkeypatch, _fake_adapter(code=0, calls=calls))
    assert calls == [("start", None, "reference_point"), ("pin",), ("start", "quick_overview", None)]
    assert record["mintpy_run"]["status"] == "SUCCEEDED"


def test_a_reference_pixel_chosen_by_the_user_is_left_alone(project, monkeypatch):
    calls = []

    class Adapter(_fake_adapter(code=0, calls=calls)):
        def write_config(self, config_path=None, geometry=None, extra_options=None):
            path = super().write_config(config_path, geometry, extra_options)
            path.write_text(path.read_text() + "mintpy.reference.yx = 12, 30\n")
            return path

    _run_inline(project, monkeypatch, Adapter)
    assert ("pin",) not in calls
