"""The ways a project's interferograms are produced, and how MintPy reads what each one leaves.

An *engine* is a source of interferograms plus what MintPy needs to read them. The app used to have
exactly one, HyP3's INSAR_GAMMA for Sentinel-1, wired into the MintPy step. Adding NISAR meant a
second one, and a third (a local dolphin run, say) would be a third, so what differs between them
is gathered here:

* how the interferograms come to exist (HyP3 jobs that cost credits, or ready-made products that
  are only downloaded),
* what has to be prepared before MintPy runs (unzip and crop for HyP3, a DEM for NISAR), and
* the `mintpy.load.*` options that point MintPy at the files.

Everything else (search, the pair plan, job tracking, download queues) differs too much to hide
behind one interface and stays with each engine's own adapter. An engine says what it is
(`EngineInfo`), whether it can run on this machine (`availability`) and how MintPy loads its output.
Register a new one with `register_engine`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import NamedTuple, Optional


class Availability(NamedTuple):
    ok: bool
    reason: str = ""


@dataclass(frozen=True)
class EngineInfo:
    id: str
    label: str
    sensor: str                      # "sentinel-1" or "nisar"
    band: str                        # "C" or "L"
    product: str                     # what the engine's interferograms are
    acquisition: str                 # "hyp3_jobs" (processed on demand) or "ready_made" (downloaded as they are)
    description: str
    credentials: tuple[str, ...]     # logins it needs, by name
    costs_credits: bool
    mintpy_processor: str            # value of mintpy.load.processor
    wavelength_m: float


class Engine(ABC):
    """One source of interferograms. See the module docstring."""

    info: EngineInfo

    def availability(self) -> Availability:
        """Whether this engine can run here. The reason is shown to the person when it cannot."""
        return Availability(True)

    @abstractmethod
    def load_options(self, data_dir: Path, work_dir: Path) -> dict[str, str]:
        """The `mintpy.load.*` options that point MintPy at this engine's files."""

    @abstractmethod
    def prepare_inputs(self, adapter, geometry: Optional[dict]) -> int:
        """Get the downloaded files into the shape MintPy reads. Returns how many inputs are ready.

        `adapter` is the MintPyAdapter of the run (it knows the data and work directories)."""

    @abstractmethod
    def has_inputs(self, data_dir: Path) -> bool:
        """Whether `data_dir` holds anything this engine's MintPy step can start from."""

    def default_mintpy_options(self) -> dict[str, str]:
        """Options this engine wants set unless the person chose otherwise."""
        return {}

    def describe(self) -> dict:
        available = self.availability()
        return {**asdict(self.info), "available": available.ok, "unavailable_reason": available.reason}


class HyP3GammaEngine(Engine):
    info = EngineInfo(
        id="hyp3_gamma", label="Sentinel-1 through HyP3 (INSAR_GAMMA)", sensor="sentinel-1", band="C",
        product="Unwrapped interferograms processed on demand by ASF HyP3, 80 m",
        acquisition="hyp3_jobs",
        description="Pairs of Sentinel-1 acquisitions are sent to HyP3, which processes them in the cloud for 10 credits each.",
        credentials=("earthdata",), costs_credits=True, mintpy_processor="hyp3", wavelength_m=0.05546576,
    )

    def load_options(self, data_dir: Path, work_dir: Path) -> dict[str, str]:
        data = Path(data_dir).resolve()
        return {
            "mintpy.load.processor": "hyp3",
            "mintpy.load.unwFile": f"{data}/*/*unw_phase.tif",
            "mintpy.load.corFile": f"{data}/*/*corr.tif",
            "mintpy.load.demFile": f"{data}/*/*dem.tif",
            "mintpy.load.incAngleFile": f"{data}/*/*lv_theta.tif",
            "mintpy.load.azAngleFile": f"{data}/*/*lv_phi.tif",
            "mintpy.load.waterMaskFile": f"{data}/*/*water_mask.tif",
        }

    def prepare_inputs(self, adapter, geometry: Optional[dict]) -> int:
        return adapter.unzip_all(geometry=geometry)

    def has_inputs(self, data_dir: Path) -> bool:
        data_dir = Path(data_dir)
        return data_dir.is_dir() and (
            any(data_dir.glob("*.zip")) or any(p.is_dir() for p in data_dir.iterdir())
        )


class HyP3BurstEngine(HyP3GammaEngine):
    """Sentinel-1 through HyP3, by bursts: only the strips that cover the area are processed.

    See insar_core.adapters.asf_burst for what a burst is and what it costs. What comes back has
    the same layout as a whole-scene product and MintPy reads it with the same loader, so only the
    search and the submission differ. ISCE2 also delivers the connected components of the
    unwrapping, which INSAR_GAMMA does not, and MintPy is pointed at them when they are there:
    they let it mask the regions the unwrapper could not tie together."""

    info = EngineInfo(
        id="hyp3_burst", label="Sentinel-1 through HyP3, by bursts (INSAR_ISCE_BURST)", sensor="sentinel-1", band="C",
        product="Unwrapped interferograms of the bursts that cover the area, processed on demand by ASF HyP3, 80, 40 or 20 m",
        acquisition="hyp3_jobs",
        description="Only the bursts over the area are processed: 1 credit per pair for up to four bursts at 80 m, where a "
                    "whole scene costs 10. For areas that fit in 15 bursts of one pass.",
        credentials=("earthdata",), costs_credits=True, mintpy_processor="hyp3", wavelength_m=0.05546576,
    )

    def load_options(self, data_dir: Path, work_dir: Path) -> dict[str, str]:
        options = super().load_options(data_dir, work_dir)
        data = Path(data_dir).resolve()
        if data.is_dir() and any(data.glob("*/*conncomp.tif")):
            options["mintpy.load.connCompFile"] = f"{data}/*/*conncomp.tif"
        return options


class NisarGunwEngine(Engine):
    """NISAR L2 GUNW: interferograms NASA already made, read by MintPy's `prep_nisar`."""

    info = EngineInfo(
        id="nisar_gunw", label="NISAR GUNW (ready-made interferograms)", sensor="nisar", band="L",
        product="Geocoded unwrapped interferograms produced by NASA, with ionosphere, troposphere and tide corrections",
        acquisition="ready_made",
        description="NASA's own L-band interferograms, downloaded as they are. No credits. The pairs are the ones NASA produced.",
        credentials=("earthdata",), costs_credits=False, mintpy_processor="nisar", wavelength_m=0.2384,
    )
    DEM_NAME = "dem/copernicus_dem.tif"

    def availability(self) -> Availability:
        try:
            import mintpy.prep_nisar  # noqa: F401
        except ImportError:
            return Availability(False, "This MintPy has no NISAR reader (prep_nisar). MintPy 1.6.4 or newer is needed.")
        return Availability(True)

    def load_options(self, data_dir: Path, work_dir: Path) -> dict[str, str]:
        return {
            "mintpy.load.processor": "nisar",
            "mintpy.load.unwFile": f"{Path(data_dir).resolve()}/*GUNW*.h5",
            "mintpy.load.demFile": str(Path(work_dir).resolve() / self.DEM_NAME),
            "mintpy.load.frequency": "A",
        }

    def has_inputs(self, data_dir: Path) -> bool:
        return Path(data_dir).is_dir() and any(Path(data_dir).glob("*GUNW*.h5"))

    def default_mintpy_options(self) -> dict[str, str]:
        # L-band phase carries a much larger ionospheric delay than C-band, and each GUNW ships the
        # split-spectrum estimate of it. On a real stack over Lorca applying it raised the agreement
        # with the Sentinel-1 deformation pattern from r = 0.66 to r = 0.73.
        return {"mintpy.ionosphericDelay.method": "split_spectrum"}

    def prepare_inputs(self, adapter, geometry: Optional[dict]) -> int:
        """Build the DEM MintPy needs for the area and count the granules on disk."""
        from insar_core.pipeline.dem import copernicus_dem
        from insar_core.pipeline.mintpy_adapter import _padded_bbox

        granules = sorted(Path(adapter.downloads_dir).glob("*GUNW*.h5"))
        if not granules:
            raise FileNotFoundError(f"No NISAR GUNW files (*GUNW*.h5) in {adapter.downloads_dir}")
        bbox = _padded_bbox(geometry) if geometry else None
        if bbox is None:
            raise ValueError("A NISAR project needs its area of interest to build the DEM.")
        # a margin beyond the processed window, so the heights are defined at its edges
        pad = 0.05
        copernicus_dem((bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad), Path(adapter.work_dir) / self.DEM_NAME)
        return len(granules)


_ENGINES: dict[str, Engine] = {}
DEFAULT_ENGINE = "hyp3_gamma"


def register_engine(engine: Engine) -> Engine:
    _ENGINES[engine.info.id] = engine
    return engine


def get_engine(engine_id: Optional[str]) -> Engine:
    """The engine called `engine_id` (the HyP3 one when None). Raises KeyError for an unknown id."""
    key = engine_id or DEFAULT_ENGINE
    if key not in _ENGINES:
        raise KeyError(f"Unknown engine '{key}'. Known: {', '.join(sorted(_ENGINES))}")
    return _ENGINES[key]


def list_engines() -> list[Engine]:
    return list(_ENGINES.values())


register_engine(HyP3GammaEngine())
register_engine(HyP3BurstEngine())
register_engine(NisarGunwEngine())
