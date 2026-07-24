from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Optional

import requests


def stream_download(
    url: str,
    dest: Path,
    *,
    headers: Optional[dict] = None,
    file_index: int = 1,
    file_count: int = 1,
    progress_cb: Optional[Callable[..., None]] = None,
    timeout: int = 600,
) -> None:
    """Download `url` to `dest` in chunks, reporting progress via `progress_cb`.

    progress_cb receives keyword args: file_index, file_count, filename,
    total_bytes, downloaded_bytes, speed_bps, eta_s.
    """
    CHUNK = 1024 * 1024  # 1 MB
    with requests.get(url, headers=headers, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        downloaded = 0
        t0 = time.monotonic()

        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=CHUNK):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb:
                        elapsed = max(time.monotonic() - t0, 0.001)
                        speed = downloaded / elapsed
                        eta = int((total - downloaded) / speed) if speed and total else None
                        progress_cb(
                            file_index=file_index,
                            file_count=file_count,
                            filename=dest.name,
                            total_bytes=total,
                            downloaded_bytes=downloaded,
                            speed_bps=speed,
                            eta_s=eta,
                        )
