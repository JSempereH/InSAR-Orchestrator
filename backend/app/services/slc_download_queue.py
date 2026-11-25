"""SLC download queue: sequential background downloader for raw Sentinel-1 SLCs.

Items are keyed by filename (there's no Job row for a raw SLC download - it's
not tied to HyP3 processing at all, just a direct ASF data-pool transfer).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from app.services import download_state
from app.services.sequential_queue import SequentialQueue

_queue = SequentialQueue(name="SLC", item_key=lambda item: item["file_name"])
_last_destination: Optional[Path] = None   # only for reporting; each item carries its own


# ── Public API ─────────────────────────────────────────────────────────────

def start(username: str, password: str, scenes: list[dict], destination: Path) -> None:
    """Replace pending queue with `scenes` and start the worker if idle.

    Each item: {"granule_name": str, "file_name": str, "download_url": str}
    """
    global _last_destination
    _last_destination = destination
    # The destination and credentials travel with each item, so a second project
    # starting a queue cannot redirect or re-credential a download already under way.
    _queue.start([{**s, "_destination": destination, "_username": username, "_password": password} for s in scenes], _download_one)


def cancel() -> None:
    _queue.cancel()


def get_state() -> dict:
    raw = _queue.get_state()
    current = raw["current_item"]
    return {
        "active": raw["active"],
        "current_filename": current["file_name"] if current else None,
        "current_progress": raw["current_progress"],
        "pending_count": raw["pending_count"],
        "destination": str(_last_destination) if _last_destination else None,
        "total": raw["total"],
        "done": raw["done"],
        "cancelled": raw["cancelled"],
    }


# ── Item download ────────────────────────────────────────────────────────────

def _download_one(item: dict) -> None:
    from insar_core.adapters.asf import ASFAdapter

    def on_progress(**kw):
        tb = kw.get("total_bytes", 0)
        dl = kw.get("downloaded_bytes", 0)
        pct = round(dl / tb * 100, 1) if tb else 0
        download_state.update(item["file_name"], status="running", pct=pct, **kw)

    ASFAdapter().download(
        item["download_url"], item["file_name"], item["_destination"], item["_username"], item["_password"],
        progress_cb=on_progress,
    )
