from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import List

from insar_core.models.scene import SearchParams, SARScene, TrackSummary
from insar_core.models.job import SubmittedJob


class SceneSearchAdapter(ABC):
    """Interface for SAR scene catalog providers (ASF, CDSE, ...)."""

    @abstractmethod
    def search(self, params: SearchParams) -> List[SARScene]:
        """Return scenes matching params, deduplicated to one per acquisition date."""
        ...

    @abstractmethod
    def available_tracks(self, params: SearchParams) -> List[TrackSummary]:
        """Return aggregate stats per (track, direction) for AOI + date range.

        Useful for the UI scene-discovery step before the user picks a track.
        """
        ...


class ProcessorAdapter(ABC):
    """Interface for InSAR cloud processors (HyP3, SNAP-on-demand, ...)."""

    @abstractmethod
    def submit_pair(
        self,
        reference: str,
        secondary: str,
        name: str = "insar-job",
        **kwargs,
    ) -> SubmittedJob:
        """Submit a single interferometric pair and return the job record."""
        ...

    @abstractmethod
    def get_status(self, job_id: str) -> SubmittedJob:
        """Query current status of a previously submitted job."""
        ...

    @abstractmethod
    def download(self, job_id: str, destination: Path) -> List[Path]:
        """Download all output files for a SUCCEEDED job. Returns downloaded paths."""
        ...
