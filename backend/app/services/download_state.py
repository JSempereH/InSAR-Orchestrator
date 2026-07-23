from __future__ import annotations

from threading import Lock

_lock = Lock()
_progress: dict[str, dict] = {}


def update(job_id: str, **fields) -> None:
    with _lock:
        _progress.setdefault(job_id, {}).update(fields)


def get(job_id: str) -> dict:
    with _lock:
        return dict(_progress.get(job_id, {}))


def clear(job_id: str) -> None:
    with _lock:
        _progress.pop(job_id, None)
