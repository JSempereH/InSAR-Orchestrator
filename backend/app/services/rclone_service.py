"""Thin wrapper around the `rclone` command line tool.

rclone is the single transport for backups: the same code reaches an S3
bucket, a mounted disk or another computer over SSH (sftp), depending only on
the destination string. Credentials for remote services live in rclone's own
config file (`rclone config`), never in this app's database.

A destination is either an absolute local path (`/mnt/backup/insar`) or
`<remote>:<path>` where `<remote>` is a name listed by `rclone listremotes`
(`s3backup:my-bucket/insar`, `nas:backups/insar`).

Every call builds an argument list (no shell), and destinations are validated
first so a value can never be mistaken for an rclone flag or an inline
connection string.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from typing import Callable, Iterable, Optional

from app.config import settings

logger = logging.getLogger(__name__)

_REMOTE_PREFIX = re.compile(r"^([^:/\\]+):")
_STDERR_TAIL_LINES = 8


class RcloneError(RuntimeError):
    """rclone is missing, rejected the input, or exited with an error."""


def binary() -> Optional[str]:
    return shutil.which(settings.rclone_path)


def is_available() -> bool:
    return binary() is not None


def _require_binary() -> str:
    path = binary()
    if path is None:
        raise RcloneError(
            "rclone was not found. Install it from https://rclone.org/install/ "
            "(or set RCLONE_PATH in backend/.env) and restart the backend."
        )
    return path


def _tail(text: str) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return "\n".join(lines[-_STDERR_TAIL_LINES:])


def run(args: list[str], timeout: Optional[float] = 120, input_bytes: Optional[bytes] = None) -> str:
    """Run `rclone <args>` and return stdout, raising RcloneError on failure."""
    cmd = [_require_binary(), *args]
    try:
        proc = subprocess.run(
            cmd, input=input_bytes, capture_output=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RcloneError(f"rclone {args[0]} timed out after {timeout:.0f}s") from exc
    if proc.returncode != 0:
        detail = _tail(proc.stderr.decode("utf-8", errors="replace"))
        raise RcloneError(f"rclone {args[0]} failed (exit {proc.returncode}): {detail}")
    return proc.stdout.decode("utf-8", errors="replace")


def version() -> Optional[str]:
    try:
        first = run(["version"], timeout=15).splitlines()[0]
    except (RcloneError, IndexError):
        return None
    return first.replace("rclone", "").strip() or None


def list_remotes() -> list[str]:
    out = run(["listremotes"], timeout=15)
    return [ln.strip().rstrip(":") for ln in out.splitlines() if ln.strip()]


def split_destination(destination: str) -> tuple[Optional[str], str]:
    """(remote name or None for a local path, path part)."""
    match = _REMOTE_PREFIX.match(destination)
    if match is None:
        return None, destination
    return match.group(1), destination[match.end():]


def validate_destination(destination: str) -> str:
    """Return a normalised destination or raise RcloneError.

    Local paths must be absolute. `name:path` requires `name` to be a configured
    rclone remote. Anything that starts with `-` or `:` is refused: the first
    would be read as a flag, the second is rclone's inline connection string
    syntax, which could smuggle in arbitrary backend options.
    """
    dest = (destination or "").strip()
    if not dest:
        raise RcloneError("The backup destination is empty.")
    if dest.startswith(("-", ":")):
        raise RcloneError("Invalid destination: it cannot start with '-' or ':'.")
    if "\n" in dest or "\x00" in dest:
        raise RcloneError("Invalid destination.")

    remote, _ = split_destination(dest)
    if remote is None:
        if not os.path.isabs(dest):
            raise RcloneError(
                "A local destination must be an absolute path (for example /mnt/backup/insar), "
                "or use <remote>:<path> for a remote configured in rclone."
            )
        return dest.rstrip("/") or "/"

    remotes = list_remotes()
    if remote not in remotes:
        known = ", ".join(remotes) if remotes else "none configured"
        raise RcloneError(f"'{remote}' is not an rclone remote (known: {known}). Run `rclone config` to add it.")
    return dest.rstrip("/") if not dest.endswith(":") else dest


def join(destination: str, *parts: str) -> str:
    """Append path components to a destination, for both local paths and `remote:path`."""
    tail = "/".join(p.strip("/") for p in parts if p)
    if not tail:
        return destination
    if destination.endswith(":") or destination.endswith("/"):
        return destination + tail
    return f"{destination}/{tail}"


def test_destination(destination: str) -> str:
    """Write, list and delete a tiny marker file. Returns the validated destination."""
    dest = validate_destination(destination)
    marker = join(dest, ".insar-orchestrator-write-test")
    run(["rcat", marker], input_bytes=b"ok", timeout=60)
    try:
        run(["lsf", dest, "--files-only", "--max-depth", "1"], timeout=60)
    finally:
        run(["deletefile", marker], timeout=60)
    return dest


def list_files(destination: str) -> list[str]:
    """File names directly under `destination`; empty when it does not exist yet."""
    try:
        out = run(["lsf", destination, "--files-only", "--max-depth", "1"], timeout=120)
    except RcloneError as exc:
        if "not found" in str(exc).lower():
            return []
        raise
    return [ln for ln in out.splitlines() if ln]


def delete_file(path: str) -> None:
    run(["deletefile", path], timeout=120)


def copyto(src_file: str, dest_file: str) -> None:
    """Copy one file to an exact destination path, verifying it after transfer."""
    run(["copyto", src_file, dest_file], timeout=None)


ProgressCb = Callable[[int, int, float, Optional[int]], None]


def copy(
    src: str,
    dest: str,
    excludes: Iterable[str] = (),
    min_age: str = "1m",
    transfers: int = 4,
    on_progress: Optional[ProgressCb] = None,
) -> None:
    """`rclone copy src dest`: uploads new or changed files, never deletes anything.

    `min_age` skips files modified more recently than that, so a download that is
    still being written is picked up by the next run instead of uploaded half done.
    `on_progress(bytes_done, bytes_total, speed_bps, eta_s)` is called about every
    two seconds, from rclone's JSON stats log.
    """
    args = [
        _require_binary(), "copy", src, dest,
        "--use-json-log", "--stats", "2s",
        "--min-age", min_age,
        "--transfers", str(transfers),
        "--create-empty-src-dirs=false",
    ]
    for pattern in excludes:
        args += ["--exclude", pattern]

    proc = subprocess.Popen(
        args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="replace",
    )
    errors: list[str] = []
    assert proc.stderr is not None
    for line in proc.stderr:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            errors.append(line)
            continue
        stats = entry.get("stats")
        if stats and on_progress:
            try:
                on_progress(
                    int(stats.get("bytes") or 0),
                    int(stats.get("totalBytes") or 0),
                    float(stats.get("speed") or 0.0),
                    int(stats["eta"]) if stats.get("eta") is not None else None,
                )
            except (TypeError, ValueError):
                logger.debug("Ignoring malformed rclone stats: %s", stats)
        elif entry.get("level") in ("error", "critical"):
            errors.append(str(entry.get("msg", "")))
    code = proc.wait()
    if code != 0:
        detail = "\n".join(errors[-_STDERR_TAIL_LINES:])
        raise RcloneError(f"rclone copy {src} failed (exit {code}): {detail}")


def check(src: str, dest: str) -> None:
    """Verify every file under `src` exists at `dest` with the same size and, where
    both sides support a common hash, the same checksum. Extra files at `dest` are
    ignored (`--one-way`). Raises RcloneError if anything is missing or differs.

    Some backends have no hash in common (SFTP without md5sum, for example); rclone
    then compares sizes only. Run it before deleting the source, never instead of
    having a second copy.
    """
    run(["check", src, dest, "--one-way", "--checkers", "8"], timeout=None)
