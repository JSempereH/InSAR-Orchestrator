import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

from insar_core.adapters.hyp3 import HyP3Adapter


def _fake_job(files):
    return SimpleNamespace(files=files)


def _make_adapter(files):
    # Bypass __init__: it authenticates against the real HyP3 service, which
    # this test has no need for (and no network access to rely on) - only
    # the download() method under test is exercised here.
    adapter = object.__new__(HyP3Adapter)
    adapter._client = SimpleNamespace(get_job_by_id=lambda job_id: _fake_job(files))
    return adapter


def _fake_stream_download(url, dest, *, file_index, file_count, progress_cb=None, **kw):
    """Stand-in for insar_core.net.stream_download: writes a tiny file and
    reports two progress ticks, like a real (very small) transfer would."""
    total = 100
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"x")
    if progress_cb:
        progress_cb(filename=dest.name, total_bytes=total, downloaded_bytes=50)
        time.sleep(0.01)  # let other threads interleave, exercising the shared lock
        progress_cb(filename=dest.name, total_bytes=total, downloaded_bytes=100)


def test_download_returns_paths_in_original_file_order(tmp_path):
    files = [
        {"url": "https://example.com/a.tif", "filename": "a.tif"},
        {"url": "https://example.com/b.tif", "filename": "b.tif"},
        {"url": "https://example.com/c.tif", "filename": "c.tif"},
    ]
    adapter = _make_adapter(files)

    with patch("insar_core.adapters.hyp3.stream_download", side_effect=_fake_stream_download):
        paths = adapter.download("job-1", tmp_path)

    assert [p.name for p in paths] == ["a.tif", "b.tif", "c.tif"]
    assert all(p.exists() for p in paths)


def test_download_returns_empty_list_without_error(tmp_path):
    adapter = _make_adapter([])
    with patch("insar_core.adapters.hyp3.stream_download", side_effect=_fake_stream_download):
        assert adapter.download("job-empty", tmp_path) == []


def test_download_progress_reports_combined_totals_across_files(tmp_path):
    files = [
        {"url": "https://example.com/a.tif", "filename": "a.tif"},
        {"url": "https://example.com/b.tif", "filename": "b.tif"},
    ]
    adapter = _make_adapter(files)

    lock = threading.Lock()
    calls = []

    def on_progress(**kw):
        with lock:
            calls.append(kw)

    with patch("insar_core.adapters.hyp3.stream_download", side_effect=_fake_stream_download):
        adapter.download("job-2", tmp_path, progress_cb=on_progress)

    assert len(calls) == 4  # two progress ticks per file, two files
    # Every reported total_bytes/downloaded_bytes is a sum across whatever
    # files are currently in flight, never a single file's own 100-byte total -
    # the whole point of aggregating is that this must exceed one file's size.
    assert any(c["total_bytes"] > 100 for c in calls)
    # Once both files have reported their final tick, the aggregate must
    # reflect the true combined total: 2 files x 100 bytes each. (Checking
    # the max across all calls, not calls[-1], since two threads' last ticks
    # can land in either order without affecting the true final total.)
    assert max(c["downloaded_bytes"] for c in calls) == 200
    assert max(c["total_bytes"] for c in calls) == 200
