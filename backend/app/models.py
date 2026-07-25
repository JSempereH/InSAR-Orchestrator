"""
ORM models.

Hierarchy: Project -> Batch -> Job

- Project: an area of interest (AOI polygon) plus search parameters
  (orbital track, date range, etc.). One project = one study area.
- Batch: a submission of SBAS pairs to HyP3 from a given project.
  Multiple batches allow extending a time series without mixing history.
- Job: a single interferogram (one scene pair) submitted to HyP3,
  with the real HyP3 job ID needed to query status and download results.
"""

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Column, String, Float, Integer, DateTime, ForeignKey, Enum, Text, JSON
)
from sqlalchemy.orm import relationship

from app.database import Base


def gen_uuid():
    return str(uuid.uuid4())


class Credential(Base):
    """Encrypted storage for platform credentials (Earthdata, future providers)."""
    __tablename__ = "credentials"

    id = Column(String, primary_key=True, default=gen_uuid)
    provider = Column(String, nullable=False, unique=True)  # e.g. "earthdata"
    username = Column(String, nullable=False)
    encrypted_password = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class EGMSDownload(Base):
    """A completed (or in-progress) EGMS product download session for one AOI.

    One row per "search + download" action - `filenames` lists every product
    file placed under `destination_path`.
    """
    __tablename__ = "egms_downloads"

    id = Column(String, primary_key=True, default=gen_uuid)
    name = Column(String, nullable=False)
    geometry = Column(JSON, nullable=False)

    level = Column(String, nullable=False)         # L2A / L2B / L3
    release = Column(String, nullable=False)
    direction = Column(String, nullable=True)
    product_type = Column(String, nullable=True)
    tile_id = Column(String, nullable=True)

    destination_path = Column(String, nullable=False)
    filenames = Column(JSON, nullable=False)        # list[str]

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class JobStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class Project(Base):
    """An area of interest with its processing parameters."""
    __tablename__ = "projects"

    id = Column(String, primary_key=True, default=gen_uuid)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)

    # AOI geometry as GeoJSON stored in JSON column.
    # MapLibre Draw produces GeoJSON natively, stored as-is.
    geometry = Column(JSON, nullable=False)

    # Scene search parameters chosen by the user
    track_number = Column(Integer, nullable=True)       # relativeOrbit
    flight_direction = Column(String, nullable=True)    # ASCENDING / DESCENDING
    date_start = Column(String, nullable=True)          # ISO date
    date_end = Column(String, nullable=True)
    max_temporal_neighbors = Column(Integer, default=3)

    # Resolved absolute directory for this project's downloads.
    # NULL means "use the app-wide default (settings.downloads_dir)".
    storage_path = Column(String, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    batches = relationship("Batch", back_populates="project", cascade="all, delete-orphan")


class Batch(Base):
    """A submission of SBAS pairs to HyP3, linked to a Project."""
    __tablename__ = "batches"

    id = Column(String, primary_key=True, default=gen_uuid)
    project_id = Column(String, ForeignKey("projects.id"), nullable=False)

    label = Column(String, nullable=True)
    total_pairs = Column(Integer, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    project = relationship("Project", back_populates="batches")
    jobs = relationship("Job", back_populates="batch", cascade="all, delete-orphan")


class Job(Base):
    """A single interferogram submitted to HyP3."""
    __tablename__ = "jobs"

    id = Column(String, primary_key=True, default=gen_uuid)
    batch_id = Column(String, ForeignKey("batches.id"), nullable=False)

    # HyP3's own job ID, used to query status and download results
    hyp3_job_id = Column(String, nullable=True, index=True)

    reference_granule = Column(String, nullable=False)
    secondary_granule = Column(String, nullable=False)
    reference_date = Column(String, nullable=True)
    secondary_date = Column(String, nullable=True)

    status = Column(Enum(JobStatus), default=JobStatus.PENDING)
    credit_cost = Column(Float, nullable=True)

    # Stored as int (0/1) for SQLite compatibility
    downloaded = Column(Integer, default=0)
    download_path = Column(String, nullable=True)

    error_message = Column(Text, nullable=True)

    submitted_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    batch = relationship("Batch", back_populates="jobs")
