from __future__ import annotations

import glob
import subprocess
import zipfile
from pathlib import Path
from typing import Optional


class MintPyAdapter:
    """Prepare HyP3 interferogram downloads for MintPy SBAS processing."""

    def __init__(self, downloads_dir: Path, work_dir: Path):
        self.downloads_dir = Path(downloads_dir)
        self.work_dir = Path(work_dir)

    def unzip_all(self) -> int:
        """Decompress any .zip files in downloads_dir not yet extracted.

        Idempotent: skips ZIPs whose output directory already exists.
        Returns the total number of extracted interferogram directories.
        """
        zips = glob.glob(str(self.downloads_dir / "*.zip"))
        for zpath in zips:
            expected_dir = Path(zpath[:-4])
            if not expected_dir.is_dir():
                with zipfile.ZipFile(zpath, "r") as zf:
                    zf.extractall(self.downloads_dir)

        return sum(
            1 for entry in self.downloads_dir.iterdir() if entry.is_dir()
        )

    def write_config(self, config_path: Optional[Path] = None) -> Path:
        """Generate a smallbaselineApp.cfg for HyP3 data layout.

        Uses glob patterns that match HyP3's one-interferogram-per-subdirectory layout.
        """
        if config_path is None:
            config_path = self.work_dir / "smallbaselineApp.cfg"

        self.work_dir.mkdir(parents=True, exist_ok=True)
        data_dir = self.downloads_dir.resolve()

        config = (
            "mintpy.load.processor      = hyp3\n"
            f"mintpy.load.unwFile        = {data_dir}/*/*unw_phase.tif\n"
            f"mintpy.load.corFile        = {data_dir}/*/*corr.tif\n"
            f"mintpy.load.demFile        = {data_dir}/*/*dem.tif\n"
            f"mintpy.load.incAngleFile   = {data_dir}/*/*lv_theta.tif\n"
            f"mintpy.load.azAngleFile    = {data_dir}/*/*lv_phi.tif\n"
            f"mintpy.load.waterMaskFile  = {data_dir}/*/*water_mask.tif\n"
        )

        config_path.write_text(config)
        return config_path

    def load_data(self, config_path: Optional[Path] = None) -> subprocess.CompletedProcess:
        """Run smallbaselineApp.py --dostep load_data."""
        if config_path is None:
            config_path = self.work_dir / "smallbaselineApp.cfg"

        cmd = [
            "smallbaselineApp.py",
            str(config_path),
            "--work-dir", str(self.work_dir),
            "--dostep", "load_data",
        ]
        return subprocess.run(cmd, check=False)

    def run_full_pipeline(self, config_path: Optional[Path] = None) -> subprocess.CompletedProcess:
        """Run the full MintPy SBAS pipeline (all steps), blocking until done."""
        if config_path is None:
            config_path = self.work_dir / "smallbaselineApp.cfg"

        cmd = [
            "smallbaselineApp.py",
            str(config_path),
            "--work-dir", str(self.work_dir),
        ]
        return subprocess.run(cmd, check=False)

    def start_full_pipeline(
        self,
        config_path: Optional[Path] = None,
        log_path: Optional[Path] = None,
    ) -> subprocess.Popen:
        """Start the full MintPy SBAS pipeline without blocking.

        Stdout/stderr are redirected to `log_path` (default: work_dir/mintpy.log)
        so a caller can tail progress while the process runs, and can call
        .terminate()/.poll() on the returned Popen to cancel or check completion.
        This can run for a long time (minutes to hours depending on stack size),
        which is why it is not simply wrapped in subprocess.run.
        """
        if config_path is None:
            config_path = self.work_dir / "smallbaselineApp.cfg"
        if log_path is None:
            log_path = self.work_dir / "mintpy.log"

        cmd = [
            "smallbaselineApp.py",
            str(config_path),
            "--work-dir", str(self.work_dir),
        ]
        log_handle = log_path.open("w", encoding="utf-8")
        return subprocess.Popen(cmd, stdout=log_handle, stderr=subprocess.STDOUT)
