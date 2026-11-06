from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import pyproj
import rasterio
from rasterio.windows import Window

_SMALLBASELINE = "smallbaselineApp.py"


def find_smallbaseline() -> Optional[str]:
    """Path to smallbaselineApp.py, or None if MintPy is not installed.

    Looks on PATH first, then next to the running interpreter: a venv that was
    never activated (e.g. `.venv/bin/uvicorn ...`) still has MintPy's scripts in
    its own bin directory even though that directory is not on PATH."""
    found = shutil.which(_SMALLBASELINE)
    if found:
        return found
    candidate = Path(sys.executable).parent / _SMALLBASELINE
    return str(candidate) if candidate.exists() else None


def _mintpy_env() -> dict:
    """Environment for MintPy subprocesses, with the interpreter's own bin
    directory first on PATH so the scripts MintPy itself launches resolve."""
    env = dict(os.environ)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    return env


# Zips are cropped concurrently: rasterio releases the GIL while GDAL reads,
# decompresses and writes, so threads give a real speedup on the I/O and
# deflate-bound crop without the memory cost of a process pool.
_UNZIP_WORKERS = min(4, os.cpu_count() or 1)

# How far past the drawn AOI's own edge to extend the processed area (as a
# fraction of the AOI's own width/height), so MintPy has room to pick a
# stable reference point and the crop isn't pixel-tight against the exact
# polygon boundary. Floored by _MIN_MARGIN_DEG so a very small or degenerate
# AOI still gets a sane amount of surrounding context.
_SUBSET_MARGIN_FRACTION = 0.25
_MIN_MARGIN_DEG = 0.005  # roughly 500 m

# (min_lon, min_lat, max_lon, max_lat), WGS84 degrees.
Bbox = tuple[float, float, float, float]

# smallbaselineApp.cfg (see write_config) only ever reads these six raster
# kinds per interferogram directory - everything else a HyP3 zip ships
# (browse images, per-band previews, KMZs, etc.) is dead weight for MintPy.
# unzip_all extracts only these, and (when an AOI is known) crops them to
# it - see unzip_all's docstring for the measured disk impact.
_NEEDED_TIF_SUFFIXES = (
    "unw_phase.tif", "corr.tif", "dem.tif",
    "lv_theta.tif", "lv_phi.tif", "water_mask.tif",
)


def _is_metadata_sidecar(member: str) -> bool:
    """True for HyP3's `<product_name>.txt` metadata file - the one
    mintpy.prep_hyp3.add_hyp3_metadata reads for orbit direction, path
    number, wavelength, etc. that aren't encoded in the GeoTIFFs themselves.

    Found by a real MintPy load_data run against a cropped-only extraction
    in this repo: filtering to just the six raster kinds above (without
    this file) still let load_data start, but it then failed with
    FileNotFoundError looking for exactly this file - the six rasters alone
    aren't sufficient input for MintPy's own hyp3 loader.

    Matched by name rather than added to _NEEDED_TIF_SUFFIXES because it is
    plain text (nothing to crop - always extracted verbatim) and because a
    naive `.endswith(".txt")` would also match the unrelated
    `<product_name>.README.md.txt` HyP3 ships in the same zip; the real
    metadata file's basename has exactly one dot, the README's has three.
    """
    basename = Path(member).name
    return basename.endswith(".txt") and basename.count(".") == 1


def _padded_bbox(geometry: dict) -> Optional[Bbox]:
    """(min_lon, min_lat, max_lon, max_lat) around `geometry`'s own extent,
    padded by _SUBSET_MARGIN_FRACTION (floored at _MIN_MARGIN_DEG).

    Shared by `_subset_lalo` (MintPy's own crop, applied when it loads the
    data) and `unzip_all`'s crop (applied earlier, to the raw downloaded
    GeoTIFFs), so both stages agree on exactly the same window - see
    `_subset_lalo` for why the crop matters in the first place.

    Returns None if `geometry` doesn't look like a usable polygon, so
    callers can fall back to the uncropped path rather than working with a
    broken bbox.
    """
    geom = geometry.get("geometry", geometry) if geometry.get("type") == "Feature" else geometry
    try:
        coords = geom["coordinates"][0]
        lons = [c[0] for c in coords]
        lats = [c[1] for c in coords]
    except (KeyError, IndexError, TypeError):
        return None
    if not lons or not lats:
        return None

    min_lon, max_lon = min(lons), max(lons)
    min_lat, max_lat = min(lats), max(lats)
    lon_margin = max((max_lon - min_lon) * _SUBSET_MARGIN_FRACTION, _MIN_MARGIN_DEG)
    lat_margin = max((max_lat - min_lat) * _SUBSET_MARGIN_FRACTION, _MIN_MARGIN_DEG)
    return (min_lon - lon_margin, min_lat - lat_margin, max_lon + lon_margin, max_lat + lat_margin)


def _subset_lalo(geometry: dict) -> Optional[str]:
    """MintPy's `mintpy.subset.lalo` value ("S:N,W:E") for a padded bounding
    box around `geometry`.

    A HyP3 interferogram covers the whole Sentinel-1 frame overlap (order of
    hundreds of km), while a project's drawn AOI is typically a few km
    across - measured on one real project in this repo: a ~4x4 km AOI
    against a ~289x225 km delivered scene, roughly a 1000x difference in
    area. Without this, MintPy's SBAS inversion processes that entire scene
    instead of just the area the user asked about, which is the difference
    between minutes and hours, and between a few hundred MB and ~30 GB of
    MintPy output on disk (see mintpy_service.get_time_series's caller for
    the project this was measured against).

    Returns None if `geometry` doesn't look like a usable polygon, so the
    caller can fall back to processing the full scene rather than writing a
    broken config value.
    """
    bbox = _padded_bbox(geometry)
    if bbox is None:
        return None
    min_lon, min_lat, max_lon, max_lat = bbox
    return f"{min_lat:.6f}:{max_lat:.6f},{min_lon:.6f}:{max_lon:.6f}"


_thread_local = threading.local()


def _transformer_from_wgs84(epsg: int) -> pyproj.Transformer:
    """Cached per thread: constructing a pyproj.Transformer re-parses the
    target CRS each time, and every raster in one project shares the same
    EPSG (the project's own UTM zone), so a real run cropping hundreds of
    files was doing that work once per file instead of once overall. Kept
    per thread because unzip_all crops several zips concurrently and a
    Transformer should not be shared across threads."""
    cache = getattr(_thread_local, "transformers", None)
    if cache is None:
        cache = _thread_local.transformers = {}
    transformer = cache.get(epsg)
    if transformer is None:
        transformer = cache[epsg] = pyproj.Transformer.from_crs(4326, epsg, always_xy=True)
    return transformer


def _native_crop_window(ds, bbox: Bbox) -> Optional[tuple[int, int, int, int]]:
    """Convert a WGS84 `bbox` into a pixel window (xoff, yoff, xsize, ysize)
    in `ds`'s own CRS and grid, clamped to the raster's own extent.

    This windows by whole pixels in the source's native grid rather than
    reprojecting/resampling (e.g. via a GDAL warp with an EPSG:4326
    output bounds): a Warp-based crop was tested against a real file in this
    repo and shifted the pixel size by about 0.2 m (80.0 m -> 79.8998 m),
    because computing a warp output also re-derives the resolution - that
    would silently interpolate every unwrapped-phase and water-mask pixel
    instead of passing it through unchanged, and risks each of the six
    files in an interferogram (or each interferogram in the stack) landing
    on a very slightly different grid. Windowing by pixel offsets keeps the
    exact source resolution and exact source pixel values - also verified
    against a real file here (grid unchanged, a sampled pixel bit-for-bit
    identical to the un-cropped source).

    Returns None if `bbox` doesn't overlap the raster at all.
    """
    transform = ds.transform
    x_origin, px_w, y_origin, px_h = transform.c, transform.a, transform.f, transform.e

    epsg = ds.crs.to_epsg() if ds.crs else None
    min_lon, min_lat, max_lon, max_lat = bbox

    if epsg is not None and epsg != 4326:
        transformer = _transformer_from_wgs84(epsg)
        corners = [(min_lon, min_lat), (min_lon, max_lat), (max_lon, min_lat), (max_lon, max_lat)]
        xs, ys = zip(*(transformer.transform(lon, lat) for lon, lat in corners), strict=True)
        min_x, max_x, min_y, max_y = min(xs), max(xs), min(ys), max(ys)
    else:
        min_x, max_x, min_y, max_y = min_lon, max_lon, min_lat, max_lat

    col_min = int((min_x - x_origin) // px_w)
    col_max = -int(-(max_x - x_origin) // px_w)  # ceil division
    row_min = int((y_origin - max_y) // abs(px_h))
    row_max = -int(-(y_origin - min_y) // abs(px_h))

    col_min, row_min = max(0, col_min), max(0, row_min)
    col_max, row_max = min(ds.width, col_max), min(ds.height, row_max)

    xsize, ysize = col_max - col_min, row_max - row_min
    if xsize <= 0 or ysize <= 0:
        return None
    return col_min, row_min, xsize, ysize


def _extract_and_crop(zip_path: Path, member: str, dest_dir: Path, bbox: Bbox) -> None:
    """Crop one zip member straight out of the zip and write only the
    cropped result under dest_dir.

    Reads via GDAL's `/vsizip/` virtual file system instead of extracting
    the full-resolution file to a temp path first (the original approach):
    GDAL can open a file inside a zip directly, so this never materializes
    the full-resolution raster on disk at all. Verified against a real
    project in this repo - 261 zips x 6 files, up to ~30 MB each - this
    removes what was ~40 GB of redundant temp-file writes over one run.

    The cropped file is DEFLATE-compressed (with the floating-point
    predictor for float rasters); pixel values are unchanged, GDAL/MintPy
    decompress transparently.

    Raises if the member's own footprint doesn't overlap `bbox` at all,
    rather than silently keeping the full file or skipping it: every HyP3
    interferogram delivered for a project was already selected because its
    footprint covers the project's AOI, so a genuine zero-overlap result
    means something is wrong (a bad bbox, a corrupt file) and should fail
    loudly instead of quietly producing an incomplete interferogram
    directory for MintPy to trip over later.
    """
    target_path = dest_dir / member
    target_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        src = rasterio.open(f"/vsizip/{zip_path}/{member}")
    except rasterio.errors.RasterioIOError as exc:
        raise RuntimeError(f"GDAL could not open member {member!r} inside {zip_path.name}") from exc

    with src:
        window = _native_crop_window(src, bbox)
        if window is None:
            raise RuntimeError(
                f"{member!r} does not overlap the project AOI bbox {bbox} "
                "- this interferogram may not actually cover the area of interest."
            )
        xoff, yoff, xsize, ysize = window
        win = Window(xoff, yoff, xsize, ysize)

        profile = src.profile
        profile.update(
            driver="GTiff", width=xsize, height=ysize, transform=src.window_transform(win),
            compress="deflate", tiled=False,
        )
        profile.pop("blockxsize", None)
        profile.pop("blockysize", None)
        if src.dtypes[0].startswith("float"):
            profile["predictor"] = 3

        tmp_path = target_path.with_name(target_path.name + ".tmp")
        try:
            with rasterio.open(tmp_path, "w", **profile) as dst:
                dst.write(src.read(window=win))
                dst.update_tags(**src.tags())
                for band, desc in enumerate(src.descriptions, start=1):
                    if desc:
                        dst.set_band_description(band, desc)
            os.replace(tmp_path, target_path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise


class MintPyAdapter:
    """Prepare HyP3 interferogram downloads for MintPy SBAS processing."""

    def __init__(self, downloads_dir: Path, work_dir: Path):
        self.downloads_dir = Path(downloads_dir)
        self.work_dir = Path(work_dir)

    def unzip_all(self, geometry: Optional[dict] = None) -> int:
        """Decompress the interferogram files MintPy actually needs (see
        write_config) from any .zip in downloads_dir not yet extracted.

        Only the six raster kinds `_NEEDED_TIF_SUFFIXES` lists, plus the
        per-scene `.txt` metadata sidecar (see `_is_metadata_sidecar`), are
        extracted - a HyP3 zip ships more than that (browse images, KMZs,
        a README, etc.) that neither smallbaselineApp.cfg nor MintPy's own
        hyp3 loader ever reads. When `geometry` is given, each extracted
        GeoTIFF (not the metadata sidecar - there's nothing to crop in a
        text file) is also cropped to its padded AOI window (see
        `_native_crop_window`) instead of kept at full scene resolution:
        measured on a real project in this repo, this turned ~80 GB of
        extracted files into a few hundred MB, since a HyP3 scene covers
        hundreds of km while a project's AOI is typically a few km across
        (the same ratio `_subset_lalo` documents for MintPy's own crop).

        When `geometry` is given, the source .zip is also deleted once every
        needed file has been cropped out of it successfully - at that point
        its data is fully captured in the (far smaller) cropped output, so
        keeping the original full-resolution download around only wastes
        the same ~80 GB the crop itself was meant to avoid. This only
        happens after `_extract_and_crop` has confirmed every member for
        that zip cropped without error; if any member fails, the zip (and
        every zip after it) is left untouched so nothing is lost and a
        re-run can pick up where it stopped.

        Idempotent: skips ZIPs whose output directory already exists - a
        directory extracted before this feature existed (full resolution,
        every file) is left as-is rather than silently replaced. Delete it
        first if it should be re-extracted filtered/cropped.

        Returns the total number of extracted interferogram directories.
        """
        bbox = _padded_bbox(geometry) if geometry else None

        pending = []
        for zpath in sorted(glob.glob(str(self.downloads_dir / "*.zip"))):
            zip_path = Path(zpath)
            if not zip_path.with_suffix("").is_dir():
                pending.append(zip_path)

        # Zips are independent, so they are processed concurrently. On the
        # first failure no new zip is started (zips already in flight finish
        # or fail on their own); the error is then re-raised, and every zip
        # that did not complete is left untouched for a re-run.
        failed = threading.Event()

        def _process(zip_path: Path) -> None:
            if failed.is_set():
                return
            try:
                self._extract_one(zip_path, bbox)
            except BaseException:
                failed.set()
                raise

        if len(pending) <= 1 or _UNZIP_WORKERS <= 1:
            for zip_path in pending:
                _process(zip_path)
        else:
            with ThreadPoolExecutor(max_workers=_UNZIP_WORKERS) as executor:
                futures = [executor.submit(_process, zip_path) for zip_path in pending]
            for future in futures:
                future.result()  # re-raises the first failure

        return sum(
            1 for entry in self.downloads_dir.iterdir()
            if entry.is_dir() and not entry.name.startswith(".")
        )

    def _extract_one(self, zip_path: Path, bbox: Optional[Bbox]) -> None:
        """Extract (and crop, if `bbox`) one zip into a hidden staging
        directory, then move it into place with a single rename.

        The final directory only ever appears fully populated, so a crash or
        failure midway can never leave a half-extracted directory that the
        "output directory exists, skip" check in unzip_all would then treat
        as done forever."""
        final_dir = zip_path.with_suffix("")
        staging = self.downloads_dir / f".{zip_path.stem}.partial"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                members = [
                    name for name in zf.namelist()
                    if name.lower().endswith(_NEEDED_TIF_SUFFIXES) or _is_metadata_sidecar(name)
                ]
                for member in members:
                    if bbox is not None and member.lower().endswith(_NEEDED_TIF_SUFFIXES):
                        _extract_and_crop(zip_path, member, staging, bbox)
                    else:
                        zf.extract(member, staging)
            produced = staging / final_dir.name
            os.replace(produced if produced.is_dir() else staging, final_dir)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        if bbox is not None:
            zip_path.unlink()

    def write_config(self, config_path: Optional[Path] = None, geometry: Optional[dict] = None) -> Path:
        """Generate a smallbaselineApp.cfg for HyP3 data layout.

        Uses glob patterns that match HyP3's one-interferogram-per-subdirectory layout.

        `geometry`, when given (the project's own drawn AOI, as GeoJSON),
        restricts processing to a padded bounding box around it instead of
        the full HyP3 scene - see `_subset_lalo` for why this matters. Falls
        back to processing the full scene if `geometry` is missing or
        doesn't parse as a usable polygon.
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

        subset = _subset_lalo(geometry) if geometry else None
        if subset:
            config += f"mintpy.subset.lalo         = {subset}\n"

        config_path.write_text(config)
        return config_path

    def load_data(self, config_path: Optional[Path] = None) -> subprocess.CompletedProcess:
        """Run smallbaselineApp.py --dostep load_data."""
        if config_path is None:
            config_path = self.work_dir / "smallbaselineApp.cfg"

        cmd = [
            find_smallbaseline() or _SMALLBASELINE,
            str(config_path),
            "--work-dir", str(self.work_dir),
            "--dostep", "load_data",
        ]
        return subprocess.run(cmd, check=False, env=_mintpy_env())

    def run_full_pipeline(self, config_path: Optional[Path] = None) -> subprocess.CompletedProcess:
        """Run the full MintPy SBAS pipeline (all steps), blocking until done."""
        if config_path is None:
            config_path = self.work_dir / "smallbaselineApp.cfg"

        cmd = [
            find_smallbaseline() or _SMALLBASELINE,
            str(config_path),
            "--work-dir", str(self.work_dir),
        ]
        return subprocess.run(cmd, check=False, env=_mintpy_env())

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
            find_smallbaseline() or _SMALLBASELINE,
            str(config_path),
            "--work-dir", str(self.work_dir),
        ]
        log_handle = log_path.open("w", encoding="utf-8")
        return subprocess.Popen(cmd, stdout=log_handle, stderr=subprocess.STDOUT, env=_mintpy_env())
