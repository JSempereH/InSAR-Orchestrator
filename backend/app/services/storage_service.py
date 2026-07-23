"""
Disk discovery: lists mounted filesystems on the machine running the backend,
so a project can be pinned to a specific disk (e.g. an external HDD) instead
of the app's default downloads folder.
"""

import os

import psutil

from app.config import settings

_EXCLUDED_FSTYPES = {
    "tmpfs", "devtmpfs", "overlay", "squashfs", "proc", "sysfs",
    "cgroup", "cgroup2", "devpts", "autofs", "mqueue", "debugfs",
    "tracefs", "securityfs", "pstore", "bpf", "configfs", "fusectl",
    "binfmt_misc", "rpc_pipefs", "efivarfs", "hugetlbfs",
}
_EXCLUDED_MOUNT_PREFIXES = ("/boot", "/snap", "/dev")


def list_storage_targets() -> list[dict]:
    """Return the app default plus real, writable disks/partitions."""
    default_path = os.path.abspath(settings.downloads_dir)
    usage = psutil.disk_usage(default_path)
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
