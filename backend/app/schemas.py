"""
Pydantic schemas: API request and response shapes.

Kept separate from SQLAlchemy models to avoid coupling the API contract
to internal table details, and to get automatic validation of incoming data.
"""

from datetime import date, datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


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


def _check_iso_date(value: Optional[str]) -> Optional[str]:
    if value is None:
        return value
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Dates must be ISO formatted (YYYY-MM-DD)") from exc
    return value


class ProjectUpdate(BaseModel):
    """Editable project fields. The AOI, track and start date are fixed once
    created because already-downloaded (and cropped) data depends on them."""
    description: Optional[str] = None
    date_end: Optional[str] = None
    max_temporal_neighbors: Optional[int] = Field(None, ge=1, le=10)
    backup_enabled: Optional[bool] = None

    _validate_date_end = field_validator("date_end")(_check_iso_date)


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
    backup_enabled: bool = True
    archived_remote: Optional[str] = None
    archived_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


# ---------- Batch ----------

class BatchOut(BaseModel):
    id: str
    project_id: str
    label: Optional[str]
    total_pairs: int
    status: str
    auto_download: bool
    run_mintpy_when_done: bool = False
    created_at: datetime

    class Config:
        from_attributes = True


class BatchUpdate(BaseModel):
    auto_download: Optional[bool] = None
    label: Optional[str] = Field(None, max_length=80)

    @field_validator("label")
    @classmethod
    def _label_not_blank(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("The name cannot be empty")
        return value


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
    auto_download: bool = False


class ProjectRefreshRequest(BaseModel):
    """Extend a project to newer acquisitions and process only what is new."""
    date_end: Optional[str] = Field(None, description="New end date. Defaults to today. Never moves the end date back.")
    max_temporal_neighbors: Optional[int] = Field(None, ge=1, le=10)
    dry_run: bool = True
    run_mintpy: bool = Field(True, description="Run MintPy once every new interferogram is downloaded")

    _validate_date_end = field_validator("date_end")(_check_iso_date)


class ProjectRefreshOut(BaseModel):
    date_end: str
    total_pairs: int
    scene_count: Optional[int] = None
    estimated_size_gb: Optional[float] = None
    batch: Optional[BatchOut] = None


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
    download_url: Optional[str] = None
    file_name: Optional[str] = None
    already_downloaded: bool = False


# ---------- Raw SLC downloads (input for external PS-InSAR pipelines) ----------

class SLCDownloadRequest(BaseModel):
    scenes: list[SceneOut]


class SLCDownloadOut(BaseModel):
    id: str
    project_id: str
    destination_path: str
    filenames: list[str]
    created_at: datetime

    class Config:
        from_attributes = True


# ---------- Batch submission ----------

class BatchPlanOut(BaseModel):
    total_pairs: int
    scene_count: int
    pairs_preview: list[list[str]]  # [[ref, sec], ...]
    estimated_size_gb: Optional[float] = None


# ---------- EGMS (Copernicus ground motion) ----------

class EGMSSearchRequest(BaseModel):
    """Search for EGMS ground-motion products covering an AOI."""
    geometry: dict[str, Any]
    level: str = Field(..., description="'L2A', 'L2B', or 'L3'")
    release: str = Field(..., description="Processing vintage, e.g. '2019-2023'")
    direction: Optional[str] = Field(None, description="'ascending'/'descending' - required for L2A/L2B")
    product_type: Optional[str] = Field(None, description="e.g. 'ORTHO-UP' - required for L3")
    tile_id: Optional[str] = None


class EGMSProductOut(BaseModel):
    query_id: str
    filename: str
    level: str
    size_mb: Optional[float]


class EGMSDownloadRequest(BaseModel):
    products: list[EGMSProductOut]
    destination_name: str = Field(..., description="Subfolder name for these downloads")
    storage_mountpoint: Optional[str] = Field(None, description="Omit for the app default")
    # Search params that produced `products`, kept so the download is browsable/visualizable later.
    geometry: dict[str, Any]
    level: str
    release: str
    direction: Optional[str] = None
    product_type: Optional[str] = None
    tile_id: Optional[str] = None


class EGMSDownloadOut(BaseModel):
    id: str
    name: str
    geometry: dict[str, Any]
    level: str
    release: str
    direction: Optional[str]
    product_type: Optional[str]
    tile_id: Optional[str]
    destination_path: str
    filenames: list[str]
    created_at: datetime

    class Config:
        from_attributes = True


# ---------- Time series (shared by EGMS and MintPy) ----------

class TimeSeriesOut(BaseModel):
    """One point's displacement history. Both the EGMS and MintPy time series
    endpoints return this same shape so the frontend has a single chart
    component instead of one per data source."""
    dates: list[str]        # "YYYYMMDD"
    values_mm: list[float]  # cumulative line-of-sight displacement, in mm
    # Statistical summary (see insar_core.analysis.timeseries); None when the
    # series has too few valid observations or the analysis failed.
    analysis: Optional[dict[str, Any]] = None


# ---------- Downloads inventory (ASF/HyP3 side) ----------

class ProjectDownloadSummaryOut(BaseModel):
    storage_path: Optional[str]
    total_jobs: int
    downloaded_jobs: int


# ---------- Storage ----------

class StorageTargetOut(BaseModel):
    mountpoint: Optional[str]  # None = app default
    device: str
    fstype: str
    total_gb: float
    free_gb: float
    writable: bool


class PathUsageOut(BaseModel):
    path: str
    used_gb: float
    free_gb: float
    total_gb: float


class MoveRequest(BaseModel):
    mountpoint: Optional[str] = None  # None = app default


class MoveStateOut(BaseModel):
    active: bool
    status: str  # "idle" | "running" | "done" | "error"
    pct: float
    src: Optional[str] = None
    dst: Optional[str] = None
    error: Optional[str] = None


# ---------- Backup ----------

class BackupConfigIn(BaseModel):
    destination: Optional[str] = Field(
        None, description="Absolute local path, or <rclone remote>:<path>. Empty clears it."
    )
    enabled: bool = False
    interval_hours: int = Field(24, ge=1, le=24 * 30)
    keep_daily: int = Field(14, ge=1, le=365)
    keep_weekly: int = Field(8, ge=0, le=520)
    include_slc: bool = False


class BackupConfigOut(BackupConfigIn):
    last_run_at: Optional[datetime] = None
    last_status: Optional[str] = None
    last_message: Optional[str] = None

    class Config:
        from_attributes = True


class RcloneInfoOut(BaseModel):
    available: bool
    version: Optional[str] = None
    remotes: list[str] = []


class BackupStateOut(BaseModel):
    running: bool
    status: str
    step: Optional[str] = None
    pct: float = 0
    error: Optional[str] = None


class BackupOverviewOut(BaseModel):
    rclone: RcloneInfoOut
    config: BackupConfigOut
    state: BackupStateOut


class BackupTestRequest(BaseModel):
    destination: str


class ArchiveRequest(BaseModel):
    destination: Optional[str] = Field(
        None, description="Base rclone destination. Defaults to the backup destination."
    )


class ArchiveStateOut(BaseModel):
    project_id: Optional[str] = None
    kind: Optional[str] = None      # "archive" | "restore"
    active: bool = False
    status: str = "idle"            # idle | running | done | error
    step: Optional[str] = None
    pct: float = 0
    error: Optional[str] = None
