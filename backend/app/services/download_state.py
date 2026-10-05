from __future__ import annotations

import time
from threading import Lock

_lock = Lock()
_progress: dict[str, dict] = {}
_updated_at: dict[str, float] = {}

# How long an entry sticks around after its last update before being pruned -
# long enough that the frontend's own polling has plenty of time to see a
# terminal ("done"/"error") state at least once, short enough that a
# long-running server doesn't keep one entry forever for every job/file/move
# it has ever processed (nothing was pruning this before).
_RETENTION_S = 3600.0


def update(job_id: str, **fields) -> None:
    with _lock:
        _progress.setdefault(job_id, {}).update(fields)
        _updated_at[job_id] = time.monotonic()
        _prune_locked()


def get(job_id: str) -> dict:
    with _lock:
        return dict(_progress.get(job_id, {}))


def clear(job_id: str) -> None:
    with _lock:
        _progress.pop(job_id, None)
        _updated_at.pop(job_id, None)


def _prune_locked() -> None:
    """Drop entries untouched for longer than _RETENTION_S. Piggybacks on
    update() (called under _lock) rather than a separate background
    thread/timer - downloads/moves call update() often enough while active
    that this keeps the dict bounded without needing its own scheduler."""
    now = time.monotonic()
    stale = [k for k, t in _updated_at.items() if now - t > _RETENTION_S]
    for k in stale:
        _progress.pop(k, None)
        _updated_at.pop(k, None)
