from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable, Optional

import requests

log = logging.getLogger(__name__)

_CHUNK = 1024 * 1024  # 1 MB

# A dropped connection partway through a multi-GB SLC/HyP3 product used to
# mean starting over from byte zero. This retries transient failures with
# backoff, and (when the server supports HTTP Range) resumes from wherever
# the previous attempt left off instead of re-downloading bytes already on
# disk.
_MAX_RETRIES = 3
_RETRY_BACKOFF_S = 2.0  # doubled after each failed attempt


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
    max_retries: int = _MAX_RETRIES,
) -> None:
    """Download `url` to `dest` in chunks, reporting progress via `progress_cb`.

    progress_cb receives keyword args: file_index, file_count, filename,
    total_bytes, downloaded_bytes, speed_bps, eta_s.

    Pass `session` (e.g. an authenticated requests.Session) when the URL
    requires auth beyond plain headers - e.g. ASF's data pool, which needs
    an Earthdata-authenticated session to follow its redirect chain.

    Retries up to `max_retries` times on connection errors, timeouts, and
    5xx responses (not on 4xx - a bad URL or expired auth won't fix itself on
    retry), with exponential backoff. Progress is written to a `.part` file
    next to `dest` and resumed via an HTTP Range request on retry when the
    server honors it (falls back to a full restart if it doesn't, e.g. a 200
    instead of 206 on the ranged request) - final file only appears at
    `dest` once fully downloaded.
    """
    dest = Path(dest)
    tmp_dest = dest.with_name(dest.name + ".part")
    requester = session if session is not None else requests

    attempt = 0
    while True:
        try:
            _attempt_download(
                requester, url, tmp_dest, headers=headers,
                file_index=file_index, file_count=file_count,
                progress_cb=progress_cb, timeout=timeout,
            )
            break
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status is not None and 400 <= status < 500:
                raise  # client error (bad URL, expired auth, ...) - retrying won't help
            attempt = _retry_or_raise(exc, dest.name, attempt, max_retries)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError) as exc:
            attempt = _retry_or_raise(exc, dest.name, attempt, max_retries)

    tmp_dest.rename(dest)


def _retry_or_raise(exc: Exception, label: str, attempt: int, max_retries: int) -> int:
    attempt += 1
    if attempt > max_retries:
        raise exc
    backoff = _RETRY_BACKOFF_S * (2 ** (attempt - 1))
    log.warning(
        "request for %s failed (attempt %d/%d): %s - retrying in %.1fs",
        label, attempt, max_retries, exc, backoff,
    )
    time.sleep(backoff)
    return attempt


def request_with_retry(method: str, url: str, *, max_retries: int = _MAX_RETRIES, **kwargs) -> requests.Response:
    """A plain (non-streaming) HTTP request with the same retry policy as
    stream_download: connection errors, timeouts, and 5xx responses are
    retried with exponential backoff; 4xx responses are not (a bad request
    or expired auth won't fix itself on retry).

    For the small JSON calls other adapters make against external APIs
    (auth token exchange, catalog search) - stream_download handles the
    large-file download case separately, since it also needs resume support.
    """
    attempt = 0
    while True:
        try:
            resp = requests.request(method, url, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status is not None and 400 <= status < 500:
                raise
            attempt = _retry_or_raise(exc, url, attempt, max_retries)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError) as exc:
            attempt = _retry_or_raise(exc, url, attempt, max_retries)


def _attempt_download(
    requester, url: str, tmp_dest: Path, *,
    headers: Optional[dict], file_index: int, file_count: int,
    progress_cb: Optional[Callable[..., None]], timeout: int,
) -> None:
    resume_from = tmp_dest.stat().st_size if tmp_dest.exists() else 0
    req_headers = dict(headers or {})
    if resume_from:
        req_headers["Range"] = f"bytes={resume_from}-"

    with requester.get(url, headers=req_headers, stream=True, timeout=timeout) as r:
        resumed = resume_from and r.status_code == 206
        if resume_from and not resumed:
            # Server ignored the Range request (plain 200) - it doesn't
            # support resuming this URL, so the partial data on disk can't
            # be trusted to align with what follows. Start over.
            resume_from = 0
        r.raise_for_status()

        total = _total_size(r, resume_from) if resumed else int(r.headers.get("content-length", 0))
        downloaded = resume_from
        t0 = time.monotonic()

        with open(tmp_dest, "ab" if resumed else "wb") as f:
            for chunk in r.iter_content(chunk_size=_CHUNK):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb:
                        elapsed = max(time.monotonic() - t0, 0.001)
                        speed = (downloaded - resume_from) / elapsed
                        eta = int((total - downloaded) / speed) if speed and total else None
                        progress_cb(
                            file_index=file_index,
                            file_count=file_count,
                            filename=tmp_dest.name.removesuffix(".part"),
                            total_bytes=total,
                            downloaded_bytes=downloaded,
                            speed_bps=speed,
                            eta_s=eta,
                        )


def _total_size(response: requests.Response, resume_from: int) -> int:
    """True total file size for a 206 Partial Content response - parsed from
    Content-Range (`bytes start-end/total`) when present, since
    Content-Length on a ranged response only covers the remaining bytes."""
    content_range = response.headers.get("content-range", "")
    try:
        return int(content_range.rsplit("/", 1)[-1])
    except (ValueError, IndexError):
        return resume_from + int(response.headers.get("content-length", 0))
