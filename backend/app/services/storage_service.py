"""
Disk discovery: lists mounted filesystems on the machine running the backend,
so a project can be pinned to a specific disk (e.g. an external HDD, or a
network share already mounted on the host) instead of the app's default
downloads folder.
"""

import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

import psutil

from app.config import settings

logger = logging.getLogger(__name__)

_EXCLUDED_FSTYPES = {
    "tmpfs", "devtmpfs", "overlay", "squashfs", "proc", "sysfs",
    "cgroup", "cgroup2", "devpts", "autofs", "mqueue", "debugfs",
    "tracefs", "securityfs", "pstore", "bpf", "configfs", "fusectl",
    "binfmt_misc", "rpc_pipefs", "efivarfs", "hugetlbfs",
}
_EXCLUDED_MOUNT_PREFIXES = ("/boot", "/snap", "/dev")


def list_storage_targets() -> list[dict]:
    """Return the app default plus real, writable disks/partitions.

    The partition backing the app-default path is skipped from the second list.
    """
    default_path = os.path.abspath(settings.downloads_dir)
    usage = psutil.disk_usage(default_path)
    default_dev = os.stat(default_path).st_dev
    targets = [{
        "mountpoint": None,
        "device": "app-default",
        "fstype": "-",
        "total_gb": round(usage.total / 1e9, 1),
        "free_gb": round(usage.free / 1e9, 1),
        "writable": True,
    }]

    for part in psutil.disk_partitions(all=False):
        if part.fstype in _EXCLUDED_FSTYPES:
            continue
        if part.mountpoint.startswith(_EXCLUDED_MOUNT_PREFIXES):
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
            if os.stat(part.mountpoint).st_dev == default_dev:
                continue
        except OSError:
            continue
        targets.append({
            "mountpoint": part.mountpoint,
            "device": part.device,
            "fstype": part.fstype,
            "total_gb": round(usage.total / 1e9, 1),
            "free_gb": round(usage.free / 1e9, 1),
            "writable": os.access(part.mountpoint, os.W_OK),
        })

    return targets


def dir_size_bytes(path: Path, timeout: int = 30) -> int:
    """Total size of everything under `path`. Uses `du` (fast even for huge
    trees, including over network mounts) with a Python walk as fallback."""
    try:
        result = subprocess.run(
            ["du", "-sb", str(path)],
            capture_output=True, text=True, timeout=timeout, check=True,
        )
        return int(result.stdout.split()[0])
    except (subprocess.SubprocessError, ValueError, IndexError, OSError):
        logger.warning("du failed for %s, falling back to Python walk", path)
        total = 0
        for entry in path.rglob("*"):
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except OSError:
                continue
        return total


def path_usage(path: Path) -> Optional[dict]:
    """Disk usage for `path`: how much it occupies plus free/total space on
    its underlying filesystem. None if the path doesn't exist."""
    if not path.exists():
        return None
    used = dir_size_bytes(path) if path.is_dir() else path.stat().st_size
    disk = psutil.disk_usage(str(path))
    return {
        "path": str(path),
        "used_gb": round(used / 1e9, 2),
        "free_gb": round(disk.free / 1e9, 2),
        "total_gb": round(disk.total / 1e9, 2),
    }
