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
