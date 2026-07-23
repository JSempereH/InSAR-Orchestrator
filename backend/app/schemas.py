"""
Pydantic schemas: API request and response shapes.

Kept separate from SQLAlchemy models to avoid coupling the API contract
to internal table details, and to get automatic validation of incoming data.
"""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------- Project ----------

class ProjectCreate(BaseModel):
    name: str
    description: Optional[str] = None
    geometry: dict[str, Any] = Field(..., description="GeoJSON Polygon or Feature for the area of interest")
    track_number: Optional[int] = None
    flight_direction: Optional[str] = None
    date_start: Optional[str] = None
    date_end: Optional[str] = None
    max_temporal_neighbors: int = 3
    storage_mountpoint: Optional[str] = Field(
        None, description="Mount point of the disk to store this project's downloads on. Omit for the app default."
    )


class ProjectOut(BaseModel):
    id: str
    name: str
    description: Optional[str]
    geometry: dict[str, Any]
    track_number: Optional[int]
    flight_direction: Optional[str]
    date_start: Optional[str]
    date_end: Optional[str]
    max_temporal_neighbors: int
    storage_path: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


# ---------- Batch ----------

class BatchOut(BaseModel):
    id: str
    project_id: str
    label: Optional[str]
    total_pairs: int
    created_at: datetime

    class Config:
        from_attributes = True


# ---------- Job ----------

class JobOut(BaseModel):
    id: str
    batch_id: str
    hyp3_job_id: Optional[str]
    reference_granule: str
    secondary_granule: str
    reference_date: Optional[str]
    secondary_date: Optional[str]
    status: str
    credit_cost: Optional[float]
    downloaded: int
    download_path: Optional[str]
    error_message: Optional[str]
    submitted_at: Optional[datetime]
    completed_at: Optional[datetime]
    is_downloading: bool = False  # live flag from download_state, not persisted

    class Config:
        from_attributes = True


# ---------- Actions ----------

class SceneSearchRequest(BaseModel):
    """Search for available scenes before creating a batch."""
    geometry: dict[str, Any]
    date_start: str
    date_end: str
    track_number: Optional[int] = None
    flight_direction: Optional[str] = None


class SceneSearchResult(BaseModel):
    available_tracks: list[dict[str, Any]]


class SubmitBatchRequest(BaseModel):
    label: Optional[str] = None
    max_temporal_neighbors: int = 3
    dry_run: bool = True


# ---------- Credentials ----------

class CredentialUpsert(BaseModel):
    provider: str = "earthdata"
    username: str
    password: str


class CredentialOut(BaseModel):
    id: str
    provider: str
    username: str
    updated_at: datetime

    class Config:
        from_attributes = True


# ---------- Scene search (response) ----------

class TrackSummaryOut(BaseModel):
    track_number: int
    flight_direction: str
    scene_count: int
    first_date: str
    last_date: str


class SceneOut(BaseModel):
    file_id: str
    granule_name: str
    acquisition_date: str
    orbit: int
    track_number: int
    flight_direction: str
    polarization: str
    size_mb: Optional[float]


# ---------- Batch submission ----------

class BatchPlanOut(BaseModel):
    total_pairs: int
    scene_count: int
    pairs_preview: list[list[str]]  # [[ref, sec], ...]


# ---------- Storage ----------

class StorageTargetOut(BaseModel):
    mountpoint: Optional[str]  # None = app default
    device: str
    fstype: str
    total_gb: float
    free_gb: float
    writable: bool
