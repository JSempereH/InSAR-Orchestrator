from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional


class JobStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass
class SubmittedJob:
    """Represents an interferometric pair submitted to a cloud processor."""
    hyp3_job_id: str
    reference_granule: str
    secondary_granule: str
    reference_date: date
    secondary_date: date
    status: JobStatus
    credit_cost: Optional[float] = None
    error_message: Optional[str] = None
    submitted_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
