"""provenance.json and the provenance endpoint: how a result was produced."""
import json
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.models import Batch, Job, JobStatus, Project
from app.services import mintpy_service, provenance_service
from insar_core.engines import get_engine

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
        def __init__(self, downloads_dir, work_dir, engine=None):
            self.downloads_dir, self.work_dir = Path(downloads_dir), Path(work_dir)
            self.engine = get_engine(engine.info.id if engine else None)

        def prepare_inputs(self, geometry=None):
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
        chosen = [line.split("=")[1].strip() for line in text.splitlines()
                  if line.startswith("mintpy.troposphericDelay.method")]
        return {"mintpy.troposphericDelay.method": chosen[-1] if chosen else method}

    monkeypatch.setattr(mintpy_service, "mintpy_effective_options", effective)


def test_without_any_weather_source_the_era5_correction_is_turned_off_and_says_so(monkeypatch):
    _patch_defaults(monkeypatch)
    monkeypatch.setattr(mintpy_service, "era5_source", lambda: None)
    options, warnings, source = mintpy_service._troposphere_options()
    assert options == {"mintpy.troposphericDelay.method": "no"} and source is None
    assert len(warnings) == 1 and "ERA5" in warnings[0] and "cdsapirc" in warnings[0] and "ARCO" in warnings[0]


@pytest.mark.parametrize("source", ["arco", "cds"])
def test_with_a_weather_source_mintpys_default_is_left_alone(monkeypatch, source):
    _patch_defaults(monkeypatch)
    monkeypatch.setattr(mintpy_service, "era5_source", lambda: source)
    assert mintpy_service._troposphere_options() == ({}, [], source)


def test_nothing_is_overridden_when_the_default_is_not_pyaps_or_unknown(monkeypatch):
    monkeypatch.setattr(mintpy_service, "era5_source", lambda: None)
    _patch_defaults(monkeypatch, method="gacos")
    assert mintpy_service._troposphere_options() == ({}, [], None)
    _patch_defaults(monkeypatch, method=None)
    assert mintpy_service._troposphere_options() == ({}, [], None)


def test_the_weather_source_is_given_to_mintpy_and_recorded(project, monkeypatch):
    _patch_defaults(monkeypatch)
    monkeypatch.setattr(mintpy_service, "era5_source", lambda: "arco")
    seen = {}

    class Adapter(_fake_adapter(code=0)):
        def start_full_pipeline(self, config_path=None, start_step=None, end_step=None):
            seen["era5_source"] = self.era5_source
            return super().start_full_pipeline(config_path, start_step, end_step)

    record, _ = _run_inline(project, monkeypatch, Adapter)
    assert seen["era5_source"] == "arco"
    assert "ARCO-ERA5" in record["mintpy"]["weather_data"]
    assert record["mintpy_run"]["warnings"] == []


def test_a_run_without_a_login_passes_the_override_and_records_the_warning(project, monkeypatch):
    seen = {}

    class Adapter:
        def __init__(self, downloads_dir, work_dir, engine=None):
            self.work_dir = Path(work_dir)
            self.engine = get_engine(None)

        def prepare_inputs(self, geometry=None):
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
    monkeypatch.setattr(mintpy_service, "era5_source", lambda: None)
    record, _ = _run_inline(project, monkeypatch, Adapter)

    # the unwrapping correction is the app's own step, so MintPy is always told to leave it
    assert seen["extra_options"] == {"mintpy.troposphericDelay.method": "no", "mintpy.unwrapError.method": "no"}
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
    assert starts == [None, "quick_overview", "invert_network"]
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
    assert calls == [("start", None, "reference_point"), ("pin",), ("start", "quick_overview", "quick_overview"),
                     ("start", "invert_network", None)]
    assert record["mintpy_run"]["status"] == "SUCCEEDED"


# ── unwrapping errors, corrected between two MintPy stages ───────────────────

def _adapter_with_a_stack(neighbours=3, calls=None, seen=None):
    """A MintPy stand-in whose first stage leaves a small stack with one patch a cycle off."""
    import h5py
    import numpy as np

    dates = [f"2023{m:02d}{d:02d}" for m in range(1, 4) for d in (3, 15, 27)]
    pairs = [(a, b) for i, a in enumerate(dates) for b in dates[i + 1:i + 1 + neighbours]]

    class Adapter(_fake_adapter(code=0, calls=calls)):
        def write_config(self, config_path=None, geometry=None, extra_options=None):
            path = super().write_config(config_path, geometry, extra_options)
            path.write_text(path.read_text() + "".join(f"{k} = {v}\n" for k, v in (extra_options or {}).items()))
            return path

        def start_full_pipeline(self, config_path=None, start_step=None, end_step=None):
            if start_step is None:
                rng = np.random.default_rng(0)
                per_date = {d: rng.normal(0, 3.0, (12, 16)) for d in dates}
                phase = np.array([per_date[b] - per_date[a] for a, b in pairs], dtype="float32")
                phase[4, 3:8, 4:10] += 2 * np.pi
                (self.work_dir / "inputs").mkdir(parents=True, exist_ok=True)
                with h5py.File(self.work_dir / "inputs" / "ifgramStack.h5", "w") as f:
                    f.create_dataset("unwrapPhase", data=phase)
                    f.create_dataset("date", data=np.array([[a.encode(), b.encode()] for a, b in pairs]))
                    f.create_dataset("dropIfgram", data=np.ones(len(pairs), dtype=bool))
                    f.attrs.update({"REF_Y": 0, "REF_X": 0})
            if start_step == "invert_network" and seen is not None:
                seen["config_at_inversion"] = Path(config_path).read_text()
            return super().start_full_pipeline(config_path, start_step, end_step)

    return Adapter


def test_unwrapping_errors_are_corrected_before_the_inversion_and_mintpy_is_pointed_at_the_result(project, monkeypatch):
    import h5py
    from insar_core.analysis import unwrap_correction

    seen = {}
    record, work_dir = _run_inline(project, monkeypatch, _adapter_with_a_stack(seen=seen))
    assert record["mintpy_run"]["status"] == "SUCCEEDED" and not record["mintpy_run"]["warnings"]
    # MintPy's inversion reads the corrected phases when its phase closure method is named
    assert "mintpy.unwrapError.method  = phase_closure" in seen["config_at_inversion"]
    assert "mintpy.unwrapError.method  = phase_closure" in record["mintpy"]["custom_config"]
    with h5py.File(work_dir / "inputs" / "ifgramStack.h5") as f:
        assert unwrap_correction.DATASET_OUT in f
    assert unwrap_correction.read_summary(work_dir)["pixels"]["corrected"] == 30


def _adapter_that_finishes(seen=None):
    """As above, and the last stage leaves the mask MintPy would."""
    import h5py
    import numpy as np

    class Adapter(_adapter_with_a_stack(seen=seen)):
        def start_full_pipeline(self, config_path=None, start_step=None, end_step=None):
            process = super().start_full_pipeline(config_path, start_step, end_step)
            if start_step is None:
                # make a patch of the stack unreadable: closures near half a cycle
                with h5py.File(self.work_dir / "inputs" / "ifgramStack.h5", "r+") as f:
                    phase = f["unwrapPhase"][:]
                    phase[:, 9:12, 10:16] += np.random.default_rng(1).normal(0, 1.6, (phase.shape[0], 3, 6)).astype("float32")
                    f["unwrapPhase"][...] = phase
            if start_step == "invert_network":
                with h5py.File(self.work_dir / "maskTempCoh.h5", "w") as f:
                    f.create_dataset("mask", data=np.ones((12, 16), dtype=bool))
                    f.attrs.update({"REF_Y": 0, "REF_X": 0})
            return process

    return Adapter


def test_pixels_whose_closures_cannot_be_read_are_left_out_of_the_result(project, monkeypatch):
    import h5py
    from insar_core.pipeline import mask_refinement

    record, work_dir = _run_inline(project, monkeypatch, _adapter_that_finishes())
    assert record["mintpy_run"]["status"] == "SUCCEEDED" and not record["mintpy_run"]["warnings"]
    summary = mask_refinement.read_summary(work_dir)
    assert summary["kept_by_mintpy"] == 192 and 10 <= summary["dropped"]["unreadable_closures"] <= 18
    with h5py.File(work_dir / "maskTempCoh.h5") as f:
        mask = f["mask"][:].astype(bool)
    assert not mask[9:12, 10:16].all() and mask[3:8, 4:10].all()        # the corrected patch stays, the noisy one goes
    with h5py.File(work_dir / mask_refinement.ORIGINAL_FILE) as f:
        assert f["mask"][:].all()


def test_the_stricter_mask_is_off_by_default_when_the_network_has_long_pairs_and_on_when_asked(project, monkeypatch):
    import h5py
    import numpy as np
    from insar_core.pipeline import mask_refinement

    work_dir = project["folder"] / "mintpy"
    base = _adapter_that_finishes()

    class WithLongPairs(base):
        def start_full_pipeline(self, config_path=None, start_step=None, end_step=None):
            process = super().start_full_pipeline(config_path, start_step, end_step)
            if start_step is None:
                with h5py.File(self.work_dir / "inputs" / "ifgramStack.h5", "r+") as f:
                    dates = f["date"][:]
                    first, last = dates[0][0], dates[-1][1]
                    n = len(dates)
                    phase = f["unwrapPhase"][:]
                    for name in ("unwrapPhase", "date", "dropIfgram"):
                        del f[name]
                    extra = np.array([[first, last]])           # one pair spanning the whole period
                    f.create_dataset("unwrapPhase", data=np.concatenate([phase, phase[:1]]))
                    f.create_dataset("date", data=np.concatenate([dates, extra]))
                    f.create_dataset("dropIfgram", data=np.ones(n + 1, dtype=bool))
                    f.attrs.update({"REF_Y": 0, "REF_X": 0})
            return process

    monkeypatch.setattr(mintpy_service, "MintPyAdapter", WithLongPairs)
    mintpy_service._runs[project["id"]] = mintpy_service.MintPyRunState(project_id=project["id"], work_dir=str(work_dir))
    mintpy_service._run(project["id"], project["folder"], work_dir, GEOMETRY, _context(project["id"]))
    assert mask_refinement.read_summary(work_dir) is None              # left to the person

    mintpy_service._runs[project["id"]] = mintpy_service.MintPyRunState(
        project_id=project["id"], work_dir=str(work_dir), options={"app.mask.unreadableClosures": "yes"})
    mintpy_service._run(project["id"], project["folder"], work_dir, GEOMETRY, _context(project["id"]))
    assert mask_refinement.read_summary(work_dir) is not None          # asked for


def test_the_stricter_mask_is_left_out_when_the_person_turns_it_off(project, monkeypatch):
    from insar_core.pipeline import mask_refinement

    work_dir = project["folder"] / "mintpy"
    monkeypatch.setattr(mintpy_service, "MintPyAdapter", _adapter_that_finishes())
    mintpy_service._runs[project["id"]] = mintpy_service.MintPyRunState(
        project_id=project["id"], work_dir=str(work_dir), options={"app.mask.unreadableClosures": "no"})
    mintpy_service._run(project["id"], project["folder"], work_dir, GEOMETRY, _context(project["id"]))
    assert mask_refinement.read_summary(work_dir) is None
    assert "app." not in (work_dir / "smallbaselineApp.cfg").read_text()   # never handed to MintPy


def test_the_correction_is_left_out_when_the_person_turns_it_off(project, monkeypatch):
    from insar_core.analysis import unwrap_correction

    seen = {}
    work_dir = project["folder"] / "mintpy"
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / unwrap_correction.SUMMARY_FILE).write_text("{}")          # the account of an earlier run
    monkeypatch.setattr(mintpy_service, "MintPyAdapter", _adapter_with_a_stack(seen=seen))
    mintpy_service._runs[project["id"]] = mintpy_service.MintPyRunState(
        project_id=project["id"], work_dir=str(work_dir), options={"mintpy.unwrapError.method": "no"})
    mintpy_service._run(project["id"], project["folder"], work_dir, GEOMETRY, _context(project["id"]))
    assert "phase_closure" not in seen["config_at_inversion"]
    assert unwrap_correction.read_summary(work_dir) is None                # and the old account does not outlive it


def test_a_network_too_open_to_correct_is_said_only_when_the_correction_was_asked_for(project, monkeypatch):
    record, _ = _run_inline(project, monkeypatch, _adapter_with_a_stack(neighbours=1))
    assert record["mintpy_run"]["status"] == "SUCCEEDED" and not record["mintpy_run"]["warnings"]

    work_dir = project["folder"] / "mintpy"
    monkeypatch.setattr(mintpy_service, "MintPyAdapter", _adapter_with_a_stack(neighbours=1))
    mintpy_service._runs[project["id"]] = mintpy_service.MintPyRunState(
        project_id=project["id"], work_dir=str(work_dir), options={"mintpy.unwrapError.method": "phase_closure"})
    mintpy_service._run(project["id"], project["folder"], work_dir, GEOMETRY, _context(project["id"]))
    record = provenance_service.read_run_record(project["folder"])
    assert record["mintpy_run"]["status"] == "SUCCEEDED"
    assert any("not corrected" in w and "triplet" in w for w in record["mintpy_run"]["warnings"])


def test_a_reference_pixel_chosen_by_the_user_is_left_alone(project, monkeypatch):
    calls = []

    class Adapter(_fake_adapter(code=0, calls=calls)):
        def write_config(self, config_path=None, geometry=None, extra_options=None):
            path = super().write_config(config_path, geometry, extra_options)
            path.write_text(path.read_text() + "mintpy.reference.yx = 12, 30\n")
            return path

    _run_inline(project, monkeypatch, Adapter)
    assert ("pin",) not in calls


# ── options and reference point chosen by the person ─────────────────────────

def _run_with(project, monkeypatch, options=None, reference=None, adapter=None):
    seen = {}
    base = adapter or _fake_adapter()

    class Adapter(base):
        def write_config(self, config_path=None, geometry=None, extra_options=None):
            seen["extra_options"] = dict(extra_options or {})
            path = super().write_config(config_path, geometry, extra_options)
            text = path.read_text() + "".join(f"{k} = {v}\n" for k, v in (extra_options or {}).items())
            path.write_text(text)
            return path

        def start_full_pipeline(self, config_path=None, start_step=None, end_step=None):
            seen.setdefault("marker", mintpy_service._read_marker(self.work_dir))
            return super().start_full_pipeline(config_path, start_step, end_step)

    monkeypatch.setattr(mintpy_service, "MintPyAdapter", Adapter)
    work_dir = project["folder"] / "mintpy"
    mintpy_service._runs[project["id"]] = mintpy_service.MintPyRunState(
        project_id=project["id"], work_dir=str(work_dir), options=options or {}, reference=reference,
    )
    mintpy_service._run(project["id"], project["folder"], work_dir, GEOMETRY, _context(project["id"]))
    return seen, provenance_service.read_run_record(project["folder"])


def test_the_persons_options_reach_the_config_and_the_provenance_record(project, monkeypatch):
    monkeypatch.setattr(mintpy_service, "era5_source", lambda: "arco")
    seen, record = _run_with(project, monkeypatch, options={"mintpy.deramp": "linear", "mintpy.network.tempBaseMax": "48"})
    assert seen["extra_options"]["mintpy.deramp"] == "linear"
    assert record["mintpy"]["custom_config"].count("mintpy.network.tempBaseMax = 48") == 1


def test_a_chosen_reference_point_is_written_and_the_app_does_not_pick_another(project, monkeypatch):
    calls = []
    seen, _ = _run_with(project, monkeypatch, reference={"lat": 40.5, "lon": -3.6}, adapter=_fake_adapter(calls=calls))
    assert seen["extra_options"]["mintpy.reference.lalo"] == "40.500000,-3.600000"
    assert ("pin",) not in calls


def test_the_request_is_kept_in_the_marker_so_a_restart_repeats_it(project, monkeypatch):
    seen, _ = _run_with(project, monkeypatch, options={"mintpy.deramp": "linear"}, reference={"lat": 40.5, "lon": -3.6})
    assert seen["marker"]["options"] == {"mintpy.deramp": "linear"}
    assert seen["marker"]["reference"] == {"lat": 40.5, "lon": -3.6}


def test_era5_asked_for_with_no_weather_source_still_falls_back_and_says_so(project, monkeypatch):
    _patch_defaults(monkeypatch, method="no")
    monkeypatch.setattr(mintpy_service, "era5_source", lambda: None)
    seen, _ = _run_with(project, monkeypatch, options={"mintpy.troposphericDelay.method": "pyaps"})
    assert seen["extra_options"]["mintpy.troposphericDelay.method"] == "no"
    assert "ERA5" in mintpy_service._runs[project["id"]].warnings[0]


def test_another_troposphere_method_needs_no_weather_data(monkeypatch):
    _patch_defaults(monkeypatch)
    monkeypatch.setattr(mintpy_service, "era5_source", lambda: pytest.fail("should not be asked"))
    chosen = {"mintpy.troposphericDelay.method": "height_correlation"}
    assert mintpy_service._troposphere_options(chosen) == ({}, [], None)


def test_a_bad_option_is_refused_before_anything_starts(project):
    with pytest.raises(RuntimeError, match="Remove a ramp"):
        mintpy_service.start_run(project["id"], project["folder"], options={"mintpy.deramp": "cubic"})
    with pytest.raises(RuntimeError, match="latitude"):
        mintpy_service.start_run(project["id"], project["folder"], reference={"lat": 95, "lon": 0})
    assert project["id"] not in mintpy_service._runs


def test_the_endpoint_hands_the_request_to_the_service(client, project, monkeypatch):
    got = {}
    monkeypatch.setattr(mintpy_service, "start_run", lambda *a, **k: got.update(k) or {"status": "RUNNING"})
    reply = client.post(f"/api/projects/{project['id']}/mintpy/run",
                        json={"options": {"mintpy.deramp": "linear"}, "reference": {"lat": 40.5, "lon": -3.6}})
    assert reply.status_code == 200
    assert got["options"] == {"mintpy.deramp": "linear"} and got["reference"] == {"lat": 40.5, "lon": -3.6}
    client.post(f"/api/projects/{project['id']}/mintpy/run")      # no body, as before
    assert got["options"] is None and got["reference"] is None


def test_a_bad_request_is_a_422_that_says_why(client, project):
    reply = client.post(f"/api/projects/{project['id']}/mintpy/run", json={"options": {"mintpy.deramp": "cubic"}})
    assert reply.status_code == 422 and "Remove a ramp" in reply.json()["detail"]


def test_the_options_endpoint_lists_every_option_with_its_default(client, project, monkeypatch):
    monkeypatch.setattr(mintpy_service, "mintpy_effective_options", lambda text: {"mintpy.deramp": "no"})
    body = client.get(f"/api/projects/{project['id']}/mintpy/options").json()
    by_key = {o["key"]: o for o in body["options"]}
    assert by_key["mintpy.deramp"]["default"] == "no" and by_key["mintpy.deramp"]["choices"]
    assert by_key["mintpy.network.tempBaseMax"]["default"] == "auto"
    assert body["last_run"] == {} and body["last_reference"] is None


# ── quality checks ───────────────────────────────────────────────────────────

def test_a_successful_run_records_its_quality_checks(project, monkeypatch):
    monkeypatch.setattr(mintpy_service, "compute_qc", lambda work_dir: {
        "status": "warn", "checks": [{"id": "coherence", "status": "warn"}], "metrics": {"velocity_std_mm_yr_median": 3.0},
        "thresholds": {},
    })
    record, _ = _run_inline(project, monkeypatch, _fake_adapter())
    assert record["quality"] == {"status": "warn", "checks": [{"id": "coherence", "status": "warn"}],
                                 "metrics": {"velocity_std_mm_yr_median": 3.0}}


def test_a_failed_run_has_no_quality_section(project, monkeypatch):
    record, _ = _run_inline(project, monkeypatch, _fake_adapter(code=1))
    assert record["quality"] is None


def test_a_crash_in_the_checks_never_breaks_the_run(project, monkeypatch):
    def boom(work_dir):
        raise OSError("disk gone")

    monkeypatch.setattr(mintpy_service, "compute_qc", boom)
    record, _ = _run_inline(project, monkeypatch, _fake_adapter())
    assert record["mintpy_run"]["status"] == "SUCCEEDED" and record["quality"] is None


def test_the_quality_endpoint_reads_the_results_on_disk(client, project):
    reply = client.get(f"/api/projects/{project['id']}/mintpy/quality")
    assert reply.status_code == 422 and "no MintPy results" in reply.json()["detail"]
    import h5py
    work = project["folder"] / "mintpy"
    work.mkdir(parents=True, exist_ok=True)
    with h5py.File(work / "velocity.h5", "w") as handle:
        handle.attrs.update({"REF_Y": 5, "REF_X": 5, "LENGTH": 10, "WIDTH": 10})
    body = client.get(f"/api/projects/{project['id']}/mintpy/quality").json()
    assert body["status"] in ("ok", "warn", "fail", "unknown") and isinstance(body["checks"], list)


def _run_with_fading(project, monkeypatch, adapter, option="yes"):
    work_dir = project["folder"] / "mintpy"
    monkeypatch.setattr(mintpy_service, "MintPyAdapter", adapter)
    mintpy_service._runs[project["id"]] = mintpy_service.MintPyRunState(
        project_id=project["id"], work_dir=str(work_dir), options={"app.fading.correction": option})
    mintpy_service._run(project["id"], project["folder"], work_dir, GEOMETRY, _context(project["id"]))
    return provenance_service.read_run_record(project["folder"]), work_dir


def test_the_fading_correction_is_not_run_unless_asked_for(project, monkeypatch):
    from insar_core.analysis import fading_correction

    monkeypatch.setattr(fading_correction, "correct_stack", lambda *a, **k: pytest.fail("ran without being asked"))
    record, work_dir = _run_inline(project, monkeypatch, _adapter_with_a_stack())
    assert record["mintpy_run"]["status"] == "SUCCEEDED"
    assert fading_correction.read_summary(work_dir) is None


def test_a_network_that_cannot_fix_the_constants_runs_on_and_says_why(project, monkeypatch):
    import h5py
    import numpy as np

    class Adapter(_adapter_with_a_stack()):
        def start_full_pipeline(self, config_path=None, start_step=None, end_step=None):
            process = super().start_full_pipeline(config_path, start_step, end_step)
            if start_step is None:
                with h5py.File(self.work_dir / "inputs" / "ifgramStack.h5", "r+") as f:
                    f.create_dataset("coherence", data=np.full(f["unwrapPhase"].shape, 0.8, "float32"))
            return process

    record, work_dir = _run_with_fading(project, monkeypatch, Adapter)
    assert record["mintpy_run"]["status"] == "SUCCEEDED"
    assert any("fading bias was not corrected" in w and "long interferogram" in w for w in record["mintpy_run"]["warnings"])
    assert "app." not in (work_dir / "smallbaselineApp.cfg").read_text()          # never handed to MintPy


def test_when_the_fading_correction_is_made_mintpy_inverts_the_corrected_phases(project, monkeypatch):
    from insar_core.analysis import fading_correction

    def fake_correct_stack(work_dir, source="unwrapPhase"):
        (Path(work_dir) / fading_correction.SUMMARY_FILE).write_text("{}")
        return {"a1": 0.45, "a2": 0.26}

    monkeypatch.setattr(fading_correction, "correct_stack", fake_correct_stack)
    seen = {}
    record, work_dir = _run_with_fading(project, monkeypatch, _adapter_with_a_stack(seen=seen))
    assert record["mintpy_run"]["status"] == "SUCCEEDED" and not record["mintpy_run"]["warnings"]
    assert "mintpy.unwrapError.method  = phase_closure" in seen["config_at_inversion"]
