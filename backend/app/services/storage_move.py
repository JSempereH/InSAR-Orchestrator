"""Background mover: relocate a project's (or a single download's) files to
a different disk - including a network share, as long as it's already
mounted on the host (e.g. NFS/SMB/SSHFS) and shows up in list_storage_targets().

Single global worker, like the download queues - only one move runs at a
time, since moving is disk-I/O heavy and we don't want several competing.
Progress is reported through download_state, keyed by a caller-chosen id
(e.g. "project:<id>" or "egms:<id>").
"""
from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from app.services import download_state, storage_service

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_current_id: Optional[str] = None
_last_id: Optional[str] = None  # kept after completion so the final state can still be polled


def is_busy() -> bool:
    with _lock:
        return _current_id is not None


def start(move_id: str, src: Path, dst: Path, on_complete: Callable[[Path], None]) -> None:
    """Move `src` to `dst` in a background thread, then call `on_complete(dst)`.

    Raises RuntimeError if another move is already running.
    """
    global _current_id, _last_id
    with _lock:
        if _current_id is not None:
            raise RuntimeError(f"A move is already in progress ({_current_id})")
        _current_id = move_id
        _last_id = move_id

    thread = threading.Thread(target=_run, args=(move_id, src, dst, on_complete), daemon=True)
    thread.start()


def get_state(move_id: str) -> dict:
    state = download_state.get(move_id)
    with _lock:
        active = _current_id == move_id
    return {
        "active": active,
        "status": state.get("status", "idle"),
        "pct": state.get("pct", 0),
        "src": state.get("src"),
        "dst": state.get("dst"),
        "error": state.get("error"),
    }


def get_current_state() -> dict:
    """State of the most recently started move (or idle if none has run yet)."""
    with _lock:
        move_id = _last_id
    if move_id is None:
        return {"active": False, "status": "idle", "pct": 0, "src": None, "dst": None, "error": None}
    return get_state(move_id)


def _run(move_id: str, src: Path, dst: Path, on_complete: Callable[[Path], None]) -> None:
    global _current_id
    download_state.update(move_id, status="running", pct=0, src=str(src), dst=str(dst), error=None)

    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        same_filesystem = src.stat().st_dev == dst.parent.stat().st_dev

        if same_filesystem:
            # Same disk: a rename is instant, no need to copy byte-by-byte.
            shutil.move(str(src), str(dst))
        else:
            total = storage_service.dir_size_bytes(src) if src.is_dir() else src.stat().st_size
            _copy_with_progress(src, dst, move_id, total)
            shutil.rmtree(src) if src.is_dir() else src.unlink()

        download_state.update(move_id, status="done", pct=100)
        on_complete(dst)
        logger.info("Move %s: %s -> %s complete", move_id, src, dst)
    except Exception as exc:
        logger.exception("Move %s failed", move_id)
        download_state.update(move_id, status="error", error=str(exc))
    finally:
        with _lock:
            _current_id = None


def _copy_with_progress(src: Path, dst: Path, move_id: str, total_bytes: int) -> None:
    CHUNK = 4 * 1024 * 1024
    copied = 0
    t0 = time.monotonic()

    def copy_file(s: Path, d: Path) -> None:
        nonlocal copied
        d.parent.mkdir(parents=True, exist_ok=True)
        with open(s, "rb") as fsrc, open(d, "wb") as fdst:
            while True:
                chunk = fsrc.read(CHUNK)
                if not chunk:
                    break
                fdst.write(chunk)
                copied += len(chunk)
                elapsed = max(time.monotonic() - t0, 0.001)
                speed = copied / elapsed
                pct = round(copied / total_bytes * 100, 1) if total_bytes else 100
                eta = int((total_bytes - copied) / speed) if speed and total_bytes else None
                download_state.update(
                    move_id, status="running", pct=pct, filename=s.name,
                    total_bytes=total_bytes, downloaded_bytes=copied,
                    speed_bps=speed, eta_s=eta,
                )
        shutil.copystat(s, d)

    if src.is_dir():
        dst.mkdir(parents=True, exist_ok=True)
        for item in src.rglob("*"):
            if item.is_file():
                copy_file(item, dst / item.relative_to(src))
    else:
        copy_file(src, dst)
