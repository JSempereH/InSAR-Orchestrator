"""
Test isolation: point the app at a throwaway SQLite DB and downloads dir
*before* app.config/app.database are imported anywhere, so tests never
touch the real insar_app.db or ./downloads.
"""

import os
import tempfile

_tmp_dir = tempfile.mkdtemp(prefix="insar-orchestrator-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_dir}/test.db"
os.environ["DOWNLOADS_DIR"] = os.path.join(_tmp_dir, "downloads")

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


# ── fake rclone (shared by the backup and archive tests) ─────────────────────

import json  # noqa: E402
import stat  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

FAKE_RCLONE = Path(__file__).parent / "fake_rclone.py"


@pytest.fixture()
def fake_rclone(tmp_path, monkeypatch):
    """Point the app at tests/fake_rclone.py instead of a real rclone binary."""
    from app.config import settings

    script = tmp_path / "rclone"
    script.write_text(f"#!{sys.executable}\n" + FAKE_RCLONE.read_text())
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    root = tmp_path / "remote-root"
    root.mkdir()
    log = tmp_path / "calls.log"
    log.touch()
    monkeypatch.setattr(settings, "rclone_path", str(script))
    monkeypatch.setenv("FAKE_RCLONE_ROOT", str(root))
    monkeypatch.setenv("FAKE_RCLONE_LOG", str(log))
    monkeypatch.delenv("FAKE_RCLONE_FAIL", raising=False)
    monkeypatch.delenv("FAKE_RCLONE_CORRUPT", raising=False)
    return {"root": root, "calls": lambda: [json.loads(x) for x in log.read_text().splitlines()]}
