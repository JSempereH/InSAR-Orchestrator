"""Checks on what a client may ask the server to download or delete."""
from __future__ import annotations

from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

# Hosts the Earthdata session may be sent to. Credentials go to whatever host the URL names,
# so anything outside the archives this app talks to is refused.
ALLOWED_DOWNLOAD_HOSTS = ("asf.alaska.edu", "earthdata.nasa.gov", "earthdatacloud.nasa.gov")


class UnsafeRequest(ValueError):
    pass


def plain_file_name(name: str) -> str:
    """`name` if it is a single file name, with no folders, no `..` and no hidden or empty names."""
    if not name or name != PurePosixPath(name).name or "\\" in name or "\x00" in name or name in (".", "..") or name.startswith("."):
        raise UnsafeRequest(f"Not a plain file name: {name!r}")
    return name


def archive_url(url: str) -> str:
    """`url` if it is an https address on one of the archives, else UnsafeRequest."""
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not any(host == h or host.endswith("." + h) for h in ALLOWED_DOWNLOAD_HOSTS):
        raise UnsafeRequest(f"Downloads are only allowed from the ASF and Earthdata archives, not from {host or url!r}")
    return url


def inside(folder: Path, name: str) -> Path:
    """The path of file `name` in `folder`, checked to really be inside it."""
    target = (Path(folder) / plain_file_name(name)).resolve()
    if Path(folder).resolve() not in target.parents:
        raise UnsafeRequest(f"{name!r} is outside the download folder")
    return target
