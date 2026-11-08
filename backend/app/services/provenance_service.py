"""How each result was produced, recorded so it can be reproduced and defended.

Three places hold it:

* `Batch.parameters` (database): the HyP3 parameters, pairing rule, search window
  and software versions each batch was submitted with.
* `<project folder>/provenance.json`: written by every MintPy run, with the
  complete picture (batches, every pair, MintPy's effective options, the crop,
  software versions). It sits at the project root rather than in `mintpy/` so
  backups and archives carry it with the data.
* `GET /api/projects/{id}/provenance`: both, merged.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.config import APP_VERSION
from insar_core.adapters.hyp3 import DEFAULT_INSAR_PARAMETERS
from insar_core.pipeline.provenance import software_versions
from insar_core.pipeline.mintpy_timeseries import locate_velocity_file, read_reference_point

SCHEMA = 1
FILE_NAME = "provenance.json"


def batch_parameters(project, max_temporal_neighbors: int) -> dict:
    """What a batch submitted for `project` right now is produced with."""
    return {
        "schema": SCHEMA,
        "processor": "HyP3 INSAR_GAMMA",
        "hyp3": dict(DEFAULT_INSAR_PARAMETERS),
        "pairing": {"method": "temporal_neighbors", "max_temporal_neighbors": max_temporal_neighbors},
        "search": {
            "track_number": project.track_number,
            "flight_direction": project.flight_direction,
            "date_start": project.date_start,
            "date_end": project.date_end,
        },
        "software": software_versions(),
        "app_version": APP_VERSION,
    }


def _iso(value) -> Optional[str]:
    return value.isoformat() if value is not None else None


def project_context(db, project) -> dict:
    """The project, its batches and every interferogram pair, as plain data."""
    from app.models import Batch, Job

    batches = (
        db.query(Batch).filter(Batch.project_id == project.id).order_by(Batch.created_at).all()
    )
    jobs = (
        db.query(Job)
        .join(Batch, Batch.id == Job.batch_id)
        .filter(Batch.project_id == project.id)
        .order_by(Job.reference_date, Job.secondary_date)
        .all()
    )
    return {
        "project": {
            "id": project.id,
            "name": project.name,
            "description": project.description,
            "track_number": project.track_number,
            "flight_direction": project.flight_direction,
            "date_start": project.date_start,
            "date_end": project.date_end,
            "max_temporal_neighbors": project.max_temporal_neighbors,
            "geometry": project.geometry,
        },
        "batches": [
            {
                "id": b.id,
                "label": b.label,
                "created_at": _iso(b.created_at),
                "total_pairs": b.total_pairs,
                "status": b.status,
                "auto_download": bool(b.auto_download),
                "parameters": b.parameters,
            }
            for b in batches
        ],
        "pairs": [
            {
                "batch_id": j.batch_id,
                "reference_granule": j.reference_granule,
                "secondary_granule": j.secondary_granule,
                "reference_date": j.reference_date,
                "secondary_date": j.secondary_date,
                "hyp3_job_id": j.hyp3_job_id,
                "status": j.status.value if j.status else None,
                "downloaded": bool(j.downloaded),
                "credit_cost": j.credit_cost,
            }
            for j in jobs
        ],
    }


def provenance_path(project_folder: Path) -> Path:
    return Path(project_folder) / FILE_NAME


def write_run_record(project_folder: Path, record: dict) -> None:
    """Write `record` as the project's provenance.json, atomically."""
    record = {**record, "schema": SCHEMA, "written_at": datetime.now(timezone.utc).isoformat()}
    target = provenance_path(project_folder)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, target)


def read_run_record(project_folder: Optional[Path]) -> Optional[dict]:
    if project_folder is None:
        return None
    path = provenance_path(project_folder)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _backfill_reference(record: dict, folder: Path) -> None:
    """Runs recorded before the reference point was saved: read it from the results
    on disk (the velocity file holds it), so older projects show it too."""
    results = record.setdefault("results", {})
    if "reference" in results:
        return
    try:
        velocity = locate_velocity_file(folder / "mintpy")
        results["reference"] = read_reference_point(velocity)
    except (FileNotFoundError, OSError):
        results["reference"] = None


def get_provenance(db, project) -> dict:
    """Database context plus the last MintPy run's own record, if its folder is here."""
    folder = Path(project.storage_path) if project.storage_path and not project.archived_remote else None
    context = project_context(db, project)
    last_run = read_run_record(folder)
    if last_run is not None and folder is not None:
        _backfill_reference(last_run, folder)
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "app_version": APP_VERSION,
        "archived": bool(project.archived_remote),
        **context,
        "last_mintpy_run": last_run,
    }
