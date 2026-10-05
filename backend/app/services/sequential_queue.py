"""Generic single-worker sequential download queue.

download_queue.py (HyP3 jobs), slc_download_queue.py (raw Sentinel-1 SLCs)
and egms_download_queue.py (EGMS products) each used to hand-roll their own
copy of this: a pending list, a daemon worker thread that processes one item
at a time, a cancel flag, and a total/done counter for get_state() - about
80% identical code, differing only in what one "item" looks like and how to
actually download one. This holds that shared machinery; each queue module
supplies `item_key` (how to name an item's download_state entry) and, at
start()/enqueue() time, a `download_fn` closure that knows how to fetch one
item (with whatever per-queue context - credentials, an adapter, a
destination folder - it needs baked in).
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from app.services import download_state

logger = logging.getLogger(__name__)


class SequentialQueue:
    def __init__(self, *, name: str, item_key: Callable[[dict], str]):
        """`name` labels this queue's log lines (e.g. "HyP3", "SLC", "EGMS").
        `item_key(item)` returns the string an item's progress is reported
        under in download_state - e.g. a HyP3 job's `job_id`, or a raw SLC's
        `file_name`."""
        self._name = name
        self._item_key = item_key
        self._lock = threading.Lock()
        self._pending: list[dict] = []
        self._current_item: Optional[dict] = None
        self._total_in_session = 0
        self._done_in_session = 0
        self._cancelled = False
        self._worker: Optional[threading.Thread] = None
        self._download_fn: Optional[Callable[[dict], None]] = None

    def start(self, items: list[dict], download_fn: Callable[[dict], None]) -> None:
        """Replace the pending queue with `items` and start the worker if
        it isn't already running. The item currently downloading (if any)
        is not interrupted - it finishes under whichever download_fn was
        active when it started."""
        with self._lock:
            self._download_fn = download_fn
            self._cancelled = False
            already_done = self._done_in_session if self._current_item is not None else 0
            self._pending = list(items)
            self._total_in_session = already_done + (1 if self._current_item is not None else 0) + len(items)
            self._done_in_session = already_done

            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._run, daemon=True)
                self._worker.start()

    def enqueue(self, items: list[dict], download_fn: Callable[[dict], None]) -> None:
        """Append `items` without clobbering what's already queued or
        currently downloading. Skips any item whose key already matches
        something pending or in flight."""
        if not items:
            return

        with self._lock:
            known_keys = {self._item_key(i) for i in self._pending}
            if self._current_item is not None:
                known_keys.add(self._item_key(self._current_item))
            new_items = [i for i in items if self._item_key(i) not in known_keys]
            if not new_items:
                return

            self._download_fn = download_fn
            self._cancelled = False
            self._pending.extend(new_items)
            self._total_in_session += len(new_items)

            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._run, daemon=True)
                self._worker.start()

    def cancel(self) -> None:
        """Clear the pending queue. The current download finishes, then the
        worker stops."""
        with self._lock:
            self._cancelled = True
            self._pending.clear()

    def get_state(self) -> dict:
        """A thread-safe snapshot. Field names are intentionally generic
        (`current_item`, `pending_items`) - each queue module's own
        get_state() reshapes this into the specific field names its API
        response has always used (e.g. `current_job_id`/`pending_job_ids`
        vs `current_filename`)."""
        with self._lock:
            current_item = self._current_item
            pending = list(self._pending)
            total = self._total_in_session
            done = self._done_in_session
            cancelled = self._cancelled

        current_key = self._item_key(current_item) if current_item is not None else None
        current_progress = download_state.get(current_key) if current_key else {}

        return {
            "active": current_item is not None or len(pending) > 0,
            "current_item": current_item,
            "pending_items": pending,
            "current_progress": current_progress or None,
            "pending_count": len(pending),
            "total": total,
            "done": done,
            "cancelled": cancelled,
        }

    def _run(self) -> None:
        while True:
            with self._lock:
                if self._cancelled or not self._pending:
                    self._current_item = None
                    break
                item = self._pending.pop(0)
                self._current_item = item
                download_fn = self._download_fn

            key = self._item_key(item)
            logger.info("%s queue: starting download for %s", self._name, key)
            download_state.update(key, status="running", pct=0)

            try:
                download_fn(item)  # type: ignore[misc]  # set by start()/enqueue() before any item is processed
                download_state.update(key, status="done", pct=100)
                logger.info("%s queue: completed download for %s", self._name, key)
            except Exception:
                logger.exception("%s queue: download failed for %s", self._name, key)
                download_state.update(key, status="error")

            with self._lock:
                self._done_in_session += 1
