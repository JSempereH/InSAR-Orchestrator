"""Download queue for NISAR GUNW granules: sequential, in the background, one file at a time.

Granules are a few GB each, so they go one after another like the SLC downloads, with the same
progress reporting. Items are keyed by file name.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from app.services import download_state
from app.services.sequential_queue import SequentialQueue

_queue = SequentialQueue(name="NISAR", item_key=lambda item: item["file_name"])
_last_destination: Optional[Path] = None   # only for reporting; each item carries its own


def start(username: str, password: str, granules: list[dict], destination: Path) -> None:
    """Replace the pending queue with `granules` ({"file_name", "download_url"}) and start the worker if idle."""
    global _last_destination
    _last_destination = destination
    _queue.start([{**g, "_destination": destination, "_username": username, "_password": password} for g in granules], _download_one)


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


def _download_one(item: dict) -> None:
    from insar_core.adapters.asf import ASFAdapter

    def on_progress(**kw):
        total, done = kw.get("total_bytes", 0), kw.get("downloaded_bytes", 0)
        download_state.update(item["file_name"], status="running", pct=round(done / total * 100, 1) if total else 0, **kw)

    ASFAdapter().download(
        item["download_url"], item["file_name"], item["_destination"], item["_username"], item["_password"], progress_cb=on_progress,
    )
