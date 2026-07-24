"""EGMS search/download endpoints. All network calls to the real EGMS API
are mocked out via egms_service.get_egms_adapter."""
import time

import pytest

from app.services import egms_download_queue, egms_service

_GEOMETRY = {"type": "Polygon", "coordinates": [[[3.0, 49.0], [3.6, 49.0], [3.6, 49.5], [3.0, 49.0]]]}


class _FakeAdapter:
    def __init__(self, hits=None, options=None, fail=False):
        self._hits = hits if hits is not None else [
            {"filename": "EGMS_L3_E40N30_U.zip", "filesize": 1_000_000},
        ]
        self._options = options or ["2019-2023"]
        self.fail = fail
        self.downloaded = []

    def list_options(self, kind):
        return self._options

    def search(self, params):
        from insar_core.models.egms import EGMSProduct
        return [
            EGMSProduct(query_id="q1", filename=h["filename"], level=params.level,
                        size_mb=h.get("filesize", 0) / 1e6)
            for h in self._hits
        ]

    def download(self, product, destination, progress_cb=None):
        if self.fail:
            raise RuntimeError("boom")
        self.downloaded.append(product.filename)
        if progress_cb:
            progress_cb(total_bytes=100, downloaded_bytes=100)


@pytest.fixture(autouse=True)
def _reset_queue():
    """The download queue module holds process-global state; drain it between tests."""
    yield
    egms_download_queue.cancel()
    for _ in range(50):
        if not egms_download_queue.get_state()["active"]:
            break
        time.sleep(0.02)


def test_search_without_credentials_returns_422(client):
    resp = client.post("/api/egms/search", json={
        "geometry": _GEOMETRY, "level": "L3", "release": "2019-2023",
    })
    assert resp.status_code == 422


def test_search_returns_products(client, monkeypatch):
    monkeypatch.setattr(egms_service, "get_egms_adapter", lambda db: _FakeAdapter())

    resp = client.post("/api/egms/search", json={
        "geometry": _GEOMETRY, "level": "L3", "release": "2019-2023", "product_type": "ORTHO-UP",
    })
    assert resp.status_code == 200
    products = resp.json()
    assert len(products) == 1
    assert products[0]["filename"] == "EGMS_L3_E40N30_U.zip"
    assert products[0]["query_id"] == "q1"
    assert products[0]["size_mb"] == 1.0


def test_list_options_proxies_adapter(client, monkeypatch):
    monkeypatch.setattr(egms_service, "get_egms_adapter", lambda db: _FakeAdapter(options=["L2A", "L2B", "L3"]))

    resp = client.get("/api/egms/options/levels")
    assert resp.status_code == 200
    assert resp.json() == ["L2A", "L2B", "L3"]


def test_download_queue_lifecycle(client, monkeypatch):
    fake = _FakeAdapter()
    monkeypatch.setattr(egms_service, "get_egms_adapter", lambda db: fake)

    resp = client.post("/api/egms/downloads/queue", json={
        "products": [{"query_id": "q1", "filename": "product.zip", "level": "L3", "size_mb": 1.0}],
        "destination_name": "Test AOI",
    })
    assert resp.status_code == 200

    state = client.get("/api/egms/downloads/queue").json()
    for _ in range(50):
        if not state["active"]:
            break
        time.sleep(0.02)
        state = client.get("/api/egms/downloads/queue").json()

    assert not state["active"]
    assert state["done"] == 1
    assert fake.downloaded == ["product.zip"]


def test_download_queue_cancel_clears_pending(client, monkeypatch):
    monkeypatch.setattr(egms_service, "get_egms_adapter", lambda db: _FakeAdapter())

    client.post("/api/egms/downloads/queue", json={
        "products": [
            {"query_id": "q1", "filename": "a.zip", "level": "L3", "size_mb": 1.0},
            {"query_id": "q1", "filename": "b.zip", "level": "L3", "size_mb": 1.0},
        ],
        "destination_name": "Test AOI",
    })
    resp = client.delete("/api/egms/downloads/queue")
    assert resp.status_code == 200
    assert resp.json() == {"cancelled": True}
