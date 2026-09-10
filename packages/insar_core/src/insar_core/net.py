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
    session: Optional[requests.Session] = None,
    file_index: int = 1,
    file_count: int = 1,
    progress_cb: Optional[Callable[..., None]] = None,
    timeout: int = 600,
) -> None:
    """Download `url` to `dest` in chunks, reporting progress via `progress_cb`.

    progress_cb receives keyword args: file_index, file_count, filename,
    total_bytes, downloaded_bytes, speed_bps, eta_s.

    Pass `session` (e.g. an authenticated requests.Session) when the URL
    requires auth beyond plain headers - e.g. ASF's data pool, which needs
    an Earthdata-authenticated session to follow its redirect chain.
    """
    CHUNK = 1024 * 1024  # 1 MB
    requester = session if session is not None else requests
    with requester.get(url, headers=headers, stream=True, timeout=timeout) as r:
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
