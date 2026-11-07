"""Facts about how a result was produced, for recording next to the result.

Two pieces:

* `software_versions()`: the versions of the tools that shaped the numbers.
* `mintpy_effective_options()`: the full set of MintPy options a run uses. The
  config this library writes only sets where to load data from and the crop, so
  everything else comes from MintPy's own defaults, and several of those are
  `auto` and resolved at run time (troposphere correction, unwrapping-error
  correction, reference point...). Recording only our small config would hide
  them, so the defaults shipped with the installed MintPy are read and merged.
"""
from __future__ import annotations

import importlib.metadata as metadata
import importlib.util
import platform
import re
from pathlib import Path
from typing import Optional

_PACKAGES = ("insar-core", "mintpy", "hyp3-sdk", "asf-search", "numpy", "h5py", "rasterio", "pyproj")

_OPTION_LINE = re.compile(r"^\s*(mintpy\.[\w.]+)\s*=\s*([^#\n]*?)\s*(?:#.*)?$")


def software_versions() -> dict:
    """Versions of Python and the packages that influence results (None if absent)."""
    versions: dict = {"python": platform.python_version(), "platform": platform.platform()}
    for package in _PACKAGES:
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = None
    return versions


def parse_mintpy_options(text: str) -> dict[str, str]:
    """`mintpy.key = value  # comment` lines of a template, as {key: value}."""
    options: dict[str, str] = {}
    for line in text.splitlines():
        match = _OPTION_LINE.match(line)
        if match:
            options[match.group(1)] = match.group(2)
    return options


def _mintpy_defaults_dir() -> Optional[Path]:
    spec = importlib.util.find_spec("mintpy")
    if spec is None or spec.origin is None:
        return None
    directory = Path(spec.origin).parent / "defaults"
    return directory if directory.is_dir() else None


def mintpy_effective_options(custom_config_text: str) -> Optional[dict[str, str]]:
    """Every MintPy option a run with this config uses, with `auto` values resolved.

    Order of precedence, highest first: the custom config, MintPy's own `auto`
    resolution table, MintPy's default template. Returns None when MintPy is not
    installed here (the options cannot be known then). An option whose `auto` value
    is not in the resolution table keeps the value "auto".
    """
    directory = _mintpy_defaults_dir()
    if directory is None:
        return None
    try:
        defaults = parse_mintpy_options((directory / "smallbaselineApp.cfg").read_text())
        resolved = parse_mintpy_options((directory / "smallbaselineApp_auto.cfg").read_text())
    except OSError:
        return None

    effective = dict(defaults)
    for key, value in defaults.items():
        if value == "auto" and key in resolved:
            effective[key] = resolved[key]
    effective.update(parse_mintpy_options(custom_config_text))
    return effective
